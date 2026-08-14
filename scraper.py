from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote_to_bytes, urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# -----------------------------------------------------------------------------
# Paths / runtime settings
# -----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
NY_TZ = ZoneInfo("America/New_York")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
BROWSER_FALLBACK = os.getenv("BROWSER_FALLBACK", "1") != "0"
SHARES_EPSILON = float(os.getenv("SHARES_EPSILON", "0.000001"))

USER_AGENT = os.getenv(
    "ETF_SCRAPER_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 ETFDailyDelta/1.0",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
LOG = logging.getLogger("etf_daily_delta")


# -----------------------------------------------------------------------------
# Source definitions
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class ETFSource:
    ticker: str
    provider: str
    fund_name: str
    landing_url: str
    direct_urls: tuple[str, ...] = ()
    browser_url: str | None = None
    browser_search_term: str | None = None
    browser_pre_click: tuple[str, ...] = ()
    browser_download_texts: tuple[str, ...] = ()
    allow_html_table: bool = False
    min_rows: int = 1
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)


SOURCES: dict[str, ETFSource] = {
    "JEPI": ETFSource(
        ticker="JEPI",
        provider="J.P. Morgan Asset Management",
        fund_name="JPMorgan Equity Premium Income ETF",
        landing_url=(
            "https://am.jpmorgan.com/us/en/asset-management/adv/products/"
            "jpmorgan-equity-premium-income-etf-etf-shares-46641q332"
        ),
        direct_urls=(
            "https://am.jpmorgan.com/FundsMarketingHandler/excel?"
            "country=us&cusip=46641Q332&locale=en-US&role=adv&type=dailyETFHoldings",
        ),
        aliases={
            "ticker": ("Ticker",),
            "company": ("Security Description", "Description"),
            "shares": ("Shares/Par", "Shares / Par", "Shares"),
            "weight": ("% of Net Assets", "% of Market Value", "Percent of Net Assets"),
        },
    ),
    "DFAC": ETFSource(
        ticker="DFAC",
        provider="Dimensional Fund Advisors",
        fund_name="Dimensional U.S. Core Equity 2 ETF",
        landing_url="https://www.dimensional.com/us-en/funds/dfac/us-core-equity-2-etf",
        # Dimensional's current full-holdings CSV URL is dynamic and date-stamped.
        # Resolve it from the official funddetail API rather than guessing a blob name.
        direct_urls=(),
        # The fund page calls fundcenter/funddetail, whose JSON exposes fullHoldingsCsvUrl.
        browser_url="https://www.dimensional.com/us-en/funds/dfac/us-core-equity-2-etf",
        browser_search_term=None,
        browser_pre_click=(),
        browser_download_texts=("Daily Holdings", "Download Holdings"),
        min_rows=500,
        aliases={
            "ticker": (
                "Ticker", "Symbol", "Trading Symbol", "Security Ticker",
                "SecurityTicker", "Security Symbol", "SecuritySymbol",
            ),
            "company": (
                "Security Name", "Name", "Description", "Issuer Name",
                "SecurityName", "Security Description", "SecurityDescription",
            ),
            "shares": (
                "Shares", "Shares Held", "Quantity", "Share Quantity",
                "Shares/Par", "Shares / Par", "Shares/Par Value",
                "Shares/Principal", "Shares/Principal Amount",
                "NumberOfShares", "SharesHeld", "QuantityHeld", "HoldingQuantity",
            ),
            "weight": (
                "Weight", "Weight (%)", "% of Net Assets", "Percent of Net Assets",
                "PortfolioWeight", "WeightPercent", "MarketValuePercent",
                "PercentageOfNetAssets",
            ),
        },
    ),
    "CGGR": ETFSource(
        ticker="CGGR",
        provider="Capital Group",
        fund_name="Capital Group Growth ETF",
        landing_url=(
            "https://www.capitalgroup.com/advisor/investments/"
            "exchange-traded-funds/holdings?etf=CGGR"
        ),
        direct_urls=(
            "https://www.capitalgroup.com/api/investments/investment-service/v1/"
            "etfs/cggr/download/daily-holdings?audience=advisor",
        ),
        aliases={
            "ticker": ("Ticker",),
            "company": ("Security Name", "Name"),
            "shares": ("Shares or Principal Amount", "Shares", "Quantity"),
            "weight": ("Percent of Net Assets", "% of Net Assets", "Weight"),
        },
    ),
    "AVUV": ETFSource(
        ticker="AVUV",
        provider="Avantis Investors",
        fund_name="Avantis U.S. Small Cap Value ETF",
        landing_url=(
            "https://www.avantisinvestors.com/avantis-investments/"
            "avantis-us-small-cap-value-etf/"
        ),
        # The product page identifies AVUV internally as fund/product id 119.
        # Avantis exposes dedicated Total Holdings pages keyed by this id.  Use the
        # holdings page directly so the browser does not spend time on unrelated
        # analytics calls from the marketing page.  The legacy query-string route
        # currently redirects/serves the same holdings application.
        browser_url=(
            "https://www.avantisinvestors.com/avantis-investments/"
            "total-holdings/119/?type=etf"
        ),
        browser_pre_click=("United States", "Accept & Continue"),
        browser_download_texts=(
            "All holdings (CSV)",
            "Download all holdings",
            "All Holdings",
            "Daily Pricing Basket",
        ),
        # The current total-holdings page may initially render only a 50-row preview.
        # Preserve HTML as a fallback, but reject it unless a sufficiently large
        # complete snapshot has been loaded after the All Holdings interaction.
        allow_html_table=True,
        min_rows=200,
        aliases={
            "ticker": ("Ticker", "Symbol", "Trading Symbol"),
            "company": ("Security Name", "Company", "Name", "Description"),
            "shares": (
                "Shares", "Shares Held", "Quantity", "Share Quantity", "Units",
                "Position Quantity", "Shares/Principal/ Notional Amount",
                "Shares/Principal/Notional Amount",
            ),
            "weight": ("Weight", "Weight (%)", "% of Net Assets", "Market Value (%)", "Percent Assets", "Percent of Portfolio", "Pct Assets"),
        },
    ),
    "BLOK": ETFSource(
        ticker="BLOK",
        provider="Amplify ETFs",
        fund_name="Amplify Transformational Data Sharing ETF",
        landing_url="https://amplifyetfs.com/blok-holdings/",
        browser_download_texts=("Download Holdings (CSV)", "Download Holdings", "Holdings"),
        allow_html_table=True,
        aliases={
            "ticker": ("Ticker", "Symbol"),
            "company": ("Name", "Company", "Security Name"),
            "shares": ("Shares", "Shares Held", "Quantity"),
            "weight": ("Market Value (%)", "Weight", "Weight (%)", "% of Net Assets"),
        },
    ),
}


GENERIC_ALIASES: dict[str, tuple[str, ...]] = {
    "ticker": (
        "ticker",
        "symbol",
        "stock ticker",
        "security ticker",
        "trading symbol",
    ),
    "company": (
        "company name",
        "company",
        "security name",
        "security description",
        "description",
        "name",
        "issuer name",
        "holding",
    ),
    "shares": (
        "shares",
        "shares held",
        "shares/par",
        "shares / par",
        "shares or principal amount",
        "share quantity",
        "quantity",
        "qty",
        "position",
    ),
    "weight": (
        "weight",
        "weight (%)",
        "portfolio weight",
        "% of net assets",
        "percent of net assets",
        "% of market value",
        "market value (%)",
        "market value %",
    ),
}

EXCLUDED_TICKERS = {
    "",
    "-",
    "--",
    "N/A",
    "NA",
    "NONE",
    "CASH",
    "USD",
    "US DOLLAR",
    "US DOLLARS",
}

DATE_PATTERNS = (
    re.compile(
        r"(?i)\bas\s+of(?:\s+date)?\s*[:,\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})"
    ),
    re.compile(
        r"(?i)[\"']?(?:asOfDate|as_of_date|holdingsDate|holdings_date|portfolioDate|portfolio_date)[\"']?\s*[:=]\s*[\"']?"
        r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    ),
    re.compile(
        r"(?i)\bdata\s+as\s+of\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})"
    ),
    re.compile(
        r"(?i)\bholdings?(?:\s+date)?\s*[:\-]?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})"
    ),
)


@dataclass
class DownloadedDocument:
    data: bytes
    source_url: str
    filename: str
    content_type: str = ""
    last_modified: str = ""


@dataclass
class ParsedHoldings:
    frame: pd.DataFrame
    as_of_date: date
    metadata_text: str


# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------
def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.2,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                "application/vnd.ms-excel,application/octet-stream,text/html;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def filename_from_response(url: str, headers: dict[str, str]) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, flags=re.I)
    if match:
        return match.group(1).strip().strip('"')
    name = Path(urlparse(url).path).name
    return name or "download"


def download_url(session: requests.Session, url: str) -> DownloadedDocument:
    LOG.info("GET %s", url)
    response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    return DownloadedDocument(
        data=response.content,
        source_url=response.url,
        filename=filename_from_response(response.url, dict(response.headers)),
        content_type=response.headers.get("content-type", ""),
        last_modified=response.headers.get("last-modified", ""),
    )


# -----------------------------------------------------------------------------
# Static discovery and browser fallback
# -----------------------------------------------------------------------------
def _download_link_score(source: ETFSource, href: str, text: str) -> int:
    s = f"{href} {text}".lower()
    score = 0
    if source.ticker.lower() in s:
        score += 3
    if "daily" in s:
        score += 3
    if "holding" in s or "portfolio" in s:
        score += 6
    if re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", href, flags=re.I):
        score += 8
    if "download" in s:
        score += 3
    if any(x in s for x in ("fact sheet", "factsheet", "prospectus", "annual report", "pdf")):
        score -= 8
    return score


def static_candidates(session: requests.Session, source: ETFSource) -> list[DownloadedDocument]:
    """Discover file links rendered in the server-side HTML.

    This catches providers where the download URL changes but is still present in an <a href>.
    """
    docs: list[DownloadedDocument] = []
    # Dimensional's DFAC fund page is client-rendered and can hold a requests.get()
    # connection open for ~45 seconds. v9 showed the static request timing out while
    # Playwright could at least render the site, so skip this nonproductive path.
    if source.ticker == "DFAC":
        LOG.info("DFAC static landing page skipped; using browser-rendered fund page")
        return docs
    try:
        page = session.get(source.landing_url, timeout=REQUEST_TIMEOUT)
        page.raise_for_status()
    except Exception as exc:
        LOG.warning("%s static landing page failed: %s", source.ticker, exc)
        return docs

    soup = BeautifulSoup(page.text, "html.parser")
    scored: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page.url, a.get("href", ""))
        score = _download_link_score(source, href, a.get_text(" ", strip=True))
        if score >= 8:
            scored.append((score, href))

    # Some sites embed escaped URLs in JSON/script tags rather than normal anchors.
    raw = page.text.replace("\\/", "/")
    url_re = re.compile(r"https?://[^\"'<>\s]+", re.I)
    for found in url_re.findall(raw):
        found = found.replace("&amp;", "&")
        score = _download_link_score(source, found, "")
        if score >= 10:
            scored.append((score, found))

    seen: set[str] = set()
    for _, href in sorted(scored, reverse=True):
        if href in seen:
            continue
        seen.add(href)
        try:
            docs.append(download_url(session, href))
        except Exception as exc:
            LOG.warning("%s discovered URL failed: %s (%s)", source.ticker, href, exc)

    if source.allow_html_table:
        docs.append(
            DownloadedDocument(
                data=page.content,
                source_url=page.url,
                filename=f"{source.ticker.lower()}-holdings.html",
                content_type=page.headers.get("content-type", "text/html"),
            )
        )
    return docs


def _playwright_click_if_present(page: Any, text: str, timeout_ms: int = 3000) -> bool:
    """Click visible text/button/link if present. Failure is intentionally non-fatal."""
    patterns = [
        lambda: page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first,
        lambda: page.get_by_role("link", name=re.compile(re.escape(text), re.I)).first,
        lambda: page.get_by_text(re.compile(re.escape(text), re.I), exact=False).first,
    ]
    for factory in patterns:
        try:
            locator = factory()
            if locator.is_visible(timeout=timeout_ms):
                try:
                    locator.scroll_into_view_if_needed(timeout=timeout_ms)
                except Exception:
                    pass
                locator.click(timeout=timeout_ms)
                page.wait_for_timeout(900)
                return True
        except Exception:
            pass
    return False


def _playwright_dimensional_set_us_professional_role(page: Any) -> list[str]:
    """Best-effort selection of Dimensional's public US financial-professional audience.

    The fund detail route can render only the generic Explore Funds shell until the
    audience/country selector is resolved. Keep the interaction permissive because
    Dimensional has shipped several selector layouts with different button labels.
    """
    clicked: list[str] = []

    # Open the combined audience/country selector when present.
    for label in (
        "FINANCIAL PROFESSIONAL | UNITED STATES",
        "Financial Professional | United States",
        "FINANCIAL PROFESSIONAL",
    ):
        if _playwright_click_if_present(page, label, timeout_ms=1800):
            clicked.append(label)
            break

    # Choose the audience and country inside any dialog/drawer that opened.
    for label in ("Financial Professional", "United States"):
        if _playwright_click_if_present(page, label, timeout_ms=1800):
            clicked.append(label)

    # Different builds use different confirmation labels.
    for label in ("Continue", "Confirm", "Apply", "Save", "Done", "Accept & Continue"):
        if _playwright_click_if_present(page, label, timeout_ms=1400):
            clicked.append(label)
            break

    return clicked


def _playwright_fill_search(page: Any, term: str, timeout_ms: int = 2500) -> bool:
    """Fill the first plausible visible search/filter input."""
    locators = [
        page.get_by_role("searchbox").first,
        page.locator('input[type="search"]').first,
        page.locator('input[placeholder*="Search" i]').first,
        page.locator('input[aria-label*="Search" i]').first,
        page.locator('input[placeholder*="Filter" i]').first,
    ]
    for locator in locators:
        try:
            if locator.is_visible(timeout=timeout_ms):
                locator.fill(term, timeout=timeout_ms)
                try:
                    locator.press("Enter", timeout=1000)
                except Exception:
                    pass
                page.wait_for_timeout(1800)
                return True
        except Exception:
            pass
    return False

def _iter_string_values(obj: Any) -> Iterable[str]:
    """Yield every string nested in a JSON-like object."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_string_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _iter_string_values(value)


def _dimensional_dfac_urls_from_json(body: bytes, base_url: str) -> list[str]:
    """Extract DFAC holdings URLs from Dimensional document-grid JSON.

    v10 retains the v9 exact-drawer fix: list items under ``linkDrawers`` are separate
    fund records, so a Daily Holdings URL from drawer 122 must never be paired with
    the DFAC identity in drawer 109. We first identify the exact DFAC drawer, then
    inspect only links inside that same record. Diagnostics print the bounded set of
    link names/URLs from the matching record so schema changes are actionable.
    """
    try:
        obj = json.loads(body.decode("utf-8", errors="strict"))
    except Exception:
        return []

    analytics_hosts = (
        "googlead", "doubleclick", "nr-data.net", "newrelic", "facebook.com",
        "linkedin.com", "adobe", "smetrics", "hubspot",
    )

    def normalize_url(raw: str) -> str | None:
        raw = raw.replace("\\/", "/").replace("&amp;", "&").strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://", "/")):
            url = urljoin(base_url, raw)
        elif re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", raw, flags=re.I):
            url = urljoin(base_url, raw)
        else:
            return None
        ul = url.lower()
        if any(host in ul for host in analytics_hosts):
            return None
        return url

    # Find every list named linkDrawers, regardless of wrapper objects.
    drawer_lists: list[list[Any]] = []

    def find_drawer_lists(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() == "linkdrawers" and isinstance(v, list):
                    drawer_lists.append(v)
                find_drawer_lists(v)
        elif isinstance(node, list):
            for v in node:
                find_drawer_lists(v)

    find_drawer_lists(obj)

    def strings_in(node: Any) -> list[str]:
        out: list[str] = []
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                out.extend(strings_in(v))
        elif isinstance(node, list):
            for v in node:
                out.extend(strings_in(v))
        return out

    def drawer_is_dfac(drawer: dict[str, Any]) -> bool:
        title = str(drawer.get("title", ""))
        subtitle = str(drawer.get("subtitle", ""))
        identity = f"{title} {subtitle}".lower()
        if re.search(r"\bdfac\b", identity):
            return True
        if "us core equity 2 etf" in identity or "u.s. core equity 2 etf" in identity:
            return True
        # 25434V708 is the current CUSIP shown by Dimensional/SEC records for DFAC.
        # It also appears in Broadridge document links in the document-grid record.
        for text in strings_in(drawer):
            tl = text.lower()
            if "25434v708" in tl or "q1_soi_dfac_" in tl:
                return True
        return False

    matching: list[tuple[int, dict[str, Any]]] = []
    for drawers in drawer_lists:
        for idx, drawer in enumerate(drawers):
            if isinstance(drawer, dict) and drawer_is_dfac(drawer):
                matching.append((idx, drawer))

    if not matching:
        LOG.info("DFAC exact drawer diagnostics: no matching linkDrawer found")
        return []

    candidates: list[tuple[int, str, str, int]] = []
    for idx, drawer in matching:
        title = str(drawer.get("title", ""))
        subtitle = str(drawer.get("subtitle", ""))
        links = drawer.get("links", [])
        if not isinstance(links, list):
            links = []

        rendered_links: list[str] = []
        for li, link in enumerate(links):
            if not isinstance(link, dict):
                continue
            name = str(link.get("name", link.get("title", link.get("label", ""))))
            raw_url = str(link.get("url", link.get("href", link.get("link", ""))))
            url = normalize_url(raw_url) if raw_url else None
            if raw_url:
                rendered_links.append(f"{li}:{name[:90]}=>{raw_url[:320]}")
            if not url:
                continue

            nl = name.lower()
            ul = url.lower()
            score = 0
            if "daily holdings" in nl:
                score += 300
            elif "holding" in nl:
                score += 220
            elif "portfolio" in nl or "position" in nl:
                score += 90
            if "holding" in ul:
                score += 180
            if "portfolio" in ul or "position" in ul:
                score += 60
            if re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", ul):
                score += 200
            if "download" in ul:
                score += 25
            if ul.endswith(".pdf") or ".pdf?" in ul:
                score -= 250
            if "prospectus-express" in ul:
                score -= 120

            if score > 0:
                candidates.append((score, url, name, idx))

        LOG.info(
            "DFAC exact drawer index=%s title=%s subtitle=%s link_count=%d links=%s",
            idx, title[:180], subtitle[:120], len(links), " | ".join(rendered_links)[:6000],
        )

    # Highest-confidence holdings/file links from the exact DFAC record only.
    candidates.sort(key=lambda row: row[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for score, url, name, idx in candidates:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        LOG.info(
            "DFAC exact drawer candidate score=%d drawer=%d name=%s url=%s",
            score, idx, name[:160], url,
        )

    if not out:
        LOG.info("DFAC exact drawer has no holdings/file candidate")
    else:
        LOG.info("DFAC exact drawer discovered %d holdings/file candidate(s)", len(out))
    return out[:12]


def _dimensional_dfac_api_diagnostics(body: bytes, endpoint_url: str) -> list[str]:
    """Inspect Dimensional's public fund APIs without guessing endpoint schemas.

    v10 exposed the official etf.dimensional.com/public/v2 APIs. v12 logs the exact
    request payload plus compact JSON objects/keys tied to DFAC, holdings, securities,
    or portfolio identifiers. It also returns any explicit holdings/file URLs found.
    """
    try:
        obj = json.loads(body.decode("utf-8", errors="strict"))
    except Exception:
        return []

    def short(v: Any, limit: int = 900) -> str:
        try:
            if isinstance(v, (dict, list)):
                text = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            else:
                text = str(v)
        except Exception:
            text = repr(v)
        text = re.sub(r"\s+", " ", text)
        return text[:limit]

    root_keys = list(obj.keys())[:60] if isinstance(obj, dict) else []
    LOG.info(
        "DFAC API diagnostics endpoint=%s root_type=%s root_keys=%s",
        endpoint_url,
        type(obj).__name__,
        root_keys,
    )

    urls: list[str] = []
    seen_urls: set[str] = set()
    matched_objects = 0
    holdings_keys = 0

    def walk(node: Any, path: tuple[str, ...] = ()) -> None:
        nonlocal matched_objects, holdings_keys
        if matched_objects >= 80 and holdings_keys >= 80:
            return

        if isinstance(node, dict):
            # Log compact objects that actually identify DFAC / US Core Equity 2.
            # IMPORTANT: inspect scalar VALUES only. v11 included key names in the
            # text and therefore falsely matched every object containing the key
            # ``dfaCurrencyCode`` because its key name begins with "dfac".
            try:
                scalar_values_text = " ".join(
                    str(v) for v in node.values()
                    if isinstance(v, (str, int, float, bool)) or v is None
                ).lower()
            except Exception:
                scalar_values_text = ""
            if (
                matched_objects < 80
                and (
                    re.search(r"(?:^|[^a-z0-9])dfac(?:[^a-z0-9]|$)", scalar_values_text)
                    or "us core equity 2" in scalar_values_text
                )
            ):
                scalar_fields = {
                    str(k): v for k, v in node.items()
                    if isinstance(v, (str, int, float, bool)) or v is None
                }
                LOG.info(
                    "DFAC API matched object path=%s fields=%s",
                    "/".join(path) or "<root>",
                    short(scalar_fields, 2400),
                )
                matched_objects += 1

            # The fundcenter response groups each fund under data/portfolios/N.
            # When the current dictionary is the exact DFAC portfolio object, log
            # its complete (bounded) shape and all identifier-like scalar fields.
            # This should expose the internal portfolio ID/code needed by funddetail.
            try:
                meta = node.get("meta") if isinstance(node.get("meta"), dict) else None
                exact_dfac = False
                if meta is not None:
                    marketing = str(meta.get("marketingName") or meta.get("name") or "").strip().lower()
                    ticker_values = []
                    for ident in meta.get("identifiers") or []:
                        if isinstance(ident, dict):
                            slug = str(ident.get("slug") or "").lower()
                            name = str(ident.get("name") or "").lower()
                            value = str(ident.get("value") or "").strip()
                            if "ticker" in slug or "ticker" in name:
                                ticker_values.append(value.upper())
                    primary = meta.get("primaryIdentifier")
                    if isinstance(primary, dict):
                        if "ticker" in str(primary.get("slug") or "").lower() or "ticker" in str(primary.get("name") or "").lower():
                            ticker_values.append(str(primary.get("value") or "").upper())
                    exact_dfac = (
                        "DFAC" in ticker_values
                        or ("us core equity 2 etf" in marketing and "world ex" not in marketing)
                    )
                if exact_dfac:
                    LOG.info(
                        "DFAC API exact portfolio object path=%s keys=%s object=%s",
                        "/".join(path) or "<root>",
                        list(node.keys())[:120],
                        short(node, 12000),
                    )
                    id_fields = []
                    def collect_ids(x: Any, xp: tuple[str, ...] = ()) -> None:
                        if isinstance(x, dict):
                            for kk, vv in x.items():
                                kp = xp + (str(kk),)
                                kl = str(kk).lower()
                                if isinstance(vv, (str, int, float, bool)) or vv is None:
                                    if (
                                        kl == "id" or kl.endswith("id") or "identifier" in kl
                                        or "ticker" in kl or "symbol" in kl or "code" in kl
                                        or "slug" in kl or "url" in kl or "href" in kl
                                    ):
                                        id_fields.append(("/".join(kp), vv))
                                elif isinstance(vv, (dict, list)):
                                    collect_ids(vv, kp)
                        elif isinstance(x, list):
                            for ii, vv in enumerate(x[:500]):
                                collect_ids(vv, xp + (str(ii),))
                    collect_ids(node)
                    LOG.info(
                        "DFAC API exact portfolio identifiers=%s",
                        short(id_fields[:120], 9000),
                    )
            except Exception as exc:
                LOG.debug("DFAC exact portfolio diagnostics failed: %s", exc)

            for k, v in node.items():
                k_s = str(k)
                k_l = k_s.lower()
                p = path + (k_s,)
                if (
                    holdings_keys < 80
                    and any(tok in k_l for tok in (
                        "holding", "position", "constituent", "security", "share",
                        "weight", "portfolioid", "portfolio_id", "portfolio-code",
                        "portfoliocode", "fundid", "fund_id", "ticker", "symbol",
                        "identifier", "internalid", "internal_id", "shareclass",
                    ))
                ):
                    LOG.info(
                        "DFAC API key path=%s type=%s value_sample=%s",
                        "/".join(p), type(v).__name__, short(v, 1200),
                    )
                    holdings_keys += 1
                walk(v, p)

        elif isinstance(node, list):
            for i, item in enumerate(node[:5000]):
                walk(item, path + (str(i),))
                if matched_objects >= 80 and holdings_keys >= 80:
                    break
        elif isinstance(node, str):
            u = node.strip()
            u_l = u.lower()
            if (
                (u.startswith("http://") or u.startswith("https://") or u.startswith("/"))
                and (
                    "holding" in u_l or "position" in u_l
                    or re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", u_l)
                )
            ):
                absolute = urljoin(endpoint_url, u)
                if absolute not in seen_urls:
                    seen_urls.add(absolute)
                    urls.append(absolute)
                    if len(urls) <= 20:
                        LOG.info("DFAC API explicit file/holdings URL path=%s url=%s", "/".join(path), absolute)

    walk(obj)
    LOG.info(
        "DFAC API diagnostics summary endpoint=%s matched_objects=%d holdings_keys=%d explicit_urls=%d",
        endpoint_url, matched_objects, holdings_keys, len(urls),
    )
    return urls


def _playwright_log_dfac_controls(page: Any) -> None:
    """Log the exact visible fund-page controls that could reveal holdings."""
    try:
        items = page.locator("a,button,[role=tab],[role=button]")
        logged = 0
        seen: set[tuple[str, str]] = set()
        for i in range(min(items.count(), 700)):
            el = items.nth(i)
            try:
                if not el.is_visible(timeout=250):
                    continue
                txt = re.sub(r"\s+", " ", el.inner_text(timeout=350)).strip()
            except Exception:
                continue
            if not txt:
                continue
            sig_l = txt.lower()
            if not any(k in sig_l for k in (
                "holding", "portfolio", "composition", "characteristic", "security", "download"
            )):
                continue
            href = ""
            try:
                href = el.get_attribute("href") or ""
            except Exception:
                pass
            key = (txt[:180], href[:500])
            if key in seen:
                continue
            seen.add(key)
            LOG.info("DFAC visible control candidate text=%s href=%s", txt[:180], href[:500])
            logged += 1
            if logged >= 30:
                break
        if logged == 0:
            LOG.info("DFAC visible control candidate: none")
    except Exception as exc:
        LOG.info("DFAC visible control diagnostics failed: %s", exc)


def _playwright_click_dfac_daily_holdings(page: Any) -> bool:
    """Click Daily Holdings only inside the DFAC / US Core Equity 2 result card/row.

    A global `get_by_text("Holdings")` is unsafe on Dimensional's Document Center
    because the page contains hundreds of unrelated fund documents.
    """
    fund_pattern = re.compile(r"(?:\bDFAC\b|US\s+Core\s+Equity\s+2(?:\s+ETF)?)", re.I)
    daily_pattern = re.compile(r"Daily\s+Holdings", re.I)

    try:
        page.wait_for_timeout(1200)
        fund_nodes = page.get_by_text(fund_pattern, exact=False)
        count = min(fund_nodes.count(), 30)
    except Exception:
        return False

    ancestor_xpaths = (
        "ancestor::tr[1]",
        "ancestor::*[@role='row'][1]",
        "ancestor::article[1]",
        "ancestor::li[1]",
        "ancestor::section[1]",
        "ancestor::div[1]",
        "ancestor::div[2]",
        "ancestor::div[3]",
        "ancestor::div[4]",
    )

    for i in range(count):
        node = fund_nodes.nth(i)
        for xpath in ancestor_xpaths:
            try:
                container = node.locator(f"xpath={xpath}")
                if container.count() == 0:
                    continue
                text = container.inner_text(timeout=1500)
                text_l = text.lower()
                if "daily holdings" not in text_l:
                    continue
                if "dfac" not in text_l and "us core equity 2" not in text_l:
                    continue

                for factory in (
                    lambda: container.get_by_role("link", name=daily_pattern).first,
                    lambda: container.get_by_role("button", name=daily_pattern).first,
                    lambda: container.get_by_text(daily_pattern, exact=False).first,
                ):
                    try:
                        target = factory()
                        if target.is_visible(timeout=1200):
                            target.scroll_into_view_if_needed(timeout=1500)
                            target.click(timeout=4000)
                            page.wait_for_timeout(1800)
                            LOG.info("DFAC clicked Daily Holdings in the matching DFAC result")
                            return True
                    except Exception:
                        pass
            except Exception:
                pass
    return False



def _document_from_data_uri(uri: str, source: ETFSource) -> DownloadedDocument | None:
    """Decode a browser-generated data: URI into a real downloadable document.

    Avantis' "All Holdings" control currently exposes the complete holdings CSV as
    a percent-encoded data:text/csv URI rather than a normal HTTP URL.  Playwright's
    request context does not reliably turn that URI into CSV bytes, so decode it
    ourselves before passing it to the normal parser.
    """
    if not uri.lower().startswith("data:") or "," not in uri:
        return None
    try:
        header, payload = uri.split(",", 1)
        meta = header[5:]
        content_type = (meta.split(";", 1)[0] or "text/plain").strip()
        if ";base64" in meta.lower():
            import base64
            data = base64.b64decode(payload)
        else:
            data = unquote_to_bytes(payload)

        ext = ".csv" if "csv" in content_type.lower() else ".txt"
        return DownloadedDocument(
            data=data,
            source_url=uri,
            filename=f"{source.ticker.lower()}-all-holdings{ext}",
            content_type=content_type,
        )
    except Exception as exc:
        LOG.debug("%s could not decode data URI: %s", source.ticker, exc)
        return None

def browser_candidates(source: ETFSource) -> list[DownloadedDocument]:
    """Use Chromium when direct/static discovery cannot parse a valid holdings file.

    Version 2 captures the *actual response bodies* of XHR/fetch requests instead of
    reissuing every candidate as a GET. This matters for sites whose holdings APIs use
    POST/GraphQL, signed URLs, cookies, or request bodies (notably dynamic fund pages).
    It also supports a source-specific browser URL such as Dimensional's Document Center.
    """
    if not BROWSER_FALLBACK:
        return []

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright fallback is enabled but playwright is not installed. "
            "Run: pip install playwright && python -m playwright install chromium"
        ) from exc

    docs: list[DownloadedDocument] = []
    captured_responses: list[DownloadedDocument] = []
    network_urls: list[str] = []
    seen_response_keys: set[str] = set()
    seen_urls: set[str] = set()
    browser_url = source.browser_url or source.landing_url
    dfac_network_seen: set[str] = set()
    dfac_network_log_count = 0

    def add_response_doc(response: Any) -> None:
        """Capture useful network response bodies while the browser session is alive."""
        nonlocal dfac_network_log_count
        try:
            url = response.url
            url_l = url.lower()
            headers = response.headers
            ct = headers.get("content-type", "").lower()
            resource_type = response.request.resource_type

            if (
                source.ticker == "DFAC"
                and resource_type in {"xhr", "fetch"}
                and ("dimensional.com" in url_l or "dimensionaltools" in url_l)
                and not any(x in url_l for x in ("google", "doubleclick", "nr-data", "newrelic", "hubspot"))
                and url not in dfac_network_seen
                and dfac_network_log_count < 60
            ):
                dfac_network_seen.add(url)
                dfac_network_log_count += 1
                try:
                    status = response.status
                except Exception:
                    status = "?"
                try:
                    req = response.request
                    req_method = req.method
                    req_post = (req.post_data or "")[:1800].replace("\n", " ")
                    raw_headers = req.headers or {}
                    safe_headers = {}
                    for hk, hv in raw_headers.items():
                        hkl = str(hk).lower()
                        # Never log cookies, authorization, or generic browser fingerprint headers.
                        if hkl in {"referer", "origin", "content-type"} or (
                            hkl.startswith("x-")
                            and any(tok in hkl for tok in ("portfolio", "fund", "ticker", "identifier", "slug", "dfa"))
                        ):
                            safe_headers[str(hk)] = str(hv)[:1000]
                    req_headers = json.dumps(safe_headers, ensure_ascii=False, separators=(",", ":"))[:2600]
                except Exception:
                    req_method = "?"
                    req_post = ""
                    req_headers = "{}"
                LOG.info(
                    "DFAC network response status=%s type=%s method=%s ct=%s url=%s post_data=%s request_headers=%s",
                    status, resource_type, req_method, ct[:100], url, req_post, req_headers,
                )

            fileish_ct = any(
                marker in ct
                for marker in (
                    "text/csv",
                    "spreadsheet",
                    "excel",
                    "octet-stream",
                    "application/json",
                    "text/json",
                )
            )
            fileish_url = bool(re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", url_l))
            xhrish = resource_type in {"xhr", "fetch"}
            likely_url = any(
                token in url_l
                for token in (
                    source.ticker.lower(),
                    "holding",
                    "portfolio",
                    "position",
                    "document",
                    "fund",
                )
            )
            # Capture all JSON XHR/fetch responses (bounded below), plus obvious file responses.
            if not ((xhrish and "json" in ct) or fileish_ct or fileish_url or likely_url):
                return

            body = response.body()
            if not body or len(body) > 25_000_000:
                return

            if (
                source.ticker == "DFAC"
                and "json" in ct
                and (
                    "etf.dimensional.com/public/v2/" in url_l
                    or "investment-api/portfolio-details" in url_l
                    or "investment-api/portfolio-disclosures" in url_l
                )
            ):
                for candidate_url in _dimensional_dfac_api_diagnostics(body, url):
                    network_urls.append(candidate_url)

            # Dimensional Document Center returns metadata through document-grid.
            # Pull only URLs tied to the DFAC Daily Holdings record instead of
            # scanning every fund document in the response.
            if source.ticker == "DFAC" and ("json" in ct or "document-grid" in url_l):
                if "document-grid" in url_l:
                    try:
                        req = response.request
                        post_data = req.post_data or ""
                        LOG.info(
                            "DFAC document-grid request method=%s url=%s post_data=%s",
                            req.method, url, post_data[:1800].replace("\n", " "),
                        )
                    except Exception:
                        pass
                for candidate_url in _dimensional_dfac_urls_from_json(body, url):
                    network_urls.append(candidate_url)

            # Dynamic document centers often return metadata JSON containing the real
            # CSV/XLSX download URL rather than the holdings rows themselves. Discover
            # those URLs while the response body is available.
            try:
                body_text_for_urls = body[:1_000_000].decode("utf-8", errors="ignore").replace("\\/", "/")
                for found in re.findall(r"https?://[^\"'<>\s]+|/[^\"'<>\s]+", body_text_for_urls, flags=re.I):
                    candidate_url = urljoin(url, found.replace("&amp;", "&"))
                    candidate_l = candidate_url.lower()
                    if (
                        "holding" in candidate_l
                        or "portfolio" in candidate_l
                        or re.search(r"\.(csv|xlsx|xls)(?:$|[?#])", candidate_l)
                    ):
                        network_urls.append(candidate_url)
            except Exception:
                pass

            # Avoid filling memory with analytics/config JSON unrelated to holdings.
            if (xhrish and "json" in ct) and not likely_url:
                sample = body[:250_000].decode("utf-8", errors="ignore").lower()
                body_signal = any(
                    token in sample
                    for token in (
                        source.ticker.lower(),
                        '"shares"',
                        '"quantity"',
                        '"ticker"',
                        '"symbol"',
                        "holding",
                        "portfolio",
                    )
                )
                if not body_signal:
                    return

            key = f"{url}|{len(body)}|{hashlib.sha1(body).hexdigest()}"
            if key in seen_response_keys:
                return
            seen_response_keys.add(key)
            captured_responses.append(
                DownloadedDocument(
                    data=body,
                    source_url=url,
                    filename=filename_from_response(url, headers),
                    content_type=ct,
                    last_modified=headers.get("last-modified", ""),
                )
            )
            network_urls.append(url)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=USER_AGENT,
            locale="en-US",
        )
        page = context.new_page()
        page.on("response", add_response_doc)

        LOG.info("%s browser fallback: %s", source.ticker, browser_url)
        page.goto(browser_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2200)

        # Common location/cookie gates. Some sites present these sequentially.
        for _ in range(2):
            for common in (
                "United States",
                "Accept & Continue",
                "Accept All",
                "Accept Cookies",
                "I Accept",
                "Agree",
            ):
                _playwright_click_if_present(page, common, timeout_ms=1800)

        # Dimensional's Document Center and similar pages work much better after filtering
        # to the ticker before looking for the Daily Holdings control.
        if source.browser_search_term:
            _playwright_fill_search(page, source.browser_search_term)

        if source.ticker == "DFAC":
            try:
                body_sample0 = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=3000))[:5000]
            except Exception:
                body_sample0 = ""
            LOG.info("DFAC initial fund-page state current_url=%s title=%s body_sample=%s", page.url, page.title()[:220], body_sample0)
            _playwright_log_dfac_controls(page)

            # v13 fast path: funddetail normally arrives during initial page rendering and
            # exposes the exact official date-stamped fullHoldingsCsvUrl. Fetch that file
            # immediately and skip minutes of generic DOM/link probing.
            official_full_urls = []
            for candidate_url in network_urls:
                if re.match(
                    r"^https://tools-blob\.dimensional\.com/etf/20\d{6}/DFAC\.csv(?:[?#].*)?$",
                    candidate_url,
                    flags=re.I,
                ) and candidate_url not in official_full_urls:
                    official_full_urls.append(candidate_url)
            if official_full_urls:
                full_url = official_full_urls[0]
                try:
                    LOG.info("DFAC fast-path official full holdings URL: %s", full_url)
                    resp = context.request.get(full_url, timeout=REQUEST_TIMEOUT * 1000)
                    if resp.ok:
                        headers = resp.headers
                        body = resp.body()
                        if body:
                            doc = DownloadedDocument(
                                data=body,
                                source_url=full_url,
                                filename=filename_from_response(full_url, headers),
                                content_type=headers.get("content-type", ""),
                                last_modified=headers.get("last-modified", ""),
                            )
                            LOG.info("DFAC fast-path downloaded %d bytes", len(body))
                            context.close()
                            browser.close()
                            return [doc]
                    LOG.info("DFAC fast-path URL did not return a usable file; continuing browser fallback")
                except Exception as exc:
                    LOG.info("DFAC fast-path fetch failed; continuing browser fallback: %s", exc)

        # On Dimensional, click Daily Holdings inside the DFAC result only.
        # IMPORTANT: a Daily Holdings control may directly start a browser download.
        # Earlier versions clicked it outside expect_download(), which meant the file
        # could be successfully delivered by the site but silently lost by our scraper.
        if source.ticker == "DFAC":
            dfac_clicked = False

            def click_dfac_and_capture_download() -> bool:
                clicked = False
                try:
                    with page.expect_download(timeout=8000) as download_info:
                        clicked = _playwright_click_dfac_daily_holdings(page)
                        if not clicked:
                            raise RuntimeError("DFAC Daily Holdings control not found")
                    download = download_info.value
                    tmp_path = Path(download.path())
                    docs.append(
                        DownloadedDocument(
                            data=tmp_path.read_bytes(),
                            source_url=download.url or page.url,
                            filename=download.suggested_filename or "dfac-daily-holdings",
                            content_type="application/octet-stream",
                        )
                    )
                    LOG.info("DFAC captured Daily Holdings browser download: %s", download.suggested_filename)
                    return True
                except RuntimeError:
                    return False
                except PlaywrightTimeoutError:
                    # The click may have produced an XHR/navigation rather than a file
                    # download. Response bodies are captured by add_response_doc().
                    return clicked
                except Exception as exc:
                    LOG.debug("DFAC specific Daily Holdings click/download failed: %s", exc)
                    return clicked

            dfac_clicked = click_dfac_and_capture_download()
            if not dfac_clicked:
                # Some Document Center builds index the fund name more reliably than
                # the exchange ticker. Retry with the exact fund name before giving up.
                if _playwright_fill_search(page, "US Core Equity 2 ETF"):
                    dfac_clicked = click_dfac_and_capture_download()
            LOG.info("DFAC targeted Daily Holdings interaction: %s", "clicked" if dfac_clicked else "not found")

            # v10 product-page path: the Document Center currently exposes the DFAC drawer but
            # may omit a Daily Holdings link. Visit the dedicated DFAC fund page and let
            # the same response listener capture holdings/portfolio XHRs. Because this
            # page is fund-specific, generic Holdings controls are safe to click here.
            if not dfac_clicked:
                try:
                    LOG.info("DFAC product-page fallback: %s", source.landing_url)
                    page.goto(source.landing_url, wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(3200)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass

                    # Resolve Dimensional's audience/country gate. The public product
                    # page can otherwise show only the generic Explore Funds shell.
                    role_clicks = _playwright_dimensional_set_us_professional_role(page)
                    for common in ("Accept All", "Accept Cookies", "I Accept", "Agree"):
                        _playwright_click_if_present(page, common, timeout_ms=1200)

                    try:
                        title = page.title()
                    except Exception:
                        title = ""
                    try:
                        body_sample = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=2500))[:1800]
                    except Exception:
                        body_sample = ""
                    LOG.info(
                        "DFAC product-page state after role selection current_url=%s title=%s role_clicks=%s body_sample=%s",
                        page.url, title[:220], role_clicks, body_sample,
                    )
                    _playwright_log_dfac_controls(page)

                    # If the role selector or client router dropped us back on the generic
                    # fund directory, navigate to DFAC again now that the role cookies are set.
                    if "/funds/dfac/" not in page.url.lower() or "us core equity 2" not in body_sample.lower():
                        LOG.info("DFAC product-page retry after role selection")
                        page.goto(source.landing_url, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(4200)
                        try:
                            page.wait_for_load_state("networkidle", timeout=12_000)
                        except Exception:
                            pass
                        try:
                            title = page.title()
                        except Exception:
                            title = ""
                        try:
                            body_sample = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=2500))[:1800]
                        except Exception:
                            body_sample = ""
                        LOG.info(
                            "DFAC product-page state after retry current_url=%s title=%s body_sample=%s",
                            page.url, title[:220], body_sample,
                        )
                        _playwright_log_dfac_controls(page)

                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.6)")
                        page.wait_for_timeout(900)
                    except Exception:
                        pass

                    interaction = "none"
                    for text in (
                        "Daily Holdings", "View Holdings", "Full Holdings",
                        "Portfolio Holdings", "Holdings", "Portfolio",
                        "Portfolio Characteristics", "Portfolio Composition",
                        "Fund Holdings", "Download",
                    ):
                        locator = None
                        for factory in (
                            lambda t=text: page.get_by_role("link", name=re.compile(re.escape(t), re.I)).first,
                            lambda t=text: page.get_by_role("button", name=re.compile(re.escape(t), re.I)).first,
                            lambda t=text: page.get_by_text(re.compile(re.escape(t), re.I), exact=False).first,
                        ):
                            try:
                                cand = factory()
                                if cand.is_visible(timeout=1200):
                                    locator = cand
                                    break
                            except Exception:
                                pass
                        if locator is None:
                            continue
                        try:
                            locator.scroll_into_view_if_needed(timeout=1800)
                        except Exception:
                            pass
                        clicked = False
                        try:
                            with page.expect_download(timeout=4500) as download_info:
                                locator.click(timeout=3500)
                                clicked = True
                            download = download_info.value
                            tmp_path = Path(download.path())
                            docs.append(
                                DownloadedDocument(
                                    data=tmp_path.read_bytes(),
                                    source_url=download.url or page.url,
                                    filename=download.suggested_filename or "dfac-product-holdings",
                                    content_type="application/octet-stream",
                                )
                            )
                            interaction = f"download:{text}"
                            LOG.info("DFAC product-page captured download via %s: %s", text, download.suggested_filename)
                            break
                        except PlaywrightTimeoutError:
                            # Click still matters: it may reveal a panel or fire an XHR,
                            # which add_response_doc() captures asynchronously.
                            if not clicked:
                                try:
                                    locator.click(timeout=2500)
                                    clicked = True
                                except Exception:
                                    pass
                            if clicked:
                                interaction = f"clicked:{text}"
                                page.wait_for_timeout(1800)
                                LOG.info("DFAC product-page clicked control: %s", text)
                                # Continue so a second control such as Full Holdings can
                                # appear after opening a Holdings tab.
                        except Exception as exc:
                            LOG.debug("DFAC product-page control %r failed: %s", text, exc)

                    try:
                        anchors = page.locator("a[href]")
                        logged = 0
                        for i in range(min(anchors.count(), 500)):
                            a = anchors.nth(i)
                            href = a.get_attribute("href") or ""
                            try:
                                txt = a.inner_text(timeout=400)
                            except Exception:
                                txt = ""
                            absolute = urljoin(page.url, href)
                            sig = f"{txt} {absolute}".lower()
                            if any(k in sig for k in ("holding", "portfolio", ".csv", ".xlsx", ".xls")):
                                network_urls.append(absolute)
                                if logged < 12:
                                    LOG.info("DFAC product-page candidate link text=%s url=%s", txt[:120], absolute)
                                    logged += 1
                    except Exception:
                        pass

                    LOG.info("DFAC product-page interaction result: %s current_url=%s", interaction, page.url)
                except Exception as exc:
                    LOG.info("DFAC product-page fallback failed: %s", exc)

        for text in source.browser_pre_click:
            _playwright_click_if_present(page, text)

        # Scroll through the page once so lazy-loaded portfolio modules initialize.
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.55)")
            page.wait_for_timeout(1000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1200)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        # Collect promising anchors after JS has rendered.
        try:
            anchors = page.locator("a[href]")
            for i in range(min(anchors.count(), 800)):
                a = anchors.nth(i)
                href = a.get_attribute("href") or ""
                try:
                    text = a.inner_text(timeout=600)
                except Exception:
                    text = ""
                absolute = urljoin(page.url, href)
                if _download_link_score(source, absolute, text) >= 7:
                    network_urls.append(absolute)
        except Exception:
            pass

        if source.ticker == "AVUV":
            LOG.info("AVUV holdings browser current URL: %s", page.url)
            # If the numeric holdings route redirects back to the marketing page, look
            # for a dynamically rendered Total Holdings link before trying controls.
            if "total-holdings" not in page.url.lower():
                try:
                    total_links = page.locator('a[href*="total-holdings"]')
                    for i in range(min(total_links.count(), 30)):
                        href = total_links.nth(i).get_attribute("href") or ""
                        absolute = urljoin(page.url, href)
                        if absolute:
                            network_urls.insert(0, absolute)
                            LOG.info("AVUV discovered Total Holdings link: %s", absolute)
                except Exception:
                    pass

        # Try download controls. If a control triggers XHR instead of a file download,
        # response bodies are already captured by add_response_doc(). For DFAC we skip
        # generic controls because clicking the first global Daily Holdings item can
        # select an unrelated fund; the targeted routine above is the only safe click.
        for text in source.browser_download_texts:
            if source.ticker == "DFAC":
                continue
            locator = None
            for factory in (
                lambda: page.get_by_role("link", name=re.compile(re.escape(text), re.I)).first,
                lambda: page.get_by_role("button", name=re.compile(re.escape(text), re.I)).first,
                lambda: page.get_by_text(re.compile(re.escape(text), re.I), exact=False).first,
            ):
                try:
                    candidate = factory()
                    if candidate.is_visible(timeout=1800):
                        locator = candidate
                        break
                except Exception:
                    pass
            if locator is None:
                continue

            try:
                locator.scroll_into_view_if_needed(timeout=2500)
            except Exception:
                pass

            try:
                with page.expect_download(timeout=8000) as download_info:
                    locator.click(timeout=5000)
                download = download_info.value
                tmp_path = Path(download.path())
                docs.append(
                    DownloadedDocument(
                        data=tmp_path.read_bytes(),
                        source_url=download.url or page.url,
                        filename=download.suggested_filename or f"{source.ticker}-holdings",
                        content_type="application/octet-stream",
                    )
                )
                # Keep going: some providers expose both a display API and a download file.
            except PlaywrightTimeoutError:
                try:
                    locator.click(timeout=2500)
                except Exception:
                    pass
                page.wait_for_timeout(1800)
            except Exception as exc:
                LOG.debug("%s click %r did not download: %s", source.ticker, text, exc)

        page.wait_for_timeout(1500)

        # Add captured POST/GraphQL/XHR bodies first; these cannot safely be reconstructed
        # later with requests.get().
        docs.extend(captured_responses)

        # Fetch promising GET-able URLs using the browser request context so cookies are shared.
        for url in network_urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)

            if url.lower().startswith("data:"):
                decoded = _document_from_data_uri(url, source)
                if decoded is not None:
                    docs.append(decoded)
                    LOG.info("%s decoded browser data URI into %d bytes", source.ticker, len(decoded.data))
                continue

            try:
                resp = context.request.get(url, timeout=REQUEST_TIMEOUT * 1000)
                if not resp.ok:
                    continue
                headers = resp.headers
                body = resp.body()
                if not body:
                    continue
                docs.append(
                    DownloadedDocument(
                        data=body,
                        source_url=url,
                        filename=filename_from_response(url, headers),
                        content_type=headers.get("content-type", ""),
                        last_modified=headers.get("last-modified", ""),
                    )
                )
            except Exception as exc:
                LOG.debug("%s browser URL candidate failed: %s", source.ticker, exc)

        # Always preserve rendered HTML for sites whose holdings table exists directly in DOM.
        if source.allow_html_table:
            docs.append(
                DownloadedDocument(
                    data=page.content().encode("utf-8"),
                    source_url=page.url,
                    filename=f"{source.ticker.lower()}-browser.html",
                    content_type="text/html",
                )
            )

        context.close()
        browser.close()

    return docs


# -----------------------------------------------------------------------------
# Robust tabular parsing
# -----------------------------------------------------------------------------
def clean_header(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    s = str(value).replace("\ufeff", " ").replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def normalized_header(value: Any) -> str:
    s = clean_header(value).lower()
    s = s.replace("％", "%")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def alias_set(source: ETFSource, key: str) -> set[str]:
    values = list(GENERIC_ALIASES.get(key, ())) + list(source.aliases.get(key, ()))
    return {normalized_header(v) for v in values}


def header_score(headers: Iterable[Any], source: ETFSource) -> int:
    norm = {normalized_header(h) for h in headers if clean_header(h)}
    score = 0
    if norm & alias_set(source, "ticker"):
        score += 4
    if norm & alias_set(source, "shares"):
        score += 4
    if norm & alias_set(source, "company"):
        score += 2
    if norm & alias_set(source, "weight"):
        score += 2
    return score


def find_column(df: pd.DataFrame, source: ETFSource, key: str) -> str | None:
    aliases = alias_set(source, key)
    by_norm = {normalized_header(c): c for c in df.columns}
    for alias in aliases:
        if alias in by_norm:
            return by_norm[alias]

    # Conservative fuzzy fallbacks for small provider naming changes.
    for norm, original in by_norm.items():
        if key == "ticker" and any(word in norm for word in ("ticker", "symbol")):
            return original
        if key == "shares" and (
            "share" in norm
            or "quantity" in norm
            or norm in {"qty", "position", "units", "unit"}
            or ("position" in norm and "value" not in norm)
        ):
            return original
        if key == "weight" and (
            "weight" in norm
            or (("percent" in norm or "pct" in norm or "%" in norm)
                and any(x in norm for x in ("asset", "portfolio", "market", "nav")))
            or ("net asset" in norm and ("%" in norm or "percent" in norm or "pct" in norm))
            or ("market value" in norm and "%" in norm)
        ):
            return original
        if key == "company" and any(
            word in norm
            for word in ("security name", "company", "description", "issuer", "security", "holding name")
        ):
            return original
    return None


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _frame_from_delimited_text(text: str, source: ETFSource) -> tuple[pd.DataFrame, str]:
    lines = text.splitlines()
    preamble = "\n".join(lines[:50])
    delimiters = (",", "\t", ";", "|")

    best: tuple[int, int, str, list[str]] | None = None
    for i, line in enumerate(lines[:60]):
        if not line.strip():
            continue
        for delimiter in delimiters:
            try:
                cells = next(csv.reader([line], delimiter=delimiter))
            except Exception:
                continue
            score = header_score(cells, source)
            candidate = (score, -i, delimiter, cells)
            if best is None or candidate > best:
                best = candidate

    if best is None or best[0] < 8:
        raise ValueError("Could not identify a header row containing both ticker and shares columns")

    header_index = -best[1]
    delimiter = best[2]
    body = "\n".join(lines[header_index:])
    df = pd.read_csv(
        io.StringIO(body),
        sep=delimiter,
        dtype=str,
        engine="python",
        on_bad_lines="skip",
    )
    df.columns = [clean_header(c) for c in df.columns]
    return df, preamble


def _excel_candidates(data: bytes, source: ETFSource) -> list[tuple[pd.DataFrame, str]]:
    candidates: list[tuple[pd.DataFrame, str]] = []
    xls = pd.ExcelFile(io.BytesIO(data))
    for sheet in xls.sheet_names:
        raw = pd.read_excel(xls, sheet_name=sheet, header=None, dtype=object)
        if raw.empty:
            continue
        meta = " | ".join(
            clean_header(x)
            for x in raw.iloc[: min(40, len(raw))].to_numpy().ravel().tolist()
            if clean_header(x)
        )
        best_score = -1
        best_row = None
        for i in range(min(60, len(raw))):
            score = header_score(raw.iloc[i].tolist(), source)
            if score > best_score:
                best_score = score
                best_row = i
        if best_row is not None and best_score >= 8:
            df = raw.iloc[best_row + 1 :].copy()
            headers = [clean_header(x) or f"unnamed_{j}" for j, x in enumerate(raw.iloc[best_row])]
            # Make duplicate headers deterministic.
            counts: dict[str, int] = {}
            unique_headers: list[str] = []
            for h in headers:
                counts[h] = counts.get(h, 0) + 1
                unique_headers.append(h if counts[h] == 1 else f"{h}_{counts[h]}")
            df.columns = unique_headers
            df = df.dropna(how="all")
            candidates.append((df, meta))
    return candidates


def _html_candidates(text: str, source: ETFSource) -> list[tuple[pd.DataFrame, str]]:
    candidates: list[tuple[pd.DataFrame, str]] = []
    try:
        # Visible text joins values split by HTML tags (e.g. "As of" + <span>08/14/2026</span>),
        # which raw HTML date regexes can otherwise miss.
        visible_text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        tables = pd.read_html(io.StringIO(text))
    except (ValueError, ImportError):
        return []
    metadata = visible_text[:100_000]
    for table in tables:
        table.columns = [clean_header(c) for c in table.columns]
        if header_score(table.columns, source) >= 8:
            candidates.append((table, metadata))
    return candidates


def _walk_json_tables(obj: Any) -> Iterable[list[dict[str, Any]]]:
    if isinstance(obj, list):
        if len(obj) >= 2 and all(isinstance(x, dict) for x in obj):
            yield obj
        for x in obj:
            yield from _walk_json_tables(x)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_json_tables(value)


def _json_candidates(text: str, source: ETFSource) -> list[tuple[pd.DataFrame, str]]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return []
    candidates: list[tuple[pd.DataFrame, str]] = []
    for rows in _walk_json_tables(obj):
        df = pd.DataFrame(rows)
        if header_score(df.columns, source) >= 8:
            candidates.append((df, text[:20_000]))
    return candidates


def parse_number(value: Any) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return float("nan")
    s = str(value).strip().replace("\xa0", "")
    if not s or s.upper() in {"N/A", "NA", "NONE", "NULL", "-", "--"}:
        return float("nan")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace(",", "").replace("$", "").replace("%", "")
    s = re.sub(r"[^0-9eE+\-.]", "", s)
    if s in {"", "+", "-", "."}:
        return float("nan")
    try:
        n = float(s)
        return -n if negative else n
    except ValueError:
        return float("nan")


def parse_weight(series: pd.Series) -> pd.Series:
    raw = series.astype(str)
    had_percent = raw.str.contains("%", regex=False, na=False).any()
    values = raw.map(parse_number).astype(float)
    finite = values[pd.notna(values)]
    if finite.empty:
        return values

    # Normalize decimal fractions (0.0123) to percentage points (1.23).
    # If a % sign is present, the numeric value is already in percentage points.
    if not had_percent:
        total = finite.abs().sum()
        max_abs = finite.abs().max()
        if max_abs <= 1.0 and total <= 2.0:
            values = values * 100.0
    return values


def parse_date_string(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None



def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    d = date(year, month, day)
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    shift = (weekday - d.weekday()) % 7
    return d + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    # Gregorian computus (Meeus/Jones/Butcher), used only to derive Good Friday.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _is_regular_nyse_holiday(d: date) -> bool:
    """Regular modern NYSE full-day holidays; enough for daily snapshot dating.

    This intentionally does not try to encode exceptional one-off closures. If a
    future exceptional closure occurs and Dimensional's file lacks an embedded date,
    strict mode will still avoid duplicate snapshots because the official blob's
    Last-Modified timestamp does not change until a new file is published.
    """
    y = d.year
    holidays = {
        _observed_fixed_holiday(y, 1, 1),                 # New Year's Day
        _nth_weekday(y, 1, 0, 3),                        # MLK Day
        _nth_weekday(y, 2, 0, 3),                        # Presidents Day
        _easter_sunday(y) - timedelta(days=2),           # Good Friday
        _last_weekday(y, 5, 0),                          # Memorial Day
        _observed_fixed_holiday(y, 7, 4),                # Independence Day
        _nth_weekday(y, 9, 0, 1),                        # Labor Day
        _nth_weekday(y, 11, 3, 4),                       # Thanksgiving
        _observed_fixed_holiday(y, 12, 25),              # Christmas
    }
    if y >= 2022:
        holidays.add(_observed_fixed_holiday(y, 6, 19))  # Juneteenth
    # New Year's observed date may fall in the prior calendar year.
    holidays.add(_observed_fixed_holiday(y + 1, 1, 1))
    return d in holidays


def _previous_nyse_session(before_date: date) -> date:
    d = before_date - timedelta(days=1)
    while d.weekday() >= 5 or _is_regular_nyse_holiday(d):
        d -= timedelta(days=1)
    return d


def _dimensional_full_holdings_url_as_of(document: DownloadedDocument) -> date | None:
    """Read the holdings date encoded in Dimensional's official fullHoldingsCsvUrl.

    Current funddetail responses expose URLs such as:
    https://tools-blob.dimensional.com/etf/20260813/DFAC.csv
    The YYYYMMDD path component is the holdings as-of date supplied by Dimensional.
    This is safer than inferring a date from fetch time or publication headers.
    """
    url = document.source_url.strip()
    m = re.search(
        r"^https://tools-blob\.dimensional\.com/etf/(20\d{6})/[^/?#]+\.csv(?:[?#].*)?$",
        url,
        flags=re.I,
    )
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _dimensional_last_modified_as_of(document: DownloadedDocument) -> date | None:
    """Infer holdings as-of date from the official Dimensional blob publication time.

    Dimensional's daily-holdings CSVs can omit an embedded date. Their Azure Blob
    Last-Modified timestamp identifies when a new daily file was published; the
    holdings are disclosed with a one-session lag, so the relevant as-of date is
    the prior NYSE session. This fallback is limited to the official Dimensional
    daily-holdings blob path and is never used for other providers.
    """
    url_l = document.source_url.lower()
    if not (
        "dimensionaltools.blob.core.windows.net" in url_l
        and "/etf/daily-holdings/" in url_l
        and document.last_modified
    ):
        return None
    try:
        modified = parsedate_to_datetime(document.last_modified)
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=ZoneInfo("UTC"))
        ny_date = modified.astimezone(NY_TZ).date()
        return _previous_nyse_session(ny_date)
    except Exception as exc:
        LOG.debug("Could not parse Dimensional Last-Modified %r: %s", document.last_modified, exc)
        return None


def extract_as_of_date(metadata_text: str, fallback_filename: str) -> date | None:
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(metadata_text):
            parsed = parse_date_string(match.group(1))
            if parsed:
                return parsed

    # Dated filenames are common for holdings exports.
    name = Path(fallback_filename).name
    for pattern in (
        re.compile(r"(20\d{2})[-_](\d{1,2})[-_](\d{1,2})"),
        re.compile(r"(\d{1,2})[-_](\d{1,2})[-_](20\d{2})"),
    ):
        m = pattern.search(name)
        if not m:
            continue
        try:
            if len(m.group(1)) == 4:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def is_probable_ticker(value: str) -> bool:
    s = value.strip().upper()
    if s in EXCLUDED_TICKERS:
        return False
    if len(s) > 24 or len(s) < 1:
        return False
    if not re.search(r"[A-Z]", s):
        return False
    # Allows BRK.B, BF-B, foreign suffixes, etc.; excludes obvious prose.
    if " " in s and len(s.split()) > 2:
        return False
    return True


def normalize_holdings(df: pd.DataFrame, source: ETFSource) -> pd.DataFrame:
    ticker_col = find_column(df, source, "ticker")
    shares_col = find_column(df, source, "shares")
    company_col = find_column(df, source, "company")
    weight_col = find_column(df, source, "weight")

    if ticker_col is None or shares_col is None:
        raise ValueError(
            f"{source.ticker}: required columns not found. "
            f"ticker={ticker_col!r}, shares={shares_col!r}, columns={list(df.columns)!r}"
        )

    out = pd.DataFrame()
    out["Stock_Ticker"] = (
        df[ticker_col]
        .astype(str)
        .str.replace("\ufeff", "", regex=False)
        .str.strip()
        .str.upper()
    )
    if company_col is not None:
        out["Company_Name"] = df[company_col].astype(str).str.strip()
    else:
        out["Company_Name"] = out["Stock_Ticker"]
    out["Shares"] = df[shares_col].map(parse_number).astype(float)
    out["Weight"] = parse_weight(df[weight_col]) if weight_col is not None else float("nan")

    out = out[out["Stock_Ticker"].map(is_probable_ticker)]
    out = out[pd.notna(out["Shares"])]
    out = out[~out["Company_Name"].str.upper().isin(EXCLUDED_TICKERS)]
    if out.empty:
        raise ValueError(f"{source.ticker}: no usable ticker/share rows after normalization")

    # A provider may split one ticker over multiple lots/lines. Aggregate deterministically.
    out["Company_Name"] = out["Company_Name"].replace({"nan": "", "None": ""})
    grouped = (
        out.groupby("Stock_Ticker", as_index=False)
        .agg(
            Company_Name=(
                "Company_Name",
                lambda s: next((x for x in s if isinstance(x, str) and x.strip()), ""),
            ),
            Shares=("Shares", "sum"),
            Weight=("Weight", lambda s: s.sum(min_count=1)),
        )
        .sort_values("Stock_Ticker")
        .reset_index(drop=True)
    )
    return grouped


def parse_document(document: DownloadedDocument, source: ETFSource) -> ParsedHoldings:
    data = document.data
    ct = document.content_type.lower()
    filename_l = document.filename.lower()
    text: str | None = None
    candidates: list[tuple[pd.DataFrame, str]] = []

    is_xlsx = data[:4] == b"PK\x03\x04" or filename_l.endswith(".xlsx")
    is_xls = data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or filename_l.endswith(".xls")

    if is_xlsx or is_xls or "spreadsheet" in ct or "excel" in ct:
        candidates.extend(_excel_candidates(data, source))
    else:
        text = _decode_text(data)
        stripped = text.lstrip()
        if stripped.startswith("{") or stripped.startswith("[") or "json" in ct:
            candidates.extend(_json_candidates(text, source))
        if "html" in ct or stripped.startswith("<"):
            candidates.extend(_html_candidates(text, source))
        # application/octet-stream often hides CSV, so always try delimited text last.
        try:
            candidates.append(_frame_from_delimited_text(text, source))
        except Exception:
            pass

    errors: list[str] = []
    for frame, meta in candidates:
        try:
            normalized = normalize_holdings(frame, source)
            if len(normalized) < source.min_rows:
                raise ValueError(
                    f"only {len(normalized)} normalized rows; expected at least {source.min_rows}"
                )
            as_of = extract_as_of_date(meta, document.filename)
            if as_of is None and source.ticker == "DFAC":
                # v13: funddetail gives the authoritative date directly in the official
                # fullHoldingsCsvUrl path, e.g. /etf/20260813/DFAC.csv.
                as_of = _dimensional_full_holdings_url_as_of(document)
                if as_of is not None:
                    today_ny = datetime.now(NY_TZ).date()
                    age_days = (today_ny - as_of).days
                    if age_days < 0 or age_days > 7:
                        raise ValueError(
                            f"Dimensional full-holdings URL has implausible as-of date {as_of.isoformat()}"
                        )
                    LOG.info(
                        "DFAC as-of %s from official fullHoldingsCsvUrl",
                        as_of.isoformat(),
                    )
                else:
                    as_of = _dimensional_last_modified_as_of(document)
                    if as_of is not None:
                        today_ny = datetime.now(NY_TZ).date()
                        if (today_ny - as_of).days > 7:
                            raise ValueError(
                                f"Dimensional daily-holdings blob appears stale: inferred as-of {as_of.isoformat()} "
                                f"from Last-Modified {document.last_modified}"
                            )
                        LOG.info(
                            "DFAC inferred as-of %s from official blob Last-Modified %s",
                            as_of.isoformat(),
                            document.last_modified,
                        )
            if as_of is None:
                # Do NOT silently use fetch date: that can turn a stale website file into
                # a false "new day" and generate fake deltas.
                raise ValueError("holdings as-of date could not be identified")
            return ParsedHoldings(normalized, as_of, meta)
        except Exception as exc:
            errors.append(str(exc))

    raise ValueError(
        f"{source.ticker}: no candidate table could be normalized from {document.source_url}. "
        f"Parser errors: {errors[-3:]}"
    )


# -----------------------------------------------------------------------------
# Fetch source using direct -> static discovery -> browser fallback
# -----------------------------------------------------------------------------
def fetch_source(source: ETFSource, session: requests.Session) -> tuple[DownloadedDocument, ParsedHoldings]:
    errors: list[str] = []
    attempted_urls: set[str] = set()

    def try_docs(docs: Iterable[DownloadedDocument]) -> tuple[DownloadedDocument, ParsedHoldings] | None:
        for doc in docs:
            key = f"{doc.source_url}|{len(doc.data)}|{hashlib.sha1(doc.data).hexdigest()}"
            if key in attempted_urls:
                continue
            attempted_urls.add(key)
            try:
                parsed = parse_document(doc, source)
                LOG.info(
                    "%s parsed %d rows, as-of %s from %s",
                    source.ticker,
                    len(parsed.frame),
                    parsed.as_of_date,
                    doc.source_url,
                )
                return doc, parsed
            except Exception as exc:
                errors.append(str(exc))
                LOG.warning("%s candidate rejected: %s", source.ticker, exc)
        return None

    direct_docs: list[DownloadedDocument] = []
    for url in source.direct_urls:
        try:
            direct_docs.append(download_url(session, url))
        except Exception as exc:
            message = f"direct {url}: {exc}"
            errors.append(message)
            LOG.warning("%s direct candidate failed: %s", source.ticker, message)
    result = try_docs(direct_docs)
    if result:
        return result

    result = try_docs(static_candidates(session, source))
    if result:
        return result

    if BROWSER_FALLBACK:
        try:
            result = try_docs(browser_candidates(source))
            if result:
                return result
        except Exception as exc:
            errors.append(f"browser fallback: {exc}")

    raise RuntimeError(
        f"{source.ticker}: all official-source retrieval strategies failed. "
        + " | ".join(errors[-8:])
    )


# -----------------------------------------------------------------------------
# Snapshot persistence and delta calculation
# -----------------------------------------------------------------------------
def guess_extension(document: DownloadedDocument) -> str:
    name = document.filename.lower()
    if document.data[:4] == b"PK\x03\x04":
        return ".xlsx"
    if document.data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".xls"
    for ext in (".csv", ".xlsx", ".xls", ".json", ".html", ".htm"):
        if name.endswith(ext):
            return ext
    ct = document.content_type.lower()
    if "json" in ct:
        return ".json"
    if "html" in ct:
        return ".html"
    return ".csv"


def save_snapshot(
    source: ETFSource,
    document: DownloadedDocument,
    parsed: ParsedHoldings,
) -> dict[str, Any]:
    snapshot_dir = DATA_DIR / parsed.as_of_date.isoformat() / source.ticker
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    raw_path = snapshot_dir / f"raw{guess_extension(document)}"
    normalized_path = snapshot_dir / "normalized.csv"
    metadata_path = snapshot_dir / "metadata.json"

    raw_path.write_bytes(document.data)
    normalized = parsed.frame.copy()
    normalized.insert(0, "Date", parsed.as_of_date.isoformat())
    normalized.insert(1, "ETF_Ticker", source.ticker)
    normalized.to_csv(normalized_path, index=False)

    metadata = {
        "ETF_Ticker": source.ticker,
        "fund_name": source.fund_name,
        "provider": source.provider,
        "as_of_date": parsed.as_of_date.isoformat(),
        "fetched_at": datetime.now(NY_TZ).isoformat(),
        "source_url": document.source_url,
        "raw_filename": document.filename,
        "content_type": document.content_type,
        "sha256": hashlib.sha256(document.data).hexdigest(),
        "normalized_rows": int(len(parsed.frame)),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return metadata


def snapshot_paths(etf_ticker: str) -> list[tuple[date, Path]]:
    snapshots: list[tuple[date, Path]] = []
    if not DATA_DIR.exists():
        return snapshots
    for path in DATA_DIR.glob(f"*/{etf_ticker}/normalized.csv"):
        try:
            d = date.fromisoformat(path.parent.parent.name)
        except ValueError:
            continue
        snapshots.append((d, path))
    snapshots.sort(key=lambda x: x[0])
    return snapshots


def load_normalized(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"Stock_Ticker": str, "Company_Name": str})
    required = {"Stock_Ticker", "Company_Name", "Shares", "Weight"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Snapshot {path} missing columns: {sorted(missing)}")
    df["Shares"] = pd.to_numeric(df["Shares"], errors="coerce")
    df["Weight"] = pd.to_numeric(df["Weight"], errors="coerce")
    return df


def calculate_delta(etf_ticker: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    paths = snapshot_paths(etf_ticker)
    if not paths:
        return pd.DataFrame(), {"state": "no_snapshot"}
    if len(paths) == 1:
        return pd.DataFrame(), {
            "state": "baseline_created",
            "latest_as_of": paths[-1][0].isoformat(),
            "previous_as_of": None,
        }

    previous_date, previous_path = paths[-2]
    current_date, current_path = paths[-1]
    prev = load_normalized(previous_path)
    curr = load_normalized(current_path)

    prev = prev[["Stock_Ticker", "Company_Name", "Shares", "Weight"]].rename(
        columns={
            "Company_Name": "Company_Name_prev",
            "Shares": "Shares_prev",
            "Weight": "Weight_prev",
        }
    )
    curr = curr[["Stock_Ticker", "Company_Name", "Shares", "Weight"]].rename(
        columns={
            "Company_Name": "Company_Name_curr",
            "Shares": "Shares_curr",
            "Weight": "Weight_curr",
        }
    )
    merged = prev.merge(curr, on="Stock_Ticker", how="outer")
    merged["Shares_prev"] = pd.to_numeric(merged["Shares_prev"], errors="coerce").fillna(0.0)
    merged["Shares_curr"] = pd.to_numeric(merged["Shares_curr"], errors="coerce").fillna(0.0)
    merged["Shares_Change"] = merged["Shares_curr"] - merged["Shares_prev"]

    prev_w = pd.to_numeric(merged["Weight_prev"], errors="coerce").fillna(0.0)
    curr_w = pd.to_numeric(merged["Weight_curr"], errors="coerce").fillna(0.0)
    merged["Weight_Change"] = curr_w - prev_w

    def action(row: pd.Series) -> str | None:
        before = float(row["Shares_prev"])
        after = float(row["Shares_curr"])
        change = float(row["Shares_Change"])
        if abs(change) <= SHARES_EPSILON:
            return None
        if abs(before) <= SHARES_EPSILON and after > SHARES_EPSILON:
            return "New Position"
        if before > SHARES_EPSILON and abs(after) <= SHARES_EPSILON:
            return "Closed Position"
        if change > SHARES_EPSILON:
            return "Buy"
        return "Sell"

    merged["Action"] = merged.apply(action, axis=1)
    merged = merged[merged["Action"].notna()].copy()
    merged["Company_Name"] = merged["Company_Name_curr"].fillna(merged["Company_Name_prev"]).fillna("")
    merged.insert(0, "Date", current_date.isoformat())
    merged.insert(1, "ETF_Ticker", etf_ticker)

    result = merged[
        [
            "Date",
            "ETF_Ticker",
            "Stock_Ticker",
            "Company_Name",
            "Action",
            "Shares_Change",
            "Weight_Change",
        ]
    ].copy()
    result["Shares_Change"] = result["Shares_Change"].round(6)
    # Weight_Change is percentage points, e.g. +0.12 means +0.12 percentage point.
    result["Weight_Change"] = result["Weight_Change"].round(6)
    result = result.sort_values(
        ["ETF_Ticker", "Action", "Shares_Change", "Stock_Ticker"],
        ascending=[True, True, False, True],
    ).reset_index(drop=True)

    return result, {
        "state": "delta_ready",
        "latest_as_of": current_date.isoformat(),
        "previous_as_of": previous_date.isoformat(),
        "trade_rows": int(len(result)),
    }


def write_outputs(status: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []

    for ticker in SOURCES:
        try:
            delta, info = calculate_delta(ticker)
            status["sources"].setdefault(ticker, {}).update(info)
            if not delta.empty:
                frames.append(delta)
        except Exception as exc:
            status["sources"].setdefault(ticker, {})["delta_error"] = str(exc)
            LOG.exception("%s delta calculation failed", ticker)

    columns = [
        "Date",
        "ETF_Ticker",
        "Stock_Ticker",
        "Company_Name",
        "Action",
        "Shares_Change",
        "Weight_Change",
    ]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    combined = combined[columns]

    csv_path = OUTPUT_DIR / "daily_trades.csv"
    json_path = OUTPUT_DIR / "daily_trades.json"
    status_path = OUTPUT_DIR / "status.json"

    combined.to_csv(csv_path, index=False)
    records = combined.where(pd.notna(combined), None).to_dict(orient="records")
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    LOG.info("Wrote %d delta rows to %s and %s", len(combined), csv_path, json_path)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch ETF holdings and calculate latest daily deltas")
    parser.add_argument(
        "--etfs",
        nargs="*",
        default=list(SOURCES.keys()),
        choices=list(SOURCES.keys()),
        help="Subset to fetch; output still summarizes any snapshots already present for all ETFs",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any requested ETF cannot be fetched/parsed",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()

    status: dict[str, Any] = {
        "generated_at": datetime.now(NY_TZ).isoformat(),
        "timezone": "America/New_York",
        "sources": {},
    }
    failures: list[str] = []

    for ticker in args.etfs:
        source = SOURCES[ticker]
        try:
            document, parsed = fetch_source(source, session)
            metadata = save_snapshot(source, document, parsed)
            status["sources"][ticker] = {
                "fetch": "ok",
                "provider": source.provider,
                "as_of_date": parsed.as_of_date.isoformat(),
                "rows": int(len(parsed.frame)),
                "source_url": document.source_url,
                "sha256": metadata["sha256"],
            }
        except Exception as exc:
            failures.append(ticker)
            status["sources"][ticker] = {
                "fetch": "error",
                "provider": source.provider,
                "error": str(exc),
            }
            LOG.exception("%s failed", ticker)

    write_outputs(status)

    if failures and args.strict:
        LOG.error("Strict mode: failed ETFs: %s", ", ".join(failures))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
