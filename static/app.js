/* NSE sector heatmap — single-page, no build step. */

(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const MIN_DOMAIN = 0.5;

  const state = {
    hasData: false,
    snapshot: null,
    stats: {},
    sectors: [],
    stocks: [],
    sectorCapChanges: new Map(),
    job: null,
    pollTimer: null,
    pollFailures: 0,
    sectorName: null,
    authKey: sessionStorage.getItem("ml_dashboard_key") || "",
  };

  const priceFmt = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const intFmt = new Intl.NumberFormat("en-IN");
  const dateFmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });

  // ---------- Formatting ----------

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function fmtPrice(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return `₹${priceFmt.format(Number(value))}`;
  }

  function fmtMove(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function fmtCap(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const abs = Math.abs(Number(value));
    if (abs >= 1e12) return `₹${(abs / 1e12).toFixed(2)} L Cr`;
    if (abs >= 1e9) return `₹${intFmt.format(Math.round(abs / 1e7))} Cr`;
    if (abs >= 1e7) return `₹${(abs / 1e7).toFixed(1)} Cr`;
    if (abs >= 1e5) return `₹${(abs / 1e5).toFixed(1)} L`;
    return `₹${intFmt.format(Math.round(abs))}`;
  }

  function fmtCapDelta(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const number = Number(value);
    const sign = number > 0 ? "+" : number < 0 ? "−" : "";
    return `${sign}${fmtCap(number)}`;
  }

  function fmtDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return dateFmt.format(date);
  }

  function moveClass(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "flat";
    if (Number(value) > 0) return "pos";
    if (Number(value) < 0) return "neg";
    return "flat";
  }

  function sectorMove(sector) {
    return sector.weightedChange !== null && sector.weightedChange !== undefined
      ? sector.weightedChange
      : sector.avgChange;
  }

  // ---------- Heatmap maths ----------

  function buildSectorCapChanges() {
    const changes = new Map();
    for (const stock of state.stocks) {
      const marketCap = Number(stock.marketCap);
      const pchange = Number(stock.pchange);
      if (!Number.isFinite(marketCap) || !Number.isFinite(pchange) || (1 + pchange / 100) <= 0) continue;
      const change = marketCap - marketCap / (1 + pchange / 100);
      changes.set(stock.sector, (changes.get(stock.sector) || 0) + change);
    }
    state.sectorCapChanges = changes;
  }

  function sectorCapChange(name) {
    return state.sectorCapChanges.get(name) || 0;
  }

  function heatDomain() {
    const maxAbs = state.sectors.reduce((maximum, sector) => {
      const move = sectorMove(sector);
      return move === null || move === undefined ? maximum : Math.max(maximum, Math.abs(move));
    }, 0);
    return Math.max(MIN_DOMAIN, Math.ceil(maxAbs * 2) / 2);
  }

  function heatColor(value, domain) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "#141c2b";
    const ratio = Math.min(1, Math.abs(Number(value)) / Math.max(0.5, domain));
    const intensity = Math.pow(ratio, 0.72);
    const neutral = [17, 24, 39];
    const target = Number(value) >= 0 ? [22, 101, 52] : [153, 27, 27];
    const rgb = neutral.map((channel, index) => Math.round(channel + (target[index] - channel) * intensity));
    return `rgb(${rgb.join(",")})`;
  }

  // ---------- API ----------

  function authHeaders() {
    return state.authKey ? { "X-Dashboard-Key": state.authKey } : {};
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}), ...authHeaders() };
    let response;
    try {
      response = await fetch(path, { cache: "no-store", ...options, headers });
    } catch (error) {
      throw new Error("Could not reach the dashboard server.");
    }
    if (response.status === 401) {
      showAuth();
      const error = new Error("Dashboard password required.");
      error.code = "auth";
      throw error;
    }
    if (!response.ok) {
      let detail = `Request failed with HTTP ${response.status}`;
      try {
        const payload = await response.json();
        if (payload && payload.detail) detail = payload.detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return response.json();
  }

  // ---------- Load / render ----------

  async function loadDashboard() {
    try {
      const data = await api("/api/dashboard");
      applyDashboard(data);
    } catch (error) {
      if (error.code !== "auth") showError(error.message);
    }
  }

  function applyDashboard(data) {
    state.hasData = Boolean(data.hasData);
    state.job = data.job || state.job;

    if (state.hasData) {
      state.snapshot = data.snapshot || {};
      state.stats = data.stats || {};
      state.sectors = data.sectors || [];
      state.stocks = data.stocks || [];
    } else {
      state.snapshot = null;
      state.stats = {};
      state.sectors = [];
      state.stocks = [];
    }

    buildSectorCapChanges();

    if (state.sectorName) {
      if (state.sectors.some((sector) => sector.name === state.sectorName)) {
        openSector(state.sectorName);
      } else {
        closeSector();
      }
    }

    render();
    if (state.job && state.job.status === "running") startPolling();
  }

  function render() {
    renderHeader();
    renderHeatmap();
  }

  function renderHeader() {
    const job = state.job || {};
    const running = job.status === "running";
    const failed = job.status === "error";
    const progress = job.progress || { done: 0, total: 0, stocks: 0 };

    $("#lastSync").textContent = state.hasData && state.snapshot?.fetchedAt
      ? `Last sync: ${fmtDate(state.snapshot.fetchedAt)}`
      : "No snapshot yet";

    const status = $("#status");
    status.classList.remove("ready", "running", "error");
    if (running) {
      status.classList.add("running");
      if (job.phase === "prices") {
        $("#statusText").textContent = progress.total
          ? `Pricing ${progress.done}/${progress.total}`
          : "Pricing…";
      } else {
        $("#statusText").textContent = progress.total
          ? `Syncing ${progress.done}/${progress.total}`
          : "Syncing…";
      }
    } else if (failed) {
      status.classList.add("error");
      $("#statusText").textContent = "Sync failed";
    } else if (state.hasData) {
      status.classList.add("ready");
      $("#statusText").textContent = "Ready";
    } else {
      $("#statusText").textContent = "No data";
    }

    $("#runBtn").disabled = running;
    $("#runLabel").textContent = running ? "Syncing…" : "Run sync";
    $("#runSpinner").hidden = !running;

    const error = $("#error");
    if (failed && job.error) {
      error.hidden = false;
      error.textContent = job.error;
    } else {
      error.hidden = true;
    }

    const progressWrap = $("#progress");
    progressWrap.hidden = !running;
    if (running) {
      const total = Number(progress.total || 0);
      const done = Number(progress.done || 0);
      $("#progressBar").style.width = `${total ? Math.max(3, (done / total) * 100) : 4}%`;
      if (job.phase === "prices") {
        $("#progressText").textContent = job.message || `Fetching Yahoo Finance prices · ${done}/${total}`;
      } else {
        $("#progressText").textContent = total
          ? `Fetching ${done}/${total} sectors · ${intFmt.format(progress.stocks || 0)} stocks`
          : "Connecting to MarketLens…";
      }
    }

    const prices = state.snapshot?.prices || {};
    $("#priceNote").textContent = prices.requested
      ? `${intFmt.format(prices.matched || 0)}/${intFmt.format(prices.requested)} tickers matched${prices.fallback ? ` · ${intFmt.format(prices.fallback)} MarketLens fallback` : ""}.`
      : "";

    $("#summary").textContent = state.hasData
      ? `${intFmt.format(state.sectors.length)} sectors · ${intFmt.format(state.stocks.length)} stocks`
      : "—";
  }

  function renderHeatmap() {
    const container = $("#heatmap");
    const job = state.job || {};
    const running = job.status === "running";

    if (!state.hasData) {
      if (running) {
        container.innerHTML = `
          <div class="empty">
            <h3>Fetching MarketLens…</h3>
            <p>Building the sector snapshot. This usually takes a few seconds.</p>
          </div>
        `;
      } else {
        container.innerHTML = `
          <div class="empty">
            <h3>No snapshot yet</h3>
            <p>Run a sync to load the NSE sector heatmap.</p>
            <button class="btn" data-action="run" type="button">Run sync</button>
          </div>
        `;
      }
      return;
    }

    const domain = heatDomain();
    $("#legendLow").textContent = `−${domain.toFixed(1)}%`;
    $("#legendHigh").textContent = `+${domain.toFixed(1)}%`;
    const sortMode = $("#sortMode")?.value || "marketCap";

    const sectors = [...state.sectors].sort((a, b) => {
      if (sortMode === "percentage") {
        const av = sectorMove(a);
        const bv = sectorMove(b);
        if (av === null || av === undefined) return 1;
        if (bv === null || bv === undefined) return -1;
        return bv - av;
      }
      const difference = sectorCapChange(b.name) - sectorCapChange(a.name);
      if (difference !== 0) return difference;
      const av = sectorMove(a) || 0;
      const bv = sectorMove(b) || 0;
      return bv - av;
    });
    if (!sectors.length) {
      container.innerHTML = `<div class="empty"><h3>No sector data</h3><p>Run a new sync to refresh the snapshot.</p></div>`;
      return;
    }

    container.innerHTML = sectors.map((sector) => {
      const move = sectorMove(sector);
      const total = Number(sector.count) || 0;
      const capChange = sectorCapChange(sector.name);
      const color = heatColor(move, domain);
      const title = `${sector.name} · ${fmtMove(move)} · ${fmtCapDelta(capChange)} market-cap change · ${total} companies`;
      return `
        <button class="tile" data-sector="${esc(sector.name)}" style="background:${color}" type="button" title="${esc(title)}">
          <span class="tile-name">${esc(sector.name)}</span>
          <span class="tile-move">${fmtMove(move)}</span>
          <span class="tile-count">${fmtCapDelta(capChange)} · ${intFmt.format(total)} companies</span>
        </button>
      `;
    }).join("");
  }

  // ---------- Sector panel ----------

  function openSector(name) {
    const sector = state.sectors.find((item) => item.name === name);
    if (!sector) return;

    const rows = state.stocks
      .filter((stock) => stock.sector === name)
      .sort((a, b) => {
        const av = Number(a.pchange);
        const bv = Number(b.pchange);
        const aValid = Number.isFinite(av);
        const bValid = Number.isFinite(bv);
        if (!aValid && !bValid) return 0;
        if (!aValid) return 1;
        if (!bValid) return -1;
        return bv - av;
      });

    const move = sectorMove(sector);
    state.sectorName = name;
    $("#sectorName").textContent = name;

    const movePill = $("#sectorMove");
    movePill.textContent = fmtMove(move);
    movePill.className = `move-pill ${moveClass(move)}`;

    $("#sectorMeta").innerHTML = `<strong>${intFmt.format(rows.length)}</strong> companies · <strong>${fmtCapDelta(sectorCapChange(name))}</strong> market-cap change`;

    const body = $("#sectorBody");
    if (!rows.length) {
      body.innerHTML = `
        <div class="panel-empty">
          <h3>No companies in this sector</h3>
          <p>The latest snapshot has no companies for ${esc(name)}.</p>
        </div>
      `;
    } else {
      body.innerHTML = `
        <div class="list-head"><span>Company</span><span>Close</span><span>1D</span></div>
        ${rows.map((stock) => `
          <div class="stock-row">
            <div class="stock-name">
              <strong>${esc(stock.ticker)}</strong>
              <span>${esc(stock.name)}</span>
            </div>
            <div class="stock-price">${fmtPrice(stock.closePrice)}</div>
            <div class="stock-change ${moveClass(stock.pchange)}">${fmtMove(stock.pchange)}</div>
          </div>
        `).join("")}
      `;
    }

    $("#sectorFoot").textContent = "All companies in the sector are listed, sorted by 1-day change (highest to lowest).";

    body.scrollTop = 0;
    $("#sectorPanel").classList.add("open");
    $("#sectorPanel").setAttribute("aria-hidden", "false");
    $("#sectorBackdrop").hidden = false;
    document.body.classList.add("modal-open");
  }

  function closeSector() {
    state.sectorName = null;
    $("#sectorPanel").classList.remove("open");
    $("#sectorPanel").setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    setTimeout(() => {
      if (!state.sectorName) $("#sectorBackdrop").hidden = true;
    }, 160);
  }

  // ---------- Sync ----------

  function showError(message) {
    const error = $("#error");
    if (!message) {
      error.hidden = true;
      return;
    }
    error.hidden = false;
    error.textContent = message;
  }

  async function startSync() {
    if (state.job && state.job.status === "running") return;
    showError("");
    try {
      const response = await api("/api/sync", { method: "POST" });
      state.job = response.job || { status: "running", progress: { done: 0, total: 0, stocks: 0 } };
      render();
      startPolling();
    } catch (error) {
      if (error.code !== "auth") showError(error.message);
    }
  }

  function startPolling() {
    stopPolling();
    state.pollFailures = 0;
    state.pollTimer = setInterval(pollStatus, 800);
    pollStatus();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function pollStatus() {
    try {
      const previous = state.job ? state.job.status : null;
      const data = await api("/api/sync/status");
      state.pollFailures = 0;
      state.job = data.job || state.job;
      render();

      if (state.job.status !== "running") {
        stopPolling();
        if (previous === "running" && state.job.status === "success") {
          await loadDashboard();
          render();
        } else if (previous === "running" && state.job.status === "error") {
          showError(state.job.error || "Sync failed.");
        }
      }
    } catch (error) {
      if (error.code === "auth") {
        stopPolling();
        return;
      }
      state.pollFailures += 1;
      if (state.pollFailures >= 5) {
        stopPolling();
        showError("Lost contact with the sync job. Refresh the page to check again.");
      }
    }
  }

  // ---------- Auth ----------

  function showAuth() {
    if (!$("#authModal").hidden) return;
    $("#authModal").hidden = false;
    $("#authError").hidden = true;
    document.body.classList.add("modal-open");
    setTimeout(() => $("#authInput").focus(), 30);
  }

  async function handleAuthSubmit(event) {
    event.preventDefault();
    const key = $("#authInput").value.trim();
    if (!key) return;
    try {
      const response = await fetch("/api/dashboard", {
        cache: "no-store",
        headers: { "X-Dashboard-Key": key },
      });
      if (response.status === 401) {
        $("#authError").hidden = false;
        $("#authError").textContent = "That password did not work.";
        return;
      }
      if (!response.ok) throw new Error("Could not unlock the dashboard.");
      const data = await response.json();
      state.authKey = key;
      sessionStorage.setItem("ml_dashboard_key", key);
      $("#authModal").hidden = true;
      $("#authInput").value = "";
      document.body.classList.remove("modal-open");
      applyDashboard(data);
    } catch (error) {
      $("#authError").hidden = false;
      $("#authError").textContent = error.message || "Could not unlock the dashboard.";
    }
  }

  // ---------- Events ----------

  function bindEvents() {
    document.addEventListener("click", (event) => {
      if (event.target.closest('[data-action="run"]')) {
        startSync();
        return;
      }

      const tile = event.target.closest("[data-sector]");
      if (tile) {
        openSector(tile.dataset.sector);
        return;
      }

      if (event.target.closest("#closeSector")) {
        closeSector();
      }
    });

    $("#sectorBackdrop").addEventListener("click", closeSector);
    $("#sortMode").addEventListener("change", renderHeatmap);
    $("#authForm").addEventListener("submit", handleAuthSubmit);

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.sectorName) closeSector();
    });
  }

  // ---------- Boot ----------

  bindEvents();
  loadDashboard();
})();
