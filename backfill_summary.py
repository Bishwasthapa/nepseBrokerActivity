"""Backfill daily_market_summary for 2026-09-09 / 2026-09-10 from the NEPSE
per-security price-history endpoint, robust to Salter token 401 expiry
(fresh session every BATCH symbols + retry-on-401). Deletes the spurious
30-symbol rows stored under the holiday dates 2026-09-04 / 2026-09-08.
"""
import os
import time
import socket
import collections

import warnings
warnings.filterwarnings("ignore")
socket.setdefaulttimeout(30)

import psycopg2
from nepse_scraper import NepseScraper

TARGET_DATES = {"2026-09-09", "2026-09-10"}
RANGE_START, RANGE_END = "2026-09-08", "2026-09-12"
BAD_DATES = ("2026-09-04", "2026-09-08")
BATCH = 200          # fresh Salter token every BATCH symbols
SLEEP = 0.15

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://quant:quantpass@db:5432/nepse_analytics"
)

def fetch_all():
    scraper = NepseScraper(verify_ssl=False)
    sec = scraper.get_all_securities()
    sec = sec.get("data") if isinstance(sec, dict) else sec
    id_by_symbol = {it.get("symbol"): it.get("id") for it in sec if isinstance(it, dict) and it.get("symbol")}
    syms = sorted(id_by_symbol)
    print("securities:", len(syms), flush=True)

    path_tpl = scraper.endpoints["ticker_price_api"]["api"]
    values = {}
    for i, sym in enumerate(syms):
        if i and i % BATCH == 0:
            scraper = NepseScraper(verify_ssl=False)   # fresh token
        sid = id_by_symbol[sym]
        try:
            resp = scraper.session.get(
                f"{path_tpl}/{sid}",
                params={"startDate": RANGE_START, "endDate": RANGE_END, "page": 0, "size": 500},
            )
            resp.raise_for_status()
            r = resp.json()
        except Exception:
            # retry once with a fresh session (token likely expired)
            try:
                scraper = NepseScraper(verify_ssl=False)
                resp = scraper.session.get(
                    f"{path_tpl}/{sid}",
                    params={"startDate": RANGE_START, "endDate": RANGE_END, "page": 0, "size": 500},
                )
                resp.raise_for_status()
                r = resp.json()
            except Exception as e2:
                print("ERR", sym, type(e2).__name__, str(e2)[:80], flush=True)
                time.sleep(0.3)
                continue
        time.sleep(0.2 + (i % 5) * 0.02)
        for it in (r.get("content") if isinstance(r, dict) else r) or []:
            d = it.get("businessDate")
            if d in TARGET_DATES and it.get("closePrice") is not None:
                prev = it.get("previousDayClosePrice")
                values[(d, sym)] = (
                    sym,
                    float(it["closePrice"]),
                    0.0 if prev in (None, 0) else float(prev),
                    int(it.get("totalTradedQuantity") or 0),
                    float(it.get("totalTradedValue") or 0.0),
                )
        if i % 25 == 0:
            print("progress", i, len(syms), "rows", len(values), flush=True)
    return values

values = fetch_all()

# Global turnover_rank per date over the full collected set
by_date = collections.defaultdict(list)
for (d, sym), (sym2, close, prev, qty, tov) in values.items():
    pct = (close - prev) / prev * 100.0 if prev else 0.0
    by_date[d].append((sym, close, pct, qty, tov))

final = []
for d, lst in by_date.items():
    lst.sort(key=lambda x: x[4], reverse=True)
    for rank, (sym, close, pct, qty, tov) in enumerate(lst, 1):
        final.append((d, sym, close, pct, qty, tov, rank))
print("summary rows:", len(final), "dates:", sorted(by_date.keys()),
      "per_date:", {d: len(v) for d, v in by_date.items()}, flush=True)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = False
upsert_sql = """
INSERT INTO daily_market_summary
    (trade_date, symbol, close_price, price_change_pct, total_qty, total_turnover, turnover_rank)
VALUES (%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (trade_date, symbol) DO UPDATE SET
    close_price = EXCLUDED.close_price,
    price_change_pct = EXCLUDED.price_change_pct,
    total_qty = EXCLUDED.total_qty,
    total_turnover = EXCLUDED.total_turnover,
    turnover_rank = EXCLUDED.turnover_rank
"""
with conn.cursor() as cur:
    if final:
        cur.executemany(upsert_sql, final)
    cur.execute("DELETE FROM daily_market_summary WHERE trade_date IN %s", (BAD_DATES,))
    cur.execute("DELETE FROM floorsheet WHERE trade_date IN %s", (BAD_DATES,))
    try:
        cur.execute("DELETE FROM daily_broker_rollup WHERE trade_date IN %s", (BAD_DATES,))
    except Exception as e:
        print("rollup delete skipped:", type(e).__name__, str(e)[:80], flush=True)
conn.commit()
print("committed.", flush=True)
conn.close()