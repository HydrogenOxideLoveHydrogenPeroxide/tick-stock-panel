from datetime import date
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import polars as pl
from app.services.backtest_history import BacktestHistoryRepository


def frame(day, close=10.):
    return pl.DataFrame(dict(symbol=['600000.SH'],date=[day],open=[close],high=[close+1],low=[close-1],close=[close],volume=[1000.],amount=[10000.]))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); folder=self.root/'backtest_history'; folder.mkdir()
        old=pl.concat([frame(date(2018,1,2)),frame(date(2026,1,2))]); old.write_parquet(folder/'old.parquet')
        old.with_columns(pl.lit('000001.SH').alias('symbol')).write_parquet(folder/'index.parquet')
        record=lambda name:dict(file=name,sha256=hashlib.sha256((folder/name).read_bytes()).hexdigest())
        (folder/'manifest.json').write_text(json.dumps(dict(complete=True,start='2018-01-02',end='2026-01-02',files=[record('old.parquet')],index=record('index.parquet'))))
        local=frame(date(2026,1,2),12.)
        self.repo=SimpleNamespace(store=SimpleNamespace(data_dir=self.root),
            get_raw_daily_range=lambda start,end,symbols=None:local.filter(pl.col('date').is_between(start,end)),
            get_index_daily=lambda *args,**kwargs:pl.DataFrame(),get_enriched_range=lambda *args,**kwargs:'local-only')
        self.history=BacktestHistoryRepository(self.repo)

    def test_old_dates_merged_and_local_rows_win(self):
        df=self.history.get_raw_daily_range(date(2018,1,1),date(2026,1,3))
        self.assertEqual(2,len(df)); self.assertEqual([10.,12.],df['close'].to_list())
        old=self.history.get_raw_daily_range(date(2018,1,1),date(2018,1,3),['600000.SH'])
        self.assertEqual(1,len(old))
        self.assertEqual('local-only',self.history.get_enriched_range(None,None))

    def test_benchmark_and_coverage_use_archive(self):
        df=self.history.get_index_daily('000001.SH',date(2018,1,1),date(2018,1,3),columns=['date','close'])
        self.assertEqual(['date','close'],df.columns); self.assertEqual(1,len(df))
        info=self.history.history_info(raw=True)
        self.assertEqual('2018-01-02',info['earliest_date']); self.assertTrue(info['archive_available'])
        self.assertFalse(self.history.history_info(raw=False)['archive_available'])

    def test_corrupt_archive_is_not_silently_used(self):
        (self.root/'backtest_history/old.parquet').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'指纹'):
            self.history.get_raw_daily_range(date(2018,1,1),date(2026,1,3))


if __name__=='__main__': unittest.main()
