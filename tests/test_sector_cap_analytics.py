"""Unit tests for sector classification, cap tier segregation, 52-week channels, and VWAP."""

from datetime import date, timedelta
from unittest import mock
import pathlib
import polars as pl
import pytest

from src.ingestion import compute_daily_market_summary
from src.screener import (
    classify_cap_tier,
    screen_track_a,
    screen_track_b,
    load_top_turnover,
    inspect_symbol,
)
from src import cli, web


class TestCapTierClassification:
    def test_classify_cap_tier_thresholds(self):
        assert classify_cap_tier(None) == "UNKNOWN"
        assert classify_cap_tier(20000.0) == "LARGE"
        assert classify_cap_tier(150000.0) == "LARGE"
        assert classify_cap_tier(19999.9) == "MID"
        assert classify_cap_tier(5000.0) == "MID"
        assert classify_cap_tier(4999.9) == "SMALL"
        assert classify_cap_tier(1200.0) == "SMALL"
        assert classify_cap_tier("invalid") == "UNKNOWN"


class TestSummaryComputationEnrichment:
    def test_compute_daily_market_summary_vwap_and_meta(self):
        d = date(2026, 9, 11)
        df = pl.DataFrame(
            [
                {
                    "trade_date": d,
                    "contract_id": 1,
                    "symbol": "LEC",
                    "buyer_broker": 1,
                    "seller_broker": 2,
                    "quantity": 100,
                    "rate": 400.0,
                    "amount": 40000.0,
                },
                {
                    "trade_date": d,
                    "contract_id": 2,
                    "symbol": "LEC",
                    "buyer_broker": 3,
                    "seller_broker": 4,
                    "quantity": 200,
                    "rate": 430.0,
                    "amount": 86000.0,
                },
            ]
        )
        meta = {
            "LEC": {
                "sector": "Hydro Power",
                "market_cap": 4500.0,
                "fifty_two_week_high": 550.0,
                "fifty_two_week_low": 300.0,
            }
        }
        summary = compute_daily_market_summary(df, meta=meta)
        assert summary.height == 1
        row = summary.row(0, named=True)
        assert row["symbol"] == "LEC"

class TestScreenerSectorAndCapFilters:
    def test_screen_track_a_with_sector_and_cap_tier(self):
        d1 = date(2026, 9, 11)
        windows = {"T_1": [d1], "T_5": [d1], "T_22": [d1], "T_66": [d1]}
        summary = pl.DataFrame(
            [
                {
                    "trade_date": d1,
                    "symbol": "NABIL",
                    "close_price": 520.0,
                    "price_change_pct": 1.5,
                    "total_qty": 50000,
                    "total_turnover": 26000000.0,
                    "turnover_rank": 1,
                    "sector": "Commercial Banks",
                    "market_cap": 145000.0,
                    "fifty_two_week_high": 650.0,
                    "fifty_two_week_low": 420.0,
                    "vwap": 518.0,
                },
                {
                    "trade_date": d1,
                    "symbol": "LEC",
                    "close_price": 420.0,
                    "price_change_pct": 0.5,
                    "total_qty": 40000,
                    "total_turnover": 16800000.0,
                    "turnover_rank": 2,
                    "sector": "Hydro Power",
                    "market_cap": 4500.0,
                    "fifty_two_week_high": 540.0,
                    "fifty_two_week_low": 310.0,
                    "vwap": 419.0,
                },
            ]
        )
        rollup = pl.DataFrame(
            [
                {
                    "trade_date": d1,
                    "symbol": "NABIL",
                    "broker_id": 58,
                    "buy_qty": 10000,
                    "buy_amount": 5200000.0,
                    "sell_qty": 1000,
                    "sell_amount": 520000.0,
                    "self_trade_qty": 0,
                    "matched_qty": 0,
                },
                {
                    "trade_date": d1,
                    "symbol": "LEC",
                    "broker_id": 58,
                    "buy_qty": 15000,
                    "buy_amount": 6300000.0,
                    "sell_qty": 500,
                    "sell_amount": 210000.0,
                    "self_trade_qty": 0,
                    "matched_qty": 0,
                },
            ]
        )

        # Unfiltered Track A
        res_all = screen_track_a(rollup, summary, windows)
        assert len(res_all) == 2
        symbols = [r["symbol"] for r in res_all]
        assert "NABIL" in symbols and "LEC" in symbols
        for r in res_all:
            if r["symbol"] == "NABIL":
                assert r["cap_tier"] == "LARGE"
                assert r["sector"] == "Commercial Banks"
            elif r["symbol"] == "LEC":
                assert r["cap_tier"] == "SMALL"
                assert r["sector"] == "Hydro Power"

        # Filter by sector = "Hydro"
        res_hydro = screen_track_a(rollup, summary, windows, sector="Hydro")
        assert len(res_hydro) == 1
        assert res_hydro[0]["symbol"] == "LEC"

        # Filter by cap_tier = "LARGE"
        res_large = screen_track_a(rollup, summary, windows, cap_tier="LARGE")
        assert len(res_large) == 1


    def test_screen_track_b_with_sector_and_cap_tier(self):
        d_dates = [date(2026, 8, 1) + timedelta(days=i) for i in range(22)]
        t1 = [d_dates[-1]]
        windows = {"T_1": t1, "T_22": d_dates}

        # Build summary for HIDCL
        summary_rows = []
        for d in d_dates:
            summary_rows.append(
                {
                    "trade_date": d,
                    "symbol": "HIDCL",
                    "close_price": 185.0,
                    "price_change_pct": 0.1,
                    "total_qty": 10000,
                    "total_turnover": 1850000.0 if d != t1[0] else 5000000.0,
                    "turnover_rank": 35,
                    "sector": "Investment",
                    "market_cap": 42000.0,
                    "fifty_two_week_high": 250.0,
                    "fifty_two_week_low": 140.0,
                    "vwap": 184.5,
                }
            )
        summary = pl.DataFrame(summary_rows)

        # Build rollup for HIDCL: broker 41 absorbs 30% of shares
        rollup_rows = []
        for d in d_dates:
            rollup_rows.append(
                {
                    "trade_date": d,
                    "symbol": "HIDCL",
                    "broker_id": 41,
                    "buy_qty": 3000,
                    "buy_amount": 555000.0,
                    "sell_qty": 0,
                    "sell_amount": 0.0,
                    "self_trade_qty": 0,
                    "matched_qty": 0,
                }
            )
            # 20 fragmented sellers (150 qty each) -> top 3 sellers have 15% share (<25%)
            for b in range(1, 21):
                rollup_rows.append(
                    {
                        "trade_date": d,
                        "symbol": "HIDCL",
                        "broker_id": b,
                        "buy_qty": 0,
                        "buy_amount": 0.0,
                        "sell_qty": 150,
                        "sell_amount": 27750.0,
                        "self_trade_qty": 0,
                        "matched_qty": 0,
                    }
                )
        rollup = pl.DataFrame(rollup_rows)

        res_b = screen_track_b(rollup, summary, windows)
        assert len(res_b) == 1
        assert res_b[0]["symbol"] == "HIDCL"
        assert res_b[0]["sector"] == "Investment"
        assert res_b[0]["cap_tier"] == "LARGE"

        # Sector filter match
        res_inv = screen_track_b(rollup, summary, windows, sector="Investment")
        assert len(res_inv) == 1
        # Sector filter mismatch
        res_hydro = screen_track_b(rollup, summary, windows, sector="Hydro Power")
        assert len(res_hydro) == 0

        # Cap tier match
        res_large = screen_track_b(rollup, summary, windows, cap_tier="LARGE")
        assert len(res_large) == 1
        # Cap tier mismatch
        res_small = screen_track_b(rollup, summary, windows, cap_tier="SMALL")
        assert len(res_small) == 0


class TestInspectSymbolDiagnostics:
    def test_inspect_symbol_returns_52w_and_sector_metrics(self):
        d1 = date(2026, 9, 11)
        summary_rows = [
            {
                "trade_date": d1,
                "symbol": "NABIL",
                "close_price": 500.0,
                "price_change_pct": 2.0,
                "total_qty": 10000,
                "total_turnover": 5000000.0,
                "turnover_rank": 1,
                "sector": "Commercial Banks",
                "market_cap": 145000.0,
                "fifty_two_week_high": 600.0,
                "fifty_two_week_low": 400.0,
                "vwap": 498.5,
            }
        ]
        rollup_rows = [
            {
                "trade_date": d1,
                "symbol": "NABIL",
                "broker_id": 58,
                "buy_qty": 5000,
                "buy_amount": 2500000.0,
                "sell_qty": 0,
                "sell_amount": 0.0,
                "self_trade_qty": 0,
                "matched_qty": 0,
            }
        ]

        with (
            mock.patch("src.screener.get_conn") as mock_conn,
            mock.patch("src.screener.fetch_trade_dates", return_value=[d1]),
            mock.patch("src.screener.load_summary", return_value=pl.DataFrame(summary_rows)),
            mock.patch("src.screener.load_rollup", return_value=pl.DataFrame(rollup_rows)),
            mock.patch("src.signals.load_signal_history", return_value=[]),
        ):
            mock_conn.return_value = type("FakeConn", (), {"close": lambda s: None})()
            res = inspect_symbol("NABIL", sessions=10)

        assert res["symbol"] == "NABIL"
        assert res["sector"] == "Commercial Banks"
        assert res["market_cap"] == 145000.0
        assert res["cap_tier"] == "LARGE"
        assert res["fifty_two_week_high"] == 600.0
        assert res["fifty_two_week_low"] == 400.0
        assert res["vwap"] == 498.5
        # Distance to high = (500 - 600) / 600 * 100 = -16.67%
        assert res["distance_52w_high_pct"] == pytest.approx(-16.67, 0.01)
        # Distance to low = (500 - 400) / 400 * 100 = +25.0%
        assert res["distance_52w_low_pct"] == 25.0
        # Range position = (500 - 400) / (600 - 400) * 100 = 50.0%
        assert res["range_52w_pct"] == 50.0

    def test_inspect_symbol_empty_returns_unknown_tier(self):
        with (
            mock.patch("src.screener.get_conn") as mock_conn,
            mock.patch("src.screener.fetch_trade_dates", return_value=[]),
            mock.patch("src.screener.load_summary", return_value=pl.DataFrame()),
            mock.patch("src.screener.load_rollup", return_value=pl.DataFrame()),
        ):
            mock_conn.return_value = type("FakeConn", (), {"close": lambda s: None})()
            res = inspect_symbol("NONEXISTENT", sessions=10)
        assert res["symbol"] == "NONEXISTENT"
        assert res["cap_tier"] == "UNKNOWN"
        assert res["recent"] == []

    def test_load_top_turnover_sector_and_cap_filtering(self):
        d1 = date(2026, 9, 11)
        mock_cursor = mock.MagicMock()
        mock_cursor.fetchone.side_effect = [(d1,), (d1,)]
        mock_cursor.fetchall.return_value = [
            ("NABIL", 520.0, 1.5, 50000, 26000000.0, 1, "Commercial Banks", 145000.0, 650.0, 420.0, 518.0),
            ("LEC", 420.0, 0.5, 40000, 16800000.0, 2, "Hydro Power", 4500.0, 540.0, 310.0, 419.0),
        ]
        mock_conn = mock.MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Unfiltered
        res_all = load_top_turnover(mock_conn, as_of=d1, limit=10)
        assert len(res_all["rows"]) == 2
        assert res_all["rows"][0]["cap_tier"] == "LARGE"
        assert res_all["rows"][1]["cap_tier"] == "SMALL"

        # Filter sector
        mock_cursor.fetchone.side_effect = [(d1,), (d1,)]
        res_hydro = load_top_turnover(mock_conn, as_of=d1, limit=10, sector="Hydro")
        assert len(res_hydro["rows"]) == 1
        assert res_hydro["rows"][0]["symbol"] == "LEC"

        # Filter cap tier
        mock_cursor.fetchone.side_effect = [(d1,), (d1,)]
        res_large = load_top_turnover(mock_conn, as_of=d1, limit=10, cap_tier="LARGE")
        assert len(res_large["rows"]) == 1
        assert res_large["rows"][0]["symbol"] == "NABIL"


class TestCLIAndWebSectorCapFiltering:
    def test_cli_parser_sector_and_cap_tier_flags(self):
        parser = cli.build_parser()
        args = parser.parse_args(["run", "--sector", "Hydro Power", "--cap-tier", "MID"])
        assert args.sector == "Hydro Power"
        assert args.cap_tier == "MID"

        args_top = parser.parse_args(["top", "--sector", "Commercial Banks", "--cap-tier", "LARGE"])
        assert args_top.sector == "Commercial Banks"
        assert args_top.cap_tier == "LARGE"

    def test_web_api_top_and_run_with_sector_and_cap(self):
        captured = {}

        def fake_load_top(conn, as_of=None, limit=20, sector=None, cap_tier=None):
            captured["top_sector"] = sector
            captured["top_cap"] = cap_tier
            return {"date": "2026-09-11", "rows": []}

        def fake_run(as_of=None, persist=False, top_turnover=20, top_holder_window=22, sector=None, cap_tier=None):
            captured["run_sector"] = sector
            captured["run_cap"] = cap_tier
            return [], [], {}

        handler = object.__new__(web.Handler)
        with (
            mock.patch.object(web, "get_conn", lambda: type("C", (), {"close": lambda s: None})()),
            mock.patch.object(web.reports, "load_snapshot", lambda *a, **k: None),
            mock.patch.object(web.reports, "save_snapshot", lambda *a, **k: None),
            mock.patch.object(web, "load_top_turnover", fake_load_top),
            mock.patch.object(web, "run_screener", fake_run),
        ):
            web.Handler.api_top(handler, {"sector": ["Hydro Power"], "cap_tier": ["small"]})
            assert captured["top_sector"] == "Hydro Power"
            assert captured["top_cap"] == "SMALL"

            web.Handler.api_run(handler, {"sector": ["Commercial Banks"], "cap_tier": ["large"]})
            assert captured["run_sector"] == "Commercial Banks"
            assert captured["run_cap"] == "LARGE"

    def test_load_top_turnover_legacy_schema_fallback(self):
        d1 = date(2026, 9, 11)
        mock_cursor = mock.MagicMock()
        mock_conn = mock.MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # First query: MAX(trade_date)
        # Second query: trade_date <= %s
        mock_cursor.fetchone.side_effect = [(d1,), (d1,)]
        # Third query fails (undefined column in legacy schema), fourth query succeeds
        def mock_execute(sql, params=None):
            if "sector, market_cap" in sql:
                raise Exception("column sector does not exist")
            return None

        mock_cursor.execute.side_effect = mock_execute
        mock_cursor.fetchall.return_value = [
            ("NABIL", 120.0, 1.5, 1000, 120000.0, 1),
            ("LEC", 80.0, -0.5, 500, 40000.0, None),  # None turnover rank test
        ]

        res = load_top_turnover(mock_conn, as_of=d1, limit=10)
        assert res["date"] == "2026-09-11"
        assert len(res["rows"]) == 2
        assert res["rows"][0]["symbol"] == "NABIL"
        assert res["rows"][0]["sector"] is None
        assert res["rows"][0]["cap_tier"] == "UNKNOWN"
        assert res["rows"][0]["rank"] == 1
        assert res["rows"][1]["symbol"] == "LEC"
        assert res["rows"][1]["rank"] == 2  # Fallback to index + 1

    def test_save_snapshot_permission_error_tolerance(self, monkeypatch, tmp_path):
        from src import reports

        monkeypatch.setattr(reports, "REPORTS_DIR", tmp_path)
        monkeypatch.setattr(reports, "_fingerprint", lambda: "fp123")
        monkeypatch.setattr(reports, "snapshot_path", lambda c, p: tmp_path / "test.json")

        def broken_write_text(*args, **kwargs):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(pathlib.Path, "write_text", broken_write_text)

        # Should not raise exception
        p = reports.save_snapshot("top", {"limit": 20}, {"rows": []})
        assert p == tmp_path / "test.json"

    def test_fetch_and_sync_securities_metadata_merge(self, monkeypatch):
        from src import fetcher

        class FakeScraper:
            def __init__(self, *a, **k):
                pass

            def get_all_securities(self):
                return [
                    {"symbol": "NABIL", "companyName": "Nabil Bank Ltd", "sectorName": "Commercial Banks", "instrumentType": "Equity"},
                    {"symbol": "LEC", "companyName": "Liberty Energy Ltd", "sectorName": "Hydro Power", "instrumentType": "Equity"},
                ]

            def get_today_price(self):
                return [
                    {"symbol": "NABIL", "marketCapitalization": 145000.0, "fiftyTwoWeekHigh": 700.0, "fiftyTwoWeekLow": 400.0},
                    {"symbol": "LEC", "marketCapitalization": 4500.0, "fiftyTwoWeekHigh": 550.0, "fiftyTwoWeekLow": 300.0},
                ]

        monkeypatch.setattr(fetcher, "NepseScraper", FakeScraper)
        monkeypatch.setattr("psycopg2.extras.execute_values", lambda cur, sql, rows: None)
        mock_cursor = mock.MagicMock()
        mock_conn = mock.MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        monkeypatch.setattr("src.db.get_conn", lambda: mock_conn)

        count = fetcher.fetch_and_sync_securities_metadata()
        assert count == 2
        assert mock_conn.commit.called

class TestPositionAndSectors:
    def test_api_sectors_query(self, monkeypatch):
        handler = object.__new__(web.Handler)
        mock_cursor = mock.MagicMock()
        mock_conn = mock.MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [("Commercial Banks",), ("Hydro Power",)]
        monkeypatch.setattr(web, "get_conn", lambda: mock_conn)
        
        res = handler.api_sectors({})
        assert not res["cached"]
        assert "Commercial Banks" in res["data"]
        assert "Hydro Power" in res["data"]
        sql_called = mock_cursor.execute.call_args[0][0]
        assert "TRIM(sector) != ''" in sql_called

    def test_position_analysis_share_structure_fallback_and_zero_div(self, monkeypatch):
        from src import screener
        d1 = date(2026, 9, 11)
        
        # Test case where public shares calculation might hit zero or fallback
        summary_rows = [
            {
                "trade_date": d1,
                "symbol": "NABIL",
                "close_price": 500.0,
                "price_change_pct": 0.0,
                "total_qty": 1000,
                "total_turnover": 500000.0,
                "turnover_rank": 1,
                "sector": "Commercial Banks",
                "market_cap": 0.0, # Will trigger fallback total_shares 10,000,000
                "fifty_two_week_high": 600.0,
                "fifty_two_week_low": 400.0,
                "vwap": 500.0,
            }
        ]
        
        with (
            mock.patch.object(screener, "get_conn", lambda: type("FakeConn", (), {"close": lambda s: None, "cursor": lambda s: mock.MagicMock(), "rollback": lambda s: None})()),
            mock.patch.object(screener, "fetch_trade_dates", return_value=[d1]),
            mock.patch.object(screener, "load_summary", return_value=pl.DataFrame(summary_rows)),
            mock.patch.object(screener, "load_rollup", return_value=pl.DataFrame()),
        ):
            res = screener.position_analysis("NABIL")
            assert "share_structure" in res
            ss = res["share_structure"]
            assert ss["total_shares"] == 10_000_000 # Fallback
            assert ss["public_shares"] == int(10_000_000 * 0.49) # Commercial bank default
            assert ss["float_turnover_pct"] == round((1000 / ss["public_shares"]) * 100, 2)
            
    def test_position_analysis_share_structure_normal(self, monkeypatch):
        from src import screener
        d1 = date(2026, 9, 11)
        
        summary_rows = [
            {
                "trade_date": d1,
                "symbol": "LEC",
                "close_price": 250.0,
                "price_change_pct": 5.0,
                "total_qty": 50000,
                "total_turnover": 12500000.0,
                "turnover_rank": 5,
                "sector": "Hydro Power",
                "market_cap": 2500.0, # 2.5B, meaning total shares = (2500 * 1m)/250 = 10,000,000
                "fifty_two_week_high": 300.0,
                "fifty_two_week_low": 150.0,
                "vwap": 245.0,
            }
        ]
        
        with (
            mock.patch.object(screener, "get_conn", lambda: type("FakeConn", (), {"close": lambda s: None, "cursor": lambda s: mock.MagicMock(), "rollback": lambda s: None})()),
            mock.patch.object(screener, "fetch_trade_dates", return_value=[d1]),
            mock.patch.object(screener, "load_summary", return_value=pl.DataFrame(summary_rows)),
            mock.patch.object(screener, "load_rollup", return_value=pl.DataFrame()),
        ):
            res = screener.position_analysis("LEC")
            ss = res["share_structure"]
            assert ss["total_shares"] == 10_000_000
            assert ss["public_shares"] == 3_000_000 # Hydro power default (30%)
            assert ss["promoter_shares"] == 7_000_000
            assert ss["float_turnover_pct"] == round((50000 / 3000000) * 100, 2)
            assert ss["public_ratio_pct"] == 30.0


    def test_fetch_page_retry_on_401(self, monkeypatch):
        from src import fetcher

        class DummySession:
            def __init__(self):
                self.calls = 0
                self.refreshed = 0

            def _get_access_token(self):
                self.refreshed += 1

            def post(self, url, params=None, payload=None):
                self.calls += 1
                if self.calls == 1:
                    raise Exception("401 Client Error: Unauthorized")
                resp_mock = mock.MagicMock()
                resp_mock.json.return_value = {"floorsheets": {"content": [], "totalPages": 1}}
                return resp_mock

        dummy_scraper = mock.MagicMock()
        dummy_session = DummySession()
        dummy_scraper.session = dummy_session

        monkeypatch.setattr(fetcher, "server_payload_id", lambda s: 9999)
        monkeypatch.setattr(fetcher.rate_limiter, "is_allowed", lambda ip, ep: (True, {}))

        data, pid = fetcher._fetch_page(dummy_scraper, "2026-09-16", 1234, 0)
        assert dummy_session.calls == 2
        assert dummy_session.refreshed == 1
        assert pid == 9999
        assert data == {"floorsheets": {"content": [], "totalPages": 1}}

    def test_load_top_turnover_distinct_dates(self, monkeypatch):
        from src import screener

        d1 = date(2026, 9, 15)
        d2 = date(2026, 9, 16)

        class MockCursor:
            def __init__(self):
                self.query = ""
                self.params = None

            def execute(self, sql, params=None):
                self.query = sql
                self.params = params

            def fetchone(self):
                if "MAX(trade_date)" in self.query:
                    return (d2,)
                if "WHERE trade_date <=" in self.query:
                    return (self.params[0],)
                return None

            def fetchall(self):
                if self.params == (d1,):
                    return [
                        ("RSML", 2879.0, 2.0, 100000, 327007876.4, 1, "Manufacturing And Processing", 54701.0, 3000.0, 2000.0, 2850.0),
                    ]
                elif self.params == (d2,):
                    return [
                        ("HDHPC", 228.0, 5.3, 1500000, 379564213.9, 1, "Hydro Power", 6062.0, 250.0, 180.0, 225.0),
                    ]
                return []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        class MockConn:
            def cursor(self):
                return MockCursor()
            def rollback(self):
                pass

        res_15 = screener.load_top_turnover(MockConn(), as_of=d1, limit=5)
        res_16 = screener.load_top_turnover(MockConn(), as_of=d2, limit=5)

        assert res_15["date"] == "2026-09-15"
        assert res_15["rows"][0]["symbol"] == "RSML"
        assert res_15["rows"][0]["turnover"] == 327007876.4

        assert res_16["date"] == "2026-09-16"
        assert res_16["rows"][0]["symbol"] == "HDHPC"
        assert res_16["rows"][0]["turnover"] == 379564213.9
        assert res_15["rows"][0]["turnover"] != res_16["rows"][0]["turnover"]

