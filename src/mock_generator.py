"""Synthetic NEPSE floorsheet generator: 66 sessions, 30 symbols, ~1M rows.

Seeds:
  LEC   -> Track A SILENT_ACCUMULATION (Broker 58 dominating, tight margin)
  HIDCL -> Track B stealth setup (absorption + fragmented selling + volume spike)
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta

import polars as pl

from src.ingestion import ingest_floorsheet

SYMBOLS = [
    "LEC", "HIDCL", "NABIL", "NICA", "GBIME", "EBL", "NMB", "SBL",
    "SANIMA", "PRVU", "NLIC", "LICN", "NIFRA", "UPPER", "CHCL", "SHIVM",
    "DCL", "API", "AKPL", "BPCL", "AHPC", "HURJA", "RHPL", "UNL",
    "BNT", "NLO", "SHL", "OHL", "TRH", "CGH",
]

BROKERS = list(range(1, 51))
LEC_BROKER = 58
HIDCL_BROKER = 41
N_DAYS = 66
TARGET_TRADES_PER_DAY = 18_000
RNG_SEED = 20260911

BASE_PRICES = {
    "LEC": 420.0, "HIDCL": 185.0, "NABIL": 520.0, "NICA": 410.0,
    "GBIME": 205.0, "EBL": 560.0, "NMB": 240.0, "SBL": 315.0,
    "SANIMA": 330.0, "PRVU": 250.0, "NLIC": 780.0, "LICN": 690.0,
    "NIFRA": 145.0, "UPPER": 430.0, "CHCL": 510.0, "SHIVM": 620.0,
    "DCL": 355.0, "API": 265.0, "AKPL": 175.0, "BPCL": 340.0,
    "AHPC": 290.0, "HURJA": 210.0, "RHPL": 395.0, "UNL": 18500.0,
    "BNT": 11200.0, "NLO": 980.0, "SHL": 620.0, "OHL": 740.0,
    "TRH": 880.0, "CGH": 1250.0,
}


def trading_calendar(n_days: int = N_DAYS, end: date | None = None) -> list[date]:
    end = end or date(2026, 9, 11)
    days: list[date] = []
    cursor = end
    while len(days) < n_days:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return days


def _lot(rng: random.Random) -> int:
    return rng.choice([10, 20, 50, 100, 200, 500, 1000]) * rng.choice([1, 1, 1, 2, 3])


def _rate(base: float, drift: float, rng: random.Random, noise: float = 0.006) -> float:
    r = base * (1.0 + drift) * (1.0 + rng.uniform(-noise, noise))
    return round(max(r, 1.0), 2)


def _pick_broker(rng: random.Random, exclude: int | None = None) -> int:
    b = rng.choice(BROKERS)
    if exclude is not None and b == exclude:
        b = BROKERS[(BROKERS.index(b) + 7) % len(BROKERS)]
    return b


def generate_session(
    session_idx: int,
    trade_date: date,
    rng: random.Random,
    contract_start: int,
) -> list[dict]:
    rows: list[dict] = []
    cid = contract_start
    n_days = N_DAYS
    t_from_end = n_days - 1 - session_idx

    weights = []
    for sym in SYMBOLS:
        if sym == "LEC":
            w = 1.35
        elif sym == "HIDCL":
            w = 0.55 if t_from_end > 0 else 1.8
        else:
            w = rng.uniform(0.7, 1.3)
        weights.append(w)
    total_w = sum(weights)
    counts = [max(350, int(TARGET_TRADES_PER_DAY * w / total_w)) for w in weights]

    for sym, n_trades in zip(SYMBOLS, counts):
        base = BASE_PRICES[sym]
        if sym == "LEC":
            drift = 0.015 * (session_idx / (n_days - 1))
            drift += 0.004 if t_from_end == 0 else 0.0
        elif sym == "HIDCL":
            drift = 0.018 * ((session_idx % 11) - 5) / 5.0
        else:
            drift = rng.uniform(-0.12, 0.18) * (session_idx / n_days) + rng.uniform(
                -0.01, 0.01
            )

        for _ in range(n_trades):
            qty = _lot(rng)
            rate = _rate(base, drift, rng)
            amount = round(qty * rate, 2)
            buyer, seller = _assign_brokers(sym, t_from_end, rng)
            rows.append(
                {
                    "trade_date": trade_date,
                    "contract_id": cid,
                    "symbol": sym,
                    "buyer_broker": buyer,
                    "seller_broker": seller,
                    "quantity": qty,
                    "rate": rate,
                    "amount": amount,
                }
            )
            cid += 1
    return rows


def _assign_brokers(sym: str, t_from_end: int, rng: random.Random) -> tuple[int, int]:
    if sym == "LEC":
        roll = rng.random()
        if roll < 0.38:
            return LEC_BROKER, _pick_broker(rng, exclude=LEC_BROKER)
        if roll < 0.46:
            return _pick_broker(rng, exclude=LEC_BROKER), LEC_BROKER
        if roll < 0.50:
            b = _pick_broker(rng)
            return b, b
        buyer = _pick_broker(rng, exclude=LEC_BROKER)
        seller = _pick_broker(rng, exclude=buyer)
        if rng.random() < 0.04:
            return buyer, buyer
        return buyer, seller

    if sym == "HIDCL":
        roll = rng.random()
        if roll < 0.28:
            return HIDCL_BROKER, _pick_broker(rng, exclude=HIDCL_BROKER)
        if roll < 0.33:
            return _pick_broker(rng, exclude=HIDCL_BROKER), HIDCL_BROKER
        buyer = _pick_broker(rng, exclude=HIDCL_BROKER)
        seller = _pick_broker(rng, exclude=buyer)
        if rng.random() < 0.03:
            return buyer, buyer
        return buyer, seller

    buyer = _pick_broker(rng)
    seller = _pick_broker(rng)
    if rng.random() < 0.03:
        seller = buyer
    if sym == "NABIL":
        trap_broker = 12
        if t_from_end >= 1:
            if rng.random() < 0.30:
                return trap_broker, _pick_broker(rng, exclude=trap_broker)
        elif rng.random() < 0.35:
            return _pick_broker(rng, exclude=trap_broker), trap_broker
    return buyer, seller



SECTOR_MAP = {
    "LEC": "Hydro Power",
    "HIDCL": "Investment",
    "NABIL": "Commercial Banks",
    "NICA": "Commercial Banks",
    "GBIME": "Commercial Banks",
    "EBL": "Commercial Banks",
    "NMB": "Commercial Banks",
    "SBL": "Commercial Banks",
    "SANIMA": "Commercial Banks",
    "PRVU": "Commercial Banks",
    "NLIC": "Life Insurance",
    "LICN": "Life Insurance",
    "NIFRA": "Investment",
    "UPPER": "Hydro Power",
    "CHCL": "Hydro Power",
    "SHIVM": "Manufacturing And Processing",
    "DCL": "Finance",
    "API": "Hydro Power",
    "AKPL": "Hydro Power",
    "BPCL": "Hydro Power",
    "AHPC": "Hydro Power",
    "HURJA": "Hydro Power",
    "RHPL": "Hydro Power",
    "UNL": "Manufacturing And Processing",
    "BNT": "Manufacturing And Processing",
    "NLO": "Hotels And Tourism",
    "SHL": "Hotels And Tourism",
    "OHL": "Hotels And Tourism",
    "TRH": "Hotels And Tourism",
    "CGH": "Hotels And Tourism",
}

MARKET_CAP_MAP = {
    "LEC": 4500.0,
    "HIDCL": 42000.0,
    "NABIL": 145000.0,
    "NICA": 62000.0,
    "GBIME": 75000.0,
    "EBL": 68000.0,
    "NMB": 45000.0,
    "SBL": 36000.0,
    "SANIMA": 41000.0,
    "PRVU": 38000.0,
    "NLIC": 39000.0,
    "LICN": 18500.0,
    "NIFRA": 32000.0,
    "UPPER": 36500.0,
    "CHCL": 15200.0,
    "SHIVM": 28000.0,
    "DCL": 3200.0,
    "API": 14800.0,
    "AKPL": 7200.0,
    "BPCL": 11500.0,
    "AHPC": 6400.0,
    "HURJA": 2800.0,
    "RHPL": 4100.0,
    "UNL": 17200.0,
    "BNT": 16800.0,
    "NLO": 3800.0,
    "SHL": 12400.0,
    "OHL": 14100.0,
    "TRH": 19500.0,
    "CGH": 8600.0,
}

COMPANY_NAMES = {
    "LEC": "Liberty Energy Company Limited",
    "HIDCL": "Hydroelectricity Investment and Development Company Ltd",
    "NABIL": "Nabil Bank Limited",
    "NICA": "NIC Asia Bank Limited",
    "GBIME": "Global IME Bank Limited",
    "EBL": "Everest Bank Limited",
    "NMB": "NMB Bank Limited",
    "SBL": "Siddhartha Bank Limited",
    "SANIMA": "Sanima Bank Limited",
    "PRVU": "Prabhu Bank Limited",
    "NLIC": "Nepal Life Insurance Co. Ltd.",
    "LICN": "Life Insurance Co. Nepal",
    "NIFRA": "Nepal Infrastructure Bank Limited",
    "UPPER": "Upper Tamakoshi Hydropower Ltd",
    "CHCL": "Chilime Hydro power Company Limited",
    "SHIVM": "Shivam Cements Ltd",
    "DCL": "Deprosc Laghubitta Bittiya Sanstha Limited",
    "API": "Api Power Company Ltd.",
    "AKPL": "Arun Valley Hydropower Development Co. Ltd.",
    "BPCL": "Butwal Power Company Limited",
    "AHPC": "Arun Kabeli Power Ltd.",
    "HURJA": "Himalaya Urja Bikas Company Limited",
    "RHPL": "RASUWA GADHI HYDROPOWER COMPANY LIMITED",
    "UNL": "Unilever Nepal Limited",
    "BNT": "Bottlers Nepal (Terai) Limited",
    "NLO": "Nepal Lube Oil Limited",
    "SHL": "Soaltee Hotel Limited",
    "OHL": "Oriental Hotels Limited",
    "TRH": "Taragaon Regency Hotel",
    "CGH": "Chandragiri Hills Limited",
}


def seed_securities_meta(conn) -> None:
    """Populate static securities metadata table."""
    import psycopg2.extras

    rows = []
    for sym in SYMBOLS:
        base = BASE_PRICES.get(sym, 100.0)
        rows.append(
            (
                sym,
                COMPANY_NAMES.get(sym, f"{sym} Limited"),
                SECTOR_MAP.get(sym, "Others"),
                "Equity",
                MARKET_CAP_MAP.get(sym, 10000.0),
                round(base * 1.35, 2),
                round(base * 0.72, 2),
            )
        )
    sql = """
        INSERT INTO securities_meta
            (symbol, company_name, sector, instrument_type, market_cap, fifty_two_week_high, fifty_two_week_low)
        VALUES %s
        ON CONFLICT (symbol) DO UPDATE SET
            company_name = EXCLUDED.company_name,
            sector = EXCLUDED.sector,
            instrument_type = EXCLUDED.instrument_type,
            market_cap = EXCLUDED.market_cap,
            fifty_two_week_high = EXCLUDED.fifty_two_week_high,
            fifty_two_week_low = EXCLUDED.fifty_two_week_low,
            updated_at = CURRENT_TIMESTAMP
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows)


def generate_floorsheet(n_days: int = N_DAYS, seed: int = RNG_SEED) -> pl.DataFrame:
    rng = random.Random(seed)
    days = trading_calendar(n_days)
    all_rows: list[dict] = []
    cid = 10_000_000
    for i, d in enumerate(days):
        session = generate_session(i, d, rng, cid)
        all_rows.extend(session)
        cid += 1_000_000
    return pl.DataFrame(all_rows)


def seed_database(n_days: int = N_DAYS, seed: int = RNG_SEED) -> dict[str, int]:
    from src.db import get_conn
    conn = get_conn()
    try:
        seed_securities_meta(conn)
        conn.commit()
    finally:
        conn.close()

    df = generate_floorsheet(n_days=n_days, seed=seed)
    stats = ingest_floorsheet(df, replace_dates=True)
    stats["days"] = n_days
    stats["symbols"] = len(SYMBOLS)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic NEPSE floorsheet data")
    parser.add_argument("--days", type=int, default=N_DAYS)
    parser.add_argument("--seed", type=int, default=RNG_SEED)
    args = parser.parse_args()
    stats = seed_database(n_days=args.days, seed=args.seed)
    print(
        f"Seeded {stats['floorsheet']:,} floorsheet rows | "
        f"rollup={stats['rollup']:,} | summary={stats['summary']:,}"
    )


if __name__ == "__main__":
    main()

