from unittest.mock import patch
from app.services import preferences


def test_all_watchlist_symbols_are_monitored_without_truncation():
    rows = [{"symbol": f"600{i:03d}.SH"} for i in range(30)]
    rows += [{"symbol": "600000.sh"}, {"symbol": " 513100.sh "}, {"symbol": ""}, {}]
    with patch("app.services.watchlist.list_symbols", return_value=rows):
        result = preferences.get_realtime_watchlist_symbols()
    assert len(result) == 31
    assert result[:30] == [r["symbol"] for r in rows[:30]]
    assert result[-1] == "513100.SH"


def test_empty_watchlist_does_not_reuse_old_selected_symbols():
    with patch("app.services.watchlist.list_symbols", return_value=[]):
        assert preferences.get_realtime_watchlist_symbols() == []
        assert preferences.set_realtime_watchlist_symbols(["601988.SH"]) == []


def test_rotating_requests_cover_all_symbols_within_provider_cap():
    from app.services.quote_service import QuoteService
    service = QuoteService()
    symbols = [f"600{i:03d}.SH" for i in range(17)]
    batches = [service._next_watchlist_batch(symbols) for _ in range(4)]
    assert [len(batch) for batch in batches] == [5, 5, 5, 2]
    assert sum(batches, []) == symbols
    assert service._next_watchlist_batch(symbols) == symbols[:5]
    assert service._next_watchlist_batch([]) == []
    assert service._next_watchlist_batch(symbols[:2]) == symbols[:2]
