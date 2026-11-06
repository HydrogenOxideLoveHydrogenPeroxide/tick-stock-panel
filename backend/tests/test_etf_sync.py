import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import polars as pl
from fastapi import HTTPException
from app.api.kline import sync_symbol, sync_watchlist_etfs
from app.services.pipeline_jobs import JobStore
from app.tickflow.repository import DataStore, KlineRepository


class ETFSyncTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.repo.get_etf_instruments.return_value = pl.DataFrame({'symbol': ['513100.SH']})
        self.caps = Mock()
        self.caps.has.return_value = True
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=self.repo, capabilities=self.caps)))

    def test_single_etf_does_not_write_stock_storage(self):
        with patch('app.services.index_sync.sync_and_persist_etf_daily', return_value=240) as etf, patch('app.services.kline_sync.sync_and_persist_daily_batch') as stock:
            result = sync_symbol(self.request, '513100.SH', 365)
        self.assertEqual(result['rows_written'], 240)
        self.assertEqual(etf.call_args.kwargs['symbols_override'], ['513100.SH'])
        self.assertEqual((etf.call_args.kwargs['end_date'] - etf.call_args.kwargs['start_date']).days, 365)
        stock.assert_not_called()

    def test_source_empty_is_error(self):
        with patch('app.services.index_sync.sync_and_persist_etf_daily', return_value=0):
            with self.assertRaises(HTTPException) as error:
                sync_symbol(self.request, '513100.SH', 365)
        self.assertEqual(error.exception.status_code, 502)

    def test_watchlist_job_ignores_auto_toggle_and_excludes_stocks(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = JobStore(store_dir=Path(directory))
                with patch('app.services.pipeline_jobs.job_store', store), patch('app.services.watchlist.list_symbols', return_value=[{'symbol': '513100.SH'}, {'symbol': '002685.SZ'}]), patch('app.services.index_sync.sync_and_persist_etf_daily', return_value=240) as sync:
                    result = await sync_watchlist_etfs(self.request, 365)
                    for _ in range(100):
                        job = store.get(result['job_id'])
                        if job['status'] in ('succeeded', 'failed'):
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(job['status'], 'succeeded')
                    self.assertEqual(sync.call_args.kwargs['symbols_override'], ['513100.SH'])
                    self.assertEqual(job['result']['etf_daily_rows'], 240)
        asyncio.run(run())

    def test_no_watchlist_etf_job_fails(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = JobStore(store_dir=Path(directory))
                with patch('app.services.pipeline_jobs.job_store', store), patch('app.services.watchlist.list_symbols', return_value=[{'symbol': '002685.SZ'}]), patch('app.services.index_sync.sync_and_persist_etf_daily') as sync:
                    result = await sync_watchlist_etfs(self.request, 365)
                    for _ in range(100):
                        job = store.get(result['job_id'])
                        if job['status'] in ('succeeded', 'failed'):
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(job['status'], 'failed')
                    sync.assert_not_called()
        asyncio.run(run())

    def test_written_etf_invalidates_old_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = KlineRepository(DataStore(Path(directory)))
            repo._etf_enriched_cache = pl.DataFrame({'close': [1.]})
            repo._etf_live_agg_cache = pl.DataFrame({'close': [1.]})
            repo.append_etf_enriched(pl.DataFrame({'symbol': ['513100.SH'], 'date': [date(2026, 11, 2)], 'close': [2.239]}))
            self.assertIsNone(repo._etf_enriched_cache)
            self.assertIsNone(repo._etf_live_agg_cache)
            self.assertTrue((Path(directory) / 'kline_etf_enriched/date=2026-11-02/part.parquet').exists())
            self.assertFalse(list((Path(directory) / 'kline_daily').rglob('*.parquet')))
