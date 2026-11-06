from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.services import quant_assistant as q
from app.api.quant_assistant import router


class TradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = patch.object(q.settings, 'data_dir', self.root); self.config.start(); self.addCleanup(self.config.stop)
        self.initial = q.Portfolio(cash=10000, holdings=[q.Holding(symbol='600000', name='测试',quantity=100, available_quantity=50, cost_price=10)])
        q.save_portfolio(self.initial)

    def payload(self, **changes):
        data = dict(client_id='test-request-0001',suggestion_key='snapshot|600000|buy',expected_portfolio=self.initial,
                    symbol='600000',side='buy',quantity=100,price=12,fees=5,
                    executed_at=datetime.now(q.SHANGHAI)-timedelta(minutes=1))
        data.update(changes)
        return q.TradeRequest(**data)

    def test_buy_weighted_cost_fees_and_idempotent_retry(self):
        request=self.payload(); result=q.record_trade(request)
        h=result['portfolio'].holdings[0]
        self.assertEqual(8795,result['portfolio'].cash)
        self.assertEqual((200,50,11.025),(h.quantity,h.available_quantity,h.cost_price))
        self.assertTrue(q.record_trade(request)['duplicate'])
        self.assertEqual(1,len(q.load_trades()))
        self.assertEqual(8795,q.load_portfolio().cash)
        with self.assertRaises(q.TradeConflict): q.record_trade(request.model_copy(update={'quantity':101}))

    def test_sell_cash_available_and_full_close(self):
        result=q.record_trade(self.payload(side='sell',quantity=50,price=11,fees=1))
        h=result['portfolio'].holdings[0]
        self.assertEqual((50,0,10),(h.quantity,h.available_quantity,h.cost_price))
        self.assertEqual(10549,result['portfolio'].cash)
        full=self.initial.model_copy(deep=True); full.holdings[0].available_quantity=100; q.save_portfolio(full)
        result=q.record_trade(self.payload(client_id='test-request-0002',suggestion_key='other',expected_portfolio=full,side='sell',quantity=100,price=11,fees=1))
        h=result['portfolio'].holdings[0]
        self.assertEqual((0,0,0),(h.quantity,h.available_quantity,h.cost_price))

    def test_three_decimal_etf_price_is_not_rounded_to_cents_before_multiplication(self):
        p=q.Portfolio(cash=10000,holdings=[q.Holding(symbol='513100',quantity=5000,available_quantity=5000,cost_price=2.384)])
        q.save_portfolio(p)
        result=q.record_trade(self.payload(expected_portfolio=p,symbol='513100',side='sell',quantity=3200,price=2.229,fees=5))
        self.assertEqual(17127.8,result['portfolio'].cash)
        self.assertEqual(7132.8,result['trade'].gross)
        self.assertEqual(1800,result['portfolio'].holdings[0].quantity)

    def test_invalid_money_available_and_stale_snapshot_do_not_change_file(self):
        before=(self.root/'user_data/quant_assistant.json').read_bytes()
        for changes in [dict(quantity=1000000),dict(side='sell',quantity=51),dict(side='sell',quantity=1,fees=100),dict(fees=.001),dict(expected_portfolio=q.Portfolio())]:
            with self.subTest(changes=changes), self.assertRaises(q.TradeConflict): q.record_trade(self.payload(**changes))
            self.assertEqual(before,(self.root/'user_data/quant_assistant.json').read_bytes())

    def test_concurrent_duplicate_only_applies_once_and_atomic_failure_preserves_state(self):
        request=self.payload()
        with patch.object(q.os,'replace',side_effect=OSError('failure')):
            with self.assertRaises(q.PortfolioStorageError): q.record_trade(request)
        self.assertEqual(self.initial,q.load_portfolio()); self.assertEqual([],q.load_trades())
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses=list(executor.map(q.record_trade,[request,request]))
        self.assertEqual([False,True],sorted(r['duplicate'] for r in responses))
        self.assertEqual(200,q.load_portfolio().holdings[0].quantity)

    def test_old_trade_times_and_nonfinite_ledger_are_rejected(self):
        import json
        first=self.payload(); q.record_trade(first)
        current=q.load_portfolio()
        with self.assertRaises(q.TradeConflict):
            q.record_trade(self.payload(client_id='test-request-0002',suggestion_key='second',expected_portfolio=current,
                executed_at=first.executed_at-timedelta(minutes=1)))
        path=self.root/'user_data/quant_assistant.json'
        data=json.loads(path.read_text()); data['trades'][0]['fees']=float('nan')
        path.write_text(json.dumps(data))
        with self.assertRaises(q.PortfolioStorageError): q.load_portfolio()

    def test_manual_save_preserves_trade_history_and_api_returns_updated_portfolio(self):
        app=FastAPI(); app.include_router(router)
        client=TestClient(app)
        request=self.payload()
        response=client.post('/api/quant-assistant/trades',json=request.model_dump(mode='json'))
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(8795,response.json()['portfolio']['cash'])
        self.assertEqual(1,len(client.get('/api/quant-assistant/trades').json()))
        current=q.load_portfolio(); current.cash=9000; q.save_portfolio(current)
        self.assertEqual(1,len(q.load_trades()))
        self.assertEqual(9000,q.record_trade(request)['portfolio'].cash)
        self.assertEqual(0o600,(self.root/'user_data/quant_assistant.json').stat().st_mode & 0o777)


if __name__=='__main__': unittest.main()
