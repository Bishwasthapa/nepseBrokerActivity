# NEPSE Institutional Accumulation Screener

An automated **institutional-footprint engine** for the Nepalese Stock Exchange
(NEPSE). It ingests raw floorsheet trade data, reconstructs per-broker net
positioning across multiple session windows, and classifies **persistent,
non-random accumulation vs. distribution traps** among the market's most
actively traded instruments — tracking *which brokers* are quietly putting on
or taking off risk, rather than reacting to price alone.

> 🚀 **Quick start:** a condensed CLI cheatsheet / daily operational runbook
> lives in **[`COMMANDS.md`](./COMMANDS.md)**.

---

## 1. Project Overview & Quantitative Thesis

### Purpose
Automated detection and tracking of **institutional (broker) accumulation and
distribution** on NEPSE, computed entirely from the published **floorsheet**
(transaction-by-transaction ledger). Every trade's buyer broker and seller
broker are public, so the full *who bought from whom at what price* is
reconstructible. This engine turns that raw ledger into a structured,
queryable footprint model.

### Core Trading Thesis
> Price is the last thing institutions reveal. The floorsheet shows their hand
> much earlier.

The strategy hunts for **persistent, non-random multi-session broker
positioning** on the highest-turnover stocks:

- A single broker building inventory across many sessions, at a **tight margin
  to its own buy VWAP**, with **no price markup yet** → **silent accumulation**.
- That same position *then* driving price higher with expanding margin →
  **active markup**.
- A broker that was a dominant buyer over 22–66 sessions but abruptly becomes
  **the top seller in one session** → a **distribution trap** (the "exit"
  giving ordinary holders the impression of strength).
- **Stealth setups** beyond the top-turnover names: a stock with fragmented
  selling, a huge 22-day net absorption by a single broker, and a fresh
  one-day **volume inflection**, still within a tight price band → an early,
  unranked accumulation signal.

Signals are **persisted** to a history table so the system can later detect
which accumulation "runs" are still live across consecutive trading sessions
(streak detection) and which were one-off noise.

---

## 2. Tech Stack & Infrastructure

| Layer | Technology |
|-------|-----------|
| **Orchestration** | Multi-container `docker-compose.yml` with two isolated services (`db`, `app`), gated by a readiness healthcheck |
| **Database** | PostgreSQL **16-alpine**, persistent `pgdata` volume, auto-initializing `schema.sql` |
| **Processing engine** | Python **3.11**, **Polars** (vectorized, parallelized group-by / rolling computation) |
| **DB access** | **psycopg2** (bulk `execute_values` / `COPY`) for writes + **SQLAlchemy 2.0** for read pooled access |
| **Presentation** | **Rich** CLI: colorized, aligned tabular output (Track A / Track B / Signal History / Streaks / Inspector) |
| **Data source** | `nepse-scraper` (reverse-engineered NEPSE SPA) for live data + GitHub open-data repo for historical backfill |

### Service topology (`docker-compose.yml`)
```
┌──────────────────────────────────────────────────────────────┐
│                            NETWORK                            │
│                                                              │
│   ┌──────────────────────────┐        ┌───────────────────┐  │
│   │            db            │        │       app         │  │
│   │  postgres:16-alpine      │        │  python:3.11-slim │  │
│   │  user=quant              │◄──────►│  PYTHONPATH=/app  │  │
│   │  db=nepse_analytics      │ 5432   │  DATABASE_URL=... │  │
│   │  volume=pgdata (persist) │        │                   │  │
│   │  init=./schema.sql       │        │  mounts:          │  │
│   │  healthcheck=pg_isready  │        │   ./src           │  │
│   └──────────────────────────┘        │   ./tests         │  │
│                                       │   ./data/real     │  │
│                                       └───────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```
- `app` uses `depends_on: db.condition: service_healthy` — no app container
  starts until `pg_isready -U quant -d nepse_analytics` passes, so schema
  auto-creation always precedes application code.
- DB listens on host port `5432`; the app reaches it internally at `db:5432`.

---
## 3. Complete Directory Structure

```
brokerActivity/
├── Dockerfile                     # python:3.11-slim image, installs requirements, copies src/ + tests/
├── docker-compose.yml            # db (postgres:16-alpine) + app services, pgdata volume, healthcheck gating
├── requirements.txt              # polars, psycopg2-binary, sqlalchemy, rich, pytest, nepse-scraper
├── pyproject.toml                # pytest config (testpaths=["tests"], pythonpath=["."]) + black line-length
├── schema.sql                    # full DDL, mounted as /docker-entrypoint-initdb.d/init.sql
├── .dockerignore                 # excludes .git, caches, .md from build context
├── .gitignore
├── README.md                     # this file
├── COMMANDS.md                   # daily operational CLI cheatsheet / runbook
│
├── src/                          # application package (imported as `src`; PYTHONPATH=/app)
│   ├── __init__.py               # package marker; __version__ = "1.0.0"
│   ├── db.py                     # connection helpers + latest_trade_date()
│   ├── fetcher.py                # live NEPSE SPA auth + history backfill from open-data repo
│   ├── ingestion.py              # Polars: floorsheet -> daily_broker_rollup + daily_market_summary
│   ├── screener.py               # dual-track quant screener + inspect_symbol()
│   ├── signals.py                # persist history, load history, current-streak detection
│   ├── analysis.py               # per-symbol rank<->broker<->price analysis + T+N prediction
│   ├── watchlist.py              # personal research journal (watch add/note/enter/exit)
│   ├── web.py                    # minimal JSON API for the SPA (serve via `src.cli serve`)
│   ├── reports.py                # SPA template + cached-JSON snapshot renderer
│   ├── cli.py                    # argparse + Rich entry point (nepse-screener)
│   └── mock_generator.py         # synthetic seed generator (tests / demos)
│
├── tests/
│   ├── __init__.py
│   └── test_engine.py            # 54 unit tests (analysis, streaks, lookbacks, momentum, wash-match ratios, web endpoints)
│
└── data/
    └── real/                     # host-mount for live floorsheet CSVs (YYYY-MM-DD.csv)
```

> **Note on `src/models.py`:** the task brief's tree referenced a `models.py`;
> this module is **not** present in the checked-in tree. All read-side data
> layout is handled by Polars DataFrames in `ingestion.py`/`screener.py`, and
> write-side DDL lives entirely in `schema.sql`. There is no ORM model layer.

---
## 4. Database Schema & Data Models

All tables are created by `schema.sql`. The file is idempotent
(`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`) and mounts into
the Postgres `docker-entrypoint-initdb.d` directory, so a fresh `pgdata`
volume is fully initialized on first boot.

### 4.1 `floorsheet` — raw tick/trade-level ledger
One row per executed contract (a buyer broker matched to a seller broker).
The grain is the same as the live NEPSE API.

| Column | Type | Notes |
|--------|------|-------|
| `trade_date` | `DATE` | trading session |
| `contract_id` | `BIGINT` | NEPSE contract id (unique per session) |
| `symbol` | `VARCHAR(20)` | ticker |
| `buyer_broker` | `SMALLINT` | buying member id |
| `seller_broker` | `SMALLINT` | selling member id |
| `quantity` | `INT` | shares traded |
| `rate` | `NUMERIC(10,2)` | execution price |
| `amount` | `NUMERIC(14,2)` | `quantity × rate` |

`PRIMARY KEY (trade_date, contract_id)`.
Indexes: `(symbol, trade_date)`, `(buyer_broker, trade_date)`,
`(seller_broker, trade_date)`.

### 4.2 `daily_broker_rollup` — aggregated daily buy/sell/VWAP per symbol/broker

| Column | Type | Notes |
|--------|------|-------|
| `trade_date` | `DATE` | |
| `symbol` | `VARCHAR(20)` | |
| `broker_id` | `SMALLINT` | |
| `buy_qty` | `BIGINT` | directional buys only |
| `buy_amount` | `NUMERIC(18,2)` | `Σ amount` of directional buys |
| `sell_qty` | `BIGINT` | directional sells only |
| `sell_amount` | `NUMERIC(18,2)` | `Σ amount` of directional sells |
| `self_trade_qty` | `BIGINT` | **wash trades** (`buyer == seller`) logged apart |
| `matched_qty` | `BIGINT` | wash qty where `buyer == seller` (alias of wash qty, powers Match %) |

`PRIMARY KEY (trade_date, symbol, broker_id)`.
Indexes: `(symbol, trade_date)`, `(broker_id, trade_date)`.

### 4.3 `daily_market_summary` — daily stock-level close/turnover/rank & fundamental metrics

| Column | Type | Notes |
|--------|------|-------|
| `trade_date` | `DATE` | |
| `symbol` | `VARCHAR(20)` | |
| `close_price` | `NUMERIC(10,2)` | last `rate` of the session (sort by `contract_id`) |
| `price_change_pct` | `NUMERIC(10,4)` | vs. prior **trading session** close |
| `total_qty` | `BIGINT` | session volume |
| `total_turnover` | `NUMERIC(18,2)` | session `Σ amount` |
| `turnover_rank` | `INT` | rank by turnover within the session (1 = highest) |
| `sector` | `VARCHAR(50)` | NEPSE industry sector (e.g. Commercial Banks, Hydro Power) |
| `market_cap` | `NUMERIC(16,2)` | Market capitalization in Million NPR |
| `fifty_two_week_high` | `NUMERIC(10,2)` | 52-week price ceiling |
| `fifty_two_week_low` | `NUMERIC(10,2)` | 52-week price floor |
| `vwap` | `NUMERIC(10,2)` | Intraday Volume Weighted Average Price (`total_turnover / total_qty`) |

`PRIMARY KEY (trade_date, symbol)`; indexes `(trade_date, turnover_rank)`, `(sector, trade_date)`.
This table also defines the **trading-session calendar** (via `SELECT DISTINCT
trade_date`) used for window offsets and streak continuity.

### 4.4 `securities_meta` — company security master & sector metadata

| Column | Type | Notes |
|--------|------|-------|
| `symbol` | `VARCHAR(20)` | `PRIMARY KEY` |
| `company_name` | `VARCHAR(120)` | Full company title |
| `sector` | `VARCHAR(50)` | NEPSE sector classification |
| `instrument_type` | `VARCHAR(30)` | Instrument type (e.g. Equity) |
| `market_cap` | `NUMERIC(16,2)` | Market capitalization (Million NPR) |
| `fifty_two_week_high` | `NUMERIC(10,2)` | 52-week high |
| `fifty_two_week_low` | `NUMERIC(10,2)` | 52-week low |
| `updated_at` | `TIMESTAMP` | Auto-updated on metadata sync |

### 4.5 `screener_signals_history` — persisted screening outputs for streaks/backtest

| Column | Type | Notes |
|--------|------|-------|
| `trade_date` | `DATE` | signal session |
| `symbol` | `VARCHAR(20)` | |
| `turnover_rank` | `INT` | intra-session rank; `0` for Track B (unranked) |
| `broker_id` | `SMALLINT` | |
| `net_1d` | `INT` | broker net shares, T_1 window |
| `net_5d` | `INT` | T_5 |
| `net_22d` | `INT` | T_22 |
| `net_66d` | `INT` | T_66 (NULL for Track B) |
| `margin_pct` | `NUMERIC(6,2)` | `(close − buy vwap)/buy vwap × 100` |
| `t1_change_pct` | `NUMERIC(6,2)` | Track A: T_1 % change; **Track B stores its 22-day % change here** |
| `signal` | `VARCHAR(30)` | one of `ACTIONABLE` set (see §6.4) |
| `track` | `VARCHAR(10)` | `TRACK_A` / `TRACK_B` |
| `created_at` | `TIMESTAMP` | default `CURRENT_TIMESTAMP`; refreshed on upsert |

`PRIMARY KEY (trade_date, symbol, broker_id, track)` — so running the screener
for the same session twice **upserts** rather than duplicates (`ON CONFLICT
… DO UPDATE`).
Indexes: `(symbol, trade_date)`, `(broker_id, trade_date)`,
`(signal, trade_date)`.

### 4.5 `watchlist` and `watchlist_notes` — personal research journal

`watchlist` stores one research thesis per ticker (with tags and active/archive
status), optional entry/target/stop/quantity, and final exit outcome. `watchlist_notes`
is an append-only, dated log of observations. The CLI joins the watchlist to the
most recent `daily_market_summary` row so `watch list` shows current price, daily
change, turnover rank, and live or realized percentage PnL without manual updates.

> **Volume migrations for existing `pgdata`:** `schema.sql` only auto-runs on a
> **fresh** `pgdata` volume. If you already have a running DB predating this
> table, re-apply the schema to add the new table + indexes idempotently:
>
> ```bash
> docker compose exec -T db psql -U quant -d nepse_analytics \
>   -f /docker-entrypoint-initdb.d/init.sql
> ```
>
> For an existing DB that predates the `matched_qty` column (Wash/Cross-Trade
> detection, §6.9), add the column and backfill it from the floorsheet:
>
> ```bash
> docker compose exec -T db psql -U quant -d nepse_analytics \
>   -c 'ALTER TABLE daily_broker_rollup ADD COLUMN IF NOT EXISTS matched_qty BIGINT NOT NULL DEFAULT 0';
> docker compose exec -T db psql -U quant -d nepse_analytics \
>   -c 'UPDATE daily_broker_rollup r SET matched_qty = m.q
>       FROM (SELECT trade_date, symbol, buyer_broker AS broker_id,
>                    SUM(quantity) AS q
>             FROM floorsheet WHERE buyer_broker = seller_broker GROUP BY 1,2,3) m
>       WHERE r.trade_date = m.trade_date AND r.symbol = m.symbol
>         AND r.broker_id = m.broker_id';
> ```

---

## 5. Ingestion & Data Sourcing Pipeline

### 5.1 Hybrid architecture
Two complementary sources feed the same canonical `floorsheet` columns
(`trade_date, contract_id, symbol, buyer_broker, seller_broker, quantity,
rate, amount`):

| Source | When | Mechanism |
|--------|------|-----------|
| **Live NEPSE SPA endpoint** | latest trading day | reverse-engineered `POST /api/nots/nepse-data/floorsheet`, paginated at 500 rows; **server-time clock correction** (below) |
| **GitHub open-data repo** | historical backfill (`fetch --days N`) | canonical `floorsheet_YYYY-MM-DD.csv` files via `raw.githubusercontent.com`, cached to `data/real/` |

Both produce identical CSVs in `data/real/YYYY-MM-DD.csv`, so `ingest_csv`
consumes them unchanged.

#### Server-time clock correction (`fetcher.server_payload_id`)
The NEPSE SPA computes a signed `payload id` from the market-open id, a salt
array from the auth token, and **the *server's* current day-of-month**. The
`nepse-scraper` package derives that day from the **client clock**
(`datetime.now().day`), which produces invalid IDs when the container clock
drifts off the NEPSE server's calendar. This project recomputes the payload id
using the day-of-month embedded in the token's `serverTime`
(Asia/Katmandu, UTC+5:45):

```python
server_utc   = datetime.utcfromtimestamp(details["serverTime"] / 1000.0)
server_local = server_utc + timedelta(hours=5, minutes=45)
raw          = parser.dummyData[market_id] + market_id + 2 * server_local.day
index_value  = 1 if raw % 10 < 5 else 3
payload_id   = raw + details[f"salt{index_value+1}"]*server_local.day \
                   - details[f"salt{index_value}"]
```

### 5.2 Idempotent fetching (`fetch --today`)
`fetch --today` no longer blindly re-downloads. It consults
`latest_trade_date()` / `_latest_ingested_date()` (the `MAX(trade_date)` in
`daily_market_summary`). If the local CSV's date already equals the DB's latest
date, it prints `Already current at YYYY-MM-DD, skipping re-ingest` and exits
`0` without touching the network. Only when the DB is behind does it fetch +
ingest.
### 5.3 Ingestion flow (`ingestion.ingest_floorsheet`)
Single-purpose pipeline, fully vectorized in Polars:

1. `floorsheet_frame()` — normalize types (dates, ints, floats) and derive a
   `matched_quantity` feature column:
   `when(buyer_broker == seller_broker).then(quantity).otherwise(0)`.
2. `compute_daily_broker_rollup()`:
   - split **directional** (`buyer ≠ seller`) from **wash** (`buyer == seller`);
   - aggregate `buy_qty/buy_amount` by buyer, `sell_qty/sell_amount` by seller;
   - log wash separately as `self_trade_qty` and `matched_qty`;
   - full outer join on all `(date, symbol, broker_id)` keys, null→0.
3. `compute_daily_market_summary()`:
   - `close_price` = last `rate` ordered by `(date, symbol, contract_id)`;
   - `price_change_pct` vs. the **previous trading session's** close (resolved
     via `fetch_prev_closes`, which walks the existing calendar so gaps don't
     produce wrong % change);
   - `total_qty`, `total_turnover`, and `turnover_rank` (desc by turnover).
4. `replace_dates` → `DELETE FROM floorsheet WHERE trade_date = ANY(...)` then
   bulk `COPY` (`execute_values`).
5. Upsert `daily_broker_rollup` and `daily_market_summary`, then `commit`.

---

## 6. Quantitative Screener Mechanics & Rules

The screener is **dual-track**. Both tracks read only the raw
rollup/summary, so results are identical whether fed live or mock data.

### 6.1 Lookback windows — session-based offsets
Windows are measured in **trading sessions**, not calendar days:

```python
WINDOWS = {"T_1": 1, "T_5": 5, "T_22": 22, "T_66": 66}
```

`window_dates(all_dates)` takes the real set of distinct `trade_date` values
from `daily_market_summary` and, for each `T_N`, returns the **last N sessions**
(`ordered[-N:]`). With fewer than N available it returns all sessions. The
**T_1 session is the most recent** — passing `--as-of` re-baselines it to the
cutoff date for forensics.

### 6.2 Core formulas
All per-broker metrics over each window via `aggregate_window()`:

| Metric | Formula |
|--------|---------|
| **Net volume / shares** | `net_qty = Σ buy_qty − Σ sell_qty` (directional only) |
| **Buy VWAP** | `buy_amount / buy_qty` (divide-by-zero guard → NULL) |
| **Sell VWAP** | `sell_amount / sell_qty` |
| **Cost-basis margin %** | `(close − buy_vwap[T_66]) / buy_vwap[T_66] × 100` |
| **Window dominance %** | `nets[T_N] / stock_total_qty[T_N] × 100` ("dominant buyer" = `T_22 ≥ 8%` **or** `T_66 ≥ 8%`) |
| **Absorption %** (Track B) | `net_qty[T_22] / total_qty[T_22] × 100` |
| **Seller dispersion %** (Track B) | `top3_sell_qty[T_22] / total_sell[T_22] × 100` |
| **Volume inflection ratio** (Track B) | `t1_turnover / avg_daily_turnover[T_22]` |

### 6.3 Track A — Top-turnover momentum & traps
- Candidate universe: symbols whose **T_1 `turnover_rank ≤ top_turnover`**,
  configurable per run and **defaulting to the top 20** symbols by T_1 turnover
  (set via the CLI `--top N` flag; the table title and header reflect the
  active threshold, e.g. `Track A — Top 20 Turnover Momentum & Traps`).
- **Sector & Market-Cap Tier filtering:** filter scans directly by sector (e.g. `--sector "Commercial Banks"`) or cap size tier (e.g. `--cap-tier LARGE|MID|SMALL`).
- **Instrument exclusions:** promoter stocks and debentures are dropped from the
  candidate universe before ranking. A promoter is any ticker ending in `P`
  (e.g. `LECP`, `NABILP`) **except** `HIDCLP` and `HEIP`, which are kept.
  Debentures carry a digit in their ticker (e.g. `H8020`, `PRVU2084`) and are
  excluded. Other specific symbols can be added to `EXCLUDED_SYMBOLS` in
  `src/screener.py`.
- For each symbol, take the **top-3 net buyers** in T_1 plus the **single top
  seller** (as a potential trap row).
- Evaluate `classify_track_a()` with the windows, buy-VWAP margin, and the
  stock's T_1 % change.
- Rows that fail all classifiers fall back to `WATCH`; in the rendered table
  only `buyer_rank == 1` `WATCH` rows are shown (to avoid noise).

**Top-Holder early signal (Hold / Sell):** every symbol's rows carry two extra
columns, populated by grouping the symbol's broker rows and picking the broker
with the **highest net in the configured top-holder window** (default `T_22`)
— the *long-term holder*. The window is configurable via `run --top-holder-window N`
(`1|5|22|66`); column headers update to match, e.g. `Top Holder (T_66D)`:

| Column | Meaning |
| :--- | :--- |
| **Top Holder** | Broker ID with the greatest `net_t{window}` for that symbol (the longest-held, dominant accumulator). |
| **Holder T1** | That holder's **one-day net** (`net_t1`). Shown only on the holder's own row; `-` elsewhere. |

Use it as a **single-glance hold/sell** read on the dominant holder's *recent*
intent:

- **Holder T1 > 0** → still **accumulating** → **HOLD** (position intact).
- **Holder T1 < 0** → **starting to distribute** → **SELL warning** (the big
  holder is beginning to offload, even if the stock looks strong on the day).

This is the key insight pairing **top turnover** (liquidity filter) with
**broker intent** (early signal): the broker who has quietly built the largest
multi-window position, and what they did *today*.

### 6.4 Signal taxonomy

| Signal | Track | Trigger rules |
|--------|-------|---------------|
| `SILENT_ACCUMULATION` | A | `n66 > 0`, `n22 > 0`, `n1 > 0`, margin ∈ [−3, +3]%, and `t1_change ≤ 2.0%` — buying across all windows at cost basis, price **not** yet moving |
| `ACTIVE_MARKUP` | A | `n22 > 0`, `n1 > 0`, `t1_change > 2.5%`, margin `> 5%` — position now pushing price up with expanding profit |
| `DISTRIBUTION_TRAP` | A | broker **was** a dominant buyer (`T_22/T_66 ≥ 8%`) but is the **top T_1 seller** with `n1 < 0` — the "strength" exit |
| `STEALTH_ACCUMULATION` | B | `price_change_pct(T_22) ∈ [−4, +4]%`, T_1 turnover ≥ **2×** 22-day average, top broker `net[T_22]` absorption `≥ 20%`, seller dispersion `< 25%` — fragmented selling + tight price + volume spike |
| `SMART_MONEY_ABSORPTION` | C | Scraped live from ShareSansar via Playwright, sent to Gemini AI for natural language analysis, and alerted to Discord/Web UI. |
| `WATCH` | A | fallback/unclassified (not persisted) |

### 6.5 Streak semantics (`signals.current_streaks`)
Only the `ACTIONABLE` signals are persisted —

```python
ACTIONABLE = {"SILENT_ACCUMULATION", "ACTIVE_MARKUP",
              "DISTRIBUTION_TRAP", "STEALTH_ACCUMULATION"}
```

`WATCH`/unclassified rows are dropped entirely, **so an absence of a persisted
row for a real session means the signal genuinely disappeared.**

A *current streak* is computed per `(symbol, broker_id, track, signal)`:

1. Build the **real trading-session timeline** from
   `SELECT DISTINCT trade_date FROM daily_market_summary ORDER BY trade_date`.
2. Find each signal's most recent occurrence; walk it **backward through that
   timeline** counting consecutive sessions where the exact signal appears.
3. Because the timeline is real trading days, **weekends/holidays do not break
   a streak** — but **any real session that lacks the signal does**.
4. Filter to `streak ≥ min_streak` and sort by `(−streak, last_date, symbol)`.

*Example:* broker 58 on `SOHL` showing `SILENT_ACCUMULATION` for both 09-07 and
09-11 (consecutive sessions) yields `streak = 2`.

### 6.6 Point-in-time backtesting (`run --as-of`)
`run_screener(as_of=..., persist=…)` filters the session list to `d ≤ as_of`
**before** slicing windows, so the T_1 date and all lookbacks are computed
exactly as they would have been known *on* that date — no look-ahead. It then
(re)persists that session's signals, which is how history is rebuilt
retroactively.

### 6.7 Personal research journal (`watch`)

`watch` is the durable manual layer over the automated scanners. Use it to record
why a candidate from `momentum`, Track A/B, or `wash` deserves attention, then
append observations after each `inspect`. It stores a thesis, tags, dated notes,
and active/archive status in PostgreSQL. Once you take a trade, record the real
entry, target, and stop; `watch list` then reports live PnL from the latest close
(or realized PnL after exit).

```bash
# Create a research item from a scanner result.
docker compose run --rm app python -m src.cli watch add LEC \
  --thesis "T22 broker accumulation with rising turnover" \
  --tags "momentum,track-a" \
  --note "Inspect broker 58 after next close"

# Record what changed after checking the next floorsheet.
docker compose run --rm app python -m src.cli watch note LEC "Broker 58 stayed net positive; hold thesis."

# When you act, capture the actual entry and risk plan. PnL is then automatic.
docker compose run --rm app python -m src.cli watch enter LEC \
  --price 240 --target 280 --stop 225 --quantity 100
docker compose run --rm app python -m src.cli watch list
docker compose run --rm app python -m src.cli watch exit LEC --price 265 --outcome WON
docker compose run --rm app python -m src.cli watch history LEC
docker compose run --rm app python -m src.cli watch archive LEC

# Evaluate whether persisted screener signals have a measurable forward edge.
docker compose run --rm app python -m src.cli signals --performance
```

The same project-wide exclusions apply: promoter symbols, debentures, and manual
exclusions such as `RSY` cannot be added to the journal.

### 6.8 Multi-window turnover momentum (`momentum`)
A secondary scanner (`screen_turnover_momentum`) compares **short-window**
liquidity (default **5 sessions**) against a **baseline** (default **22
sessions**) to find structural turnover shifts, independent of price moves:

| Metric | Definition |
|--------|-----------|
| `avg_turnover_short` / `avg_turnover_base` | Mean `total_turnover` over the last short / base sessions |
| `avg_rank_short` / `avg_rank_base` | Mean `turnover_rank` over the last short / base sessions |
| `rank_drift` | `avg_rank_base - avg_rank_short` — positive = climbing the board |
| `turnover_ratio` | `avg_turnover_short / avg_turnover_base` |
| `price_change_pct_window` | Net close % change across the short window |

Classification (both windows use global session-based lookbacks; a symbol
present in the short window but with fewer baseline sessions is simply
excluded, never an error):

| Bucket | Filter | Sort |
|--------|--------|------|
| **MOMENTUM_GAINER** | `avg_rank_short ≤ 50`, `rank_drift ≥ 15`, `turnover_ratio ≥ 1.75` — structural liquidity building | `rank_drift` desc |
| **MOMENTUM_LOSER** | `avg_rank_base ≤ 40` (was active), `rank_drift ≤ -15`, `turnover_ratio ≤ 0.50` — liquidity drying / capital exit | `rank_drift` asc |

Each candidate is **enriched with broker footprint**: the dominant net buyer
(`top_accumulator`) and net seller (`top_distributor`) broker over the short
window, drawn from `daily_broker_rollup` net flows.

### 6.9 Internal matching & cross/wash-trade detection (`wash`)
Wash/cross-trade detection quantifies how much volume is booked by the **same
broker on both sides** of a trade (`buyer_broker == seller_broker`). Two
metrics derive directly from the rollup's `matched_qty` column.

**Broker-level matching — `compute_broker_match_pct(rollup, dates, per_symbol=False)`**

| Metric | Definition |
|--------|-----------|
| `buy_qty` | `Σ quantity` where `buyer_broker = B` |
| `sell_qty` | `Σ quantity` where `seller_broker = B` |
| `matched_qty` | `Σ quantity` where `buyer_broker = seller_broker = B` |
| `gross_volume` | `buy_qty + sell_qty` |
| **`match_pct`** | `2 × matched_qty / gross_volume × 100` — **0.0 when gross = 0** |

With `per_symbol=True` the same aggregation is produced per `(symbol, broker_id)`
pair instead of per broker.

**Session-level matching — `compute_session_match_pct(rollup, summary, dates)`**

| Metric | Definition |
|--------|-----------|
| `crossed_qty` | `Σ contract_quantity` where `buyer_broker = seller_broker` for a `(date, symbol)` |
| `total_qty` | `daily_market_summary.total_qty` for the same session/symbol |
| **`session_match_pct`** | `crossed_qty / total_qty × 100` — **0.0 when total = 0** |

Notes:

- A 0.0 denominator always yields 0.0% (no `NaN`/`inf`).
- `matched_qty` is a **wash-trade alias**: it equals `self_trade_qty`, which is
  the `Σ matched_quantity` (the `when(buyer==seller).then(quantity).otherwise(0)`
  feature emitted in `floorsheet_frame`).
- Because `matched_quantity` is derived in `floorsheet_frame`, but
  `compute_daily_broker_rollup` also derives it defensively when it is absent,
  the pipeline works on raw in-memory floorsheet frames passed in directly.
- Existing databases (already-populated `pgdata`) need a one-line migration; see
  the volume-migration note in §4.

### 6.10 Deterministic Scoring Engine (Position Verdicts)
The system calculates a continuous score (from -5 to +5) for any queried symbol to produce a real-time positional verdict (**Strong Buy, Buy, Hold, Avoid / Exit**). This avoids LLM hallucination and runs completely deterministically based on:
1. **Price Rejection**: Did the stock close near its intraday high (< 25% rejection) or was it severely dumped from highs (> 60% rejection)?
2. **Volume Validation (RVOL)**: Does the daily volume exceed 1.5x the 20-day average, giving legitimacy to the move?
3. **Institutional Absorption**: A Net Absorption Ratio > 1.3 (Top 3 Buyers vs Top 3 Sellers) scores positive; a ratio < 0.7 scores negative.
4. **Wash Trade Penalty**: Over 25% internal crossed volume deducts points to filter out artificial markup.
The final verdict is asynchronously loaded into the web UI (`lazy-verdict`) as a colored badge across most symbol tables.

---
## 7. CLI Usage & Daily Operations

The entire daily workflow, including market scanners, momentum tracking, wash trade detection, and data ingestion, is executed via the `src.cli` module.

**For the complete daily runbook, command syntax, and flags, please refer to [`COMMANDS.md`](./COMMANDS.md).**

---

## 8. Quick Reference — End-to-End Workflow

```bash
# 1. Start Postgres (schema auto-creates; healthcheck gates the app)
docker compose up -d db

# 2. Build the app image
docker compose build app

# 3. Ingest data (idempotent)
docker compose run --rm app python -m src.cli fetch --today

# 4. Run the screener (persists signals)
docker compose run --rm app python -m src.cli run

# 5. Inspect a symbol's footprint, a broker's holdings, + detect live streaks
docker compose run --rm app python -m src.cli inspect LEC
docker compose run --rm app python -m src.cli broker 58 --top 5
docker compose run --rm app python -m src.cli signals --streak 2

# 6. Per-symbol analysis + next-session prediction (rank<->broker<->price)
docker compose run --rm app python -m src.cli analyze ADBL --sessions 30

# 7. Web UI (http://localhost:18000 — includes an "Analyze" tab and "Smart Money" AI Alerts)
docker compose run --rm -p 18000:8000 app python -m src.cli serve --port 8000

# 8. Test
docker compose run --rm app python -m pytest
```

### Data flow at a glance
```
NEPSE SPA / GitHub repo
   │  fetch_*()
   ▼
data/real/YYYY-MM-DD.csv  (canonical columns)
   │  ingest_csv()
   ▼
floorsheet ──► daily_broker_rollup ──┐
   │                                 ├─► screener (windows T_1/T_5/T_22/T_66)
   └────────► daily_market_summary ──┘        │   │  Track A | Track B
                                             │   │  classify_track_a / Track B rules
                                             │   ▼
                                             signal rows (WATCH dropped)
                                             │  persist_signals (upsert)
                                             ▼
                                   screener_signals_history
                                             │
        signals                      │  current_streaks()
   ────►  ── history query ───────────/      ▼
                                             current streaks table
```

## Environment Variables
| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `postgresql://quant:quantpass@db:5432/nepse_analytics` | psycopg2/SQLAlchemy connection string |

## Notes
- The live endpoint requires a signed payload each request; this project
  disables SSL verification for the NEPSE HTTPS call (`verify_ssl=False`) and
  recomputes the server-time payload id (see §5.1) to survive clock drift.
- The synthetic-data generator (`src/mock_generator.py`) seeds reproducible
  scenarios — `LEC` → Track A `SILENT_ACCUMULATION` (Broker 58 dominating),
  `HIDCL` → Track B stealth setup (Broker 41, absorption + fragmented selling +
  volume spike) — and is exercised by the automated tests.

## Roadmap & Future Enhancements

### 1. Broker-Window Profitability Matrix (Backtesting)
A planned quantitative module (`src/ml/broker_backtest.py`) to answer: *"Which broker, trading which stock, under which accumulation window, generates the highest T+5 and T+20 yields?"* 
- Maps top accumulators (e.g., Broker 58, 14, 94) to their optimal accumulation windows (e.g., 5D vs 22D baseline).
- Outputs a backtested win-rate and yield matrix for high-conviction entries.

### 2. Multi-Agent Data Pipeline
Potential evolution of the pipeline using frameworks like `CrewAI` or `LangGraph` where specialized AI agents orchestrate the workflow:
- **Data Engineer Agent:** Cleans daily PostgreSQL inputs.
- **Modeler Agent:** Automatically retrains the XGBoost model to optimize forward probability windows.
- **Critic Agent:** Reviews screener outputs against historical backtests to construct human-readable daily briefings.
