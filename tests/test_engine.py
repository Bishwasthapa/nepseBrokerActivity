"""Unit tests: VWAP math, wash-trade filtering, windowing, signal classification."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from src.ingestion import compute_daily_broker_rollup, compute_daily_market_summary
from src.screener import (
    WINDOWS,
    aggregate_window,
    classify_track_a,
    compute_broker_match_pct,
    compute_session_match_pct,
    screen_track_a,
    screen_track_b,
    screen_turnover_momentum,
    stock_window_totals,
    window_dates,
)
from src.signals import build_signal_rows, current_streaks, signal_performance
from src.watchlist import add_note, add_symbol, archive_symbol, enter_position, exit_position, history


def _row(d, cid, sym, buyer, seller, qty, rate):
    return {
        "trade_date": d,
        "contract_id": cid,
        "symbol": sym,
        "buyer_broker": buyer,
        "seller_broker": seller,
        "quantity": qty,
        "rate": rate,
        "amount": round(qty * rate, 2),
    }


class TestVWAP:
    def test_buy_and_sell_vwap(self):
        d = date(2026, 9, 1)
        df = pl.DataFrame(
            [
                _row(d, 1, "LEC", 58, 1, 100, 100.0),
                _row(d, 2, "LEC", 58, 2, 200, 110.0),
                _row(d, 3, "LEC", 3, 58, 50, 120.0),
            ]
        )
        rollup = compute_daily_broker_rollup(df)
        b58 = rollup.filter(pl.col("broker_id") == 58).row(0, named=True)
        assert b58["buy_qty"] == 300
        assert b58["buy_amount"] == pytest.approx(100 * 100 + 200 * 110)
        buy_vwap = b58["buy_amount"] / b58["buy_qty"]
        assert buy_vwap == pytest.approx((10000 + 22000) / 300)
        assert b58["sell_qty"] == 50
        assert b58["sell_amount"] / b58["sell_qty"] == pytest.approx(120.0)

    def test_zero_volume_vwap_is_null(self):
        d = date(2026, 9, 1)
        df = pl.DataFrame([_row(d, 1, "LEC", 1, 2, 10, 100.0)])
        agg = aggregate_window(compute_daily_broker_rollup(df), [d])
        seller = agg.filter(pl.col("broker_id") == 2).row(0, named=True)
        assert seller["buy_qty"] == 0
        assert seller["buy_vwap"] is None
        buyer = agg.filter(pl.col("broker_id") == 1).row(0, named=True)
        assert buyer["sell_vwap"] is None


class TestWashTrades:
    def test_wash_excluded_from_directional_and_logged(self):
        d = date(2026, 9, 1)
        df = pl.DataFrame(
            [
                _row(d, 1, "LEC", 58, 58, 1000, 100.0),
                _row(d, 2, "LEC", 58, 7, 200, 101.0),
                _row(d, 3, "LEC", 9, 58, 50, 102.0),
            ]
        )
        rollup = compute_daily_broker_rollup(df)
        b58 = rollup.filter(pl.col("broker_id") == 58).row(0, named=True)
        assert b58["self_trade_qty"] == 1000
        assert b58["matched_qty"] == 1000
        assert b58["buy_qty"] == 200
        assert b58["sell_qty"] == 50
        assert b58["buy_amount"] == pytest.approx(200 * 101.0)

    def test_pure_wash_broker_has_zero_directional(self):
        d = date(2026, 9, 1)
        df = pl.DataFrame([_row(d, 1, "LEC", 10, 10, 500, 50.0)])
        rollup = compute_daily_broker_rollup(df)
        row = rollup.filter(pl.col("broker_id") == 10).row(0, named=True)
        assert row["buy_qty"] == 0
        assert row["sell_qty"] == 0
        assert row["self_trade_qty"] == 500
        assert row["matched_qty"] == 500


class TestMatchDetection:
    def _rollup(self):
        # Window over two sessions with known buy/sell/matched quantities.
        rows = [
            # Broker 7: self-match 300 + buys/sells
            {"trade_date": date(2026, 9, 1), "symbol": "A", "broker_id": 7,
             "buy_qty": 1000, "sell_qty": 1000, "self_trade_qty": 300, "matched_qty": 300},
            {"trade_date": date(2026, 9, 2), "symbol": "B", "broker_id": 7,
             "buy_qty": 500, "sell_qty": 2500, "self_trade_qty": 500, "matched_qty": 500},
            # Broker 3: only directional, no wash
            {"trade_date": date(2026, 9, 1), "symbol": "A", "broker_id": 3,
             "buy_qty": 2000, "sell_qty": 0, "self_trade_qty": 0, "matched_qty": 0},
        ]
        return pl.DataFrame(rows)

    def test_broker_match_pct_formula(self):
        rollup = self._rollup()
        res = compute_broker_match_pct(rollup, [date(2026, 9, 1), date(2026, 9, 2)])
        b7 = res.filter(pl.col("broker_id") == 7).row(0, named=True)
        # buy 1500, sell 3500, matched 800, gross 5000
        assert b7["buy_qty"] == 1500
        assert b7["sell_qty"] == 3500
        assert b7["matched_qty"] == 800
        assert b7["gross_volume"] == 5000
        assert b7["match_pct"] == pytest.approx(2 * 800 / 5000 * 100.0)

    def test_broker_match_zero_gross_is_zero(self):
        rollup = self._rollup().filter(pl.col("broker_id") == 3)
        res = compute_broker_match_pct(rollup, [date(2026, 9, 1)])
        b3 = res.row(0, named=True)
        assert b3["gross_volume"] == 2000
        # no matched qty -> 0%, not NaN/inf
        assert b3["match_pct"] == 0.0

    def test_broker_match_per_symbol(self):
        rollup = self._rollup()
        res = compute_broker_match_pct(
            rollup, [date(2026, 9, 1), date(2026, 9, 2)], per_symbol=True
        )
        # Broker 7 has two distinct symbol rows
        assert res.filter(pl.col("broker_id") == 7).height == 2

    def test_session_match_pct(self):
        rollup = self._rollup()
        summary = pl.DataFrame(
            [
                {"trade_date": date(2026, 9, 1), "symbol": "A", "close_price": 10.0,
                 "price_change_pct": 0.0, "total_qty": 5000, "total_turnover": 10000.0,
                 "turnover_rank": 1},
            ]
        )
        res = compute_session_match_pct(rollup, summary, [date(2026, 9, 1)])
        row = res.filter(pl.col("symbol") == "A").row(0, named=True)
        # crossed qty on 2026-09-01 = 300 (broker7) + 0 (broker3) = 300
        assert row["crossed_qty"] == 300
        assert row["total_qty"] == 5000
        assert row["session_match_pct"] == pytest.approx(300 / 5000 * 100.0)

    def test_session_match_zero_total_is_zero(self):
        rollup = self._rollup()
        summary = pl.DataFrame(
            [
                {"trade_date": date(2026, 9, 2), "symbol": "B", "close_price": 10.0,
                 "price_change_pct": 0.0, "total_qty": 0, "total_turnover": 0.0,
                 "turnover_rank": 1},
            ]
        )
        res = compute_session_match_pct(rollup, summary, [date(2026, 9, 2)])
        row = res.filter(pl.col("symbol") == "B").row(0, named=True)
        assert row["crossed_qty"] == 500
        assert row["session_match_pct"] == 0.0


class TestMarketSummary:
    def test_price_change_and_rank(self):
        d2 = date(2026, 9, 2)
        df = pl.DataFrame(
            [
                _row(d2, 1, "AAA", 1, 2, 10, 110.0),
                _row(d2, 2, "BBB", 1, 2, 100, 50.0),
            ]
        )
        prev = {(d2, "AAA"): 100.0, (d2, "BBB"): 50.0}
        summary = compute_daily_market_summary(df, prev)
        aaa = summary.filter(pl.col("symbol") == "AAA").row(0, named=True)
        bbb = summary.filter(pl.col("symbol") == "BBB").row(0, named=True)
        assert aaa["price_change_pct"] == pytest.approx(10.0)
        assert bbb["price_change_pct"] == pytest.approx(0.0)
        assert bbb["turnover_rank"] == 1
        assert aaa["turnover_rank"] == 2

    def test_zero_prev_close_guard(self):
        d = date(2026, 9, 1)
        df = pl.DataFrame([_row(d, 1, "AAA", 1, 2, 10, 100.0)])
        summary = compute_daily_market_summary(df, {(d, "AAA"): 0.0})
        assert summary["price_change_pct"][0] == 0.0


class TestWindows:
    def test_session_lookbacks_not_calendar(self):
        start = date(2026, 8, 31)
        dates = [start + timedelta(days=i) for i in range(10)]
        sessions = [d for d in dates if d.weekday() < 5]
        windows = window_dates(sessions)
        assert len(windows["T_1"]) == 1
        assert windows["T_1"][0] == sessions[-1]
        assert windows["T_5"] == sessions[-5:]
        assert len(windows["T_22"]) == len(sessions)
        assert WINDOWS["T_66"] == 66


class TestSignals:
    def test_silent_accumulation(self):
        sig = classify_track_a(
            58,
            {"T_1": 1000, "T_5": 4000, "T_22": 20000, "T_66": 50000},
            1.2,
            0.8,
            True,
            False,
        )
        assert sig == "SILENT_ACCUMULATION"

    def test_silent_accumulation_margin_bounds(self):
        nets = {"T_1": 1, "T_22": 1, "T_66": 1}
        assert classify_track_a(58, nets, 3.01, 0.5, True, False) is None
        assert classify_track_a(58, nets, -3.01, 0.5, True, False) is None

    def test_active_markup(self):
        sig = classify_track_a(
            12,
            {"T_1": 800, "T_5": 200, "T_22": 5000, "T_66": -100},
            6.5,
            3.1,
            False,
            False,
        )
        assert sig == "ACTIVE_MARKUP"

    def test_distribution_trap_takes_priority(self):
        sig = classify_track_a(
            12,
            {"T_1": -9000, "T_5": 100, "T_22": 40000, "T_66": 80000},
            1.0,
            0.5,
            True,
            True,
        )
        assert sig == "DISTRIBUTION_TRAP"

    def test_watch_when_no_rule_matches(self):
        assert classify_track_a(
            1, {"T_1": 10, "T_22": -5, "T_66": -1}, 0.0, 0.1, False, False
        ) is None

def _mini_market():
    """In-memory market: LEC silent-accum + HIDCL stealth."""
    days = [date(2026, 6, 1) + timedelta(days=i) for i in range(66)]
    roll_rows = []
    sum_rows = []
    for i, d in enumerate(days):
        buff = {}
        buff["LEC"] = {
            "buy": 5000, "sell": 1000, "amt_buy": 500000.0, "amt_sell": 100000.0,
            "close": 101.0, "chg": 0.4, "qty": 10000,
            "to": 1000000.0 * (2 if i == len(days) - 1 else 1), "rank": 1,
        }
        spike = 8 if i == len(days) - 1 else 1
        buff["HIDCL"] = {
            "buy": 3000 * spike, "sell": 100, "amt_buy": 3000 * spike * 50.0,
            "amt_sell": 100 * 50.0, "close": 50.5, "chg": 0.2,
            "qty": 10000 * spike, "to": 10000 * spike * 50.0, "rank": 8,
        }
        for sym, s in buff.items():
            roll_rows.append(
                {
                    "trade_date": d, "symbol": sym, "broker_id": 58 if sym == "LEC" else 41,
                    "buy_qty": s["buy"], "buy_amount": s["amt_buy"],
                    "sell_qty": s["sell"], "sell_amount": s["amt_sell"],
                    "self_trade_qty": 0,
                }
            )
            if sym == "LEC":
                roll_rows.append(
                    {
                        "trade_date": d, "symbol": sym, "broker_id": 2,
                        "buy_qty": 500, "buy_amount": 50000.0,
                        "sell_qty": 4000, "sell_amount": 400000.0, "self_trade_qty": 0,
                    }
                )
            else:
                hid_qty = s["qty"]
                hid_buy = s["buy"]
                for b in range(1, 21):
                    sq = int((hid_qty - hid_buy + 100) / 20)
                    roll_rows.append(
                        {
                            "trade_date": d, "symbol": sym, "broker_id": b,
                            "buy_qty": 50, "buy_amount": 2500.0,
                            "sell_qty": sq, "sell_amount": sq * 50.0, "self_trade_qty": 0,
                        }
                    )
            sum_rows.append(
                {
                    "trade_date": d, "symbol": sym,
                    "close_price": s["close"], "price_change_pct": s["chg"],
                    "total_qty": s["qty"], "total_turnover": float(s["to"]),
                    "turnover_rank": s["rank"],
                }
            )
    return pl.DataFrame(roll_rows), pl.DataFrame(sum_rows), window_dates(days)


class TestScreeners:
    def test_track_a_flags_lec_silent(self):
        rollup, summary, windows = _mini_market()
        rows = screen_track_a(rollup, summary, windows)
        lec = [r for r in rows if r["symbol"] == "LEC" and r["broker_id"] == 58]
        assert lec, "expected LEC / broker 58 in Track A"
        assert lec[0]["signal"] == "SILENT_ACCUMULATION"
        assert lec[0]["net_t1"] > 0
        assert lec[0]["net_t22"] > 0
        assert lec[0]["net_t66"] > 0

    def test_track_a_top_holder_per_symbol(self):
        rollup, summary, windows = _mini_market()
        rows = screen_track_a(rollup, summary, windows)
        # Broker 58 is the dominant net T_22 accumulator on LEC; broker 2 is a net seller.
        lec_rows = [r for r in rows if r["symbol"] == "LEC"]
        assert lec_rows, "expected LEC rows in Track A"
        assert all(r["top_holder_broker_id"] == 58 for r in lec_rows)
        # The top holder's own row carries a positive one-day net (still accumulating -> HOLD).
        holder_row = next(r for r in lec_rows if r["broker_id"] == 58)
        assert holder_row["top_holder_net_1d"] > 0
        assert holder_row["top_holder_net_22d"] > 0
        # Non-holder rows expose the holder id but leave Holder T1 unset on render.
        non_holder = next(r for r in lec_rows if r["broker_id"] != 58)
        assert non_holder["top_holder_broker_id"] == 58

    def test_track_b_flags_hidcl(self):
        rollup, summary, windows = _mini_market()
        rows = screen_track_b(rollup, summary, windows)
        hits = [r for r in rows if r["symbol"] == "HIDCL"]
        assert hits, "expected HIDCL stealth hit"
        hit = hits[0]
        assert hit["broker_id"] == 41
        assert hit["absorption_pct"] >= 20.0
        assert hit["dispersion_pct"] < 25.0
        assert hit["volume_inflection"] >= 2.0
        assert -4.0 <= hit["t22_price_change_pct"] <= 4.0
        assert hit["signal"] == "STEALTH_ACCUMULATION"

    def test_dominance_zero_qty_guard(self):
        empty_tot = stock_window_totals(
            pl.DataFrame(
                schema={
                    "trade_date": pl.Date,
                    "symbol": pl.Utf8,
                    "close_price": pl.Float64,
                    "price_change_pct": pl.Float64,
                    "total_qty": pl.Int64,
                    "total_turnover": pl.Float64,
                    "turnover_rank": pl.Int32,
                }
            ),
            [date(2026, 9, 1)],
        )
        assert empty_tot.is_empty()


class FakeCursor:
    def __init__(self, *result_sets):
        self._result_sets = [list(r) for r in result_sets]
        self._i = 0

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        if self._i < len(self._result_sets):
            r = self._result_sets[self._i]
            self._i += 1
            return list(r)
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *result_sets):
        self._cursor = FakeCursor(*result_sets)

    def cursor(self):
        return self._cursor


class TestSignalPersistence:
    def test_build_rows_skips_watch_and_unclassified(self):
        d = date(2026, 9, 11)
        track_a = [
            {
                "symbol": "LEC",
                "turnover_rank": 1,
                "broker_id": 58,
                "net_t1": 100,
                "net_t5": 200,
                "net_t22": 300,
                "net_t66": 400,
                "margin_pct": 1.5,
                "t1_change_pct": 0.5,
                "signal": "SILENT_ACCUMULATION",
            },
            {"symbol": "NRN", "broker_id": 34, "signal": "WATCH"},
        ]
        rows = build_signal_rows(track_a, [], d)
        assert len(rows) == 1
        r = rows[0]
        assert r[0] == d
        assert r[1] == "LEC"
        assert r[2] == 1  # turnover_rank
        assert r[3] == 58
        assert r[4] == 100  # net_1d
        assert r[11] == "TRACK_A"

    def test_track_b_unranked_and_22d_only(self):
        d = date(2026, 9, 11)
        track_b = [
            {
                "symbol": "HIDCL",
                "broker_id": 41,
                "net_t22": 5000,
                "margin_pct": 2.0,
                "t22_price_change_pct": 0.1,
                "signal": "STEALTH_ACCUMULATION",
            },
            {"symbol": "OTHER", "broker_id": 9, "signal": "WATCH"},
        ]
        rows = build_signal_rows([], track_b, d)
        assert len(rows) == 1
        r = rows[0]
        assert r[2] == 0  # unranked
        assert r[4] is None and r[5] is None  # net_1d/5d null
        assert r[6] == 5000  # net_22d
        assert r[7] is None  # net_66d
        assert r[11] == "TRACK_B"


class TestStreaks:
    # helper: each fake conn gets (session_calendar, signal_rows)
    CAL = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)]

    def test_continuous_signal_counts_current_streak(self):
        d1, d2, d3, d4 = self.CAL
        signals = [
            (d1, "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (d2, "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (d3, "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (d4, "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (d4, "SOHL", 10, "TRACK_A", "ACTIVE_MARKUP"),
        ]
        cal = [(d,) for d in self.CAL]
        streaks = current_streaks(FakeConn(cal, signals))
        lec = next(r for r in streaks if r["symbol"] == "LEC")
        assert lec["streak"] == 4
        sohl = next(r for r in streaks if r["symbol"] == "SOHL")
        assert sohl["streak"] == 1

    def test_missing_session_breaks_streak(self):
        # 09-03 is a real session but LEC had no actionable signal that day.
        signals = [
            (self.CAL[0], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (self.CAL[1], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (self.CAL[3], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
        ]
        cal = [(d,) for d in self.CAL]
        streaks = current_streaks(FakeConn(cal, signals))
        lec = next(r for r in streaks if r["symbol"] == "LEC")
        assert lec["streak"] == 1

    def test_other_signal_breaks_streak(self):
        # 09-03 changed signal -> SILENT streak resets to 1 at 09-04.
        signals = [
            (self.CAL[0], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (self.CAL[1], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (self.CAL[2], "LEC", 58, "TRACK_A", "ACTIVE_MARKUP"),
            (self.CAL[3], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
        ]
        cal = [(d,) for d in self.CAL]
        streaks = current_streaks(FakeConn(cal, signals))
        silent = next(r for r in streaks if r["signal"] == "SILENT_ACCUMULATION")
        assert silent["streak"] == 1

    def test_weekend_gap_does_not_break_streak(self):
        # Real trading sessions skip the weekend: 09-04 (Fri) -> 09-07 (Mon).
        calendar = [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)]
        signals = [
            (calendar[0], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (calendar[1], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
            (calendar[2], "LEC", 58, "TRACK_A", "SILENT_ACCUMULATION"),
        ]
        cal = [(d,) for d in calendar]
        streaks = current_streaks(FakeConn(cal, signals))
        lec = next(r for r in streaks if r["symbol"] == "LEC")
        assert lec["streak"] == 3

    def test_min_streak_filter(self):
        d1, d2, d3, _ = self.CAL
        signals = [
            (d1, "AAA", 1, "TRACK_A", "SILENT_ACCUMULATION"),
            (d2, "AAA", 1, "TRACK_A", "SILENT_ACCUMULATION"),
            (d3, "AAA", 1, "TRACK_A", "SILENT_ACCUMULATION"),
            (d3, "BBB", 2, "TRACK_A", "SILENT_ACCUMULATION"),
        ]
        cal = [(d,) for d in self.CAL]
        streaks = current_streaks(FakeConn(cal, signals), min_streak=2)
        assert [r["symbol"] for r in streaks] == ["AAA"]
        assert len(streaks) == 1


class TestTurnoverMomentum:
    """Multi-window turnover momentum scanner (gainers & losers)."""

    def _market(self):
        """25 sessions: G ramps up in the last 5, L dries up, A flat, S recent."""
        dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(25)]
        rows = []
        for i, d in enumerate(dates):
            if i >= 20:  # last 5 sessions (short window)
                rows.append({"trade_date": d, "symbol": "G", "close_price": 120.0,
                             "price_change_pct": 5.0, "total_qty": 1000,
                             "total_turnover": 500.0, "turnover_rank": 5})
                rows.append({"trade_date": d, "symbol": "L", "close_price": 40.0,
                             "price_change_pct": -5.0, "total_qty": 1000,
                             "total_turnover": 60.0, "turnover_rank": 80})
                rows.append({"trade_date": d, "symbol": "A", "close_price": 50.0,
                             "price_change_pct": 0.0, "total_qty": 1000,
                             "total_turnover": 200.0, "turnover_rank": 2})
                rows.append({"trade_date": d, "symbol": "S", "close_price": 30.0,
                             "price_change_pct": 0.0, "total_qty": 100,
                             "total_turnover": 10.0, "turnover_rank": 90})
            else:
                rows.append({"trade_date": d, "symbol": "G", "close_price": 100.0,
                             "price_change_pct": 0.0, "total_qty": 1000,
                             "total_turnover": 100.0, "turnover_rank": 60})
                rows.append({"trade_date": d, "symbol": "L", "close_price": 50.0,
                             "price_change_pct": 0.0, "total_qty": 1000,
                             "total_turnover": 800.0, "turnover_rank": 5})
                rows.append({"trade_date": d, "symbol": "A", "close_price": 50.0,
                             "price_change_pct": 0.0, "total_qty": 1000,
                             "total_turnover": 200.0, "turnover_rank": 2})
        return pl.DataFrame(rows)

    def _rollup(self):
        dates = [date(2026, 5, 1) + timedelta(days=i) for i in range(25)]
        rows = []
        for d in dates[-5:]:
            rows.append({"trade_date": d, "symbol": "G", "broker_id": 58,
                         "buy_qty": 1000, "buy_amount": 100000.0,
                         "sell_qty": 0, "sell_amount": 0.0, "self_trade_qty": 0})
            rows.append({"trade_date": d, "symbol": "G", "broker_id": 2,
                         "buy_qty": 0, "buy_amount": 0.0,
                         "sell_qty": 900, "sell_amount": 90000.0, "self_trade_qty": 0})
        return pl.DataFrame(rows)

    def test_gainer_and_loser_classification(self):
        gainers, losers = screen_turnover_momentum(self._market(), short_window=5, base_window=22)
        g = [r for r in gainers if r["symbol"] == "G"]
        l = [r for r in losers if r["symbol"] == "L"]
        assert g, "G should be flagged as a momentum gainer"
        assert g[0]["avg_rank_short"] <= 50
        assert g[0]["rank_drift"] >= 15
        assert g[0]["turnover_ratio"] >= 1.75
        assert l, "L should be flagged as a momentum loser"
        assert l[0]["avg_rank_base"] <= 40
        assert l[0]["rank_drift"] <= -15
        assert l[0]["turnover_ratio"] <= 0.50
        # Controls: flat A and recent-only S appear in neither bucket.
        assert not [r for r in gainers if r["symbol"] in ("A", "S")]
        assert not [r for r in losers if r["symbol"] in ("A", "S")]

    def test_gainers_sorted_by_drift_desc(self):
        gainers, losers = screen_turnover_momentum(self._market())
        assert [r["rank_drift"] for r in gainers] == sorted(
            (r["rank_drift"] for r in gainers), reverse=True
        )
        assert [r["rank_drift"] for r in losers] == sorted(
            (r["rank_drift"] for r in losers)
        )

    def test_broker_footprint_enrichment(self):
        gainers, _ = screen_turnover_momentum(self._market(), rollup=self._rollup())
        g = next(r for r in gainers if r["symbol"] == "G")
        assert g["top_accumulator"] == 58
        assert g["top_distributor"] == 2

    def test_fewer_sessions_than_base_window_no_crash(self):
        # A symbol with only 3 sessions (below the 22-session baseline) must not
        # error and simply yields no qualifying candidates.
        short = pl.DataFrame(
            [
                {"trade_date": date(2026, 9, 1), "symbol": "X", "close_price": 10.0,
                 "price_change_pct": 0.0, "total_qty": 100, "total_turnover": 50.0,
                 "turnover_rank": 25},
                {"trade_date": date(2026, 9, 2), "symbol": "X", "close_price": 10.0,
                 "price_change_pct": 0.0, "total_qty": 100, "total_turnover": 50.0,
                 "turnover_rank": 25},
                {"trade_date": date(2026, 9, 3), "symbol": "X", "close_price": 10.0,
                 "price_change_pct": 0.0, "total_qty": 100, "total_turnover": 50.0,
                 "turnover_rank": 25},
            ]
        )
        gainers, losers = screen_turnover_momentum(short, short_window=5, base_window=22)
        assert gainers == [] and losers == []


class TestWatchlist:
    def test_add_reactivate_note_archive_and_history(self):
        class Cursor:
            def __init__(self):
                self.rowcount = 1
                self.description = [("symbol",), ("status",), ("thesis",), ("tags",), ("added_date",), ("updated_date",)]
                self.calls = []
            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                if "SELECT note_date, note, created_at" in sql:
                    self.description = [("note_date",), ("note",), ("created_at",)]
                elif "SELECT symbol, status" in sql:
                    self.description = [
                        ("symbol",), ("status",), ("thesis",), ("tags",),
                        ("added_date",), ("updated_date",),
                    ]

            def fetchone(self):
                sql = self.calls[-1][0]
                if "SELECT 1 FROM watchlist" in sql:
                    return (1,)
                if "SELECT symbol, status" in sql:
                    return ("LEC", "WATCHING", "accumulation", "momentum", date(2026, 9, 11), date(2026, 9, 11))
                return None

            def fetchall(self):
                return [(date(2026, 9, 11), "Broker flow remains positive", None)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Conn:
            def __init__(self):
                self.cur = Cursor()
                self.commits = 0

            def cursor(self):
                return self.cur

            def commit(self):
                self.commits += 1

        conn = Conn()
        add_symbol(conn, "LEC", thesis="accumulation", tags="momentum", note="initial scan")
        assert add_note(conn, "LEC", "Broker flow remains positive") is True
        assert archive_symbol(conn, "LEC") is True
        metadata, notes = history(conn, "LEC")
        assert metadata["symbol"] == "LEC"
        assert notes[0]["note"] == "Broker flow remains positive"
        assert conn.commits == 3



    def test_enter_and_exit_position(self):
        class Cursor:
            def __init__(self):
                self.rowcount = 1
            def execute(self, sql, params=None):
                self.sql, self.params = sql, params
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def __init__(self): self.cur, self.commits = Cursor(), 0
            def cursor(self): return self.cur
            def commit(self): self.commits += 1
        conn = Conn()
        assert enter_position(conn, "LEC", 100, 120, 90, 10) is True
        assert "outcome = 'OPEN'" in conn.cur.sql
        assert exit_position(conn, "LEC", 115, "WON") is True
        assert "outcome = %s" in conn.cur.sql
        assert conn.commits == 2


class TestSignalPerformance:
    def test_horizons_are_queried(self):
        class Cursor:
            def __init__(self):
                self.description = [("signal",), ("track",), ("samples",), ("win_rate_pct",), ("avg_return_pct",), ("worst_return_pct",), ("best_return_pct",), ("best_broker",)]
                self.params = []
            def execute(self, sql, params): self.params.append(params)
            def fetchall(self): return [("ACTIVE_MARKUP", "TRACK_A", 3, 66.67, 2.5, -1.0, 5.0, 58)]
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def __init__(self): self.cur = Cursor()
            def cursor(self): return self.cur
        conn = Conn()
        rows = signal_performance(conn)
        assert [params[0] for params in conn.cur.params] == [1, 5, 10, 22]
        assert {r["horizon"] for r in rows} == {1, 5, 10, 22}
        assert rows[0]["best_broker"] == 58


class TestSymbolExclusion:
    """Promoter stocks (except HIDCLP/HEIP) and debentures are filtered out."""

    def test_promoter_suffix_excluded(self):
        from src.screener import _is_excluded
        assert _is_excluded("LECP") is True      # promoter
        assert _is_excluded("NABILP") is True    # promoter
        assert _is_excluded("HIDCLP") is False   # allowlisted
        assert _is_excluded("HEIP") is False     # allowlisted

    def test_debenture_with_digit_excluded(self):
        from src.screener import _is_excluded
        assert _is_excluded("H8020") is True     # debenture, digit in ticker
        assert _is_excluded("PRVU2084") is True  # debenture
        assert _is_excluded("NBLD83") is True    # debenture, digit embedded

    def test_regular_equities_included(self):
        from src.screener import _is_excluded
        assert _is_excluded("LEC") is False
        assert _is_excluded("NABIL") is False
        assert _is_excluded("PRVU") is False

    def test_excluded_symbols_override(self):
        from src.screener import EXCLUDED_SYMBOLS, _is_excluded
        EXCLUDED_SYMBOLS.append("GHOST")
        try:
            assert _is_excluded("GHOST") is True
        finally:
            EXCLUDED_SYMBOLS.remove("GHOST")


class TestWatchlistWriteWeb:
    """POST endpoints for mutating the personal watchlist (web.api_watch_write)."""

    @staticmethod
    def _dummy_handler():
        # api_watch_write never touches self, so a plain object suffices.
        from src import web

        return type("H", (object,), {})()

    def test_add_uppercases_and_passes_fields(self):
        from unittest import mock
        from src import web

        captured = {}

        class FakeConn:
            def close(self):
                pass

        def fake_get_conn():
            return FakeConn()

        def fake_add(conn, symbol, **kw):
            captured["symbol"] = symbol
            captured.update(kw)

        with mock.patch.object(web, "get_conn", fake_get_conn), mock.patch.object(
            web, "watch_add", fake_add
        ):
            res = web.Handler.api_watch_write(
                self._dummy_handler(),
                "add",
                {"symbol": " lec ", "thesis": "t", "tags": "mom", "note": "n"},
            )
        assert captured["symbol"] == "LEC"
        assert captured["thesis"] == "t"
        assert captured["note"] == "n"
        assert res == {"ok": True, "action": "add", "symbol": "LEC"}

    def test_missing_symbol_rejected(self):
        from unittest import mock
        from src import web

        with mock.patch.object(web, "get_conn"), mock.patch.object(web, "watch_add"):
            try:
                web.Handler.api_watch_write(self._dummy_handler(), "add", {})
            except ValueError as exc:
                assert "symbol" in str(exc)
            else:
                raise AssertionError("expected ValueError")

    def test_enter_coerces_numbers_and_closes_conn(self):
        from unittest import mock
        from src import web

        captured, closed = {}, []

        class FakeConn:
            def close(self):
                closed.append(True)

        def fake_enter(conn, symbol, entry_price, target_price=None, stop_price=None,
                       quantity=None, entry_date=None):
            captured.update(
                symbol=symbol, entry=entry_price, target=target_price,
                stop=stop_price, quantity=quantity, entry_date=entry_date,
            )
            return True

        with mock.patch.object(web, "get_conn", lambda: FakeConn()), mock.patch.object(
            web, "watch_enter", fake_enter
        ):
            res = web.Handler.api_watch_write(
                self._dummy_handler(),
                "enter",
                {"symbol": "LEC", "price": "115.5", "target": "130", "quantity": "50"},
            )
        assert captured == {
            "symbol": "LEC", "entry": 115.5, "target": 130.0,
            "stop": None, "quantity": 50, "entry_date": None,
        }
        assert closed == [True]
        assert res["ok"] is True

    def test_enter_requires_price(self):
        from unittest import mock
        from src import web

        with mock.patch.object(web, "get_conn"), mock.patch.object(web, "watch_enter"):
            try:
                web.Handler.api_watch_write(self._dummy_handler(), "enter", {"symbol": "LEC"})
            except ValueError as exc:
                assert "price" in str(exc)
            else:
                raise AssertionError("expected ValueError")

    def test_exit_rejects_bad_outcome(self):
        from unittest import mock
        from src import web

        with mock.patch.object(web, "get_conn"), mock.patch.object(web, "watch_exit"):
            try:
                web.Handler.api_watch_write(
                    self._dummy_handler(), "exit", {"symbol": "LEC", "price": 120, "outcome": "BANANA"}
                )
            except ValueError as exc:
                assert "outcome" in str(exc)
            else:
                raise AssertionError("expected ValueError")

    def test_unknown_action_rejected(self):
        from unittest import mock
        from src import web

        with mock.patch.object(web, "get_conn"):
            try:
                web.Handler.api_watch_write(self._dummy_handler(), "rename", {"symbol": "LEC"})
            except ValueError as exc:
                assert "unknown action" in str(exc)
            else:
                raise AssertionError("expected ValueError")
class TestAnalysisSignatures:
    """Pure logic for accumulation-signature classification and broker flows."""

    def test_classify_rule_table(self):
        from src.analysis import classify_signature
        assert classify_signature(500, 4) == "MULTI"      # >=3 net-buyers
        assert classify_signature(500, 1) == "SINGLE"     # single dominant buyer
        assert classify_signature(-300, 4) == "DISTRIBUTE"  # top broker sold
        assert classify_signature(200, 2) == "NEUTRAL"    # 2 positive -> no label
        assert classify_signature(None, None) == "NEUTRAL"  # no activity

    def test_daily_flows_top_and_breadth(self):
        import polars as pl
        from src.analysis import daily_flows
        roll = pl.DataFrame(
            [
                {"trade_date": date(2026, 9, 1), "symbol": "LEC", "broker_id": 1,
                 "buy_qty": 1000, "sell_qty": 0},
                {"trade_date": date(2026, 9, 1), "symbol": "LEC", "broker_id": 2,
                 "buy_qty": 500, "sell_qty": 0},
                {"trade_date": date(2026, 9, 1), "symbol": "LEC", "broker_id": 3,
                 "buy_qty": 200, "sell_qty": 0},
                {"trade_date": date(2026, 9, 1), "symbol": "LEC", "broker_id": 4,
                 "buy_qty": 0, "sell_qty": 900},
            ]
        )
        row = daily_flows(roll).row(0, named=True)
        assert row["top_accum_id"] == 1
        assert row["top_accum_net"] == 1000
        assert row["top_distrib_net"] == -900
        assert row["net_breadth"] == 3
        # concentration = |1000| / (1000+500+200+900)
        assert row["concentration"] == pytest.approx(1000 / 2600, abs=0.001)


class TestAnalysisStats:
    @staticmethod
    def _rows():
        # Ves: predictable rows with a signature and forward returns.
        rows = []
        for i, sig in enumerate(["MULTI", "MULTI", "SINGLE", "SINGLE", "DISTRIBUTE"]):
            rows.append({
                "trade_date": date(2026, 8, i + 1),
                "rank": (i % 5) + 1,
                "rank_pctile": (i % 5 + 1) / 20.0,
                "close": 100.0 + i,
                "signature": sig,
                "top_accum_id": 7 + (i % 3),
                "net_breadth": 1 if sig == "SINGLE" else 3,
                "sustained": i > 0,
                "fwd_1": [2.0, -1.0, 1.5, 0.0, -2.0][i],
                "fwd_3": [3.0, 1.0, 2.0, -1.0, -4.0][i],
            })
        return rows

    def test_forward_returns_win_rate(self):
        from src.analysis import forward_returns
        perf = forward_returns(self._rows())
        multi = perf["MULTI"]["horizons"]
        assert multi[1]["samples"] == 2
        assert multi[1]["win_rate_pct"] == 50.0   # 2.0 win, -1.0 loss
        assert multi[1]["avg_return_pct"] == pytest.approx(0.5)
        assert perf["DISTRIBUTE"]["horizons"][1]["avg_return_pct"] == -2.0
        assert perf["NEUTRAL"]["samples"] == 0

    def test_rank_price_relation_builds_buckets(self):
        from src.analysis import rank_price_relation
        rel = rank_price_relation(self._rows())
        assert rel["n"] == 5
        assert len(rel["buckets"]) == 3
        assert rel["spearman_rank_t1_return"] is not None
        names = {b["bucket"] for b in rel["buckets"]}
        assert {"LEADER", "MID", "MINOR"} == names

    def test_predict_next_uses_latest_signature(self):
        from src.analysis import predict_next
        preds = predict_next(self._rows())
        # latest predictable row is DISTRIBUTE (last in list) with fwd_3 filled
        assert preds[0]["signature"] == "DISTRIBUTE"
        assert preds[0]["bias"] == "BEARISH"        # hist avg negative
        assert preds[1]["horizon"] == 3
        assert preds[0]["confidence"] == "LOW"       # n=1 -> LOW

    def test_confidence_band(self):
        from src.analysis import confidence
        assert confidence(1) == "LOW"
        assert confidence(5) == "MEDIUM"
        assert confidence(20) == "HIGH"

    def test_cli_analyze_parser_wired(self):
        from src import cli
        parser = cli.build_parser()
        ns = parser.parse_args(["analyze", "LEC", "--sessions", "10"])
        assert ns.func is cli.cmd_analyze
        assert ns.symbol == "LEC"
        assert ns.sessions == 10
