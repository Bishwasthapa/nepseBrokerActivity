"""Fetch real NEPSE floorsheet data into `data/real/YYYY-MM-DD.csv`.

The NEPSE API requires a signed payload ID that is derived from the market-open
id, a salt array from the auth token, and the *server's current day-of-month*.
The `nepse-scraper` package computes this from the client clock, which breaks
when the container clock is out of sync with the NEPSE server. We therefore
recompute the payload ID from the token's `serverTime` instead.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path

from nepse_scraper import NepseScraper
from nepse_scraper.auth import PayloadParser

from src.api_client import rate_limiter

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "real"
DATA_DIR.mkdir(parents=True, exist_ok=True)

FLOOR_CSV_COLUMNS = [
    "trade_date",
    "contract_id",
    "symbol",
    "buyer_broker",
    "seller_broker",
    "quantity",
    "rate",
    "amount",
]

# Map of raw NEPSE floorsheet field -> DB column name.
FIELD_MAP = {
    "businessDate": "trade_date",
    "contractId": "contract_id",
    "stockSymbol": "symbol",
    "buyerMemberId": "buyer_broker",
    "sellerMemberId": "seller_broker",
    "contractQuantity": "quantity",
    "contractRate": "rate",
    "contractAmount": "amount",
}

_PAGE_SIZE = 500


def server_payload_id(scraper: NepseScraper) -> int:
    """Compute the floorsheet payload ID using the NEPSE server's day-of-month.

    `nepse-scraper` computes the payload id from the client clock
    (`datetime.now().day`), which produces invalid IDs when the machine running
    this tool is on a different calendar day than the NEPSE server. We recompute
    using the day-of-month embedded in the auth token's `serverTime`.
    """
    details = scraper.session.token_details
    if not details:
        scraper.session._get_access_token()
        details = scraper.session.token_details
    server_utc = datetime.datetime.utcfromtimestamp(details["serverTime"] / 1000.0)
    server_local = server_utc + datetime.timedelta(hours=5, minutes=45)
    sday = server_local.day

    market_id = scraper.session._fetch_market_open_id()
    parser = PayloadParser()
    raw = parser.dummyData[market_id] + market_id + 2 * sday
    index_value = 1 if raw % 10 < 5 else 3
    payload_id = raw + details[f"salt{index_value + 1}"] * sday - details[f"salt{index_value}"]
    return payload_id


def _fetch_page(scraper: NepseScraper, date_str: str, payload_id: int, page: int) -> tuple[dict, int]:
    params = {
        "startDate": date_str,
        "endDate": date_str,
        "size": str(_PAGE_SIZE),
        "sort": "contractId,desc",
    }
    if page > 0:
        params["page"] = str(page)

    for attempt in range(3):
        allowed, info = rate_limiter.is_allowed("local", "/floorsheet")
        while not allowed:
            time.sleep(0.5)
            allowed, info = rate_limiter.is_allowed("local", "/floorsheet")

        try:
            resp = scraper.session.post(
                "/api/nots/nepse-data/floorsheet", params=params, payload={"id": payload_id}
            )
            return resp.json(), payload_id
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)
            try:
                scraper.session._get_access_token()
                payload_id = server_payload_id(scraper)
            except Exception:
                pass
    return {}, payload_id


def fetch_floorsheet_date(date_str: str) -> list[dict]:
    """Fetch and map one full trading day's floorsheet into DB columns."""
    scraper = NepseScraper(verify_ssl=False)
    scraper.session._get_access_token()
    payload_id = server_payload_id(scraper)

    rows: list[dict] = []
    page = 0
    while True:
        data, payload_id = _fetch_page(scraper, date_str, payload_id, page)
        # With an oversized `size`, NEPSE may return a bare list instead of the
        # paginated dict wrapper; treat a non-dict (empty) as "no data here".
        floorsheets = data.get("floorsheets") or {} if isinstance(data, dict) else {}
        total_pages = floorsheets.get("totalPages", 0)
        content = floorsheets.get("content") or []

        for raw in content:
            mapped = {}
            for src, dst in FIELD_MAP.items():
                mapped[dst] = raw.get(src)
            rows.append(mapped)

        if page >= (total_pages - 1) or not content:
            break
        page += 1
        time.sleep(0.05)

    return rows
def write_floorsheet_csv(date_str: str, rows: list[dict]) -> Path:
    """Write mapped rows to data/real/YYYY-MM-DD.csv (empty file if no rows)."""
    import csv

    path = DATA_DIR / f"{date_str}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FLOOR_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in FLOOR_CSV_COLUMNS})
    return path


def trading_dates(limit: int | None = None) -> list[str]:
    """Most-recent trading dates from NEPSE market-summary history (desc order)."""
    scraper = NepseScraper(verify_ssl=False)
    hist = scraper.get_market_summary_history()
    data = hist.json() if hasattr(hist, "status_code") else hist
    dates = sorted(
        {row["businessDate"] for row in data if row.get("businessDate")},
        reverse=True,
    )
    return dates if limit is None else dates[:limit]


REPO = "socrateai-official/nepse-open-data"


def list_repo_floorsheet_dates(limit: int | None = None) -> list[str]:
    """List trading dates available as floorsheet CSVs in the GitHub repo.

    Downloads are much faster and cover true historical sessions, unlike the
    live NEPSE floorsheet API which only ever returns the *current* market
    regardless of the requested date.
    """
    import requests

    api = f"https://api.github.com/repos/{REPO}/contents/floorsheet"
    resp = requests.get(api, timeout=60)
    resp.raise_for_status()
    dates = sorted(
        (
            item["name"].replace("floorsheet_", "").replace(".csv", "")
            for item in resp.json()
            if item["name"].startswith("floorsheet_") and item["name"].endswith(".csv")
        ),
        reverse=True,
    )
    return dates if limit is None else dates[:limit]


def download_floorsheet_from_repo(date_str: str) -> Path | None:
    """Download one day's floorsheet from the GitHub repo and write canonical CSV.

    Returns the written path, or None if the date is not present in the repo.
    """
    import csv
    import io

    import requests

    url = (
        f"https://raw.githubusercontent.com/{REPO}/main/"
        f"floorsheet/floorsheet_{date_str}.csv"
    )
    resp = requests.get(url, timeout=120)
    if resp.status_code != 200:
        return None

    rows: list[dict] = []
    text = io.StringIO(resp.text)
    reader = csv.DictReader(text)
    for raw in reader:
        try:
            rows.append(
                {
                    "trade_date": raw.get("date", date_str).strip(),
                    "contract_id": raw["transaction"].strip(),
                    "symbol": raw["symbol"].strip(),
                    "buyer_broker": int(float(raw["buyer"])),
                    "seller_broker": int(float(raw["seller"])),
                    "quantity": int(float(raw["quantity"])),
                    "rate": float(raw["rate"]),
                    "amount": float(raw["amount"]),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue

    return write_floorsheet_csv(date_str, rows)


def fetch_historical_floorsheets(days: int = 66) -> list[Path]:
    """Backfill the last `days` trading sessions from the GitHub repo.

    The live NEPSE floorsheet endpoint cannot serve true historical dates, so we
    prefer the open-data repo for backfill. The CSV produced is in canonical
    form so ``ingest_csv`` works unchanged.
    """
    dates = list_repo_floorsheet_dates(limit=days)
    written: list[Path] = []
    for d in dates:
        path = DATA_DIR / f"{d}.csv"
        if path.exists():
            written.append(path)
            print(f"  {d}: already cached -> {path.name}")
            continue
        out = download_floorsheet_from_repo(d)
        if out is None:
            print(f"  {d}: not in repo, skipping")
            continue
        n = sum(1 for _ in out.open()) - 1
        written.append(out)
        print(f"  {d}: {n:,} rows -> {out.name}")
        time.sleep(0.1)
    return written


def fetch_today() -> Path | None:
    """Fetch today's (latest trading day's) floorsheet."""
    dates = trading_dates(limit=1)
    if not dates:
        return None
    d = dates[0]
    path = DATA_DIR / f"{d}.csv"
    if not path.exists():
        rows = fetch_floorsheet_date(d)
        write_floorsheet_csv(d, rows)
    return path


def fetch_and_sync_securities_metadata() -> int:
    """Fetch live security master and market caps from NEPSE and sync to DB."""
    try:
        from src.db import get_conn
        scraper = NepseScraper(verify_ssl=False)

        # 1. Fetch authoritative security master with sector mappings
        sec_map: dict[str, dict] = {}
        try:
            all_sec = scraper.get_all_securities()
            all_list = all_sec.json() if hasattr(all_sec, "status_code") else all_sec
            if isinstance(all_list, list):
                for item in all_list:
                    sym = (item.get("symbol") or "").strip().upper()
                    if sym:
                        sec_map[sym] = {
                            "company_name": item.get("companyName") or item.get("securityName"),
                            "sector": item.get("sectorName") or item.get("sector"),
                            "instrument_type": item.get("instrumentType") or "Equity",
                        }
        except Exception as e:
            print(f"Warning: could not fetch get_all_securities: {e}")

        # 2. Fetch live market caps and 52-week ranges
        price_map: dict[str, dict] = {}
        try:
            today_data = scraper.get_today_price()
            price_list = today_data.json() if hasattr(today_data, "status_code") else today_data
            if isinstance(price_list, list):
                for item in price_list:
                    sym = (item.get("symbol") or "").strip().upper()
                    if sym:
                        price_map[sym] = {
                            "company_name": item.get("securityName") or item.get("companyName"),
                            "sector": item.get("sectorName") or item.get("sector"),
                            "market_cap": item.get("marketCapitalization"),
                            "fifty_two_week_high": item.get("fiftyTwoWeekHigh"),
                            "fifty_two_week_low": item.get("fiftyTwoWeekLow"),
                        }
        except Exception as e:
            print(f"Warning: could not fetch get_today_price: {e}")

        # 3. Merge all known symbols
        all_symbols = sorted(set(sec_map.keys()) | set(price_map.keys()))
        if not all_symbols:
            return 0

        rows = []
        for sym in all_symbols:
            s_info = sec_map.get(sym, {})
            p_info = price_map.get(sym, {})

            name = s_info.get("company_name") or p_info.get("company_name")
            sector = s_info.get("sector") or p_info.get("sector")
            itype = s_info.get("instrument_type") or "Equity"
            mcap = p_info.get("market_cap")
            h52 = p_info.get("fifty_two_week_high")
            l52 = p_info.get("fifty_two_week_low")

            rows.append(
                (
                    sym,
                    name.strip() if name else None,
                    sector.strip() if sector else None,
                    itype.strip() if itype else "Equity",
                    float(mcap) if mcap is not None else None,
                    float(h52) if h52 is not None else None,
                    float(l52) if l52 is not None else None,
                )
            )

        if not rows:
            return 0

        conn = get_conn()
        try:
            import psycopg2.extras
            sql = """
                INSERT INTO securities_meta
                    (symbol, company_name, sector, instrument_type, market_cap, fifty_two_week_high, fifty_two_week_low)
                VALUES %s
                ON CONFLICT (symbol) DO UPDATE SET
                    company_name = COALESCE(EXCLUDED.company_name, securities_meta.company_name),
                    sector = COALESCE(EXCLUDED.sector, securities_meta.sector),
                    instrument_type = COALESCE(EXCLUDED.instrument_type, securities_meta.instrument_type),
                    market_cap = COALESCE(EXCLUDED.market_cap, securities_meta.market_cap),
                    fifty_two_week_high = COALESCE(EXCLUDED.fifty_two_week_high, securities_meta.fifty_two_week_high),
                    fifty_two_week_low = COALESCE(EXCLUDED.fifty_two_week_low, securities_meta.fifty_two_week_low),
                    updated_at = CURRENT_TIMESTAMP
            """
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, sql, rows)
                # Backfill historical daily_market_summary rows missing sector / market cap
                cur.execute("""
                    UPDATE daily_market_summary s
                    SET sector = COALESCE(s.sector, m.sector),
                        market_cap = COALESCE(s.market_cap, m.market_cap),
                        fifty_two_week_high = COALESCE(s.fifty_two_week_high, m.fifty_two_week_high),
                        fifty_two_week_low = COALESCE(s.fifty_two_week_low, m.fifty_two_week_low)
                    FROM securities_meta m
                    WHERE s.symbol = m.symbol
                      AND (s.sector IS NULL OR s.market_cap IS NULL OR s.fifty_two_week_high IS NULL);
                """)
            conn.commit()
            return len(rows)
        finally:
            conn.close()
    except Exception as e:
        print(f"Warning: could not sync securities metadata: {e}")
        return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fetch NEPSE floorsheet data")
    parser.add_argument("--days", type=int, default=90, help="Trading sessions to backfill")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Single date to fetch (YYYY-MM-DD); overrides --days",
    )
    parser.add_argument("--today", action="store_true", help="Fetch latest trading day only")
    args = parser.parse_args()

    if args.date:
        rows = fetch_floorsheet_date(args.date)
        path = write_floorsheet_csv(args.date, rows)
        print(f"{args.date}: {len(rows):,} rows -> {path}")
    elif args.today:
        path = fetch_today()
        print(f"today -> {path}")
    else:
        paths = fetch_historical_floorsheets(days=args.days)
        print(f"Wrote {len(paths)} floorsheets to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())