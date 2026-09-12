# NEPSE Screener CLI Cheatsheet

Quick reference guide for running daily scans, inspecting symbols, and maintaining the floorsheet database.

All commands run via Docker Compose from the project root:
`docker compose run --rm app python -m src.cli <command> [options]`

---

## 1. Daily Market Scanners

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`run`** | Screens Top 20 for active markup/silent accumulation (Track A) and market-wide stealth absorption (Track B). | `docker compose run --rm app python -m src.cli run` |
| **`momentum`** | Tracks turnover velocity and rank drift (gainers & losers) comparing short vs. baseline windows. | `docker compose run --rm app python -m src.cli momentum --short 5 --base 22` |
| **`wash`** | Detects internal broker matching (`buyer == seller`) and crossed volume churn. | `docker compose run --rm app python -m src.cli wash --window 22` |

### Key Flags & Window Parameters
*   `--short N`: Short lookback window in trading sessions (`5` = 1 trading week).
*   `--base N` / `--window N`: Baseline/lookback window in trading sessions (`22` = 1 trading month, `66` = 1 quarter).
*   `--top N`: Adjusts Track A turnover universe depth (default: 20).
*   `--top-holder-window N`: Window that defines the Track A **Top Holder**
    (`1|5|22|66`; default: `22`). Column headers update to match, e.g.
    `Top Holder (T_66D)`.
*   `--no-persist`: Runs the screener without upserting signals into `screener_signals_history`.
*   `--as-of YYYY-MM-DD`: Backtests the market as of a specific past session close.

> **Track A "Top Holder" columns:** the `run` output's Track A table includes
> **Top Holder** (the broker with the highest net in the configured window, i.e.
> the dominant long-term accumulator) and **Holder T1** (that holder's latest
> one-day net, shown only on its own row). Use `--top-holder-window N` to pick
> which window defines the holder (default `22`). Quick hold/sell read:
> **Holder T1 > 0 → still accumulating (HOLD)**; **Holder T1 < 0 → starting to
> distribute (SELL warning)**.

### Examples
*   `run` — today's screen: `docker compose run --rm app python -m src.cli run`
*   `run --top 40` — widen the Track A turnover universe to the top 40 symbols.
*   `run --no-persist` — dry-run; prints signals but writes nothing to `screener_signals_history`.
*   `run --as-of 2026-09-10` — backtest the market as of a past session close.
*   `run --top-holder-window 66` — define the Top Holder by 66D net instead of the 22D default.
*   `momentum` / `momentum --short 5 --base 22` — 5-vs-22-day rotation (defaults; `--base 66` = quarterly).
*   `momentum --short 10 --base 66 --as-of 2026-09-10` — as-of variant of the above.
*   `wash` / `wash --window 22` — detect internal matching over the last 22 sessions (default).
*   `wash --window 66 --as-of 2026-09-10` — broader window, point-in-time.

---

## 2. Deep Dives & Audits

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`inspect`** | Displays recent session OHLCV, multi-window broker flows, and signal logs for a symbol. | `docker compose run --rm app python -m src.cli inspect <SYMBOL> --sessions 22` |
| **`broker`** | Deep-dive one broker's holdings across all stocks and windows (T1/T5/T22/T66). | `docker compose run --rm app python -m src.cli broker <ID> --top 5` |
| **`signals`** | Audits historical persisted signals and multi-session accumulation streaks. | `docker compose run --rm app python -m src.cli signals` |
| **`watch`** | Maintains your personal research watchlist and dated notes, with latest market context. | `docker compose run --rm app python -m src.cli watch list` |

*   `--sessions N`: Number of historical sessions to display in the OHLCV table (default: 22).

### Broker deep-dive flags
*   `broker <ID>`: Broker ID to inspect (required).
*   `--top N`: Number of top holdings (by net T22) to show (default: 5).
*   `--sessions N`: Session lookback for the deep-dive (default: 66).

### Examples
*   `inspect LEC` — deep dive on LEC over the last 22 sessions.
*   `inspect NRN --sessions 66` — show 66 sessions of OHLCV and multi-window broker flows.
*   `signals` — audit the full persisted signal history.
*   `signals --symbol LEC` — filter signal history to a single ticker.
*   `signals --track TRACK_A --limit 50` — latest 50 Track A signals.
*   `signals --streak 3` — tickers currently accumulating for 3+ consecutive sessions.
*   `signals --signal ACTIVE_MARKUP --broker 38` — combine signal-type and broker filters.
*   `broker 58` — deep dive on broker 58's top 5 holdings (last 66 sessions).
*   `broker 58 --top 10 --sessions 22` — top 10 holdings over the last 22 sessions.
*   `watch add LEC --thesis "Broker 58 accumulating" --tags "momentum,track-a"` — start researching a ticker.
*   `watch note LEC "T22 remains positive; recheck next session"` — append a dated observation after `inspect`.
*   `watch list` — show active research names with their latest close, daily change, turnover rank, and latest note.
*   `watch history LEC` — review the full thesis and journal for one ticker.
*   `watch archive LEC` — hide a completed/invalidated idea without deleting its history.

---

## 3. Data Pipeline & Maintenance

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`fetch`** | Downloads raw trade-level floorsheet contracts from NEPSE and ingests them. | `docker compose run --rm app python -m src.cli fetch --today` |
| **`ingest`** | Ingests one floorsheet file into PostgreSQL, rebuilds summaries, and updates broker rollups. | `docker compose run --rm app python -m src.cli ingest --file data/real/2026-09-11.csv` |
| **`seed`** | Initializes database schemas, tables, and runs historical seed migrations. | `docker compose run --rm app python -m src.cli seed` |
| **Backup** | Creates a timestamped PostgreSQL dump and removes archives older than 7 days. | `./scripts/backup_db.sh` |

*   `fetch --today`: Downloads and ingests the latest trading day.
*   `fetch --days N`: Backfills and ingests the last `N` trading sessions from the GitHub open-data repo.
*   `ingest --file PATH`: Loads a specific existing CSV from disk (idempotent; replaces that date's rows).
*   `ingest --file data/real/YYYY-MM-DD.csv` is how you backfill/repair a single missed or stale session.

### Examples
*   `fetch --today` — download and ingest the latest trading day (the daily recipe).
*   `fetch --days 30` — backfill and ingest the last 30 trading sessions from the GitHub open-data repo.
*   `ingest --file data/real/2026-09-11.csv` — load one existing CSV (idempotent; replaces that date's rows).
*   `seed` — init / rebuild the schema (default 66 days).
*   `seed --days 90 --seed 42` — reseed 90 days using a fixed RNG seed.
*   `./scripts/backup_db.sh` — timestamped PostgreSQL dump; keeps the last 7 days of archives.

---

## 4. Daily Post-Market Workflow (15:30)

Run in sequence after market close:

1. **Fetch Today's Trades:**
   `docker compose run --rm app python -m src.cli fetch --today`

2. **Process into Database:**
   `docker compose run --rm app python -m src.cli ingest --file data/real/2026-09-11.csv`
   (or rely on the auto-ingest that `fetch --today` already performed)

3. **Preview Signals:**
   `docker compose run --rm app python -m src.cli run --top 20 --no-persist`

4. **Check Capital Rotation:**
   `docker compose run --rm app python -m src.cli momentum`

5. **Check Wash Trades:**
   `docker compose run --rm app python -m src.cli wash`

6. **Inspect High-Conviction Tickers:**
   `docker compose run --rm app python -m src.cli inspect <SYMBOL>`

7. **Journal the decision:**
   `docker compose run --rm app python -m src.cli watch add <SYMBOL> --thesis "why it is on watch"`
   then append observations with `watch note <SYMBOL> "what changed today"`.

---

## 5. Scheduling (cron)

NEPSE trades roughly 11:00–15:00 local time (NPT, UTC+5:45), so the post-market
runbook runs ~15:30 NPT. The project ships a wrapper that runs the daily workflow
(`fetch --today` → `run --no-persist` → `momentum` → `wash`) and appends a log:

```bash
# ~/bin or crontab PATH must include docker/docker-compose.
/mnt/personal/stock/brokerActivity/scripts/daily_market.sh
```

Add the daily cron (adjust time to your host's timezone; NPT = UTC+5:45):

```
# Every weekday at 15:30 NPT (09:45 UTC if your host is UTC).
30 15 * * 1-5 cd /mnt/personal/stock/brokerActivity && ./scripts/daily_market.sh >> logs/daily_market.log 2>&1
```

Use `crontab -e` if you only need it for today's user, or install under
`/etc/cron.d/` (root) if system-wide. Set `TZ=Asia/Kathmandu` on a UTC host if you
want the literal wall-clock to match NPT.

Optionally add a weekly backup (Sundays 04:00):

```
# Weekly Postgres dump; the script prunes archives older than 7 days.
0 4 * * 0 /mnt/personal/stock/brokerActivity/scripts/backup_db.sh >> /mnt/personal/stock/brokerActivity/logs/backup.log 2>&1
```

Notes:
*   `1-5` runs Mon–Fri; NEPSE market holidays still fire these jobs and will simply
    log "No floorsheet available for today" / skip — harmless.
*   Make sure cron's `PATH` (and `docker` group membership) allows `docker compose` to run non-interactively.
*   `run --no-persist` previews signals without writing; drop the flag in the wrapper
    if you want signals persisted every day.