"""Minimal web layer: serve saved report snapshots + a JSON API.

The engine stays a CLI/batch pipeline; this module only serves already-computed
or on-demand-computed data. Every API endpoint accepts the same parameters as
its CLI command and caches the result as a JSON snapshot under ``reports/`` so
that re-running the exact same request against unchanged data returns instantly
instead of recomputing.

Start with:  python -m src.cli serve  (or ``docker compose run ... serve``)
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import polars as pl

from src import reports
from src.analysis import symbol_analyze
from src.db import get_conn
from src.screener import (
    load_top_turnover,
    run_screener,
    screen_turnover_momentum,
    symbol_turnover_momentum,
    find_similar_momentum,
    wash_report,
    broker_holdings,
    fetch_trade_dates,
    load_rollup,
    load_summary,
    inspect_symbol,
    position_analysis,
    screen_track_c_smart_money,
    market_overview,
    sector_overview,
    detect_syndicates,
    backtest_signals,
)
from src.watchlist import (
    add_note as watch_note,
    add_symbol as watch_add,
    archive_symbol as watch_archive,
    enter_position as watch_enter,
    exit_position as watch_exit,
    history as watch_history,
    list_symbols as watch_list,
)


def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _first(query: dict, key: str, default=None):
    vals = query.get(key)
    if not vals:
        return default
    return vals[0] if isinstance(vals, list) else vals


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int(_first(query, key, default))
    except (TypeError, ValueError):
        return default


def _bool(query: dict, key: str, default: bool = False) -> bool:
    v = str(_first(query, key, default)).lower()
    return v in ("1", "true", "yes", "on")


def _number(value):
    """Parse a value as float, or None when blank/invalid."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):

    # ---- helpers -----------------------------------------------------------
    def _send_json(self, obj, status: int = 200) -> None:
        body = reports.to_json(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self, msg: str = "Not found") -> None:
        self._send_text(msg, "text/plain; charset=utf-8", 404)

    def _read_json(self) -> dict:
        """Parse the JSON request body into a dict (best-effort)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
        except Exception:  # noqa: BLE001
            return {}
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def _cached(self, command: str, params: dict, compute) -> dict:
        """Return compute(conn) result, serving the stored snapshot when valid."""
        cached = reports.load_snapshot(command, params)
        if cached is not None:
            return {"cached": True, "params": params, "data": cached}
        conn = get_conn()
        try:
            data = compute(conn)
        finally:
            conn.close()
        reports.save_snapshot(command, params, data)
        return {"cached": False, "params": params, "data": data}

    def log_message(self, fmt, *args):
        # keep the console readable
        print(f"[web] {self.address_string()} {fmt % args}")

    def _validate_as_of(self, conn, as_of: date | None) -> tuple[date | None, dict | None]:
        """
        Ensures freshness if as_of is today/none, or validates historical date.
        Returns (validated_as_of, market_closed_dict_or_None).
        """
        today = date.today()
        try:
            trade_dates = fetch_trade_dates(conn)
        except Exception:
            trade_dates = [as_of] if as_of else []
        latest_date = trade_dates[-1] if trade_dates else None

        if as_of is None or as_of == today:
            if not trade_dates or today not in trade_dates:
                try:
                    from src.fetcher import fetch_today
                    from src.ingestion import ingest_csv
                    csv_path = fetch_today()
                    if csv_path and csv_path.exists():
                        ingested = ingest_csv(str(csv_path))
                        if ingested.get("floorsheet", 0) == 0:
                            # Incomplete data (e.g., fetched during market hours, missing broker IDs).
                            # Delete the cached file so it can be re-fetched later when market closes.
                            csv_path.unlink(missing_ok=True)
                        else:
                            trade_dates = fetch_trade_dates(conn)
                            latest_date = trade_dates[-1] if trade_dates else None
                except Exception as e:
                    print(f"[web] Auto-fetch today failed: {e}")

        if as_of is not None:
            if trade_dates and as_of in trade_dates:
                return as_of, None
            else:
                return None, {
                    "market_closed": True,
                    "requested_date": as_of.isoformat(),
                    "latest_available": latest_date.isoformat() if latest_date else "none",
                    "error": f"Market closed or no trading session on {as_of.isoformat()}. Latest available trading session is {(latest_date.isoformat() if latest_date else 'none')}."
                }
        else:
            return latest_date, None

    # ---- API endpoints -----------------------------------------------------
    def api_top(self, q):
        raw_as_of = _parse_date(_first(q, "as_of"))
        conn = get_conn()
        try:
            as_of, closed_info = self._validate_as_of(conn, raw_as_of)
            if closed_info:
                self._send_json(closed_info)
                return
        finally:
            conn.close()

        limit = _int(q, "limit", 20)
        sector = _first(q, "sector") or None
        cap_tier = _first(q, "cap_tier") or None
        if cap_tier:
            cap_tier = cap_tier.upper().strip()
        params = {
            "as_of": as_of.isoformat() if as_of else None,
            "limit": limit,
            "sector": sector,
            "cap_tier": cap_tier,
        }
        return self._cached(
            "top",
            params,
            lambda conn: load_top_turnover(
                conn, as_of=as_of, limit=limit, sector=sector, cap_tier=cap_tier
            ),
        )

    def api_run(self, q):
        raw_as_of = _parse_date(_first(q, "as_of"))
        conn = get_conn()
        try:
            as_of, closed_info = self._validate_as_of(conn, raw_as_of)
            if closed_info:
                self._send_json(closed_info)
                return
        finally:
            conn.close()

        top = _int(q, "top", 20)
        window = _int(q, "top_holder_window", 22)
        no_persist = _bool(q, "no_persist", True)
        sector = _first(q, "sector") or None
        cap_tier = _first(q, "cap_tier") or None
        if cap_tier:
            cap_tier = cap_tier.upper().strip()
        params = {
            "as_of": as_of.isoformat() if as_of else None,
            "top": top,
            "top_holder_window": window,
            "sector": sector,
            "cap_tier": cap_tier,
        }

        def compute(conn):
            track_a, track_b, meta = run_screener(
                as_of=as_of,
                persist=not no_persist,
                top_turnover=top,
                top_holder_window=window,
                sector=sector,
                cap_tier=cap_tier,
            )
            return {"meta": meta, "track_a": track_a, "track_b": track_b}

        return self._cached("run", params, compute)

    def api_momentum(self, q):
        raw_as_of = _parse_date(_first(q, "as_of"))
        conn = get_conn()
        try:
            as_of, closed_info = self._validate_as_of(conn, raw_as_of)
            if closed_info:
                self._send_json(closed_info)
                return
        finally:
            conn.close()

        short = _int(q, "short", 5)
        base = _int(q, "base", 22)
        sym = _first(q, "symbol")
        symbol = sym.upper().strip() if sym else None
        params = {
            "as_of": as_of.isoformat() if as_of else None,
            "short": short,
            "base": base,
            "symbol": symbol,
        }

        def compute(conn):
            dates = fetch_trade_dates(conn)
            if as_of is not None:
                dates = [d for d in dates if d <= as_of]
            needed = dates[-max(base, short):]
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
                short_window=short,
                base_window=base,
                rollup=rollup,
            )
            sym_mom = None
            similar = []
            if symbol:
                sym_mom = symbol_turnover_momentum(
                    summary,
                    symbol=symbol,
                    short_window=short,
                    base_window=base,
                    rollup=rollup,
                )
                similar = find_similar_momentum(
                    summary,
                    symbol=symbol,
                    short_window=short,
                    base_window=base,
                    rollup=rollup,
                    top_n=5,
                )
            return {
                "gainers": gainers,
                "losers": losers,
                "symbol_momentum": sym_mom,
                "similar": similar,
            }

        return self._cached("momentum", params, compute)

    def api_smartmoney(self, q):
        raw_as_of = _parse_date(_first(q, "as_of"))
        conn = get_conn()
        try:
            as_of, closed_info = self._validate_as_of(conn, raw_as_of)
            if closed_info:
                self._send_json(closed_info)
                return
        finally:
            conn.close()

        def run(conn):
            all_dates = fetch_trade_dates(conn)
            if not all_dates:
                return {"error": "no trade dates"}
            if as_of:
                dates = [d for d in all_dates if d <= as_of]
            else:
                dates = all_dates
            return screen_track_c_smart_money(conn, dates)

        return self._cached("smartmoney", {"as_of": as_of.isoformat() if as_of else None}, run)

    def api_market(self, q):
        def run(conn):
            return {"market": market_overview()}
        return self._cached("market", {}, run)

    def api_sector(self, q):
        def run(conn):
            return {"sectors": sector_overview(conn)}
        return self._cached("sector", {}, run)

    def api_syndicate(self, q):
        def run(conn):
            return {"syndicates": detect_syndicates(conn)}
        return self._cached("syndicate", {}, run)

    def api_backtest(self, q):
        def run(conn):
            return {"backtest": backtest_signals(conn)}
        return self._cached("backtest", {}, run)

    def api_wash(self, q):
        raw_as_of = _parse_date(_first(q, "as_of"))
        conn = get_conn()
        try:
            as_of, closed_info = self._validate_as_of(conn, raw_as_of)
            if closed_info:
                self._send_json(closed_info)
                return
        finally:
            conn.close()

        window = _int(q, "window", 22)
        min_qty = _int(q, "min_qty", 5000)
        include_all = _bool(q, "all", False)
        params = {
            "as_of": as_of.isoformat() if as_of else None,
            "window": window,
            "min_qty": min_qty,
            "all": include_all,
        }
        return self._cached(
            "wash",
            params,
            lambda conn: wash_report(
                conn, window=window, min_qty=min_qty, include_all=include_all, as_of=as_of
            ),
        )

    def api_inspect(self, symbol: str, q):
        sessions = _int(q, "sessions", 22)
        params = {"symbol": symbol.upper(), "sessions": sessions}
        return self._cached(
            "inspect",
            params,
            lambda conn: inspect_symbol(symbol.upper(), sessions=sessions),
        )

    def api_analyze(self, symbol: str, q):
        sessions = _int(q, "sessions", 30)
        params = {"symbol": symbol.upper(), "sessions": sessions}
        return self._cached(
            "analyze",
            params,
            lambda conn: symbol_analyze(conn, symbol.upper(), sessions=sessions),
        )

    def api_sectors(self, q):
        """Return distinct sector names from the market summary table."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT sector FROM daily_market_summary "
                    "WHERE sector IS NOT NULL AND TRIM(sector) != '' "
                    "ORDER BY sector ASC"
                )
                sectors = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
        return {"cached": False, "data": sectors}

    def api_position(self, symbol: str, q):
        data = position_analysis(symbol.upper())
        if data.get("error"):
            return {"cached": False, "params": {"symbol": symbol.upper()}, "data": data}
        return {"cached": False, "params": {"symbol": symbol.upper()}, "data": data}

    def api_broker(self, broker_id: int, q):
        sessions = _int(q, "sessions", 66)
        top = _int(q, "top", 5)
        params = {"broker_id": broker_id, "sessions": sessions, "top": top}

        def _fetch(conn):
            all_holdings = broker_holdings(conn, broker_id, sessions)
            accumulations = [h for h in all_holdings if h.get("net_t22", 0) > 0][:top]
            dist_candidates = [
                h for h in all_holdings
                if h.get("net_t22", 0) < 0 or h.get("net_t1", 0) < 0 or h.get("net_t5", 0) < 0
            ]
            distributions = sorted(
                dist_candidates,
                key=lambda h: (h.get("net_t22", 0), h.get("net_t1", 0))
            )[:top]
            return {
                "broker_id": broker_id,
                "sessions": sessions,
                "top": top,
                "holdings": all_holdings[:top],
                "accumulations": accumulations,
                "distributions": distributions,
            }

        return self._cached("broker", params, _fetch)

    def api_signals(self, q):
        from src.signals import current_streaks, load_signal_history, signal_performance

        if _bool(q, "performance", False):
            params = {"performance": True}
            return self._cached(
                "signals",
                params,
                lambda conn: {"performance": signal_performance(conn)},
            )
        streak = _int(q, "streak", 0)
        symbol = _first(q, "symbol") or None
        broker = _int(q, "broker", 0) or None
        signal = _first(q, "signal") or None
        track = _first(q, "track") or None
        limit = _int(q, "limit", 100)
        params = {
            "streak": streak,
            "symbol": symbol,
            "broker": broker,
            "signal": signal,
            "track": track,
            "limit": limit,
        }
        if streak:
            return self._cached(
                "signals",
                params,
                lambda conn: {"streaks": current_streaks(conn, min_streak=max(1, streak))},
            )
        return self._cached(
            "signals",
            params,
            lambda conn: {
                "rows": load_signal_history(
                    conn,
                    symbol=symbol,
                    broker_id=broker,
                    signal=signal,
                    track=track,
                    limit=limit,
                )
            },
        )

    def api_watchlist(self, q):
        conn = get_conn()
        try:
            data = watch_list(conn, include_archived=_bool(q, "all", False))
        finally:
            conn.close()
        # Live user data is never snapshot-cached (unlike scanner endpoints).
        return {"cached": False, "params": {"all": _bool(q, "all", False)}, "data": data}

    def api_watchitem(self, symbol: str, q):
        conn = get_conn()
        try:
            metadata, notes = watch_history(
                conn, symbol.upper(), limit=_int(q, "limit", 100)
            )
        finally:
            conn.close()
        return {
            "cached": False,
            "params": {"symbol": symbol.upper()},
            "data": {"metadata": metadata, "notes": notes},
        }

    def api_watch_write(self, action: str, body: dict) -> dict:
        """Apply a mutating watchlist action from a POST body."""
        sym = str(body.get("symbol") or "").strip().upper()
        if not sym:
            raise ValueError("symbol is required")
        conn = get_conn()
        try:
            if action == "add":
                watch_add(
                    conn,
                    sym,
                    thesis=(body.get("thesis") or None),
                    tags=(body.get("tags") or None),
                    note=(body.get("note") or None),
                )
            elif action == "note":
                note = str(body.get("note") or "").strip()
                if not note:
                    raise ValueError("note is required")
                if not watch_note(
                    conn,
                    sym,
                    note,
                    note_date=_parse_date(body.get("note_date")),
                ):
                    raise ValueError(f"{sym} is not on the watchlist")
            elif action == "enter":
                price = _number(body.get("price"))
                if price is None:
                    raise ValueError("price is required")
                quantity = _number(body.get("quantity"))
                if quantity is not None:
                    quantity = int(quantity)
                if not watch_enter(
                    conn,
                    sym,
                    price,
                    target_price=_number(body.get("target")),
                    stop_price=_number(body.get("stop")),
                    quantity=quantity,
                    entry_date=_parse_date(body.get("entry_date")),
                ):
                    raise ValueError(f"{sym} is not on the watchlist")
            elif action == "exit":
                price = _number(body.get("price"))
                if price is None:
                    raise ValueError("price is required")
                outcome = str(body.get("outcome") or "CLOSED").upper()
                if outcome not in ("WON", "STOPPED", "CLOSED"):
                    raise ValueError("outcome must be WON, STOPPED or CLOSED")
                if not watch_exit(
                    conn,
                    sym,
                    price,
                    outcome=outcome,
                    exit_date=_parse_date(body.get("exit_date")),
                ):
                    raise ValueError(f"{sym} has no open position")
            elif action == "archive":
                if not watch_archive(conn, sym):
                    raise ValueError(f"{sym} is not on the watchlist")
            else:
                raise ValueError(f"unknown action: {action}")
        finally:
            conn.close()
        return {"ok": True, "action": action, "symbol": sym}

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        # Landing page
        if path in ("/", "/index.html"):
            reports.regenerate_index()
            index_path = reports.REPORTS_DIR / "index.html"
            if index_path.exists():
                self._send_text(index_path.read_text(), "text/html; charset=utf-8")
            else:
                self._send_404("index not generated")
            return

        # Static report files
        if path.startswith("/reports/"):
            rel = path[len("/reports/"):]
            target = (reports.REPORTS_DIR / rel).resolve()
            if (
                target.is_file()
                and target.is_relative_to(reports.REPORTS_DIR.resolve())
            ):
                ctype = (
                    "application/json; charset=utf-8"
                    if target.suffix == ".json"
                    else "text/html; charset=utf-8"
                )
                self._send_text(target.read_text(), ctype)
            else:
                self._send_404("file not found")
            return

        # JSON API
        if path.startswith("/api/"):
            parts = [p for p in path[len("/api/"):].split("/") if p]
            if not parts:
                self._send_json({"error": "no endpoint"}, 400)
                return
            command = parts[0]
            try:
                if command == "top":
                    self._send_json(self.api_top(q))
                elif command == "run":
                    self._send_json(self.api_run(q))
                elif command == "market":
                    self._send_json(self.api_market(q))
                elif command == "sector":
                    self._send_json(self.api_sector(q))
                elif command == "syndicate":
                    self._send_json(self.api_syndicate(q))
                elif command == "backtest":
                    self._send_json(self.api_backtest(q))
                elif command == "smartmoney":
                    self._send_json(self.api_smartmoney(q))
                elif command == "momentum":
                    self._send_json(self.api_momentum(q))
                elif command == "wash":
                    self._send_json(self.api_wash(q))
                elif command == "inspect" and len(parts) >= 2:
                    self._send_json(self.api_inspect(parts[1], q))
                elif command == "analyze" and len(parts) >= 2:
                    self._send_json(self.api_analyze(parts[1], q))
                elif command == "broker" and len(parts) >= 2:
                    try:
                        self._send_json(self.api_broker(int(parts[1]), q))
                    except ValueError:
                        self._send_json({"error": "broker id must be an integer"}, 400)
                elif command == "signals":
                    self._send_json(self.api_signals(q))
                elif command == "sectors":
                    self._send_json(self.api_sectors(q))
                elif command == "position" and len(parts) >= 2:
                    self._send_json(self.api_position(parts[1], q))
                elif command == "watchlist" and len(parts) >= 2:
                    self._send_json(self.api_watchitem(parts[1], q))
                elif command == "watchlist":
                    self._send_json(self.api_watchlist(q))
                elif command == "dates":
                    self._send_json({"dates": reports._available_dates()})
                else:
                    self._send_json({"error": f"unknown endpoint: {command}"}, 404)
            except Exception as exc:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                self._send_json({"error": str(exc) or repr(exc)}, 500)
            return

        self._send_404("Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/watchlist/"):
            self._send_json({"error": "unsupported POST endpoint"}, 404)
            return
        action = path[len("/api/watchlist/"):].split("/")[0] or None
        body = self._read_json()
        try:
            result = self.api_watch_write(action, body)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
            return
        self._send_json(result)


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    reports.regenerate_index()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"NEPSE report server on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
