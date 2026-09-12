"""CLI entry point: seed, ingest, and screen NEPSE broker activity."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from time import perf_counter

import polars as pl

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from src.db import get_conn
from src.screener import (
    compute_broker_match_pct,
    compute_session_match_pct,
    fetch_trade_dates,
    load_rollup,
    load_summary,
    run_screener,
    screen_turnover_momentum,
)

console = Console()

SIGNAL_STYLE = {
    "SILENT_ACCUMULATION": "bold cyan",
    "ACTIVE_MARKUP": "bold green",
    "DISTRIBUTION_TRAP": "bold red",
    "STEALTH_ACCUMULATION": "bold magenta",
    "WATCH": "dim",
}


def _fmt_num(v, digits: int = 0) -> str:
    if v is None:
        return "-"
    if digits == 0:
        return f"{int(v):,}"
    return f"{float(v):,.{digits}f}"


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    return f"{float(v):+.2f}%"


def render_track_a(rows: list[dict], top_turnover: int = 20) -> Table:
    table = Table(
        title=f"Track A — Top {top_turnover} Turnover Momentum & Traps",
        title_style="bold white",
        header_style="bold yellow",
        expand=True,
    )
    for col, justify in [
        ("Rank", "right"),
        ("Symbol", "left"),
        ("Broker", "right"),
        ("Net 1D", "right"),
        ("Net 5D", "right"),
        ("Net 22D", "right"),
        ("Net 66D", "right"),
        ("Margin %", "right"),
        ("T1 Δ%", "right"),
        ("Signal", "left"),
    ]:
        table.add_column(col, justify=justify)

    ordered = sorted(
        rows,
        key=lambda r: (
            0 if r["signal"] != "WATCH" else 1,
            r["turnover_rank"],
            r["buyer_rank"] if r["buyer_rank"] else 99,
        ),
    )
    seen_watch: set[str] = set()
    for r in ordered:
        if r["signal"] == "WATCH":
            if r["buyer_rank"] != 1:
                continue
            if r["symbol"] in seen_watch:
                continue
            seen_watch.add(r["symbol"])
        style = SIGNAL_STYLE.get(r["signal"], "")
        table.add_row(
            str(r["turnover_rank"]),
            r["symbol"],
            str(r["broker_id"]),
            _fmt_num(r["net_t1"]),
            _fmt_num(r["net_t5"]),
            _fmt_num(r["net_t22"]),
            _fmt_num(r["net_t66"]),
            _fmt_pct(r["margin_pct"]),
            _fmt_pct(r["t1_change_pct"]),
            Text(r["signal"], style=style),
        )
    return table


def render_track_b(rows: list[dict]) -> Table:
    table = Table(
        title="Track B — Stealth Radar (Unranked Market-Wide Scan)",
        title_style="bold white",
        header_style="bold yellow",
        expand=True,
    )
    for col, justify in [
        ("Symbol", "left"),
        ("Broker", "right"),
        ("Net 22D", "right"),
        ("Absorption %", "right"),
        ("Dispersion %", "right"),
        ("T22 Δ%", "right"),
        ("Vol Inflection", "right"),
        ("Margin %", "right"),
        ("Signal", "left"),
    ]:
        table.add_column(col, justify=justify)

    if not rows:
        table.add_row("-", "-", "-", "-", "-", "-", "-", "-", Text("none", style="dim"))
        return table

    for r in rows:
        table.add_row(
            r["symbol"],
            str(r["broker_id"]),
            _fmt_num(r["net_t22"]),
            _fmt_pct(r["absorption_pct"]),
            f"{r['dispersion_pct']:.2f}%",
            _fmt_pct(r["t22_price_change_pct"]),
            f"{r['volume_inflection']:.2f}x" if r["volume_inflection"] else "-",
            _fmt_pct(r["margin_pct"]),
            Text(r["signal"], style=SIGNAL_STYLE.get(r["signal"], "bold magenta")),
        )
    return table


def cmd_run(args: argparse.Namespace) -> int:
    as_of = None
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            console.print(f"[red]Invalid --as-of date: {args.as_of!r} (expected YYYY-MM-DD)[/red]")
            return 2
    t0 = perf_counter()
    track_a, track_b, meta = run_screener(
        as_of=as_of, persist=not args.no_persist, top_turnover=args.top
    )
    elapsed = perf_counter() - t0
    mode = f"  as_of={meta['as_of']}" if meta.get("as_of") else ""
    persist_info = f"  persisted={meta['persisted']} signals" if meta.get("persisted") else "  (not persisted)"
    top_info = f"  top={meta['top_turnover']}"
    header = (
        f"NEPSE Institutional Accumulation Screener\n"
        f"Sessions={meta['sessions']}  T1={meta['t1']}{mode}{top_info}{persist_info}  "
        f"elapsed={elapsed:.2f}s"
    )
    console.print(Panel(header, style="bold blue"))
    console.print(render_track_a(track_a, args.top))
    console.print()
    console.print(render_track_b(track_b))
    a_hits = [r for r in track_a if r["signal"] != "WATCH"]
    console.print(
        f"\n[dim]Track A signals: {len(a_hits)} | Track B hits: {len(track_b)}[/dim]"
    )
    return 0


def render_momentum(rows: list[dict], title: str) -> Table:
    table = Table(title=title, title_style="bold white", header_style="bold yellow", expand=True)
    for col, justify in [
        ("Symbol", "left"),
        ("Avg Rank (Base)", "right"),
        ("Avg Rank (Short)", "right"),
        ("Rank Drift", "right"),
        ("Turnover Ratio", "right"),
        ("Close", "right"),
        ("Window Δ%", "right"),
        ("Top Accumulator", "right"),
        ("Top Distributor", "right"),
    ]:
        table.add_column(col, justify=justify)
    for r in rows:
        table.add_row(
            r["symbol"],
            _fmt_num(r["avg_rank_base"], 1),
            _fmt_num(r["avg_rank_short"], 1),
            f"{r['rank_drift']:+.1f}",
            _fmt_num(r["turnover_ratio"], 2),
            _fmt_num(r["close"], 2),
            _fmt_pct(r["price_change_pct_window"]),
            str(r["top_accumulator"]) if r["top_accumulator"] is not None else "-",
            str(r["top_distributor"]) if r["top_distributor"] is not None else "-",
        )
    return table


def cmd_momentum(args: argparse.Namespace) -> int:
    if args.short <= 0 or args.base <= 0:
        console.print("[red]--short and --base must be positive integers[/red]")
        return 2
    if args.base < args.short:
        console.print("[red]--base must be >= --short[/red]")
        return 2
    as_of = None
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            console.print(f"[red]Invalid --as-of date: {args.as_of!r} (expected YYYY-MM-DD)[/red]")
            return 2
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        if as_of is not None:
            dates = [d for d in dates if d <= as_of]
        needed = dates[-max(args.base, args.short):]
        summary = load_summary(conn, needed)
        rollup = load_rollup(conn, needed)
        if not summary.is_empty():
            summary = summary.with_columns(
                pl.col("close_price").cast(pl.Float64),
                pl.col("total_turnover").cast(pl.Float64),
                pl.col("turnover_rank").cast(pl.Int32),
            )
        if not rollup.is_empty():
            rollup = rollup.with_columns(
                pl.col("broker_id").cast(pl.Int32),
                pl.col("buy_qty").cast(pl.Int64),
                pl.col("sell_qty").cast(pl.Int64),
                pl.col("self_trade_qty").cast(pl.Int64),
                pl.col("buy_amount").cast(pl.Float64),
                pl.col("sell_amount").cast(pl.Float64),
            )
        gainers, losers = screen_turnover_momentum(
            summary,
            short_window=args.short,
            base_window=args.base,
            rollup=rollup,
        )
    finally:
        conn.close()
    console.print(render_momentum(
        gainers,
        f"Turnover Momentum Gainers (Last {args.short} Sessions vs {args.base}-Session Baseline)",
    ))
    console.print()
    console.print(render_momentum(losers, "Turnover Momentum Losers (Liquidity Drying / Capital Exit)"))
    return 0


def render_wash(
    df: pl.DataFrame,
    title: str,
    broker_level: bool,
) -> Table:
    table = Table(title=title, title_style="bold white", header_style="bold cyan", expand=True)
    if broker_level:
        for col, justify in [
            ("Broker", "right"),
            ("Buy Qty", "right"),
            ("Sell Qty", "right"),
            ("Matched Qty", "right"),
            ("Gross Vol", "right"),
            ("Match %", "right"),
        ]:
            table.add_column(col, justify=justify)
        for r in df.sort("match_pct", descending=True).iter_rows(named=True):
            table.add_row(
                str(r["broker_id"]),
                _fmt_num(r["buy_qty"]),
                _fmt_num(r["sell_qty"]),
                _fmt_num(r["matched_qty"]),
                _fmt_num(r["gross_volume"]),
                _fmt_pct(r["match_pct"]),
            )
    else:
        for col, justify in [
            ("Symbol", "left"),
            ("Total Qty", "right"),
            ("Crossed Qty", "right"),
            ("Session Match %", "right"),
        ]:
            table.add_column(col, justify=justify)
        for r in df.sort("session_match_pct", descending=True).iter_rows(named=True):
            table.add_row(
                r["symbol"],
                _fmt_num(r["total_qty"]),
                _fmt_num(r["crossed_qty"]),
                _fmt_pct(r["session_match_pct"]),
            )
    return table


def cmd_wash(args: argparse.Namespace) -> int:
    """Detect broker internal matching & session-level cross/wash trades."""
    if args.window <= 0:
        console.print("[red]--window must be a positive integer[/red]")
        return 2
    as_of = None
    if args.as_of:
        try:
            as_of = date.fromisoformat(args.as_of)
        except ValueError:
            console.print(f"[red]Invalid --as-of date: {args.as_of!r} (expected YYYY-MM-DD)[/red]")
            return 2
    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        if as_of is not None:
            dates = [d for d in dates if d <= as_of]
        if not dates:
            console.print("[yellow]No trade dates available.[/yellow]")
            return 0
        window_dates_src = dates[-args.window:]
        summary = load_summary(conn, window_dates_src)
        rollup = load_rollup(conn, window_dates_src)
        if not rollup.is_empty():
            rollup = rollup.with_columns(
                pl.col("broker_id").cast(pl.Int32),
                pl.col("buy_qty").cast(pl.Int64),
                pl.col("sell_qty").cast(pl.Int64),
                pl.col("self_trade_qty").cast(pl.Int64),
                pl.col("matched_qty").cast(pl.Int64),
                pl.col("buy_amount").cast(pl.Float64),
                pl.col("sell_amount").cast(pl.Float64),
            )
        broker_match = compute_broker_match_pct(rollup, window_dates_src)
        session_match = compute_session_match_pct(rollup, summary, window_dates_src)
    finally:
        conn.close()

    latest_session = window_dates_src[-1]
    console.print(
        render_wash(
            broker_match,
            f"Broker Internal Matching (last {args.window} sessions ending {latest_session}, top by Match %)",
            broker_level=True,
        )
    )
    console.print()
    console.print(
        render_wash(
            session_match,
            f"Session Cross / Wash Trade Detection — {latest_session} (top by Session Match %)",
            broker_level=False,
        )
    )
    return 0


def render_signals(rows: list[dict], title: str) -> Table:
    table = Table(title=title, title_style="bold white", header_style="bold yellow", expand=True)
    for col, justify in [
        ("Trade Date", "left"),
        ("Symbol", "left"),
        ("Broker", "right"),
        ("Track", "left"),
        ("Signal", "left"),
        ("Rank", "right"),
        ("Net 1D", "right"),
        ("Net 5D", "right"),
        ("Net 22D", "right"),
        ("Net 66D", "right"),
        ("Margin %", "right"),
        ("Δ%", "right"),
    ]:
        table.add_column(col, justify=justify)
    for r in rows:
        table.add_row(
            str(r["trade_date"]),
            r["symbol"],
            str(r["broker_id"]),
            r["track"],
            Text(r["signal"], style=SIGNAL_STYLE.get(r["signal"], "")),
            str(r["turnover_rank"]),
            _fmt_num(r["net_1d"]),
            _fmt_num(r["net_5d"]),
            _fmt_num(r["net_22d"]),
            _fmt_num(r["net_66d"]),
            _fmt_pct(r["margin_pct"]),
            _fmt_pct(r["t1_change_pct"]),
        )
    return table


def render_streaks(rows: list[dict], min_streak: int) -> Table:
    table = Table(
        title=f"Current Signal Streaks (≥{min_streak} consecutive sessions)",
        title_style="bold white",
        header_style="bold yellow",
        expand=True,
    )
    for col, justify in [
        ("Streak", "right"),
        ("Symbol", "left"),
        ("Broker", "right"),
        ("Track", "left"),
        ("Signal", "left"),
        ("Last Date", "left"),
    ]:
        table.add_column(col, justify=justify)
    for r in rows:
        table.add_row(
            str(r["streak"]),
            r["symbol"],
            str(r["broker_id"]),
            r["track"],
            Text(r["signal"], style=SIGNAL_STYLE.get(r["signal"], "")),
            str(r["last_date"]),
        )
    return table


def cmd_signals(args: argparse.Namespace) -> int:
    from src.db import get_conn
    from src.signals import current_streaks, load_signal_history

    conn = get_conn()
    try:
        if args.streak:
            rows = current_streaks(conn, min_streak=max(1, args.streak))
            if not rows:
                console.print(f"[dim]No signals with streak ≥ {args.streak} yet[/dim]")
                return 0
            console.print(render_streaks(rows, args.streak))
            return 0
        rows = load_signal_history(
            conn,
            symbol=args.symbol,
            broker_id=args.broker,
            signal=args.signal,
            track=args.track,
            limit=args.limit,
        )
        if not rows:
            console.print("[dim]No signals match the given filters[/dim]")
            return 0
        console.print(render_signals(rows, "Signal History — Persistent Screening Outputs"))
        return 0
    finally:
        conn.close()


def render_inspect(data: dict) -> None:
    from rich.panel import Panel

    console.print(Panel(f"Symbol Inspector — {data['symbol']}", style="bold blue"))
    if not data["recent"]:
        console.print("[yellow]No market data for this symbol[/yellow]")
        return

    recent = Table(
        title=f"Recent Sessions (last {len(data['recent'])})",
        title_style="bold white",
        header_style="bold yellow",
    )
    for col, justify in [
        ("Date", "left"),
        ("Close", "right"),
        ("Δ%", "right"),
        ("Qty", "right"),
        ("Turnover", "right"),
        ("Rank", "right"),
    ]:
        recent.add_column(col, justify=justify)
    for r in data["recent"]:
        recent.add_row(
            str(r["trade_date"]),
            f"{r['close_price']:,.2f}",
            _fmt_pct(r["change_pct"]),
            f"{r['qty']:,}",
            f"{r['turnover']:,.0f}",
            str(r["rank"]),
        )
    console.print(recent)

    brokers = Table(
        title="Broker Net Flows (shares, across windows)",
        title_style="bold white",
        header_style="bold yellow",
    )
    for col, justify in [
        ("Broker", "right"),
        ("Net 1D", "right"),
        ("Net 5D", "right"),
        ("Net 22D", "right"),
        ("Net 66D", "right"),
        ("Margin %", "right"),
    ]:
        brokers.add_column(col, justify=justify)
    for b in data["brokers"]:
        brokers.add_row(
            str(b["broker_id"]),
            _fmt_num(b["net_1d"]),
            _fmt_num(b["net_5d"]),
            _fmt_num(b["net_22d"]),
            _fmt_num(b["net_66d"]),
            _fmt_pct(b["margin_pct"]),
        )
    console.print(brokers)

    if data["signals"]:
        console.print(render_signals(data["signals"], "Signal History"))
    else:
        console.print("[dim]No persisted signal history for this symbol[/dim]")


def cmd_inspect(args: argparse.Namespace) -> int:
    from src.screener import inspect_symbol

    data = inspect_symbol(args.symbol, sessions=args.sessions)
    render_inspect(data)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from src.mock_generator import seed_database

    t0 = perf_counter()
    stats = seed_database(n_days=args.days, seed=args.seed)
    elapsed = perf_counter() - t0
    console.print(
        Panel(
            f"Seeded {stats['floorsheet']:,} floorsheet rows across "
            f"{stats['days']} sessions / {stats['symbols']} symbols\n"
            f"rollup={stats['rollup']:,}  summary={stats['summary']:,}  "
            f"elapsed={elapsed:.2f}s",
            style="bold green",
        )
    )
    return 0



def cmd_ingest(args: argparse.Namespace) -> int:
    from src.ingestion import ingest_csv

    t0 = perf_counter()
    stats = ingest_csv(args.file)
    elapsed = perf_counter() - t0
    console.print(
        f"[green]Ingested[/green] floorsheet={stats['floorsheet']:,} "
        f"rollup={stats['rollup']:,} summary={stats['summary']:,} "
        f"in {elapsed:.2f}s"
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from src.fetcher import fetch_historical_floorsheets, fetch_today
    from src.ingestion import ingest_csv
    from src.db import get_conn

    if args.today:
        path = fetch_today()
        if not path:
            console.print("[yellow]No floorsheet available for today[/yellow]")
            return 0
        # Idempotent: if this date is already fully ingested, skip re-fetch/ingest.
        try:
            conn = get_conn()
            try:
                latest = _latest_ingested_date(conn)
            finally:
                conn.close()
            if latest is not None and path.stem == latest.isoformat():
                console.print(
                    f"[dim]Already current at {latest.isoformat()}, skipping re-ingest[/dim]"
                )
                return 0
        except Exception:
            pass  # DB unavailable; fall through to ingest anyway
        t0 = perf_counter()
        stats = ingest_csv(str(path))
        elapsed = perf_counter() - t0
        console.print(
            f"[green]Fetched & ingested[/green] {path.name} "
            f"floorsheet={stats['floorsheet']:,} "
            f"rollup={stats['rollup']:,} summary={stats['summary']:,} "
            f"in {elapsed:.2f}s"
        )
        return 0

    paths = fetch_historical_floorsheets(days=args.days)
    console.print(f"[green]Fetched {len(paths)} floorsheets[/green]\n")

    t0 = perf_counter()
    total_f = total_r = total_s = 0
    for p in paths:
        stats = ingest_csv(str(p))
        total_f += stats["floorsheet"]
        total_r += stats["rollup"]
        total_s += stats["summary"]
    elapsed = perf_counter() - t0
    console.print(
        f"[green]Ingested all[/green] floorsheet={total_f:,} "
        f"rollup={total_r:,} summary={total_s:,} in {elapsed:.2f}s"
    )
    return 0


def _latest_ingested_date(conn):
    """Return the most recent trade_date present in market summary."""
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(trade_date) FROM daily_market_summary")
        row = cur.fetchone()
        return row[0] if row else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nepse-screener",
        description="NEPSE institutional accumulation screening engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run Track A / Track B screener")
    p_run.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Point-in-time backtest as of YYYY-MM-DD (uses only sessions <= date)",
    )
    p_run.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write signals to screener_signals_history",
    )
    p_run.add_argument(
        "--top",
        type=int,
        default=20,
        help="Number of top turnover stocks to scan in Track A (default: 20)",
    )
    p_run.set_defaults(func=cmd_run)

    p_seed = sub.add_parser("seed", help="Generate and load synthetic floorsheet data")
    p_seed.add_argument("--days", type=int, default=66)
    p_seed.add_argument("--seed", type=int, default=20260911)
    p_seed.set_defaults(func=cmd_seed)

    p_ing = sub.add_parser("ingest", help="Ingest a floorsheet CSV")
    p_ing.add_argument("--file", required=True)
    p_ing.set_defaults(func=cmd_ingest)

    p_fetch = sub.add_parser("fetch", help="Fetch real NEPSE floorsheets and ingest")
    p_fetch.add_argument("--days", type=int, default=90, help="Trading sessions to backfill")
    p_fetch.add_argument("--today", action="store_true", help="Fetch only latest trading day")
    p_fetch.set_defaults(func=cmd_fetch)

    p_signals = sub.add_parser(
        "signals", help="Query persisted signal history / streak detection"
    )
    p_signals.add_argument("--symbol", type=str, default=None, help="Filter by symbol")
    p_signals.add_argument("--broker", type=int, default=None, help="Filter by broker ID")
    p_signals.add_argument("--signal", type=str, default=None, help="Filter by signal name")
    p_signals.add_argument(
        "--track", type=str, default=None, choices=["TRACK_A", "TRACK_B", "track_a", "track_b"]
    )
    p_signals.add_argument(
        "--streak",
        type=int,
        default=0,
        help="Show current streaks of at least N consecutive sessions instead of rows",
    )
    p_signals.add_argument("--limit", type=int, default=100)
    p_signals.set_defaults(func=cmd_signals)

    p_inspect = sub.add_parser("inspect", help="Deep-dive a single symbol")
    p_inspect.add_argument("symbol", type=str, help="NEPSE ticker, e.g. LEC")
    p_inspect.add_argument("--sessions", type=int, default=22, help="Recent sessions to show")
    p_inspect.set_defaults(func=cmd_inspect)

    p_mom = sub.add_parser(
        "momentum", help="Scan multi-window turnover momentum gainers / losers"
    )
    p_mom.add_argument("--short", type=int, default=5, help="Short window sessions (default: 5)")
    p_mom.add_argument("--base", type=int, default=22, help="Baseline window sessions (default: 22)")
    p_mom.add_argument(
        "--as-of", type=str, default=None, help="Point-in-time calculation as of YYYY-MM-DD"
    )
    p_mom.set_defaults(func=cmd_momentum)

    p_wash = sub.add_parser(
        "wash",
        help="Detect broker internal matching & session cross/wash trades",
    )
    p_wash.add_argument(
        "--window", type=int, default=22, help="Broker matching window sessions (default: 22)"
    )
    p_wash.add_argument(
        "--as-of", type=str, default=None, help="Point-in-time calculation as of YYYY-MM-DD"
    )
    p_wash.set_defaults(func=cmd_wash)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

