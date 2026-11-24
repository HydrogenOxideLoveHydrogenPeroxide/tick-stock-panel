import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl

from app.services.watchlist import latest_local_stock_rows


class WatchlistLocalRecoveryTests(unittest.TestCase):
    def make_repo(self, root, latest):
        path = Path(root) / 'kline_daily' / f'date={latest}' / 'part.parquet'
        path.parent.mkdir(parents=True)
        dates = [latest - timedelta(days=79-i) for i in range(80)]
        prices = [5 + i * .01 for i in range(80)]
        raw = pl.DataFrame({'symbol': ['601988.SH'] * 80, 'date': dates,
            'open': prices, 'high': [p+.1 for p in prices], 'low': [p-.1 for p in prices],
            'close': prices, 'volume': [10000.] * 80, 'amount': [50000.] * 80})
        raw.write_parquet(path)
        repo = Mock()
        repo.store = SimpleNamespace(data_dir=Path(root))
        repo.get_raw_daily_range.return_value = raw
        repo.get_instruments.return_value = pl.DataFrame({'symbol': ['601988.SH'], 'name': ['中国银行']})
        repo.get_daily_batch.return_value = raw.filter(pl.col('date') < latest - timedelta(days=3)).select(['symbol', 'date'])
        return repo, raw

    def test_raw_quotes_fill_missing_metrics_and_only_persist_new_history(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = date.today() - timedelta(days=1)
            repo, raw = self.make_repo(directory, latest)
            result = latest_local_stock_rows(repo, ['601988.SH'], latest)
        self.assertEqual(1, result.height)
        self.assertEqual(raw['close'][-1], result['close'][0])
        self.assertIsNotNone(result['rsi_14'][0])
        self.assertEqual(latest, result['date'][0])
        self.assertEqual('local_daily', result['quote_source'][0])
        self.assertEqual('none', result['adjustment'][0])
        self.assertEqual(4, repo.append_enriched.call_args.args[0].height)
        self.assertEqual('stock', repo.merge_live_enriched_asset.call_args.args[0])

    def test_older_symbol_does_not_move_global_latest_cache_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = date.today() - timedelta(days=5)
            repo, _ = self.make_repo(directory, latest)
            result = latest_local_stock_rows(repo, ['601988.SH'], date.today())
        self.assertEqual(latest, result['date'][0])
        repo.merge_live_enriched_asset.assert_not_called()

    def test_no_local_history_stays_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Mock()
            repo.store = SimpleNamespace(data_dir=Path(directory))
            result = latest_local_stock_rows(repo, ['601988.SH'], date.today())
        self.assertTrue(result.is_empty())
        repo.append_enriched.assert_not_called()
