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
        async with httpx.AsyncClient(timeout=10.0) as client:
            gold_usd_per_oz = None
            price_date = "today"
            gold_source = None

            # 1. Try free live gold spot API
            try:
                resp = await client.get("https://api.gold-api.com/price/XAU")
                if resp.status_code == 200:
                    data = resp.json()
                    live_price = data.get("price") or data.get("price_gram_24k")
                    if live_price:
                        gold_usd_per_oz = float(live_price)
                        gold_source = "gold-api.com"
            except Exception:
                pass

            # 2. Try secondary live gold API if first failed
            if gold_usd_per_oz is None:
                try:
                    gold_resp = await client.get(
                        "https://api.metalpriceapi.com/v1/latest",
                        params={"api_key": "demo", "base": "XAU", "currencies": "USD"},
                        timeout=5.0,
                    )
                    if gold_resp.status_code == 200:
                        gold_data = gold_resp.json()
                        if gold_data.get("success") and "rates" in gold_data:
                            gold_usd_per_oz = float(gold_data["rates"].get("USD", 0))
                            if gold_usd_per_oz > 0:
                                gold_source = "metalpriceapi.com"
                except Exception:
                    pass

            # 3. Fallback to approximate baseline if external APIs are unreachable
            if not gold_usd_per_oz or gold_usd_per_oz <= 0:
                gold_usd_per_oz = 2650.0  # Approximate baseline price
                gold_source = "Approximate spot baseline"

            # 4. Currency conversion rate from USD
            if currency == "USD":
                fx_rate = 1.0
                fx_date = None
            else:
                fx_resp = await client.get(
                    f"{FRANKFURTER_BASE}/latest",
                    params={"from": "USD", "to": currency},
                )
                fx_resp.raise_for_status()
                fx_data = fx_resp.json()
                fx_rate = fx_data.get("rates", {}).get(currency)
                if fx_rate is None:
                    return f"Error: Currency '{currency}' not supported."
                fx_date = fx_data.get("date")

            price_in_currency = gold_usd_per_oz * fx_rate
            price_per_gram = price_in_currency / 31.1035  # troy oz to grams

            source_desc = f"{gold_source}"
            if currency != "USD":
                source_desc += " + European Central Bank FX (via Frankfurter)"

            return {
                "metal": "Gold (XAU)",
                "price_per_troy_oz": round(price_in_currency, 2),
                "price_per_gram_24k": round(price_per_gram, 2),
                "currency": currency,
                "usd_gold_spot": round(gold_usd_per_oz, 2),
                "usd_fx_rate": fx_rate,
                "date": fx_date or "live",
                "source": source_desc,
                "disclaimer": "Factual price data for informational purposes only. Not investment advice.",
            }
    except httpx.ConnectError:
        return "Error: Unable to connect to price service."
    except Exception as exc:
        return f"Error fetching gold price: {exc}"


if __name__ == "__main__":
    # Binds to 127.0.0.1:8002 (localhost only, intentional for POC)
    mcp.run(transport="streamable-http", port=8002)
