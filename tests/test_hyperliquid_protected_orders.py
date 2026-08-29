from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.hyperliquid_protected_orders import (
    connector_account_address,
    lookup_order_by_cloid,
    normalize_order_status,
    place_protected_order,
)

ENTRY = "0x11111111111111111111111111111111"
STOP = "0x22222222222222222222222222222222"


def request():
    return {
        "request_id": "request-0001",
        "executor_id": "condor-real-" + "a" * 32,
        "controller_id": "controller-a",
        "account_name": "master_account",
        "connector_name": "hyperliquid_perpetual",
        "trading_pair": "BTC-USD",
        "grouping": "na",
        "orders": [
            {
                "role": "ENTRY",
                "cloid": ENTRY,
                "side": "BUY",
                "amount": "0.010",
                "reduce_only": False,
                "limit_price": "65000",
                "order_type": {"limit": {"tif": "Ioc"}},
            },
            {
                "role": "STOP",
                "cloid": STOP,
                "side": "SELL",
                "amount": "0.010",
                "reduce_only": True,
                "limit_price": "58500",
                "order_type": {
                    "trigger": {
                        "triggerPx": "59000",
                        "isMarket": True,
                        "tpsl": "sl",
                    }
                },
            },
        ],
    }


class Auth:
    wallet = object()
    _vault_address = None
    _api_address = "0x" + "3" * 40

    @staticmethod
    def _get_timestamp():
        return 1234.0

    def sign_l1_action(self, wallet, action, vault, nonce, is_mainnet):
        self.signed = (action, nonce, is_mainnet)
        return {"r": "0x1", "s": "0x2", "v": 27}


class Connector:
    def __init__(self, responses):
        self.trading_rules = {"BTC-USD": SimpleNamespace(min_order_size=Decimal("0.001"), min_notional_size=Decimal("10"))}
        self.coin_to_asset = {"BTC": 0}
        self._auth = Auth()
        self._is_testnet = False
        self.responses = list(responses)
        self.posts = []

    async def exchange_symbol_associated_to_pair(self, trading_pair):
        return "BTC"

    def quantize_order_amount(self, trading_pair, value):
        return value

    def quantize_order_price(self, trading_pair, value):
        return value

    def _build_builder_field(self):
        return None

    async def _api_post(self, **kwargs):
        self.posts.append(kwargs)
        return self.responses.pop(0)


def test_capability_attests_the_connector_public_account_address():
    connector = Connector([])
    connector._auth._api_address = "0x3003A55B5140F63A260A8C0324390D30319C65BD"
    assert connector_account_address(connector) == ("0x3003a55b5140f63a260a8c0324390d30319c65bd")


@pytest.mark.asyncio
async def test_exact_protected_action_is_one_signed_grouping_na_post():
    connector = Connector([{"status": "ok", "response": {"data": {"statuses": []}}}])
    result = await place_protected_order(connector, request())

    assert result["entry_cloid"] == ENTRY
    assert result["stop_cloid"] == STOP
    assert len(connector.posts) == 1
    post = connector.posts[0]
    assert post["is_auth_required"] is False
    action = post["data"]["action"]
    assert action == {
        "type": "order",
        "grouping": "na",
        "orders": [
            {
                "a": 0,
                "b": True,
                "p": "65000",
                "s": "0.01",
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
                "c": ENTRY,
            },
            {
                "a": 0,
                "b": False,
                "p": "58500",
                "s": "0.01",
                "r": True,
                "t": {
                    "trigger": {
                        "triggerPx": "59000",
                        "tpsl": "sl",
                        "isMarket": True,
                    }
                },
                "c": STOP,
            },
        ],
    }
    assert connector._auth.signed[0] == action


def test_partial_ioc_status_and_live_stop_are_normalized_by_cloid():
    entry = normalize_order_status(
        {
            "status": "order",
            "order": {
                "status": "canceled",
                "order": {
                    "coin": "BTC",
                    "side": "B",
                    "sz": "0.006",
                    "origSz": "0.010",
                    "oid": 101,
                    "isTrigger": False,
                    "reduceOnly": False,
                    "orderType": "Market",
                    "cloid": ENTRY,
                },
            },
        },
        ENTRY,
    )
    stop = normalize_order_status(
        {
            "status": "order",
            "order": {
                "status": "open",
                "order": {
                    "coin": "BTC",
                    "side": "A",
                    "sz": "0.010",
                    "origSz": "0.010",
                    "oid": 202,
                    "isTrigger": True,
                    "triggerPx": "59000",
                    "reduceOnly": True,
                    "orderType": "Stop Market",
                    "cloid": STOP,
                },
            },
        },
        STOP,
    )
    assert entry["filled"] == "0.004"
    assert stop["status"] == "open"
    assert stop["orderType"]["trigger"] == {
        "triggerPx": "59000",
        "isMarket": True,
        "tpsl": "sl",
    }


@pytest.mark.asyncio
async def test_status_recovery_is_read_only_and_uses_cloid():
    response = {"status": "unknownOid"}
    connector = Connector([response])
    assert await lookup_order_by_cloid(connector, ENTRY) is None
    assert len(connector.posts) == 1
    assert connector.posts[0]["data"] == {
        "type": "orderStatus",
        "user": connector._auth._api_address,
        "oid": ENTRY,
    }
    assert connector.posts[0]["is_auth_required"] is False
