# NSE Sector Heatmap

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Jai-mehndiratta/heatmap)

A single-page dashboard that pulls the MarketLens NSE universe and renders a
sector heatmap from Yahoo Finance 1-day returns.

Locally, the app can fetch the sector universe live from MarketLens. On Render,
MarketLens often returns HTTP 403 to cloud IPs, so the app automatically uses
the bundled **`universe.json`** sector map and refreshes every price from Yahoo
Finance on each sync. The sector mapping stays fixed; prices stay live.

Click **Run sync** and the app:

1. Reads the MarketLens sector index and fetches every sector's stock list in
   parallel, with retries. On Render, it loads the committed `universe.json`
   instead, because MarketLens blocks cloud data-centre IPs.
2. Keeps the sector mapping fixed and de-duplicates by ticker.
3. Fetches each stock's latest NSE price and previous close from **Yahoo
   Finance** (ticker `.NS`), then computes the 1-day change from those two
   prices. MarketLens prices are not used when Yahoo returns a quote.
4. Renders one tile per sector, coloured from deep red through neutral to deep
   green by the sector's aggregate market-cap percentage change.
5. Orders the tiles by **total rupee market-cap change** by default, highest
   increase to deepest decrease. A sort selector can switch the heatmap to pure
   percentage order. The percentage shown on each tile remains the weighted
   1-day sector return.
6. Shows the sector's total company count and absolute market-cap change on
   every tile.
7. Opens a simple sector detail panel on click, listing **all** companies in
   that sector sorted by Yahoo Finance 1-day change from highest to lowest.

There are no other views, tables, filters or download controls in the UI. The
JSON/CSV API remains available for automation.

---

## Quick start (local)

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --reload
```

Open <http://localhost:8000> and click **Run sync**.

The snapshot is stored in `./data/marketlens.db` and reloaded automatically on
the next start.

### Refreshing the bundled sector universe

Render uses `universe.json` for the fixed ticker/sector map. When you want to
refresh that map from live MarketLens access, run a local sync first, then:

```bash
python scripts/build_universe.py
git add universe.json
git commit -m "Refresh bundled sector universe"
git push
```

The local app can also use the bundled map by setting
`USE_STATIC_UNIVERSE=1` before starting it.

---

## Deploy to Render

### Option A — Blueprint (recommended)

1. Push this folder to a GitHub repository.
2. In Render, choose **New + → Blueprint** and connect the repository.
3. Render reads [`render.yaml`](render.yaml), creates the service and deploys it.
4. Open the service URL and click **Run sync**.

The blueprint uses:

| Setting | Value |
| --- | --- |
| Runtime | Python 3.12 |
| Region | Singapore (closest public Render region to India) |
| Build | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start | `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers` |
| Health check | `/api/health` |
| Plan | Free (can be changed) |

### Option B — Manual web service

Create a **New Web Service** in Render with:

- **Runtime:** Python 3.12
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers`
- **Health check path:** `/api/health`

Keep **one worker**. The sync job runs in process memory, so multiple workers
would each have their own job state.

### Persistent storage (optional)

Render's free instances have an ephemeral filesystem. To keep snapshots across
deploys, attach a persistent disk on a paid plan:

```yaml
disk:
  name: marketlens-data
  mountPath: /var/data
  sizeGB: 1
```

Then set `DATA_DIR=/var/data`.

---

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `./data` | Where `marketlens.db` is written. |
| `SYNC_CONCURRENCY` | `6` | Parallel sector requests to MarketLens. Lower it if the source rate-limits you. |
| `YAHOO_CONCURRENCY` | `6` | Parallel price batches to Yahoo Finance. Lower it if Yahoo rate-limits you. |
| `USE_STATIC_UNIVERSE` | `1` on Render, `0` locally | Use the bundled `universe.json` sector map instead of fetching MarketLens. Automatically enabled when `RENDER=true`. |
| `STATIC_UNIVERSE_PATH` | `./universe.json` | Path to the bundled sector universe. |
| `MARKETLENS_BASE_URL` | `https://marketlens.nseindia.com` | Override the upstream base URL. |
| `MARKETLENS_PROXY` | unset | Optional HTTP/HTTPS proxy for MarketLens requests. Useful if NSE blocks the Render region. |
| `DASHBOARD_PASSWORD` | unset | Optional password. The UI asks for it and API calls require the `X-Dashboard-Key` header. |
| `LOG_LEVEL` | `INFO` | Python log level. |
| `PORT` | `8000` | Provided automatically by Render. |

The app reads environment variables directly and does not auto-load `.env`
files. `.env.example` is included for reference.

---

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Health/uptime, also available at `/health`. |
| `GET` | `/api/dashboard` | Latest snapshot metadata, stats, sectors and all stocks. |
| `POST` | `/api/sync` | Start a background sync. Returns `409` if one is already running. |
| `GET` | `/api/sync/status` | Job status, progress and live sector log. |
| `GET` | `/api/history` | Metadata for the last 12 snapshots. |
| `GET` | `/api/export.csv` | CSV of the latest snapshot. Filters: `search`, `sector`, `sort`, `order`. |

```bash
curl -X POST http://localhost:8000/api/sync
curl http://localhost:8000/api/sync/status

curl -o nse_snapshot.csv \
  "http://localhost:8000/api/export.csv?sort=marketCap&order=desc"
```

---

## Data coverage & caveats

- MarketLens currently exposes about **2,200–2,300 scrips across ~58 sectors**.
  That is the universe this dashboard reports. It is not every NSE-listed
  company (some BE/BZ-series and smaller SME scrips are absent from the source).
- Render uses the committed `universe.json` sector map because MarketLens
  returns HTTP 403 from Render's Singapore region. Local runs use live
  MarketLens unless `USE_STATIC_UNIVERSE=1` is set.
- Prices and 1-day changes come from **Yahoo Finance** using the `.NS` symbol.
  The sector mapping and stock universe are kept fixed.
- Yahoo Finance does not cover a handful of MarketLens tickers. Those use the
  MarketLens price as a fallback, and the UI footnote shows the matched/fallback
  count. This is typically 6 out of ~2,229 tickers.
- Market caps are adjusted to the Yahoo price using the share count implied by
  MarketLens's market cap and close, so sector weights stay consistent with the
  corrected prices.
- Closing prices and changes are whatever Yahoo returns, typically delayed/EOD
  data. This dashboard is a research tool, not investment advice. Respect the
  sources' terms when using the data.
- Sector colours use the **aggregate percentage change in the sector's total
  market cap**: for each company, current market cap is compared with its
  implied previous market cap from the 1-day change, then the whole sector is
  aggregated. This gives large companies proportionally more influence without
  simply averaging stock percentages.
- Heatmap tiles default to the **absolute rupee change in sector market cap**.
  Use the **Sort by** selector to switch to percentage order. The percentage
  displayed on each tile is unchanged in either mode.
- The sector detail lists **all** companies in that sector, sorted by 1-day
  change from highest to lowest. There is no minimum-move filter.

---

## Troubleshooting

**`Sync failed … HTTP 403` or an empty result on Render**
MarketLens blocks cloud IPs. The app automatically uses the bundled
`universe.json` on Render, so this should not occur. If you removed that file,
set `USE_STATIC_UNIVERSE=1` and restore `universe.json`. Alternatively, set
`MARKETLENS_PROXY` to an Indian proxy if you want live MarketLens sector
refreshes from Render.

**The first request after a while is slow**
Render's free plan spins down idle services. The first request has to cold-start
the container; subsequent requests are fast.

**Snapshots disappear after a redeploy**
Expected on an ephemeral Render filesystem. Attach a persistent disk and set
`DATA_DIR` as described above.

**`409 A sync is already running`**
Only one sync can run at a time. Wait for the current job or refresh the page.

---

## Project layout

```
.
├── app.py              FastAPI app, job state, CSV export, static serving
├── marketlens.py       MarketLens client, static universe loader, aggregation
├── yahoo.py            Yahoo Finance NSE price and previous-close enrichment
├── db.py               SQLite snapshot persistence
├── universe.json       Fixed MarketLens ticker/sector map used on Render
├── scripts/
│   └── build_universe.py  Regenerate universe.json from a local sync
├── static/
│   ├── index.html      Single-page heatmap shell
│   ├── styles.css      Heatmap, legend, sector panel, responsive styles
│   ├── app.js          Fetch/render, sync polling, sector drilldown
│   └── favicon.svg
├── requirements.txt
├── render.yaml
├── .python-version
└── README.md
```
