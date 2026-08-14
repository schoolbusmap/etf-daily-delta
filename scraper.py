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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
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
        browser_url="https://www.dimensional.com/us-en/document-center",
        browser_search_term="DFAC",
        browser_pre_click=(),
        browser_download_texts=("Daily Holdings", "Download Holdings", "Holdings"),
        aliases={
            "ticker": ("Ticker", "Symbol", "Trading Symbol"),
            "company": ("Security Name", "Name", "Description", "Issuer Name"),
            "shares": ("Shares", "Shares Held", "Quantity", "Share Quantity"),
            "weight": ("Weight", "Weight (%)", "% of Net Assets", "Percent of Net Assets"),
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
        browser_pre_click=("United States", "Accept & Continue", "Portfolio"),
        browser_download_texts=(
            "Download Holdings",
            "Full Holdings",
            "Daily Holdings",
            "Holdings",
        ),
        aliases={
            "ticker": ("Ticker", "Symbol", "Trading Symbol"),
            "company": ("Security Name", "Company", "Name", "Description"),
            "shares": ("Shares", "Shares Held", "Quantity", "Share Quantity", "Units", "Position Quantity"),
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
        r"(?i)\bas\s+of(?:\s+date)?\s*[:\-]?\s*"
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

    def add_response_doc(response: Any) -> None:
        """Capture useful network response bodies while the browser session is alive."""
        try:
            url = response.url
            url_l = url.lower()
            headers = response.headers
            ct = headers.get("content-type", "").lower()
            resource_type = response.request.resource_type

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

        # Try download controls. If a control triggers XHR instead of a file download,
        # response bodies are already captured by add_response_doc().
        for text in source.browser_download_texts:
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
            as_of = extract_as_of_date(meta, document.filename)
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
            errors.append(f"direct {url}: {exc}")
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
