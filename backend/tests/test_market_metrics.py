import unittest
from app.services.market_metrics import price_metrics

class PriceMetricTests(unittest.TestCase):
    def metrics(self, closes):
        return price_metrics([dict(close=v,date=str(i)) for i,v in enumerate(closes)],'m5')
    def test_constant(self):
        result=self.metrics([2.]*80)
        self.assertEqual(result['ma']['20'],2.)
        self.assertEqual(result['rsi14'],50.)
        self.assertEqual(result['macd'],{'dif':0.,'dea':0.,'hist':0.})
    def test_samples_and_direction(self):
        self.assertIsNone(self.metrics([1.,2.])['rsi14'])
        self.assertIsNone(self.metrics([1.,2.])['macd'])
        self.assertEqual(self.metrics(list(range(1,80)))['rsi14'],100.)
        self.assertEqual(self.metrics(list(range(80,1,-1)))['rsi14'],0.)
        self.assertEqual(self.metrics(list(range(1,80)))['ma']['5'],77.)
    def test_invalid(self):
        with self.assertRaises(ValueError): self.metrics([float('nan')])

if __name__=='__main__': unittest.main()
