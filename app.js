const ETF_LIST = ["JEPI", "DFAC", "CGGR", "AVUV", "BLOK"];

let allRows = [];
let filteredRows = [];
let statusData = null;
let sortState = { key: "Date", direction: "desc" };

const els = {
  lastUpdated: document.getElementById("lastUpdated"),
  totalChanges: document.getElementById("totalChanges"),
  newPositionsCount: document.getElementById("newPositionsCount"),
  closedPositionsCount: document.getElementById("closedPositionsCount"),
  statusPill: document.getElementById("statusPill"),
  etfCards: document.getElementById("etfCards"),
  topBuys: document.getElementById("topBuys"),
  topSells: document.getElementById("topSells"),
  etfFilter: document.getElementById("etfFilter"),
  actionFilter: document.getElementById("actionFilter"),
  searchInput: document.getElementById("searchInput"),
  clearFilters: document.getElementById("clearFilters"),
  tableSummary: document.getElementById("tableSummary"),
  tradesBody: document.getElementById("tradesBody"),
  emptyState: document.getElementById("emptyState"),
  tableWrap: document.querySelector(".table-wrap"),
};

function normalizeAction(value) {
  const raw = String(value ?? "").trim();
  const upper = raw.toUpperCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
  if (upper === "NEW" || upper === "NEW POSITION") return "New Position";
  if (upper === "CLOSED" || upper === "CLOSE" || upper === "CLOSED POSITION") return "Closed Position";
  if (upper === "BUY" || upper === "ADD" || upper === "INCREASE") return "Buy";
  if (upper === "SELL" || upper === "TRIM" || upper === "DECREASE") return "Sell";
  return raw || "Unknown";
}

function pick(obj, keys, fallback = "") {
  for (const key of keys) {
    if (obj && Object.prototype.hasOwnProperty.call(obj, key) && obj[key] !== null && obj[key] !== "") {
      return obj[key];
    }
  }
  return fallback;
}

function toNumber(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  if (value === null || value === undefined || value === "") return 0;
  const cleaned = String(value).replace(/[%,$,\s]/g, "");
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : 0;
}

function normalizeRow(row) {
  return {
    Date: String(pick(row, ["Date", "date", "as_of", "AsOfDate", "As_of_date"], "")),
    ETF_Ticker: String(pick(row, ["ETF_Ticker", "etf_ticker", "ETF", "etf"], "")).toUpperCase(),
    Stock_Ticker: String(pick(row, ["Stock_Ticker", "stock_ticker", "Ticker", "ticker", "symbol"], "")),
    Company_Name: String(pick(row, ["Company_Name", "company_name", "Company", "company", "description", "name"], "")),
    Action: normalizeAction(pick(row, ["Action", "action", "change_type", "Change_Type"], "")),
    Shares_Change: toNumber(pick(row, ["Shares_Change", "shares_change", "share_change", "SharesChange"], 0)),
    Weight_Change: toNumber(pick(row, ["Weight_Change", "weight_change", "WeightChange"], 0)),
  };
}

function extractRows(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const key of ["trades", "rows", "data", "daily_trades", "changes"]) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  return [];
}

function formatSignedNumber(value, decimals = 0) {
  const n = Number(value || 0);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString(undefined, {
    maximumFractionDigits: decimals,
    minimumFractionDigits: 0,
  })}`;
}

function formatWeight(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? "+" : "";
  const abs = Math.abs(n);

  // Support either decimal fractions (0.0012 = 0.12%) or already-percent values (0.12 = 0.12%).
  const pct = abs <= 1 ? n * 100 : n;
  const precision = Math.abs(pct) < 0.01 ? 3 : 2;
  return `${sign}${pct.toFixed(precision)}%`;
}

function actionClass(action) {
  if (action === "Buy") return "buy";
  if (action === "Sell") return "sell";
  if (action === "New Position") return "new";
  if (action === "Closed Position") return "closed";
  return "closed";
}

function displayStockTicker(raw) {
  const ticker = String(raw || "").trim();
  // Dimensional sometimes appends a country suffix like "BLK US".
  // Keep the source value intact internally; only shorten the display when it is a simple "<ticker> US" pattern.
  const m = ticker.match(/^([A-Z0-9.\-]+)\s+US$/i);
  return m ? m[1].toUpperCase() : ticker;
}

function latestDateFromRows(rows) {
  const dates = rows.map(r => r.Date).filter(Boolean).sort();
  return dates.length ? dates[dates.length - 1] : "";
}

function findAnyDate(obj) {
  if (!obj || typeof obj !== "object") return "";
  const dateKeys = ["last_updated", "updated_at", "generated_at", "run_at", "date", "as_of", "asOfDate"];
  for (const key of dateKeys) {
    if (obj[key]) return String(obj[key]);
  }
  for (const value of Object.values(obj)) {
    if (value && typeof value === "object") {
      const nested = findAnyDate(value);
      if (nested) return nested;
    }
  }
  return "";
}

function formatDateTime(value) {
  if (!value) return "No update time";
  const d = new Date(value);
  if (!Number.isNaN(d.getTime())) {
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit"
    });
  }
  return value;
}

function getETFDate(etf) {
  const rows = allRows.filter(r => r.ETF_Ticker === etf);
  const tradeDate = latestDateFromRows(rows);
  if (tradeDate) return tradeDate;

  if (statusData && typeof statusData === "object") {
    const candidates = [
      statusData[etf],
      statusData.etfs?.[etf],
      statusData.funds?.[etf],
      statusData.status?.[etf],
    ].filter(Boolean);
    for (const item of candidates) {
      if (typeof item === "object") {
        const d = pick(item, ["as_of", "as_of_date", "date", "latest_date", "snapshot_date"], "");
        if (d) return String(d);
      }
    }
  }
  return "—";
}

function getETFStatus(etf) {
  if (!statusData || typeof statusData !== "object") return "";
  const candidates = [
    statusData[etf],
    statusData.etfs?.[etf],
    statusData.funds?.[etf],
    statusData.status?.[etf],
  ].filter(Boolean);

  for (const item of candidates) {
    if (typeof item === "string") return item;
    if (typeof item === "object") {
      const s = pick(item, ["status", "result", "state"], "");
      if (s) return String(s);
    }
  }
  return "";
}

function renderSummary() {
  const newCount = allRows.filter(r => r.Action === "New Position").length;
  const closedCount = allRows.filter(r => r.Action === "Closed Position").length;

  els.totalChanges.textContent = allRows.length.toLocaleString();
  els.newPositionsCount.textContent = newCount.toLocaleString();
  els.closedPositionsCount.textContent = closedCount.toLocaleString();

  let updated = findAnyDate(statusData) || latestDateFromRows(allRows);
  els.lastUpdated.textContent = formatDateTime(updated);

  const statuses = ETF_LIST.map(getETFStatus).filter(Boolean);
  const bad = statuses.filter(s => !/ok|success|passed|complete/i.test(s));
  if (!allRows.length) {
    els.statusPill.textContent = "Waiting for second snapshot";
    els.statusPill.className = "status-pill warn";
  } else if (bad.length) {
    els.statusPill.textContent = "Some sources need attention";
    els.statusPill.className = "status-pill warn";
  } else {
    els.statusPill.textContent = "All sources operational";
    els.statusPill.className = "status-pill ok";
  }
}

function renderETFCards() {
  els.etfCards.innerHTML = ETF_LIST.map(etf => {
    const rows = allRows.filter(r => r.ETF_Ticker === etf);
    const buys = rows.filter(r => r.Action === "Buy" || r.Action === "New Position").length;
    const sells = rows.filter(r => r.Action === "Sell" || r.Action === "Closed Position").length;
    const newCount = rows.filter(r => r.Action === "New Position").length;
    const closed = rows.filter(r => r.Action === "Closed Position").length;

    return `
      <article class="etf-card">
        <div class="etf-card-top">
          <span class="etf-symbol">${etf}</span>
          <span class="etf-date">${getETFDate(etf)}</span>
        </div>
        <div class="etf-stats">
          <div class="etf-stat"><strong>${buys}</strong><span>Increases</span></div>
          <div class="etf-stat"><strong>${sells}</strong><span>Decreases</span></div>
          <div class="etf-stat"><strong>${newCount}</strong><span>New</span></div>
          <div class="etf-stat"><strong>${closed}</strong><span>Closed</span></div>
        </div>
      </article>`;
  }).join("");
}

function renderRankList(target, rows, positive = true) {
  const eligible = rows
    .filter(r => positive
      ? (r.Action === "Buy" || r.Action === "New Position" || r.Weight_Change > 0)
      : (r.Action === "Sell" || r.Action === "Closed Position" || r.Weight_Change < 0))
    .sort((a, b) => positive
      ? Math.abs(b.Weight_Change) - Math.abs(a.Weight_Change)
      : Math.abs(b.Weight_Change) - Math.abs(a.Weight_Change))
    .slice(0, 5);

  if (!eligible.length) {
    target.innerHTML = `<div class="small-note">No qualifying changes yet.</div>`;
    return;
  }

  target.innerHTML = eligible.map((row, i) => `
    <div class="rank-item">
      <div class="rank-number">${String(i + 1).padStart(2, "0")}</div>
      <div class="rank-main">
        <strong>${displayStockTicker(row.Stock_Ticker) || "—"} · ${row.ETF_Ticker || "—"}</strong>
        <span>${row.Company_Name || "Unknown company"}</span>
      </div>
      <div class="rank-value ${positive ? "positive" : "negative"}">${formatWeight(row.Weight_Change)}</div>
    </div>
  `).join("");
}

function applyFilters() {
  const etf = els.etfFilter.value;
  const action = els.actionFilter.value;
  const search = els.searchInput.value.trim().toLowerCase();

  filteredRows = allRows.filter(row => {
    const etfOk = etf === "ALL" || row.ETF_Ticker === etf;
    const actionOk = action === "ALL" || row.Action.toUpperCase() === action;
    const searchOk = !search ||
      row.Stock_Ticker.toLowerCase().includes(search) ||
      row.Company_Name.toLowerCase().includes(search);
    return etfOk && actionOk && searchOk;
  });

  sortRows();
  renderTable();
}

function sortRows() {
  const { key, direction } = sortState;
  const multiplier = direction === "asc" ? 1 : -1;

  filteredRows.sort((a, b) => {
    if (["Shares_Change", "Weight_Change"].includes(key)) {
      return (Number(a[key]) - Number(b[key])) * multiplier;
    }
    return String(a[key] ?? "").localeCompare(String(b[key] ?? "")) * multiplier;
  });
}

function renderTable() {
  els.tableSummary.textContent = `${filteredRows.length.toLocaleString()} change${filteredRows.length === 1 ? "" : "s"} shown`;

  if (!filteredRows.length) {
    els.tradesBody.innerHTML = "";
    els.tableWrap.hidden = true;
    els.emptyState.hidden = false;
    return;
  }

  els.tableWrap.hidden = false;
  els.emptyState.hidden = true;

  els.tradesBody.innerHTML = filteredRows.map(row => {
    const sharesClass = row.Shares_Change > 0 ? "value-positive" : row.Shares_Change < 0 ? "value-negative" : "";
    const weightClass = row.Weight_Change > 0 ? "value-positive" : row.Weight_Change < 0 ? "value-negative" : "";

    return `
      <tr>
        <td>${row.Date || "—"}</td>
        <td><strong>${row.ETF_Ticker || "—"}</strong></td>
        <td class="stock-cell">
          <strong>${displayStockTicker(row.Stock_Ticker) || "—"}</strong>
          ${displayStockTicker(row.Stock_Ticker) !== row.Stock_Ticker ? `<span>${row.Stock_Ticker}</span>` : ""}
        </td>
        <td>${row.Company_Name || "—"}</td>
        <td><span class="action-badge ${actionClass(row.Action)}">${row.Action}</span></td>
        <td class="numeric ${sharesClass}">${formatSignedNumber(row.Shares_Change, 2)}</td>
        <td class="numeric ${weightClass}">${formatWeight(row.Weight_Change)}</td>
      </tr>`;
  }).join("");
}

function bindEvents() {
  [els.etfFilter, els.actionFilter].forEach(el => el.addEventListener("change", applyFilters));
  els.searchInput.addEventListener("input", applyFilters);

  els.clearFilters.addEventListener("click", () => {
    els.etfFilter.value = "ALL";
    els.actionFilter.value = "ALL";
    els.searchInput.value = "";
    applyFilters();
  });

  document.querySelectorAll("th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortState.key === key) {
        sortState.direction = sortState.direction === "asc" ? "desc" : "asc";
      } else {
        sortState = { key, direction: key === "Date" ? "desc" : "asc" };
      }
      sortRows();
      renderTable();
    });
  });
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

async function init() {
  bindEvents();

  const results = await Promise.allSettled([
    fetchJson("./output/daily_trades.json"),
    fetchJson("./output/status.json"),
  ]);

  if (results[0].status === "fulfilled") {
    allRows = extractRows(results[0].value).map(normalizeRow);
  } else {
    console.error("Could not load daily trades:", results[0].reason);
    allRows = [];
  }

  if (results[1].status === "fulfilled") {
    statusData = results[1].value;
  } else {
    console.warn("Could not load status.json:", results[1].reason);
  }

  filteredRows = [...allRows];
  renderSummary();
  renderETFCards();
  renderRankList(els.topBuys, allRows, true);
  renderRankList(els.topSells, allRows, false);
  applyFilters();
}

init();
