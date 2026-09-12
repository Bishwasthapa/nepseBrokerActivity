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


def render_track_a(rows: list[dict], top_turnover: int = 20, top_holder_window: int = 22) -> Table:
    table = Table(
        title=f"Track A — Top {top_turnover} Turnover Momentum & Traps",
        title_style="bold white",
        header_style="bold yellow",
        expand=True,
    )
    holder_col_name = f"Top Holder (T_{top_holder_window}D)"
    holder_net_key = f"top_holder_net_{top_holder_window}d"
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
        (holder_col_name, "right"),
    ]:
        table.add_column(col, justify=justify)

    # Group rows by symbol so a stock's broker rows stay together. The group key
    # is the symbol's aggregate T_1 net flow (sum across its broker rows), so the
    # stock with the strongest net 1D leads, followed by that stock's other
    # brokers (ordered by buyer_rank), then the next stock, etc.
    sym_net1: dict[str, int] = {}
    for _r in rows:
        sym_net1[_r["symbol"]] = sym_net1.get(_r["symbol"], 0) + _r["net_t1"]

    ordered = sorted(
        rows,
        key=lambda r: (
            -sym_net1[r["symbol"]],                     # symbol net 1D -> strongest stock first
            r["symbol"],                                # keep the same stock's brokers together
            r["buyer_rank"] if r["buyer_rank"] else 99,  # broker order within a stock
            r["turnover_rank"],
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
            str(r["top_holder_broker_id"]) if r["top_holder_broker_id"] else "-",
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
        as_of=as_of, persist=not args.no_persist, top_turnover=args.top,
        top_holder_window=args.top_holder_window
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
    console.print(render_track_a(track_a, args.top, top_holder_window=args.top_holder_window))
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

    # Filter out promoter and debenture symbols automatically
    from src.screener import _is_excluded

    # Get unique symbols from the rollup, excluding promoters/debentures
    unique_symbols = set()
    if not rollup.is_empty():
        unique_symbols = set(sym for sym in rollup["symbol"].unique().to_list() if not _is_excluded(sym))

    if not args.all:
        # Broker-level: filter by gross_volume (matched_qty * 2) >= min_qty
        if not broker_match.is_empty() and unique_symbols:
            broker_match = broker_match.filter(
                pl.col("broker_id").is_in(
                    rollup.filter(
                        (pl.col("symbol").is_in(unique_symbols))
                        & (pl.col("matched_qty") * 2 >= args.min_qty)
                    )["broker_id"].unique().to_list()
                )
            )
        # Session-level: filter by crossed_qty >= min_qty (wash volume, not market volume)
        if not session_match.is_empty() and unique_symbols:
            session_match = session_match.filter(
                pl.col("symbol").is_in(unique_symbols)
                & (pl.col("crossed_qty") >= args.min_qty)
            )
    else:
        # Even with --all, still exclude promoters/debentures
        if not broker_match.is_empty() and unique_symbols:
            broker_match = broker_match.filter(
                pl.col("broker_id").is_in(
                    rollup.filter(pl.col("symbol").is_in(unique_symbols))["broker_id"].unique().to_list()
                )
            )
        if not session_match.is_empty() and unique_symbols:
            session_match = session_match.filter(pl.col("symbol").is_in(unique_symbols))
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


def render_signal_performance(rows: list[dict]) -> Table:
    table = Table(
        title="Signal Forward Performance (close-to-close; completed observations only)",
        title_style="bold white", header_style="bold yellow", expand=True,
    )
    for col, justify in [
        ("Signal", "left"), ("Track", "left"), ("Horizon", "right"),
        ("Samples", "right"), ("Win Rate", "right"), ("Avg Return", "right"),
        ("Worst", "right"), ("Best", "right"), ("Best Broker*", "right"),
    ]:
        table.add_column(col, justify=justify)
    for r in rows:
        table.add_row(
            Text(r["signal"], style=SIGNAL_STYLE.get(r["signal"], "")), r["track"],
            f"+{r['horizon']}D", str(r["samples"]), _fmt_pct(r["win_rate_pct"]),
            _fmt_pct(r["avg_return_pct"]), _fmt_pct(r["worst_return_pct"]),
            _fmt_pct(r["best_return_pct"]), str(r["best_broker"] or "-"),
        )
    return table



def render_watchlist(rows: list[dict], include_archived: bool = False) -> Table:
    title = "Research Watchlist"
    if include_archived:
        title += " (including archived)"
    table = Table(title=title, title_style="bold white", header_style="bold yellow", expand=True)
    for col, justify in [
        ("Symbol", "left"),
        ("Status", "left"),
        ("Close", "right"),
        ("Trade", "left"),
        ("PnL", "right"),
        ("Target / Stop", "right"),
        ("1D Δ%", "right"),
        ("Rank", "right"),
        ("Tags", "left"),
        ("Thesis", "left"),
        ("Latest Note", "left"),
        ("Updated", "left"),
    ]:
        table.add_column(col, justify=justify)
    for r in rows:
        note = r["note"] or "-"
        if r["note_date"]:
            note = f"{r['note_date']}: {note}"
        reference_price = r["exit_price"] if r["exit_price"] is not None else r["close_price"]
        pnl = None
        if r["entry_price"] is not None and reference_price is not None:
            pnl = 100 * (float(reference_price) / float(r["entry_price"]) - 1)
        trade = r["outcome"] or "-"
        if r["entry_price"] is not None:
            trade += f" @ {_fmt_num(r['entry_price'], 2)}"
        target_stop = " / ".join(
            v for v in (_fmt_num(r["target_price"], 2) if r["target_price"] is not None else None,
                        _fmt_num(r["stop_price"], 2) if r["stop_price"] is not None else None) if v
        ) or "-"
        table.add_row(
            r["symbol"],
            r["status"],
            _fmt_num(r["close_price"], 2),
            trade,
            _fmt_pct(pnl),
            target_stop,
            _fmt_pct(r["price_change_pct"]),
            _fmt_num(r["turnover_rank"]),
            r["tags"] or "-",
            r["thesis"] or "-",
            note,
            str(r["updated_date"]),
        )
    return table


def render_watch_history(metadata: dict, notes: list[dict]) -> None:
    details = [
        f"[bold]Status:[/bold] {metadata['status']}",
        f"[bold]Added:[/bold] {metadata['added_date']}",
        f"[bold]Updated:[/bold] {metadata['updated_date']}",
    ]
    if metadata["tags"]:
        details.append(f"[bold]Tags:[/bold] {metadata['tags']}")
    if metadata["thesis"]:
        details.append(f"[bold]Thesis:[/bold] {metadata['thesis']}")
    if metadata["entry_price"] is not None:
        plan = f"[bold]Trade:[/bold] {metadata['outcome'] or 'UNSET'} @ {_fmt_num(metadata['entry_price'], 2)}"
        if metadata["target_price"] is not None or metadata["stop_price"] is not None:
            plan += f" | Target {_fmt_num(metadata['target_price'], 2)} | Stop {_fmt_num(metadata['stop_price'], 2)}"
        if metadata["exit_price"] is not None:
            realized = 100 * (float(metadata["exit_price"]) / float(metadata["entry_price"]) - 1)
            plan += f" | Exit {_fmt_num(metadata['exit_price'], 2)} | PnL {_fmt_pct(realized)}"
        details.append(plan)
    console.print(Panel("\n".join(details), title=f"Research Journal — {metadata['symbol']}", style="bold blue"))
    if not notes:
        console.print("[dim]No journal notes yet[/dim]")
        return
    table = Table(title="Journal Notes", title_style="bold white", header_style="bold yellow", expand=True)
    table.add_column("Date", justify="left")
    table.add_column("Note", justify="left")
    for row in notes:
        table.add_row(str(row["note_date"]), row["note"])
    console.print(table)


def cmd_watch(args: argparse.Namespace) -> int:
    from src.screener import _is_excluded
    from src.watchlist import (
        add_note, add_symbol, archive_symbol, enter_position, exit_position,
        history, list_symbols,
    )

    symbol = getattr(args, "symbol", None)
    if symbol:
        symbol = symbol.upper()
        if _is_excluded(symbol):
            console.print(f"[yellow]{symbol} is excluded from this project and cannot be watched[/yellow]")
            return 2

    conn = get_conn()
    try:
        if args.watch_command == "add":
            add_symbol(conn, symbol, thesis=args.thesis, tags=args.tags, note=args.note)
            console.print(f"[green]Watching {symbol}[/green]")
            return 0
        if args.watch_command == "note":
            if not add_note(conn, symbol, args.note):
                console.print(f"[yellow]{symbol} is not on the watchlist; add it first with `watch add {symbol}`[/yellow]")
                return 2
            console.print(f"[green]Journal note added for {symbol}[/green]")
            return 0
        if args.watch_command == "enter":
            if args.price <= 0 or (args.quantity is not None and args.quantity <= 0):
                console.print("[red]--price and --quantity must be positive[/red]")
                return 2
            if not enter_position(conn, symbol, args.price, args.target, args.stop, args.quantity):
                console.print(f"[yellow]{symbol} is not on the watchlist; add it first[/yellow]")
                return 2
            console.print(f"[green]Opened trade plan for {symbol} at {_fmt_num(args.price, 2)}[/green]")
            return 0
        if args.watch_command == "exit":
            if args.price <= 0:
                console.print("[red]--price must be positive[/red]")
                return 2
            if not exit_position(conn, symbol, args.price, args.outcome):
                console.print(f"[yellow]{symbol} has no open trade to close[/yellow]")
                return 2
            console.print(f"[green]Closed {symbol} at {_fmt_num(args.price, 2)} ({args.outcome})[/green]")
            return 0
        if args.watch_command == "archive":
            if not archive_symbol(conn, symbol):
                console.print(f"[yellow]{symbol} is not an active watched symbol[/yellow]")
                return 2
            console.print(f"[green]Archived {symbol}; its journal history is retained[/green]")
            return 0
        if args.watch_command == "history":
            metadata, notes = history(conn, symbol, limit=args.limit)
            if metadata is None:
                console.print(f"[yellow]{symbol} is not on the watchlist[/yellow]")
                return 2
            render_watch_history(metadata, notes)
            return 0
        rows = list_symbols(conn, include_archived=args.all)
        if not rows:
            console.print("[dim]No active watchlist symbols. Add one with `watch add LEC --thesis \"...\"`[/dim]")
            return 0
        console.print(render_watchlist(rows, include_archived=args.all))
        return 0
    finally:
        conn.close()


def cmd_signals(args: argparse.Namespace) -> int:
    from src.db import get_conn
    from src.signals import current_streaks, load_signal_history, signal_performance

    conn = get_conn()
    try:
        if args.performance:
            rows = signal_performance(conn)
            if not rows:
                console.print("[dim]No completed forward observations yet; ingest more sessions and persist daily signals.[/dim]")
                return 0
            console.print(render_signal_performance(rows))
            console.print("[dim]* Best Broker requires at least two completed observations.[/dim]")
            return 0
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

    holder = data.get("top_holder_22d")
    mover = data.get("top_holder_1d")
    if holder or mover:
        summary = Table(
            title="Holdings Snapshot (22D holder & recent mover)",
            title_style="bold white",
            header_style="bold green",
            expand=True,
        )
        for col, justify in [
            ("Role", "left"),
            ("Broker", "right"),
            ("Net 1D", "right"),
            ("Net 22D", "right"),
            ("Net 66D", "right"),
            ("Margin %", "right"),
        ]:
            summary.add_column(col, justify=justify)

        def _row(role: str, b: dict) -> None:
            summary.add_row(
                role,
                str(b["broker_id"]),
                _fmt_num(b["net_1d"]),
                _fmt_num(b["net_22d"]),
                _fmt_num(b["net_66d"]),
                _fmt_pct(b["margin_pct"]),
            )

        if holder and (mover is None or mover["broker_id"] != holder["broker_id"]):
            _row("Longest Holder (22D)", holder)
        if mover:
            _row(
                "Top Recent Mover (1D)",
                mover,
            )
        if holder and mover and mover["broker_id"] == holder["broker_id"]:
            _row("Longest Holder + Top Mover", holder)
        console.print(summary)
        console.print()

    if data["signals"]:
        console.print(render_signals(data["signals"], "Signal History"))
    else:
        console.print("[dim]No persisted signal history for this symbol[/dim]")
    console.print()

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


def cmd_inspect(args: argparse.Namespace) -> int:
    from src.screener import inspect_symbol

    symbol = args.symbol.upper()
    data = inspect_symbol(symbol, sessions=args.sessions)
    render_inspect(data)
    return 0


def render_broker(broker_id: int, holdings: list[dict], sessions: int, top: int) -> None:
    """Render a broker deep-dive table showing top positions across windows."""
    console.print()
    if not holdings:
        console.print(
            f"[yellow]No holdings found for broker {broker_id} in the last "
            f"{sessions} sessions[/yellow]"
        )
        return

    table = Table(
        title=f"Broker {broker_id} — Top {min(top, len(holdings))} Holdings (last {sessions} sessions)",
        title_style="bold white",
        header_style="bold yellow",
        expand=True,
    )
    for col, justify in [
        ("Rank", "right"),
        ("Symbol", "left"),
        ("Net T1", "right"),
        ("Net T5", "right"),
        ("Net T22", "right"),
        ("Net T66", "right"),
        ("Margin %", "right"),
    ]:
        table.add_column(col, justify=justify)

    for rank, h in enumerate(holdings[:top], 1):
        table.add_row(
            str(rank),
            h["symbol"],
            _fmt_num(h["net_t1"]),
            _fmt_num(h["net_t5"]),
            _fmt_num(h["net_t22"]),
            _fmt_num(h["net_t66"]),
            _fmt_pct(h["margin_pct"]),
        )
    console.print(table)
    console.print()

    # Summary stats across the full (pre-truncation) holdings set.
    t1_buys = sum(h["net_t1"] for h in holdings if h["net_t1"] > 0)
    t1_sells = abs(sum(h["net_t1"] for h in holdings if h["net_t1"] < 0))
    t22_buys = sum(h["net_t22"] for h in holdings if h["net_t22"] > 0)
    t22_sells = abs(sum(h["net_t22"] for h in holdings if h["net_t22"] < 0))
    net_t22 = sum(h["net_t22"] for h in holdings)
    console.print(f"[bold]Activity Summary ({len(holdings)} positions over last {sessions} sessions):[/bold]")
    console.print(
        f"  T1 Buys: {_fmt_num(t1_buys)} | T1 Sells: {_fmt_num(t1_sells)}"
    )
    console.print(
        f"  T22 Buys: {_fmt_num(t22_buys)} | T22 Sells: {_fmt_num(t22_sells)}"
        f" | Net T22: {net_t22:+,}"
    )


def cmd_broker(args: argparse.Namespace) -> int:
    from src.screener import (
        aggregate_window,
        fetch_trade_dates,
        load_rollup,
        load_summary,
    )

    broker_id = args.broker_id
    top = args.top
    sessions = args.sessions

    conn = get_conn()
    try:
        dates = fetch_trade_dates(conn)
        window_dates = dates[-sessions:] if sessions and dates else []
        if not window_dates:
            console.print("[yellow]No trade data available for the requested window[/yellow]")
            return 0

        rollup = load_rollup(conn, window_dates)
        summary = load_summary(conn, window_dates)
        t1_date = window_dates[-1]

        # Precompute one aggregate per window (all symbols/brokers at once).
        window_aggs: dict[str, list[date]] = {
            "T_1": window_dates[-1:],
            "T_5": window_dates[-5:],
            "T_22": window_dates[-22:],
            "T_66": window_dates[-66:],
        }
        aggs = {
            name: aggregate_window(rollup, dts) for name, dts in window_aggs.items()
        }
        if aggs["T_1"].is_empty():
            console.print(f"[yellow]No activity today for the requested window[/yellow]")
            return 0

        # Symbol -> close on the latest session for margin computation.
        t1_sum = summary.filter(pl.col("trade_date") == t1_date)
        close_map = {
            r["symbol"]: float(r["close_price"])
            for r in t1_sum.iter_rows(named=True)
            if r.get("close_price") is not None
        }

        # All symbols this broker traded on the latest session (excluding
        # promoter stocks and debentures).
        from src.screener import _is_excluded as _sym_excluded
        symbols = [
            s
            for s in aggs["T_1"].filter(pl.col("broker_id") == broker_id)["symbol"].to_list()
            if not _sym_excluded(s)
        ]
        if not symbols:
            console.print(
                f"[yellow]No activity found for broker {broker_id} on {t1_date}[/yellow]"
            )
            return 0

        holdings: list[dict] = []
        for sym in symbols:
            nets: dict[str, int] = {}
            vwap = None
            for name, agg in aggs.items():
                hit = agg.filter(
                    (pl.col("symbol") == sym) & (pl.col("broker_id") == broker_id)
                )
                nets[name] = int(hit["net_qty"][0]) if hit.height else 0
                if hit.height and name == "T_66":
                    vwap = hit["buy_vwap"][0]
            close = close_map.get(sym)
            margin = None
            if vwap and close:
                margin = (close - float(vwap)) / float(vwap) * 100.0
            holdings.append(
                {
                    "symbol": sym,
                    "net_t1": nets["T_1"],
                    "net_t5": nets["T_5"],
                    "net_t22": nets["T_22"],
                    "net_t66": nets["T_66"],
                    "margin_pct": margin,
                }
            )

        # Rank by net T22 (conviction) across the requested window.
        holdings.sort(key=lambda h: h["net_t22"], reverse=True)
        render_broker(broker_id, holdings, sessions, top)
        return 0
    finally:
        conn.close()


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
    p_run.add_argument(
        "--top-holder-window",
        type=int,
        choices=[1, 5, 22, 66],
        default=22,
        help="Window that defines the Top Holder (default: 22)",
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
        "--performance", action="store_true",
        help="Summarize completed +1/+5/+10/+22-session forward returns by signal",
    )
    p_signals.add_argument(
        "--streak",
        type=int,
        default=0,
        help="Show current streaks of at least N consecutive sessions instead of rows",
    )
    p_signals.add_argument("--limit", type=int, default=100)
    p_signals.set_defaults(func=cmd_signals)

    p_watch = sub.add_parser("watch", help="Manage personal research watchlist and journal")
    p_watch_sub = p_watch.add_subparsers(dest="watch_command", required=True)
    p_watch_add = p_watch_sub.add_parser("add", help="Add or reactivate a research ticker")
    p_watch_add.add_argument("symbol", type=str, help="NEPSE ticker, e.g. LEC")
    p_watch_add.add_argument("--thesis", type=str, default=None, help="Why the ticker is on watch")
    p_watch_add.add_argument("--tags", type=str, default=None, help="Comma-separated research tags")
    p_watch_add.add_argument("--note", type=str, default=None, help="Optional first dated journal note")
    p_watch_add.set_defaults(func=cmd_watch)
    p_watch_note = p_watch_sub.add_parser("note", help="Append a dated journal observation")
    p_watch_note.add_argument("symbol", type=str, help="Watched NEPSE ticker")
    p_watch_note.add_argument("note", type=str, help="Observation to record")
    p_watch_note.set_defaults(func=cmd_watch)
    p_watch_enter = p_watch_sub.add_parser("enter", help="Record an open trade plan for a watched ticker")
    p_watch_enter.add_argument("symbol", type=str, help="Watched NEPSE ticker")
    p_watch_enter.add_argument("--price", type=float, required=True, help="Actual entry price")
    p_watch_enter.add_argument("--target", type=float, default=None, help="Planned target price")
    p_watch_enter.add_argument("--stop", type=float, default=None, help="Planned stop-loss price")
    p_watch_enter.add_argument("--quantity", type=int, default=None, help="Shares bought")
    p_watch_enter.set_defaults(func=cmd_watch)
    p_watch_exit = p_watch_sub.add_parser("exit", help="Close an open watched trade and record its outcome")
    p_watch_exit.add_argument("symbol", type=str, help="Watched NEPSE ticker")
    p_watch_exit.add_argument("--price", type=float, required=True, help="Actual exit price")
    p_watch_exit.add_argument("--outcome", choices=["WON", "STOPPED", "CLOSED"], default="CLOSED")
    p_watch_exit.set_defaults(func=cmd_watch)
    p_watch_list = p_watch_sub.add_parser("list", help="List active research tickers")
    p_watch_list.add_argument("--all", action="store_true", help="Include archived research tickers")
    p_watch_list.set_defaults(func=cmd_watch)
    p_watch_history = p_watch_sub.add_parser("history", help="Show full research journal for a ticker")
    p_watch_history.add_argument("symbol", type=str, help="Watched NEPSE ticker")
    p_watch_history.add_argument("--limit", type=int, default=100, help="Maximum notes to show (default: 100)")
    p_watch_history.set_defaults(func=cmd_watch)
    p_watch_archive = p_watch_sub.add_parser("archive", help="Archive a ticker but keep its journal")
    p_watch_archive.add_argument("symbol", type=str, help="Watched NEPSE ticker")
    p_watch_archive.set_defaults(func=cmd_watch)

    p_inspect = sub.add_parser("inspect", help="Deep-dive a single symbol")
    p_inspect.add_argument("symbol", type=str, help="NEPSE ticker, e.g. LEC")
    p_inspect.add_argument("--sessions", type=int, default=22, help="Recent sessions to show")
    p_inspect.set_defaults(func=cmd_inspect)

    p_broker = sub.add_parser("broker", help="Deep-dive broker activity")
    p_broker.add_argument("broker_id", type=int, help="Broker ID to inspect")
    p_broker.add_argument("--top", type=int, default=5, help="Number of top holdings to show (default: 5)")
    p_broker.add_argument("--sessions", type=int, default=66, help="Recent sessions to show")
    p_broker.set_defaults(func=cmd_broker)

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
    p_wash.add_argument(
        "--min-qty", type=int, default=5000, help="Minimum total quantity to include (default: 5000)"
    )
    p_wash.add_argument(
        "--all", action="store_true", help="Show all results (ignore quantity threshold)"
    )
    p_wash.set_defaults(func=cmd_wash)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

