import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from kalshi_mcp.server import _resolve_endpoint, _base_url, kalshi_endpoint, _request_schema, _validate_body

def test_all_catalog_entries_resolve():
    from kalshi_mcp.endpoint_catalog import ENDPOINTS
    for method, endpoint in ENDPOINTS:
        assert _resolve_endpoint(method, endpoint) == (method, endpoint)

def test_placeholder_resolves():
    assert _resolve_endpoint('GET', '/markets/ABC') == ('GET', '/markets/{ticker}')


def test_environment_switch(monkeypatch):
    monkeypatch.setenv('KALSHI_ENV', 'production')
    assert 'external-api.kalshi.com' in _base_url()


def test_generic_rejects_unknown_query():
    try:
        kalshi_endpoint('GET', '/exchange/status', {'nope': 1})
    except ValueError as exc:
        assert 'unknown query' in str(exc)
    else:
        raise AssertionError('expected validation failure')

def test_generic_requires_body():
    try:
        kalshi_endpoint('POST', '/portfolio/events/orders')
    except ValueError as exc:
        assert 'body is required' in str(exc)
    else:
        raise AssertionError('expected validation failure')


def test_fixed_point_rejects_float_edges():
    from kalshi_mcp.server import create_order_v2
    for count, price in [('1e1', '0.5'), ('nan', '0.5'), (10, '0.5')]:
        try: create_order_v2('T', 'bid', count, price)
        except ValueError: pass
        else: raise AssertionError('expected strict fixed-point rejection')


    from kalshi_mcp.server import create_order_group, update_order_group_limit
    try: create_order_group()
    except ValueError as exc: assert 'provide' in str(exc)
    else: raise AssertionError('expected missing limit rejection')
    try: update_order_group_limit('x')
    except ValueError as exc: assert 'provide' in str(exc)
    else: raise AssertionError('expected missing limit rejection')

    from kalshi_mcp.server import create_orders_v2_batch, cancel_orders_v2_batch
    try: create_orders_v2_batch([])
    except ValueError as exc: assert 'not be empty' in str(exc)
    else: raise AssertionError('expected empty batch rejection')
    try: cancel_orders_v2_batch([{}])
    except ValueError as exc: assert 'order_id' in str(exc)
    else: raise AssertionError('expected missing order id rejection')

    from kalshi_mcp.endpoint_catalog import ENDPOINT_METADATA
    metadata = ENDPOINT_METADATA[('POST', '/portfolio/events/orders')]
    try:
        _validate_body(metadata, {})
    except ValueError as exc:
        assert 'invalid request body' in str(exc)
    else:
        raise AssertionError('expected schema validation failure')
