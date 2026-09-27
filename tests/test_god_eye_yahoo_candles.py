import pytest

from nova_api.god_eye.models import InstrumentType, MarketInstrument
from nova_api.god_eye.providers import MalformedResponse, YahooProvider


class Client:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url):
        return self.payload, url


@pytest.mark.parametrize("symbol", ["GC=F", "CL=F"])
@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_yahoo_empty_session_is_not_malformed(symbol, interval):
    instrument = MarketInstrument(symbol, symbol, InstrumentType.COMMODITY)
    empty = {"chart": {"result": [{"indicators": {"quote": [{"open": [], "high": [], "low": [], "close": [], "volume": []}]}}], "error": None}}
    assert YahooProvider(Client(empty)).candles(instrument, interval, 10) == []
    bad = {"chart": {"result": [{"timestamp": [1], "indicators": {"quote": [{"open": [], "high": [], "low": [], "close": []}]}}], "error": None}}
    with pytest.raises(MalformedResponse):
        YahooProvider(Client(bad)).candles(instrument, interval, 10)
