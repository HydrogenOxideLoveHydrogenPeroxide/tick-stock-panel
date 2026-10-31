import unittest
from types import SimpleNamespace
from datetime import date
from unittest.mock import Mock
import polars as pl
from app.api.kline import search_instruments, instruments_names, get_daily_batch

class ETFTests(unittest.TestCase):
    def setUp(self):
        self.repo=Mock()
        self.repo.get_instruments.return_value=pl.DataFrame({'symbol':['000001.SZ'],'name':['平安银行'],'code':['000001']})
        self.repo.get_etf_instruments.return_value=pl.DataFrame({'symbol':['513100.SH'],'name':['纳指ETF国泰'],'code':['513100']})
        self.req=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=self.repo)))
    def test_search(self):
        for q in ['513100','纳指国泰ETF','国泰 纳指','纳指ETF国泰']:
            self.assertEqual(search_instruments(self.req,q,20)['results'][0]['symbol'],'513100.SH')
        self.assertEqual(search_instruments(self.req,'平安',20)['results'][0]['symbol'],'000001.SZ')
        self.assertEqual(search_instruments(self.req,'不存在',20)['results'],[])
    def test_names(self):
        self.assertEqual(instruments_names(self.req,['513100.SH'])['names']['513100.SH'],'纳指ETF国泰')
    def test_batch(self):
        def frame(s): return pl.DataFrame({'symbol':[s],'date':[date.today()],'open':[1.0],'high':[2.0],'low':[1.0],'close':[2.0],'volume':[100.0]})
        self.repo.get_daily_batch.return_value=frame('000001.SZ')
        self.repo.get_etf_daily.return_value=frame('513100.SH')
        result=get_daily_batch(self.req,{'symbols':['000001.SZ','513100.SH'],'days':12})
        self.assertEqual(set(result['data']),{'000001.SZ','513100.SH'})
        self.assertEqual(self.repo.get_daily_batch.call_args.args[0],['000001.SZ'])

if __name__=='__main__': unittest.main()
