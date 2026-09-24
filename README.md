# Kalshi MCP Server

An open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for Kalshi’s Trade API v2. It gives MCP clients a practical interface for researching markets, reading an account, and managing orders with Kalshi’s RSA-PSS authentication.

> **Important:** This server can place and cancel real orders. It defaults to Kalshi’s **production** environment; set `KALSHI_ENV=demo` explicitly for demo trading. Review the code, configure conservative limits, and test with demo credentials before using production credentials.

## What it provides

### Market research

- Exchange status
- Markets and events
- Individual market data
- Order books
- Public trades

### Account and trading

- Balance, positions, and orders
- Current V2 order placement
- Batched order placement
- Individual and bulk cancellation
- Order amendment and quantity reduction
- Order-group creation, limits, reset, trigger, listing, and deletion
- Legacy order helpers retained for compatibility

The `kalshi_endpoint` tool also exposes the validated operations from the bundled Kalshi OpenAPI catalog. It checks the HTTP method, endpoint template, query parameters, request body, and authentication requirements before making a request.

## Requirements

- Python 3.11 or newer
- A Kalshi API key and RSA private key
- An MCP-compatible client

## Installation

From a released package or a source checkout:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For environments where `cryptography` must use prebuilt wheels:

```bash
python -m pip install --only-binary=:all: httpx cryptography mcp
```

## Configuration

Copy the example configuration and provide credentials through your secret manager or process environment:

```bash
cp .env.example .env
```

Required for authenticated tools:

```text
KALSHI_API_KEY_ID=your-api-key-id
KALSHI_PRIVATE_KEY_BASE64=base64-encoded-PEM-private-key
```

Environment selection is explicit. Production is the default:

```bash
# Default: production
KALSHI_ENV=production

# Explicit demo selection
KALSHI_ENV=demo
```

Optional settings:

- `KALSHI_BASE_URL` — override the selected API base URL.
- `KALSHI_PRIVATE_KEY_BASE64` — base64-encoded PEM RSA private key; this avoids requiring the MCP host to provide filesystem access.
- `KALSHI_MAX_ORDER_CONTRACTS` — maximum contracts per order.
- `KALSHI_MAX_ORDER_CENTS` — maximum per-order notional in cents.
- `KALSHI_DAILY_CONTRACT_LIMIT` — intended daily contract ceiling.
- `KALSHI_MIN_CASH_CENTS` — intended minimum cash reserve.

Never commit `.env`, private keys, API credentials, or request signatures.

## Running the server

Run the stdio server directly:

```bash
KALSHI_ENV=production kalshi-degen-mcp
```

Example configuration for an MCP client that supports local stdio servers:

```json
{
  "mcpServers": {
    "kalshi": {
      "command": "/absolute/path/to/.venv/bin/kalshi-degen-mcp",
      "env": {
        "KALSHI_ENV": "production",
        "KALSHI_API_KEY_ID": "your-api-key-id",
        "KALSHI_PRIVATE_KEY_BASE64": "base64-encoded-PEM-private-key"
      }
    }
  }
}
```

For Hermes, use the same executable under its local MCP server configuration.

## Safety and operational guidance

- Production is the default; select demo explicitly with `KALSHI_ENV=demo`.
- Production writes are not silently redirected to demo.
- Order tools reject invalid sides, prices, counts, and unsupported fixed-point formats.
- Per-order contract and notional caps are configurable.
- Start with demo credentials and small limits.
- Confirm the selected environment and account before placing or cancelling orders.
- Treat AI-generated trade decisions as untrusted until reviewed.

This project does not provide investment advice and does not guarantee profitable or error-free trading. You are responsible for credentials, orders, losses, and compliance with Kalshi’s terms.

## Development

```bash
. .venv/bin/activate
pytest -q
python -m py_compile kalshi_mcp/*.py
```

The test suite covers endpoint resolution, request validation, order safety checks, batch validation, order-group validation, and strict fixed-point input rejection.

## License

See the repository for licensing information.
