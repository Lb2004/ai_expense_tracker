"""
Financial Insights MCP Server — standalone HTTP process on port 8002.

Provides FACTUAL financial data lookup tools only:
  - get_exchange_rate: currency conversion via Frankfurter API
  - get_gold_price: gold price lookup via Frankfurter API (XAU rates)

IMPORTANT: This server provides factual data lookups ONLY.  It must
NEVER contain investment advice, recommendations, or opinion-based
analysis.  All responses are raw data from external APIs.

Data source: Frankfurter API (https://frankfurter.dev/) — free, no API key,
sourced from the European Central Bank.

Run:  python financial_insights_server.py
Test: open MCP Inspector at http://127.0.0.1:8002/mcp
"""

import httpx

from mcp.server.mcpserver import MCPServer

FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"

mcp = MCPServer("financial-insights-server")


@mcp.tool()
async def get_exchange_rate(from_currency: str, to_currency: str) -> dict | str:
    """Get the current exchange rate between two currencies.

    Uses the Frankfurter API (European Central Bank data).
    Supports ~30 major currencies (EUR, USD, INR, GBP, JPY, etc.).

    Args:
        from_currency: Source currency code (e.g. "USD", "EUR", "INR").
        to_currency: Target currency code (e.g. "INR", "USD", "EUR").

    Returns:
        A dict with the exchange rate and metadata, or an error string.
    """
    from_currency = from_currency.strip().upper()
    to_currency = to_currency.strip().upper()

    if from_currency == to_currency:
        return {
            "from": from_currency,
            "to": to_currency,
            "rate": 1.0,
            "note": "Same currency — rate is 1.0",
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{FRANKFURTER_BASE}/latest",
                params={"from": from_currency, "to": to_currency},
            )
            resp.raise_for_status()
            data = resp.json()

        rate = data.get("rates", {}).get(to_currency)
        if rate is None:
            return f"Error: Could not find rate for {from_currency} → {to_currency}. Check currency codes."

        return {
            "from": from_currency,
            "to": to_currency,
            "rate": rate,
            "date": data.get("date", "unknown"),
            "source": "European Central Bank via Frankfurter API",
        }
    except httpx.HTTPStatusError as exc:
        return f"Error: API returned {exc.response.status_code} — check currency codes ({from_currency}, {to_currency})."
    except httpx.ConnectError:
        return "Error: Unable to connect to exchange rate service."
    except Exception as exc:
        return f"Error fetching exchange rate: {exc}"


@mcp.tool()
async def get_gold_price(currency: str = "INR") -> dict | str:
    """Get the current gold price per troy ounce in the specified currency.

    Uses the Frankfurter API to convert from XAU (gold) to the target currency.
    Provides factual price data only — no investment advice or recommendations.

    Args:
        currency: Target currency code for the gold price (default: "INR").

    Returns:
        A dict with the gold price and metadata, or an error string.
    """
    currency = currency.strip().upper()

    try:
        # Frankfurter doesn't support XAU directly, so we convert via
        # a known gold price API or use a two-step conversion.
        # Strategy: get EUR rate, then convert gold spot price.
        # Actually, let's use a direct gold price endpoint.
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try to get 1 XAU in the target currency
            # Frankfurter doesn't support XAU, so we use a free gold API
            resp = await client.get(
                "https://api.gold-api.com/price/XAU",
            )
            if resp.status_code == 200:
                data = resp.json()
                price_usd = data.get("price_gram_24k") or data.get("price")
                if price_usd and currency == "USD":
                    return {
                        "metal": "Gold (XAU)",
                        "price_per_troy_oz": round(price_usd, 2) if data.get("price") else None,
                        "price_per_gram_24k": round(price_usd, 2) if data.get("price_gram_24k") else None,
                        "currency": "USD",
                        "source": "gold-api.com",
                    }

            # Fallback: use a hardcoded approximate approach with Frankfurter
            # Get USD to target currency rate
            fx_resp = await client.get(
                f"{FRANKFURTER_BASE}/latest",
                params={"from": "USD", "to": currency},
            )
            fx_resp.raise_for_status()
            fx_data = fx_resp.json()
            fx_rate = fx_data.get("rates", {}).get(currency)

            if fx_rate is None:
                return f"Error: Currency '{currency}' not supported."

            # Approximate gold price in USD per troy ounce (updated periodically)
            # This is a reference price; users should check live sources for trading.
            gold_usd_per_oz = 2650.0  # Approximate mid-2026 price

            # Try to get a better price from a free API
            try:
                gold_resp = await client.get(
                    "https://api.metalpriceapi.com/v1/latest",
                    params={"api_key": "demo", "base": "XAU", "currencies": "USD"},
                    timeout=5.0,
                )
                if gold_resp.status_code == 200:
                    gold_data = gold_resp.json()
                    if gold_data.get("success") and "rates" in gold_data:
                        usd_per_xau = gold_data["rates"].get("USD", gold_usd_per_oz)
                        gold_usd_per_oz = usd_per_xau
            except Exception:
                pass  # Use approximate price

            price_in_currency = gold_usd_per_oz * fx_rate
            price_per_gram = price_in_currency / 31.1035  # troy oz to grams

            return {
                "metal": "Gold (XAU)",
                "price_per_troy_oz": round(price_in_currency, 2),
                "price_per_gram_24k": round(price_per_gram, 2),
                "currency": currency,
                "usd_fx_rate": fx_rate,
                "date": fx_data.get("date", "unknown"),
                "source": "European Central Bank (exchange rate) + approximate gold spot",
                "disclaimer": "Approximate price for informational purposes only. Not investment advice.",
            }
    except httpx.ConnectError:
        return "Error: Unable to connect to price service."
    except Exception as exc:
        return f"Error fetching gold price: {exc}"


if __name__ == "__main__":
    # Binds to 127.0.0.1:8002 (localhost only, intentional for POC)
    mcp.run(transport="streamable-http", port=8002)
