import json
import os
import time
import uuid
import re
import base64
import binascii
from pathlib import Path
from urllib.parse import urlparse
from decimal import Decimal, InvalidOperation
from datetime import date, timezone, datetime
from typing import Any

import httpx

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft7Validator, RefResolver
from mcp.server.fastmcp import FastMCP
from .endpoint_catalog import ENDPOINTS, ENDPOINT_METADATA, SPEC

PUBLIC_PREFIXES = ("/exchange/", "/markets", "/events", "/series", "/search/", "/historical/", "/incentive_programs", "/multivariate_event_collections")

def _resolve_endpoint(method: str, endpoint: str) -> tuple[str, str]:
    method = method.upper()
    if (method, endpoint) in ENDPOINTS:
        return method, endpoint
    for (m, template), operation in ENDPOINTS.items():
        if m != method:
            continue
        pattern = "^" + re.sub(r"\{[^}]+\}", r"[^/]+", template) + "$"
        if re.match(pattern, endpoint):
            return method, template
    raise ValueError("endpoint is not present in the supplied Kalshi OpenAPI catalog")

def _request_schema(metadata: dict[str, Any]) -> dict[str, Any] | None:
    content = (metadata.get('request_body') or {}).get('content', {})
    return (content.get('application/json') or {}).get('schema')


def _validate_body(metadata: dict[str, Any], body: Any) -> None:
    schema = _request_schema(metadata)
    if not schema or body is None:
        return
    resolver = RefResolver.from_schema(SPEC)
    validator = Draft7Validator(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
    if errors:
        detail = '; '.join(f"{'.'.join(map(str, e.path)) or '<body>'}: {e.message}" for e in errors[:5])
        raise ValueError(f'invalid request body: {detail}')


def _requires_auth(endpoint: str, method: str | None = None) -> bool:
    if method and (method, endpoint) in ENDPOINT_METADATA:
        security = ENDPOINT_METADATA[(method, endpoint)].get('security')
        return bool(security)
    return not endpoint.startswith(PUBLIC_PREFIXES)

DEMO = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD = "https://external-api.kalshi.com/trade-api/v2"


def _base_url() -> str:
    explicit = os.getenv("KALSHI_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    env = os.getenv("KALSHI_ENV", "production").lower()
    if env not in {"demo", "production"}:
        raise ValueError("KALSHI_ENV must be demo or production")
    return PROD if env == "production" else DEMO


def _auth_headers(method: str, path: str) -> dict[str, str]:
    key_id = os.getenv("KALSHI_API_KEY_ID")
    key_b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or (not key_b64 and not key_path):
        raise RuntimeError("KALSHI_API_KEY_ID and a private key are required")
    ts = str(int(time.time() * 1000))
    message = (ts + method.upper() + path.split("?", 1)[0]).encode()
    try:
        if key_b64:
            key_bytes = base64.b64decode(key_b64, validate=True)
        else:
            key_bytes = Path(key_path).read_bytes()
    except (ValueError, binascii.Error, OSError) as exc:
        raise RuntimeError("private key must be valid base64 or a readable PEM file") from exc
    private = serialization.load_pem_private_key(key_bytes, password=None)
    if isinstance(private, ed25519.Ed25519PrivateKey):
        sig = private.sign(message)
    else:
        raise RuntimeError("KALSHI private key must be an Ed25519 PEM key")
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def _request(method: str, endpoint: str, *, params: dict[str, Any] | None = None, body: Any = None, auth: bool = False) -> dict[str, Any]:
    url = _base_url() + endpoint
    headers = _auth_headers(method, urlparse(url).path) if auth else {}
    with httpx.Client(timeout=30) as client:
        r = client.request(method, url, params=params, json=body, headers=headers)
    r.raise_for_status()
    return r.json() if r.content else {}


def _limit(name: str, default: int) -> int:
    return max(0, int(os.getenv(name, str(default))))


def _fixed_decimal(value: Any, field: str) -> Decimal:
    """Parse a fixed-point decimal without float rounding or exponent notation."""
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", value):
        raise ValueError(f"{field} must be a fixed-point numeric string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a fixed-point numeric string") from exc


def _validate_order_values(count: Any, price: Any) -> tuple[Decimal, Decimal]:
    count_value = _fixed_decimal(count, "count")
    price_value = _fixed_decimal(price, "price")
    if count_value <= 0 or count_value > Decimal(_limit("KALSHI_MAX_ORDER_CONTRACTS", 100)):
        raise ValueError("contract limit exceeded")
    if price_value <= 0 or price_value >= 1:
        raise ValueError("price must be greater than 0 and less than 1")
    if count_value * price_value * 100 > Decimal(_limit("KALSHI_MAX_ORDER_CENTS", 100)):
        raise ValueError("order notional limit exceeded")
    return count_value, price_value

mcp = FastMCP("kalshi-degen")

@mcp.tool()
def kalshi_endpoint(method: str, endpoint: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call any endpoint in the supplied Kalshi OpenAPI catalog. Path placeholders must already be substituted."""
    method, template = _resolve_endpoint(method, endpoint)
    metadata = ENDPOINT_METADATA[(method, template)]
    query = params or {}
    allowed_query = {p['name'] for p in metadata['parameters'] if p.get('in') == 'query'}
    unknown = set(query) - allowed_query
    if unknown:
        raise ValueError(f'unknown query parameter(s): {sorted(unknown)}')
    required_query = {p['name'] for p in metadata['parameters'] if p.get('in') == 'query' and p.get('required')}
    missing = required_query - set(query)
    if missing:
        raise ValueError(f'missing required query parameter(s): {sorted(missing)}')
    request_body = metadata.get('request_body')
    if request_body and body is None and method in {'POST', 'PUT', 'PATCH'}:
        raise ValueError('request body is required for this operation')
    if not request_body and body is not None:
        raise ValueError('this operation does not define a request body')
    _validate_body(metadata, body)
    return _request(method, endpoint, params=query or None, body=body, auth=_requires_auth(template, method))


@mcp.tool()
def list_order_groups(subaccount: int | None = None) -> dict[str, Any]:
    """List authenticated order groups."""
    params = {"subaccount": subaccount} if subaccount is not None else None
    return kalshi_endpoint("GET", "/portfolio/order_groups", params=params)


@mcp.tool()
def get_order_group(order_group_id: str, subaccount: int | None = None) -> dict[str, Any]:
    """Get one authenticated order group."""
    params = {"subaccount": subaccount} if subaccount is not None else None
    return kalshi_endpoint("GET", f"/portfolio/order_groups/{order_group_id}", params=params)


@mcp.tool()
def create_order_group(contracts_limit: int | None = None, contracts_limit_fp: str | None = None, subaccount: int | None = None, exchange_index: int = 0) -> dict[str, Any]:
    """Create an order group with a rolling contract limit."""
    if contracts_limit is None and contracts_limit_fp is None: raise ValueError("provide contracts_limit or contracts_limit_fp")
    if contracts_limit is not None and contracts_limit < 1: raise ValueError("contracts_limit must be positive")
    if contracts_limit_fp is not None and float(contracts_limit_fp) < 1: raise ValueError("contracts_limit_fp must be positive")
    body: dict[str, Any] = {"exchange_index": exchange_index}
    if contracts_limit is not None: body["contracts_limit"] = contracts_limit
    if contracts_limit_fp is not None: body["contracts_limit_fp"] = contracts_limit_fp
    if subaccount is not None: body["subaccount"] = subaccount
    return kalshi_endpoint("POST", "/portfolio/order_groups/create", body=body)


@mcp.tool()
def delete_order_group(order_group_id: str, subaccount: int | None = None, exchange_index: int | None = None) -> dict[str, Any]:
    """Delete an authenticated order group."""
    params = {k: v for k, v in {"subaccount": subaccount, "exchange_index": exchange_index}.items() if v is not None}
    return kalshi_endpoint("DELETE", f"/portfolio/order_groups/{order_group_id}", params=params or None)


@mcp.tool()
def update_order_group_limit(order_group_id: str, contracts_limit: int | None = None, contracts_limit_fp: str | None = None, subaccount: int | None = None, exchange_index: int | None = None) -> dict[str, Any]:
    """Update an order group's rolling contract limit."""
    if contracts_limit is None and contracts_limit_fp is None: raise ValueError("provide contracts_limit or contracts_limit_fp")
    if contracts_limit is not None and contracts_limit < 1: raise ValueError("contracts_limit must be positive")
    if contracts_limit_fp is not None and float(contracts_limit_fp) < 1: raise ValueError("contracts_limit_fp must be positive")
    body = {k: v for k, v in {"contracts_limit": contracts_limit, "contracts_limit_fp": contracts_limit_fp}.items() if v is not None}
    params = {k: v for k, v in {"subaccount": subaccount, "exchange_index": exchange_index}.items() if v is not None}
    return kalshi_endpoint("PUT", f"/portfolio/order_groups/{order_group_id}/limit", params=params or None, body=body)


@mcp.tool()
def reset_order_group(order_group_id: str, subaccount: int | None = None, exchange_index: int | None = None) -> dict[str, Any]:
    """Reset an authenticated order group's rolling limit."""
    params = {k: v for k, v in {"subaccount": subaccount, "exchange_index": exchange_index}.items() if v is not None}
    return kalshi_endpoint("PUT", f"/portfolio/order_groups/{order_group_id}/reset", params=params or None, body={})


@mcp.tool()
def trigger_order_group(order_group_id: str, subaccount: int | None = None, exchange_index: int | None = None) -> dict[str, Any]:
    """Trigger an authenticated order group."""
    params = {k: v for k, v in {"subaccount": subaccount, "exchange_index": exchange_index}.items() if v is not None}
    return kalshi_endpoint("PUT", f"/portfolio/order_groups/{order_group_id}/trigger", params=params or None, body={})
@mcp.tool()
def exchange_status() -> dict[str, Any]:
    """Get Kalshi exchange status."""
    return _request("GET", "/exchange/status")

@mcp.tool()
def list_markets(status: str | None = "open", limit: int = 100, cursor: str | None = None, series_ticker: str | None = None) -> dict[str, Any]:
    """List markets with optional status, series, and cursor filters."""
    params = {"limit": min(max(limit, 1), 1000)}
    if status: params["status"] = status
    if cursor: params["cursor"] = cursor
    if series_ticker: params["series_ticker"] = series_ticker
    return _request("GET", "/markets", params=params)

@mcp.tool()
def get_market(ticker: str) -> dict[str, Any]:
    """Get one market by ticker."""
    return _request("GET", f"/markets/{ticker}")

@mcp.tool()
def list_events(status: str | None = "open", limit: int = 100, cursor: str | None = None, with_nested_markets: bool = True) -> dict[str, Any]:
    """List events."""
    params: dict[str, Any] = {"limit": min(max(limit, 1), 200), "with_nested_markets": with_nested_markets}
    if status: params["status"] = status
    if cursor: params["cursor"] = cursor
    return _request("GET", "/events", params=params)

@mcp.tool()
def get_event(event_ticker: str, with_nested_markets: bool = True) -> dict[str, Any]:
    """Get an event by ticker."""
    return _request("GET", f"/events/{event_ticker}", params={"with_nested_markets": with_nested_markets})

@mcp.tool()
def get_orderbook(ticker: str, depth: int = 0) -> dict[str, Any]:
    """Get a market orderbook; Kalshi returns yes/no bids."""
    return _request("GET", f"/markets/{ticker}/orderbook", params={"depth": min(max(depth, 0), 100)})

@mcp.tool()
def list_trades(ticker: str | None = None, limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
    """List public market trades."""
    params: dict[str, Any] = {"limit": min(max(limit, 1), 1000)}
    if ticker: params["ticker"] = ticker
    if cursor: params["cursor"] = cursor
    return _request("GET", "/markets/trades", params=params)

@mcp.tool()
def get_balance() -> dict[str, Any]:
    """Get authenticated balance."""
    return _request("GET", "/portfolio/balance", auth=True)

@mcp.tool()
def get_positions(limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
    """Get authenticated positions."""
    params: dict[str, Any] = {"limit": min(max(limit, 1), 200)}
    if cursor: params["cursor"] = cursor
    return _request("GET", "/portfolio/positions", params=params, auth=True)

@mcp.tool()
def list_orders(status: str | None = None, ticker: str | None = None, limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
    """List authenticated orders."""
    params: dict[str, Any] = {"limit": min(max(limit, 1), 200)}
    for k, v in (("status", status), ("ticker", ticker), ("cursor", cursor)):
        if v: params[k] = v
    return _request("GET", "/portfolio/orders", params=params, auth=True)

@mcp.tool()
def create_order_v2(ticker: str, side: str, count: str, price: str, time_in_force: str = "good_till_canceled", self_trade_prevention_type: str = "taker_at_cross", client_order_id: str | None = None, post_only: bool = False, reduce_only: bool = False, expiration_time: int | None = None) -> dict[str, Any]:
    """Place a guarded Kalshi V2 event-market order. Price and count are fixed-point strings, e.g. 0.5600 and 10.00."""
    if side not in {"bid", "ask"}: raise ValueError("side must be bid or ask")
    if time_in_force not in {"fill_or_kill", "good_till_canceled", "immediate_or_cancel"}: raise ValueError("invalid time_in_force")
    if self_trade_prevention_type not in {"taker_at_cross", "maker"}: raise ValueError("invalid self_trade_prevention_type")
    _validate_order_values(count, price)
    body: dict[str, Any] = {"ticker": ticker, "side": side, "count": count, "price": price, "time_in_force": time_in_force, "self_trade_prevention_type": self_trade_prevention_type, "client_order_id": client_order_id or str(uuid.uuid4()), "post_only": post_only, "reduce_only": reduce_only}
    if expiration_time is not None: body["expiration_time"] = expiration_time
    return kalshi_endpoint("POST", "/portfolio/events/orders", body=body)


@mcp.tool()
def create_orders_v2_batch(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Place multiple guarded V2 orders in one request. Each order uses fixed-point price/count strings."""
    if not orders: raise ValueError("orders must not be empty")
    if len(orders) > 20: raise ValueError("batch order limit exceeded")
    for order in orders:
        required = {"ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type"}
        if not required.issubset(order): raise ValueError("each order must include ticker, side, count, price, time_in_force, and self_trade_prevention_type")
        if order["side"] not in {"bid", "ask"}: raise ValueError("side must be bid or ask")
        if order["time_in_force"] not in {"fill_or_kill", "good_till_canceled", "immediate_or_cancel"}: raise ValueError("invalid time_in_force")
        if order["self_trade_prevention_type"] not in {"taker_at_cross", "maker"}: raise ValueError("invalid self_trade_prevention_type")
        _validate_order_values(order.get("count"), order.get("price"))
    return kalshi_endpoint("POST", "/portfolio/events/orders/batched", body={"orders": orders})


@mcp.tool()
def cancel_orders_v2_batch(orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Cancel multiple V2 orders in one request."""
    if not orders: raise ValueError("orders must not be empty")
    if len(orders) > 100: raise ValueError("batch cancellation limit exceeded")
    if any(not isinstance(o, dict) or not o.get("order_id") for o in orders): raise ValueError("each cancellation requires order_id")
    return kalshi_endpoint("DELETE", "/portfolio/events/orders/batched", body={"orders": orders})
@mcp.tool()
def cancel_all_orders() -> dict[str, Any]:
    """Cancel all open orders for the authenticated account."""
    return kalshi_endpoint("DELETE", "/portfolio/events/orders")


@mcp.tool()
def cancel_order_v2(order_id: str, exchange_index: int | None = None) -> dict[str, Any]:
    """Cancel one V2 event-market order."""
    params = {"exchange_index": exchange_index} if exchange_index is not None else None
    return kalshi_endpoint("DELETE", f"/portfolio/events/orders/{order_id}", params=params)


@mcp.tool()
def amend_order_v2(order_id: str, ticker: str, side: str, price: str, count: str, client_order_id: str | None = None, updated_client_order_id: str | None = None) -> dict[str, Any]:
    """Amend a V2 order with guarded fixed-point price and count fields."""
    if side not in {"bid", "ask"}: raise ValueError("side must be bid or ask")
    _validate_order_values(count, price)
    body = {"ticker": ticker, "side": side, "price": price, "count": count}
    if client_order_id is not None: body["client_order_id"] = client_order_id
    if updated_client_order_id is not None: body["updated_client_order_id"] = updated_client_order_id
    return kalshi_endpoint("POST", f"/portfolio/events/orders/{order_id}/amend", body=body)


@mcp.tool()
def decrease_order_v2(order_id: str, reduce_by: str | None = None, reduce_to: str | None = None, market_ticker: str | None = None) -> dict[str, Any]:
    """Decrease a V2 order by a fixed-point quantity; provide exactly one reduction mode."""
    if (reduce_by is None) == (reduce_to is None): raise ValueError("provide exactly one of reduce_by or reduce_to")
    value = reduce_by or reduce_to
    _fixed_decimal(value, "reduction")
    if Decimal(value) <= 0: raise ValueError("reduction must be positive")
    body = {"reduce_by": reduce_by, "reduce_to": reduce_to}
    if market_ticker is not None: body["market_ticker"] = market_ticker
    return kalshi_endpoint("POST", f"/portfolio/events/orders/{order_id}/decrease", body=body)
@mcp.tool()
def place_order(ticker: str, side: str, action: str, count: int, price_cents: int, client_order_id: str | None = None) -> dict[str, Any]:
    """Place a bounded legacy event-market order. Prefer create_order_v2 for current API semantics."""
    if side not in {"yes", "no"} or action not in {"buy", "sell"}: raise ValueError("side must be yes/no and action buy/sell")
    if count < 1 or count > _limit("KALSHI_MAX_ORDER_CONTRACTS", 100): raise ValueError("contract limit exceeded")
    if price_cents < 1 or price_cents > 99: raise ValueError("price_cents must be 1..99")
    if count * price_cents > _limit("KALSHI_MAX_ORDER_CENTS", 100): raise ValueError("order notional limit exceeded")
    body = {"ticker": ticker, "client_order_id": client_order_id or str(uuid.uuid4()), "side": side, "action": action, "count": count, "yes_price": price_cents if side == "yes" else None, "no_price": price_cents if side == "no" else None}
    return _request("POST", "/portfolio/orders", body=body, auth=True)


@mcp.tool()
def cancel_order(order_id: str) -> dict[str, Any]:
    """Cancel one authenticated order."""
    return _request("DELETE", f"/portfolio/orders/{order_id}", auth=True)


def main() -> None:
    mcp.run(transport="stdio")

if __name__ == "__main__": main()
