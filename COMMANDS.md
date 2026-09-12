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
*   `--no-persist`: Runs the screener without upserting signals into `screener_signals_history`.
*   `--as-of YYYY-MM-DD`: Backtests the market as of a specific past session close.

---

## 2. Deep Dives & Audits

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`inspect`** | Displays recent session OHLCV, multi-window broker flows, and signal logs for a symbol. | `docker compose run --rm app python -m src.cli inspect <SYMBOL> --sessions 22` |
| **`signals`** | Audits historical persisted signals and multi-session accumulation streaks. | `docker compose run --rm app python -m src.cli signals` |

*   `--sessions N`: Number of historical sessions to display in the OHLCV table (default: 22).

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