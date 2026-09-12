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

