from decimal import Decimal
from types import SimpleNamespace

import pytest

from services.hyperliquid_protected_orders import (
    cancel_protected_stop,
    connector_account_address,
    lookup_order_by_cloid,
    lookup_position,
    normalize_order_status,
    place_protected_close,
    place_protected_order,
)

ENTRY = "0x11111111111111111111111111111111"
STOP = "0x22222222222222222222222222222222"
CLOSE = "0x33333333333333333333333333333333"


def book(*, time=1_234_000, bid="64990", ask="65010"):
    return {
        "coin": "BTC",
        "time": time,
        "levels": [[{"px": bid, "sz": "1", "n": 1}], [{"px": ask, "sz": "1", "n": 1}]],
    }


async def assert_error(awaitable, message):
    try:
        await awaitable
    except Exception as exc:
        assert message in str(exc)
    else:
        raise AssertionError(f"expected error containing {message!r}")


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
    connector = Connector([book(), {"status": "ok", "response": {"data": {"statuses": []}}}])
    result = await place_protected_order(connector, request())

    assert result["entry_cloid"] == ENTRY
    assert result["stop_cloid"] == STOP
    assert len(connector.posts) == 2
    assert connector.posts[0]["data"] == {"type": "l2Book", "coin": "BTC"}
    post = connector.posts[1]
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


@pytest.mark.asyncio
async def test_stale_or_adverse_entry_price_never_reaches_a_mutation_post():
    stale = Connector([book(time=1_220_000)])
    await assert_error(place_protected_order(stale, request()), "stale or future-dated")
    assert [post["path_url"] for post in stale.posts] == ["/info"]

    adverse_request = request()
    adverse_request["orders"][0]["limit_price"] = "70000"
    adverse = Connector([book()])
    await assert_error(place_protected_order(adverse, adverse_request), "adverse-price envelope")
    assert [post["path_url"] for post in adverse.posts] == ["/info"]


def close_request():
    return {
        "request_id": "request-0001",
        "executor_id": "condor-real-" + "a" * 32,
        "controller_id": "controller-a",
        "account_name": "master_account",
        "connector_name": "hyperliquid_perpetual",
        "trading_pair": "BTC-USD",
        "grouping": "na",
        "order": {
            "role": "CLOSE",
            "cloid": CLOSE,
            "side": "SELL",
            "amount": "0.004",
            "reduce_only": True,
            "order_type": {"limit": {"tif": "Ioc"}},
        },
    }


def clearinghouse(size):
    positions = [] if Decimal(str(size)) == 0 else [{"position": {"coin": "BTC", "szi": str(size)}}]
    return {"assetPositions": positions}


@pytest.mark.asyncio
async def test_close_is_exact_full_position_reduce_only_ioc_with_deterministic_cloid():
    connector = Connector(
        [
            clearinghouse("0.004"),
            book(),
            {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 303}}]}}},
        ]
    )
    result = await place_protected_close(connector, close_request())
    assert result["close_cloid"] == CLOSE
    action = connector.posts[-1]["data"]["action"]
    assert action == {
        "type": "order",
        "grouping": "na",
        "orders": [
            {
                "a": 0,
                "b": False,
                "p": "61750",
                "s": "0.004",
                "r": True,
                "t": {"limit": {"tif": "Ioc"}},
                "c": CLOSE,
            }
        ],
    }


@pytest.mark.asyncio
async def test_close_rejects_mismatched_exchange_position_before_signing():
    connector = Connector([clearinghouse("0.003")])
    await assert_error(
        place_protected_close(connector, close_request()),
        "does not match exchange position truth",
    )
    assert [post["path_url"] for post in connector.posts] == ["/info"]


@pytest.mark.asyncio
async def test_position_truth_is_read_directly_for_the_bound_public_account():
    connector = Connector([clearinghouse("-0.004")])
    position = await lookup_position(connector, "BTC-USD")
    assert position == {
        "account_address": "0x" + "3" * 40,
        "asset": "BTC",
        "size": "-0.004",
    }
    assert connector.posts[0]["data"] == {
        "type": "clearinghouseState",
        "user": "0x" + "3" * 40,
    }


@pytest.mark.asyncio
async def test_stop_cancel_is_signed_by_cloid_only_after_exchange_flatness():
    stop_status = {
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
    }
    connector = Connector(
        [
            clearinghouse("0"),
            stop_status,
            {"status": "ok", "response": {"data": {"statuses": ["success"]}}},
        ]
    )
    payload = close_request()
    payload.pop("grouping")
    payload.pop("order")
    payload["stop_cloid"] = STOP
    result = await cancel_protected_stop(connector, payload)
    assert result == {"stop_cloid": STOP, "acknowledged": True, "already_terminal": False}
    assert connector.posts[-1]["data"]["action"] == {
        "type": "cancelByCloid",
        "cancels": [{"asset": 0, "cloid": STOP}],
    }

    nonflat = Connector([clearinghouse("0.001")])
    await assert_error(
        cancel_protected_stop(nonflat, payload),
        "requires exchange-flat position",
    )
    assert [post["path_url"] for post in nonflat.posts] == ["/info"]


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
