import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch
import polars as pl
from app.api.watchlist import watchlist_enriched, _watchlist_instruments


class WatchlistETFDisplayTests(unittest.TestCase):
    def test_etf_name_survives_stock_name_column_and_join(self):
        repo=Mock()
        repo.get_enriched_latest.return_value=(pl.DataFrame({'symbol':['002685.SZ'],'name':['华东重机'],'close':[4.51]}),date(2026,11,2))
        repo.get_enriched_latest_asset.return_value=(pl.DataFrame({'symbol':['513100.SH'],'close':[2.239],'momentum_60d':[.12]}),date(2026,10,30))
        repo.get_instruments.return_value=pl.DataFrame({'symbol':['002685.SZ'],'name':['华东重机']})
        repo.get_etf_instruments.return_value=pl.DataFrame({'symbol':['513100.SH'],'name':['纳指ETF国泰']})
        req=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
        with patch('app.api.watchlist.watchlist.list_symbols',return_value=[{'symbol':'513100.SH'},{'symbol':'002685.SZ'}]):
            rows=watchlist_enriched(req,ext_columns=None)['rows']
        self.assertEqual('纳指ETF国泰',rows[0]['name'])
        self.assertEqual(.12,rows[0]['momentum_60d'])
        self.assertEqual('etf',rows[0]['asset_type'])
        self.assertIsNone(rows[0].get('turnover_rate'))
        self.assertEqual('华东重机',rows[1]['name'])
        self.assertEqual('stock',rows[1]['asset_type'])

    def test_legacy_blank_name_does_not_hide_etf_reference_name(self):
        repo=Mock()
        repo.get_instruments.return_value=pl.DataFrame({'symbol':['513100.SH'],'name':[' ']})
        repo.get_etf_instruments.return_value=pl.DataFrame({'symbol':['513100.SH'],'name':['纳指ETF国泰']})
        self.assertEqual('纳指ETF国泰',_watchlist_instruments(repo)['name'][0])
