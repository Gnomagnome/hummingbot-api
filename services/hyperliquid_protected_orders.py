"""Minimal signed boundary for one Hyperliquid IOC + standalone STOP batch."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Mapping

from fastapi import HTTPException

CLOID_PATTERN = r"^0x[0-9a-f]{32}$"
ADDRESS_PATTERN = r"^0x[0-9a-f]{40}$"
CREATE_ORDER_URL = "/exchange"
ORDER_STATUS_URL = "/info"
MAX_BOOK_AGE_MS = 5_000
MAX_FUTURE_BOOK_MS = 1_000
MAX_ADVERSE_SLIPPAGE = Decimal("0.05")


def _terminal_order_status(value: Any) -> bool:
    status = str(value or "").strip().lower()
    return status == "filled" or status.endswith("canceled") or status.endswith("cancelled") or status.endswith("rejected")


def connector_account_address(connector: Any) -> str:
    """Return the public account whose orders/statuses this connector owns."""
    address = str(getattr(getattr(connector, "_auth", None), "_api_address", ""))
    address = address.strip().lower()
    if not re.fullmatch(ADDRESS_PATTERN, address):
        raise HTTPException(
            status_code=503,
            detail="Hyperliquid connector public account address is unavailable",
        )
    return address


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


def _position_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{field} must be numeric") from exc
    if not result.is_finite():
        raise HTTPException(status_code=502, detail=f"{field} must be finite")
    return result


async def _fresh_mid(connector: Any, coin: str) -> Decimal:
    """Read one uncached exchange book and reject stale or ambiguous prices."""
    book = await connector._api_post(
        path_url=ORDER_STATUS_URL,
        data={"type": "l2Book", "coin": coin},
        is_auth_required=False,
    )
    if not isinstance(book, Mapping) or book.get("coin") != coin:
        raise HTTPException(status_code=503, detail="fresh Hyperliquid book is unavailable")
    timestamp = book.get("time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise HTTPException(status_code=503, detail="Hyperliquid book timestamp is unavailable")
    now_ms = int(connector._auth._get_timestamp() * 1e3)
    age_ms = now_ms - timestamp
    if age_ms > MAX_BOOK_AGE_MS or age_ms < -MAX_FUTURE_BOOK_MS:
        raise HTTPException(status_code=503, detail="Hyperliquid book is stale or future-dated")
    levels = book.get("levels")
    if (
        not isinstance(levels, list)
        or len(levels) != 2
        or not isinstance(levels[0], list)
        or not levels[0]
        or not isinstance(levels[1], list)
        or not levels[1]
    ):
        raise HTTPException(status_code=503, detail="Hyperliquid book has no two-sided top of book")
    try:
        bid = _decimal(levels[0][0]["px"], "best bid")
        ask = _decimal(levels[1][0]["px"], "best ask")
    except (KeyError, TypeError) as exc:
        raise HTTPException(status_code=503, detail="Hyperliquid top of book is malformed") from exc
    if bid >= ask:
        raise HTTPException(status_code=503, detail="Hyperliquid top of book is crossed")
    return (bid + ask) / Decimal("2")


def _require_price_envelope(
    connector: Any,
    trading_pair: str,
    *,
    side: str,
    limit_price: Decimal,
    mid: Decimal,
) -> None:
    if side == "BUY":
        boundary = connector.quantize_order_price(trading_pair, mid * (Decimal("1") + MAX_ADVERSE_SLIPPAGE))
        safe = limit_price <= boundary
    elif side == "SELL":
        boundary = connector.quantize_order_price(trading_pair, mid * (Decimal("1") - MAX_ADVERSE_SLIPPAGE))
        safe = limit_price >= boundary
    else:
        safe = False
    if not safe:
        raise HTTPException(status_code=400, detail="IOC limit exceeds the audited adverse-price envelope")


def _signed_payload(connector: Any, action: Mapping[str, Any]) -> dict[str, Any]:
    nonce = int(connector._auth._get_timestamp() * 1e3)
    signature = connector._auth.sign_l1_action(
        connector._auth.wallet,
        action,
        connector._auth._vault_address,
        nonce,
        not connector._is_testnet,
    )
    return {
        "action": dict(action),
        "nonce": nonce,
        "signature": signature,
        "vaultAddress": connector._auth._vault_address,
    }


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

    mid = await _fresh_mid(connector, coin)
    _require_price_envelope(
        connector,
        trading_pair,
        side=request["orders"][0]["side"],
        limit_price=_decimal(request["orders"][0]["limit_price"], "ENTRY limit_price"),
        mid=mid,
    )

    action = {
        "type": "order",
        "orders": [_order_spec_to_wire(spec) for spec in specs],
        "grouping": "na",
    }
    builder = connector._build_builder_field()
    if builder is not None:
        action["builder"] = builder
    response = await connector._api_post(
        path_url=CREATE_ORDER_URL,
        data=_signed_payload(connector, action),
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


async def lookup_position(connector: Any, trading_pair: str) -> dict[str, Any]:
    """Read the exact public-account perpetual position directly from Hyperliquid."""
    coin = await connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
    address = connector_account_address(connector)
    payload = await connector._api_post(
        path_url=ORDER_STATUS_URL,
        data={"type": "clearinghouseState", "user": address},
        is_auth_required=False,
    )
    positions = payload.get("assetPositions") if isinstance(payload, Mapping) else None
    if not isinstance(positions, list):
        raise HTTPException(status_code=502, detail="Hyperliquid clearinghouse state is malformed")
    matches = []
    for item in positions:
        position = item.get("position") if isinstance(item, Mapping) else None
        if isinstance(position, Mapping) and position.get("coin") == coin:
            matches.append(position)
    if len(matches) > 1:
        raise HTTPException(status_code=502, detail="Hyperliquid returned duplicate asset positions")
    size = Decimal("0") if not matches else _position_decimal(matches[0].get("szi"), "position.szi")
    return {
        "account_address": address,
        "asset": coin,
        "size": str(size),
    }


async def place_protected_close(connector: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Submit one full residual reduce-only IOC with a caller-owned CLOID."""
    if request.get("connector_name") != "hyperliquid_perpetual" or request.get("grouping") != "na":
        raise HTTPException(status_code=400, detail="unsupported connector/grouping")
    trading_pair = str(request.get("trading_pair") or "")
    if not connector.trading_rules or trading_pair not in connector.trading_rules:
        raise HTTPException(status_code=503, detail="connector trading rules are not ready")
    order = request.get("order")
    if not isinstance(order, Mapping):
        raise HTTPException(status_code=400, detail="CLOSE order is required")
    if (
        order.get("role") != "CLOSE"
        or not isinstance(order.get("cloid"), str)
        or not re.fullmatch(CLOID_PATTERN, order["cloid"])
        or order.get("side") not in {"BUY", "SELL"}
        or order.get("reduce_only") is not True
        or order.get("order_type") != {"limit": {"tif": "Ioc"}}
    ):
        raise HTTPException(status_code=400, detail="invalid protected CLOSE order")
    amount = _decimal(order.get("amount"), "CLOSE amount")
    if connector.quantize_order_amount(trading_pair, amount) != amount:
        raise HTTPException(status_code=400, detail="amount violates exchange precision")
    rule = connector.trading_rules[trading_pair]
    if amount < rule.min_order_size:
        raise HTTPException(status_code=400, detail="amount is below minimum order size")
    coin = await connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
    position = await lookup_position(connector, trading_pair)
    position_size = _position_decimal(position["size"], "position size")
    expected_size = amount if order["side"] == "SELL" else -amount
    if position_size != expected_size:
        raise HTTPException(status_code=409, detail="CLOSE amount/side does not match exchange position truth")
    mid = await _fresh_mid(connector, coin)
    multiplier = Decimal("1") + MAX_ADVERSE_SLIPPAGE if order["side"] == "BUY" else Decimal("1") - MAX_ADVERSE_SLIPPAGE
    limit_price = connector.quantize_order_price(trading_pair, mid * multiplier)
    if not isinstance(limit_price, Decimal) or not limit_price.is_finite() or limit_price <= 0:
        raise HTTPException(status_code=503, detail="safe CLOSE limit could not be quantified")
    asset = connector.coin_to_asset[coin]
    spec = {
        "asset": int(asset),
        "isBuy": order["side"] == "BUY",
        "limitPx": float(limit_price),
        "sz": float(amount),
        "reduceOnly": True,
        "orderType": {"limit": {"tif": "Ioc"}},
        "cloid": order["cloid"],
    }
    action = {
        "type": "order",
        "orders": [_order_spec_to_wire(spec)],
        "grouping": "na",
    }
    builder = connector._build_builder_field()
    if builder is not None:
        action["builder"] = builder
    response = await connector._api_post(
        path_url=CREATE_ORDER_URL,
        data=_signed_payload(connector, action),
        is_auth_required=False,
    )
    if not isinstance(response, Mapping) or response.get("status") != "ok":
        raise HTTPException(status_code=502, detail="Hyperliquid rejected protected CLOSE")
    return {
        "request_id": request["request_id"],
        "executor_id": request["executor_id"],
        "controller_id": request["controller_id"],
        "close_cloid": order["cloid"],
        "acknowledged": True,
    }


async def cancel_protected_stop(connector: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Cancel the owned STOP by CLOID, but only after exchange position truth is flat."""
    if request.get("connector_name") != "hyperliquid_perpetual":
        raise HTTPException(status_code=400, detail="unsupported connector")
    trading_pair = str(request.get("trading_pair") or "")
    cloid = request.get("stop_cloid")
    if not isinstance(cloid, str) or not re.fullmatch(CLOID_PATTERN, cloid):
        raise HTTPException(status_code=400, detail="invalid STOP CLOID")
    position = await lookup_position(connector, trading_pair)
    if _position_decimal(position["size"], "position size") != 0:
        raise HTTPException(status_code=409, detail="STOP cancellation requires exchange-flat position")
    coin = await connector.exchange_symbol_associated_to_pair(trading_pair=trading_pair)
    observed = await lookup_order_by_cloid(connector, cloid)
    if observed is None:
        raise HTTPException(status_code=409, detail="owned STOP status is unavailable")
    trigger = observed.get("orderType", {}).get("trigger", {})
    if (
        observed.get("cloid") != cloid
        or observed.get("asset") != coin
        or observed.get("reduceOnly") is not True
        or trigger.get("isMarket") is not True
        or trigger.get("tpsl") != "sl"
    ):
        raise HTTPException(status_code=409, detail="owned STOP identity is inconsistent")
    status = str(observed.get("status") or "").lower()
    if status != "open":
        if not _terminal_order_status(status):
            raise HTTPException(status_code=502, detail="owned STOP terminal state is ambiguous")
        return {"stop_cloid": cloid, "acknowledged": True, "already_terminal": True}
    action = {
        "type": "cancelByCloid",
        "cancels": [{"asset": int(connector.coin_to_asset[coin]), "cloid": cloid}],
    }
    response = await connector._api_post(
        path_url=CREATE_ORDER_URL,
        data=_signed_payload(connector, action),
        is_auth_required=False,
    )
    statuses = response.get("response", {}).get("data", {}).get("statuses", []) if isinstance(response, Mapping) else []
    if not isinstance(response, Mapping) or response.get("status") != "ok" or statuses != ["success"]:
        raise HTTPException(status_code=502, detail="Hyperliquid STOP cancellation was not acknowledged")
    return {"stop_cloid": cloid, "acknowledged": True, "already_terminal": False}


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
