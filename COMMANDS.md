# NEPSE Screener CLI Cheatsheet

Daily operational reference for the NEPSE broker-activity engine.

Run interactive commands from the project root:

```bash
docker compose run --rm app python -m src.cli <command> [options]
```

Do **not** add `-T` when viewing Rich tables in a terminal: Docker then allocates a TTY, allowing Rich to use the terminal's actual width instead of its narrow non-interactive fallback. Use `-T` only for cron, scripts, or redirected output.

---

## 1. Daily workflow

After the market closes, run these commands in order:

```bash
# 1. Download and automatically ingest the latest available floorsheet.
docker compose run --rm app python -m src.cli fetch --today

# 2. Track A (top-turnover broker flows) and Track B (stealth accumulation).
# This also saves actionable signals for later history/performance analysis.
docker compose run --rm app python -m src.cli run

# 3. Find stocks whose liquidity/rank is expanding or drying up.
docker compose run --rm app python -m src.cli momentum

# 4. Review meaningful internal broker matching / wash activity.
docker compose run --rm app python -m src.cli wash

# 5. Review active research ideas.
docker compose run --rm app python -m src.cli watch list
```

`fetch --today` **already ingests** the downloaded floorsheet. Do not run `ingest` again unless repairing a specific CSV file.

---

## 2. Scanners

| Command | Purpose | Main options |
|---|---|---|
| `run` | Track A broker-flow screen plus Track B stealth-accumulation screen. | `--top`, `--top-holder-window`, `--as-of`, `--no-persist` |
| `top` | Top turnover by a chosen date, snapshot-cached for reuse by the web API. | `--as-of`, `--limit` |
| `momentum` | Turnover/rank movement scanner & single-stock peer matching. | `--symbol`, `--short`, `--base`, `--as-of` |
| `wash` | Internal broker matching and cross-trade scanner. | `--window`, `--min-qty`, `--all`, `--as-of` |

### `run` — Track A and Track B

```bash
docker compose run --rm app python -m src.cli run
```

- **Track A:** the top 20 stocks by latest-session turnover, with broker accumulation/distribution signals.
- **Track B:** market-wide stealth accumulation scan.
- Signals persist by default. Re-running the same session updates its history records; it does not create duplicate signals.

```bash
# Scan the top 40 liquid/high-turnover stocks instead of 20.
docker compose run --rm app python -m src.cli run --top 40

# Define the displayed long-term Top Holder using 66 sessions.
docker compose run --rm app python -m src.cli run --top-holder-window 66

# Historical scan without saving new history rows.
docker compose run --rm app python -m src.cli run --as-of 2026-09-11 --no-persist
```

`--top-holder-window` accepts `1`, `5`, `22`, or `66` and only changes the window used to select the informational **Top Holder**. It does not change the Track A/Track B signal rules. `Holder T1` is that same holder's latest-session net flow: positive means it is still accumulating; negative is a distribution warning.

> **Important:** `run --short 5` is invalid. `run` always evaluates broker flow on fixed T1/T5/T22/T66 session windows. Use `momentum --short 5 --base 22` for turnover momentum.

### `momentum` — turnover/rank rotation

```bash
docker compose run --rm app python -m src.cli momentum
```

Finds turnover momentum gainers and losers by comparing recent liquidity with a baseline. Defaults are **5 sessions versus 22 sessions**.

Optionally pass `--symbol` to deep-dive a specific stock's turnover expansion, market rank drift percentile, momentum state badge (`MOMENTUM_GAINER`, `STEALTH_BUILDING`, `HIGH_VOLUME_STABLE`, etc.), and find similar momentum peers via normalized Euclidean distance.

```bash
# Analyze a specific stock's momentum diagnostic and find similar peers.
docker compose run --rm app python -m src.cli momentum --symbol SAPIL

# Separate command; --short and --base belong here, not to run.
docker compose run --rm app python -m src.cli momentum --short 5 --base 22

# Ten-session activity relative to a quarterly baseline, as of a past date.
docker compose run --rm app python -m src.cli momentum --short 10 --base 66 --as-of 2026-09-11
```

### `wash` — internal matching / cross trades

```bash
docker compose run --rm app python -m src.cli wash
```

Shows broker self-matching and symbol-level crossed volume. The default view only shows meaningful volume (at least 5,000 shares).

```bash
# Broader 66-session matching review.
docker compose run --rm app python -m src.cli wash --window 66

# Lower the materiality threshold, or show every result.
docker compose run --rm app python -m src.cli wash --min-qty 20000
docker compose run --rm app python -m src.cli wash --all
```

---

## 3. Research and broker deep dives

### One-stock inspection

```bash
docker compose run --rm app python -m src.cli inspect GHL --sessions 22
```

Displays recent prices/turnover, the dominant holder and recent mover, saved signals, and full broker net flows. `--sessions N` controls displayed recent history (default: `22`). Symbols are case-insensitive.

### One-broker inspection

```bash
docker compose run --rm app python -m src.cli broker 58
docker compose run --rm app python -m src.cli broker 58 --top 5 --sessions 66
```

`--top` controls the number of holdings shown (default: `5`); `--sessions` controls the data scope (default: `66`).

### Signal history and evidence

```bash
docker compose run --rm app python -m src.cli signals --performance
```

```bash
docker compose run --rm app python -m src.cli signals --symbol GHL
docker compose run --rm app python -m src.cli signals --broker 58
docker compose run --rm app python -m src.cli signals --streak 3
docker compose run --rm app python -m src.cli signals --signal SILENT_ACCUMULATION --track TRACK_A --limit 50
```

`--performance` reports completed +1/+5/+10/+22 trading-session forward returns. Treat small sample sizes as research, not proof of an edge.

### `analyze` — rank ↔ broker ↔ price prediction

```bash
docker compose run --rm app python -m src.cli analyze ADBL --sessions 30
```

Combines three dimensions for a single symbol over a recent session window:

1. **Turnover rank** — where the symbol ranked by turnover each session, the
   Spearman correlation of rank with its T+1 return and with close price, and
   forward returns bucketed by rank (LEADER / MID / MINOR vs. the session's
   traded-symbol count). Turnover rank is **per session**, so synthetic seed
   sessions are handled gracefully.
2. **Broker accumulation signature** — the session's broker crowd: the top
   accumulator/distributor, net breadth (# net-buying brokers), concentration,
   a `sustained` flag (same dominant broker as the prior session), and a
   signature label:
   - `MULTI` — ≥3 brokers net-buying (broad accumulation)
   - `SINGLE` — exactly one net-buying broker (single dominant accumulator)
   - `DISTRIBUTE` — the top netting broker sold (net outflow)
   - `NEUTRAL` — no clear signal (no activity, or two positive brokers)
3. **Forward returns + prediction** — close-to-close T+N returns per signature
   and a next-trade prediction for each horizon (T+1, T+3) with a bias
   (BULLISH / BEARISH / NEUTRAL) and a sample-size confidence band
   (LOW < 3 · MEDIUM < 8 · HIGH ≥ 8).

`--sessions N` sets the prediction window (default: `30`); forward returns
are measured over the immediately following sessions, so a larger window
improves the confidence samples. Symbols are case-insensitive.

---

## 4. Watchlist, journal, and trade outcomes

```bash
# Create a research item and record later observations.
docker compose run --rm app python -m src.cli watch add GHL \
  --thesis "T22 broker accumulation and improving turnover" \
  --tags "momentum,track-a"
docker compose run --rm app python -m src.cli watch note GHL \
  "Top holder remains net positive; turnover rank improved."

# Record an actual trade and its eventual outcome.
docker compose run --rm app python -m src.cli watch enter GHL \
  --price 239.90 --target 270 --stop 225 --quantity 100
docker compose run --rm app python -m src.cli watch exit GHL --price 265 --outcome WON
```

| Command | Purpose |
|---|---|
| `watch list` | Active research items, market context, and open-trade PnL. |
| `watch history GHL` | Full thesis, notes, and trade history for one ticker. |
| `watch archive GHL` | Archive an invalidated/finished idea while retaining its history. |
| `watch list --all` | Include archived items. |

---

## 5. Data, backfills, and backups

```bash
# Latest available session: download + ingest.
docker compose run --rm app python -m src.cli fetch --today

# Backfill N trading sessions: download + ingest.
docker compose run --rm app python -m src.cli fetch --days 30

# Repair/re-ingest a CSV already on disk.
docker compose run --rm app python -m src.cli ingest --file data/real/YYYY-MM-DD.csv

# Timestamped PostgreSQL backup; archives older than seven days are removed.
./scripts/backup_db.sh
```

**Replicate this project to another machine (Docker installed).** The code is fully
self-contained, so no code changes are needed — but raw floorsheets (`data/real/`)
and the populated database are git-ignored, so a fresh clone starts with an empty DB.
Carry the data over with one backup file (recommended):

```bash
# On the source machine: produce data/backups/nepse_backup_*.sql.gz.
./scripts/backup_db.sh

# On the new machine (after `git clone` + `docker compose up -d db`):
gunzip -c nepse_backup_*.sql.gz | docker compose exec -T db psql -U quant -d nepse_analytics
docker compose build app
docker compose run --rm -p 18000:8000 app python -m src.cli serve --port 8000   # http://localhost:18000
```

Alternatively, re-fetch history straight from NEPSE (needs live internet access):
`docker compose run --rm app python -m src.cli fetch --days 90`.

`seed` creates synthetic test data. Do **not** use it against the real-data database unless that is intentional:

```bash
docker compose run --rm app python -m src.cli seed --days 66 --seed 20260911
```

---

## 6. Help and automation

```bash
docker compose run --rm app python -m src.cli run --help
docker compose run --rm app python -m src.cli momentum --help
docker compose run --rm app python -m src.cli watch --help
```

The supplied daily wrapper runs `fetch --today`, `run --no-persist`, `momentum`, and `wash`, appending output to `logs/daily_market.log`. For non-interactive automation, use `-T` to disable TTY allocation:

```bash
/mnt/personal/stock/brokerActivity/scripts/daily_market.sh
```

NEPSE normally trades Sunday–Thursday. On an NPT host, use this 15:30 post-market cron entry:

```cron
30 15 * * 0-4 cd /mnt/personal/stock/brokerActivity && ./scripts/daily_market.sh >> logs/daily_market.log 2>&1
```

On a UTC host, 15:30 NPT is 09:45 UTC:

```cron
45 9 * * 0-4 cd /mnt/personal/stock/brokerActivity && ./scripts/daily_market.sh >> logs/daily_market.log 2>&1
```

Market holidays are harmless: `fetch` reports when no new floorsheet is available. For persisted daily signal history, remove `--no-persist` from `/mnt/personal/stock/brokerActivity/scripts/daily_market.sh`.

## 8. Snapshot reports and the web server

Every scanner can cache its result as an immutable JSON snapshot under `reports/`
(keyed by command + normalized parameters). The snapshot is only recomputed when
the underlying market data changes (a lightweight fingerprint of the latest trade
date and row counts), so re-running an identical request is instant — this is what
lets the web layer serve "just data" with no recompute and no server-side state.

### `top` — top turnover for a date (snapshot-cached)

```bash
docker compose run --rm app python -m src.cli top
docker compose run --rm app python -m src.cli top --as-of 2026-09-10 --limit 5
```

`--as-of` defaults to the latest trading session and falls back to the most recent
session on or before the requested date (non-trading days are harmless). A second
run with identical parameters that hits cached data prints `(cached snapshot)`.

### `serve` — HTML index + JSON API over `http://host:8000`

```bash
docker compose run --rm app python -m src.cli serve --port 8000
```

| Route | Purpose |
|---|---|
| `/` | Navigable single-page UI: **Top Turnover · Inspect Symbol · Broker · Momentum · Wash · Smart Money Alerts · Full Scan · Signals · Watchlist** (click any symbol/broker to jump between views) |
| `/api/top?as_of=YYYY-MM-DD&limit=N` | Clean top-turnover ranking (cached) |
| `/api/run?as_of&top&top_holder_window` | Full Track A / Track B scan result (cached) |
| `/api/smartmoney?as_of` | Track C AI Insights via Playwright (cached) |
| `/api/momentum?short&base&as_of` | Multi-window momentum gainers / losers (cached) |
| `/api/wash?window&min_qty&all&as_of` | Internal-matching broker + session wash scan (cached) |
| `/api/inspect/<SYMBOL>?sessions` | Single-symbol deep dive (cached) |
| `/api/broker/<ID>?sessions&top` | Broker deep-dive holdings (cached) |
| `/api/signals[?streak=N|symbol=S|signal=X|track=T]` | Signal history / streak detection (cached) |
| `/api/watchlist` | Personal watchlist with market context + PnL (live, not cached) |
| `/api/watchlist/<SYMBOL>` | Watch item metadata + full dated journal notes (live, not cached) |
| `POST /api/watchlist/add` | Add/reactivate a symbol — body `{symbol, thesis?, tags?, note?}` |
| `POST /api/watchlist/note` | Append a dated journal note — `{symbol, note, note_date?}` |
| `POST /api/watchlist/enter` | Record an open trade plan — `{symbol, price, target?, stop?, quantity?, entry_date?}` |
| `POST /api/watchlist/exit` | Close an open trade — `{symbol, price, outcome?(WON/STOPPED/CLOSED)}` |
| `POST /api/watchlist/archive` | Archive a symbol, keep its journal — `{symbol}` |
| `/api/dates` | Distinct trading sessions present in the DB |
| `/reports/<key>.json` | A stored snapshot, viewable/curl-able directly |

Each `cached` flag in a JSON response tells whether the snapshot was reused. The
`reports/` directory is git-ignored; it is bind-mounted into the `app` container so
snapshots persist between runs.
