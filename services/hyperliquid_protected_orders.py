"""Minimal signed boundary for one Hyperliquid IOC + standalone STOP batch."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from fastapi import HTTPException

CLOID_PATTERN = r"^0x[0-9a-f]{32}$"
CREATE_ORDER_URL = "/exchange"
ORDER_STATUS_URL = "/info"


def _float_to_wire(value: Any) -> str:
    number = float(value)
    rounded = f"{number:.8f}"
    if abs(float(rounded) - number) >= 1e-12:
        raise HTTPException(status_code=400, detail="value exceeds Hyperliquid wire precision")
    normalized = Decimal(rounded).normalize()
    return f"{normalized:f}"


def _order_spec_to_wire(order: Mapping[str, Any]) -> dict[str, Any]:
    order_type = order["orderType"]
    if "limit" in order_type:
        wire_type = {"limit": dict(order_type["limit"])}
    else:
        trigger = order_type["trigger"]
        wire_type = {
            "trigger": {
                "triggerPx": _float_to_wire(trigger["triggerPx"]),
                "tpsl": trigger["tpsl"],
                "isMarket": trigger["isMarket"],
            }
        }
    return {
        "a": order["asset"],
        "b": order["isBuy"],
        "p": _float_to_wire(order["limitPx"]),
        "s": _float_to_wire(order["sz"]),
        "r": order["reduceOnly"],
        "t": wire_type,
        "c": order["cloid"],
    }


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise HTTPException(status_code=400, detail=f"{field} must be positive")
    return result


def _wire_specs(asset: int, orders: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(orders) != 2 or [order.get("role") for order in orders] != ["ENTRY", "STOP"]:
        raise HTTPException(status_code=400, detail="orders must be exactly ENTRY then STOP")
    entry, stop = orders
    cloids = [entry.get("cloid"), stop.get("cloid")]
    if any(not isinstance(value, str) or not re.fullmatch(CLOID_PATTERN, value) for value in cloids):
        raise HTTPException(status_code=400, detail="invalid Hyperliquid CLOID")
    if cloids[0] == cloids[1]:
        raise HTTPException(status_code=400, detail="ENTRY and STOP CLOIDs must differ")
    if entry.get("reduce_only") is not False or stop.get("reduce_only") is not True:
        raise HTTPException(status_code=400, detail="ENTRY/STOP reduce-only invariant failed")
    if entry.get("side") == stop.get("side") or entry.get("side") not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="STOP must oppose ENTRY")
    if _decimal(entry.get("amount"), "ENTRY amount") != _decimal(stop.get("amount"), "STOP amount"):
        raise HTTPException(status_code=400, detail="STOP must cover the full IOC amount")
    entry_type = entry.get("order_type")
    stop_type = stop.get("order_type")
    if entry_type != {"limit": {"tif": "Ioc"}}:
        raise HTTPException(status_code=400, detail="ENTRY must be LIMIT/IOC")
    trigger = stop_type.get("trigger") if isinstance(stop_type, Mapping) else None
    if trigger is None or dict(trigger) != {
        "triggerPx": trigger.get("triggerPx"),
        "isMarket": True,
        "tpsl": "sl",
    }:
        raise HTTPException(status_code=400, detail="STOP must be trigger-market sl")
    specs = []
    for order in orders:
        specs.append(
            {
                "asset": int(asset),
                "isBuy": order["side"] == "BUY",
                "limitPx": float(_decimal(order.get("limit_price"), "limit_price")),
                "sz": float(_decimal(order.get("amount"), "amount")),
                "reduceOnly": order["reduce_only"],
                "orderType": dict(order["order_type"]),
                "cloid": order["cloid"],
            }
        )
    return specs


async def place_protected_order(connector: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if request.get("connector_name") != "hyperliquid_perpetual" or request.get("grouping") != "na":
        raise HTTPException(status_code=400, detail="unsupported connector/grouping")
    trading_pair = str(request.get("trading_pair") or "")
    if not connector.trading_rules or trading_pair not in connector.trading_rules:
        raise HTTPException(status_code=503, detail="connector trading rules are not ready")
    coin = await connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
    specs = _wire_specs(connector.coin_to_asset[coin], request["orders"])
    rule = connector.trading_rules[trading_pair]
    for original, spec in zip(request["orders"], specs):
        amount = _decimal(original["amount"], "amount")
        price = _decimal(original["limit_price"], "limit_price")
        if connector.quantize_order_amount(trading_pair, amount) != amount:
            raise HTTPException(status_code=400, detail="amount violates exchange precision")
        if connector.quantize_order_price(trading_pair, price) != price:
            raise HTTPException(status_code=400, detail="price violates exchange precision")
        if amount < rule.min_order_size:
            raise HTTPException(status_code=400, detail="amount is below minimum order size")
    entry_notional = _decimal(request["orders"][0]["amount"], "ENTRY amount") * _decimal(
        request["orders"][0]["limit_price"], "ENTRY limit_price"
    )
    if entry_notional < rule.min_notional_size:
        raise HTTPException(status_code=400, detail="ENTRY is below minimum notional size")

    action = {
        "type": "order",
        "orders": [_order_spec_to_wire(spec) for spec in specs],
        "grouping": "na",
    }
    builder = connector._build_builder_field()
    if builder is not None:
        action["builder"] = builder
    auth = connector._auth
    nonce = int(auth._get_timestamp() * 1e3)
    signature = auth.sign_l1_action(
        auth.wallet,
        action,
        auth._vault_address,
        nonce,
        not connector._is_testnet,
    )
    response = await connector._api_post(
        path_url=CREATE_ORDER_URL,
        data={
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": auth._vault_address,
        },
        is_auth_required=False,
    )
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise HTTPException(status_code=502, detail="Hyperliquid rejected protected action")
    return {
        "request_id": request["request_id"],
        "executor_id": request["executor_id"],
        "controller_id": request["controller_id"],
        "entry_cloid": request["orders"][0]["cloid"],
        "stop_cloid": request["orders"][1]["cloid"],
        "acknowledged": True,
    }


def normalize_order_status(payload: Mapping[str, Any], expected_cloid: str) -> dict[str, Any] | None:
    if payload.get("status") == "unknownOid":
        return None
    envelope = payload.get("order")
    raw = envelope.get("order") if isinstance(envelope, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("cloid") != expected_cloid:
        raise HTTPException(status_code=502, detail="orderStatus CLOID mismatch")
    orig = _decimal(raw.get("origSz"), "orderStatus.origSz")
    remaining = Decimal(str(raw.get("sz", "0")))
    if not remaining.is_finite() or remaining < 0 or remaining > orig:
        raise HTTPException(status_code=502, detail="orderStatus size is invalid")
    is_trigger = raw.get("isTrigger") is True
    order_type = (
        {
            "trigger": {
                "triggerPx": str(raw.get("triggerPx")),
                "isMarket": "Market" in str(raw.get("orderType") or ""),
                "tpsl": "sl" if "Stop" in str(raw.get("orderType") or "") else "tp",
            }
        }
        if is_trigger
        else {"limit": {"tif": "Ioc"}}
    )
    side = raw.get("side")
    if side not in {"A", "B"}:
        raise HTTPException(status_code=502, detail="orderStatus side is invalid")
    return {
        "asset": raw.get("coin"),
        "cloid": expected_cloid,
        "oid": raw.get("oid"),
        "status": envelope.get("status"),
        "side": "BUY" if side == "B" else "SELL",
        "amount": str(orig),
        "filled": str(orig - remaining),
        "reduceOnly": raw.get("reduceOnly"),
        "orderType": order_type,
    }


async def lookup_order_by_cloid(connector: Any, cloid: str) -> dict[str, Any] | None:
    auth = connector._auth
    payload = await connector._api_post(
        path_url=ORDER_STATUS_URL,
        data={"type": "orderStatus", "user": auth._api_address, "oid": cloid},
        is_auth_required=False,
    )
    return normalize_order_status(payload, cloid)
