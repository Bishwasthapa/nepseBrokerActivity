"""Tests for the broker alpha and accumulation intelligence engine."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
import polars as pl
import pandas as pd
from unittest.mock import MagicMock, patch

from src.ml.broker_alpha import analyze_broker_accumulation, get_top_broker_alpha_leaderboard

class TestBrokerAlpha(unittest.TestCase):
    @patch('src.ml.broker_alpha.pd.read_sql')
    def test_analyze_broker_accumulation_empty(self, mock_read_sql):
        mock_read_sql.return_value = pd.DataFrame()
        conn = MagicMock()
        df = analyze_broker_accumulation(conn)
        self.assertTrue(df.empty)

    @patch('src.ml.broker_alpha.pd.read_sql')
    def test_analyze_broker_accumulation_data(self, mock_read_sql):
        mock_read_sql.return_value = pd.DataFrame({
            'symbol': ['SHIVM', 'NICA'],
            'broker_id': [58, 44],
            'net_5d': [5000, -2000],
            'net_22d': [20000, 10000],
            'net_66d': [50000, 30000],
            'avg_buy_price': [600.0, 800.0],
            'current_price': [676.3, 850.0]
        })
        conn = MagicMock()
        df = analyze_broker_accumulation(conn, min_net_qty=1000)
        self.assertEqual(len(df), 2)
        self.assertIn('unrealized_margin_pct', df.columns)
        self.assertIn('broker_pattern', df.columns)
        self.assertEqual(df.loc[df['symbol'] == 'SHIVM', 'broker_pattern'].values[0], 'STEALTH_ACCUMULATION')
        self.assertEqual(df.loc[df['symbol'] == 'NICA', 'broker_pattern'].values[0], 'DISTRIBUTION_EXIT')
