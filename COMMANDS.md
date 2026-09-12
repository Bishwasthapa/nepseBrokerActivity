# NEPSE Screener CLI Cheatsheet

Quick reference guide for running scans, inspecting symbols, and maintaining the floorsheet database.

All commands run via Docker Compose from the project root:
`docker compose run --rm app python -m src.cli <command> [options]`

---

## 1. Daily Market Scanners

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`run`** | Screens Top 20 for active markup/silent accumulation (Track A) and market-wide stealth absorption (Track B). | `docker compose run --rm app python -m src.cli run` |
| **`momentum`** | Tracks turnover velocity and rank drift (gainers & losers) comparing short vs. baseline sessions. | `docker compose run --rm app python -m src.cli momentum --short 5 --base 22` |
| **`wash`** | Detects internal broker matching (`buyer == seller`) and crossed volume churn. | `docker compose run --rm app python -m src.cli wash --window 22` |

### Key Flags for Scanners
*   `--no-persist`: Runs the screener without upserting signals into `screener_signals_history`.
*   `--as-of YYYY-MM-DD`: Backtests the market at a specific historical session close.
*   `--top N`: Adjusts Track A turnover universe depth (default: 20).

---

## 2. Deep Dives & Audits

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`inspect`** | Displays recent session OHLCV, multi-window broker flows, and signal logs for a symbol. | `docker compose run --rm app python -m src.cli inspect <SYMBOL> --sessions 22` |
| **`signals`** | Audits historical persisted signals and multi-session accumulation streaks. | `docker compose run --rm app python -m src.cli signals` |

---

## 3. Data Pipeline & Maintenance

| Command | Description | Default Syntax |
| :--- | :--- | :--- |
| **`fetch`** | Downloads raw trade-level floorsheet contracts from NEPSE. | `docker compose run --rm app python -m src.cli fetch --today` |
| **`ingest`** | Ingests downloaded files into PostgreSQL, builds summaries, and updates broker rollups. | `docker compose run --rm app python -m src.cli ingest` |
| **`seed`** | Initializes database schemas, tables, and runs historical seed migrations. | `docker compose run --rm app python -m src.cli seed` |
| **Backup** | Creates a timestamped PostgreSQL dump and removes archives older than 7 days. | `./scripts/backup_db.sh` |

---

## 4. Daily Post-Market Workflow (15:30)

1. **Preview Signals:** `docker compose run --rm app python -m src.cli run --top 20 --no-persist`
2. **Check Momentum:** `docker compose run --rm app python -m src.cli momentum`
3. **Check Wash Trades:** `docker compose run --rm app python -m src.cli wash`
4. **Inspect Ticker:** `docker compose run --rm app python -m src.cli inspect <SYMBOL>`