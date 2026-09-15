import asyncio
import yfinance as yf
from mcp.server.mcpserver import MCPServer
from schemas import PricePoint


mcp = MCPServer("stock-server")


@mcp.tool()
async def get_stock_price(ticker: str) -> float:
    """Last closing price on the latest daily bar (not a live tick)."""
    ticker = ticker.strip().upper()

    def _fetch() -> float:
        stock = yf.Ticker(ticker)
        history = stock.history(period="1d")
        if history.empty:
            raise ValueError(f"No price data for ticker {ticker!r}. Check the symbol.")
        return float(history["Close"].iloc[-1])

    return await asyncio.to_thread(_fetch)


@mcp.tool()
async def get_price_history(ticker: str, days: int) -> list[PricePoint]:
    """Daily closing prices for the past `days` days."""
    ticker = ticker.strip().upper()

    if days <= 0:
        raise ValueError(f"Days must be positive, got {days}")

    def _fetch() -> list[PricePoint]:
        history = yf.Ticker(ticker).history(period=f"{days}d")
        if history.empty:
            raise ValueError(f"No price data for ticker {ticker!r}. Check the symbol.")
        return [
            PricePoint(date=str(index.date()), close=float(row["Close"]))
            for index, row in history.iterrows()
        ]

    return await asyncio.to_thread(_fetch)


@mcp.tool()
async def compare_stocks(ticker_a: str, ticker_b: str) -> str:
    """Compare current prices of two stock tickers and say which is higher."""
    ticker_a = ticker_a.strip().upper()
    ticker_b = ticker_b.strip().upper()

    try:
        price_a = await get_stock_price(ticker_a)
    except Exception as exc:
        return f"Could not retrieve price for {ticker_a}: {exc}"

    try:
        price_b = await get_stock_price(ticker_b)
    except Exception as exc:
        return f"Could not retrieve price for {ticker_b}: {exc}"

    if price_a > price_b:
        return f"{ticker_a} (${price_a:.2f}) is higher than {ticker_b} (${price_b:.2f})"
    if price_b > price_a:
        return f"{ticker_b} (${price_b:.2f}) is higher than {ticker_a} (${price_a:.2f})"
    return f"{ticker_a} and {ticker_b} are equal at ${price_a:.2f}"


@mcp.tool()
async def moving_average(ticker: str, days: int, window: int) -> float:
    """Compute the simple moving average of a stock's closing prices."""
    if window <= 0:
        raise ValueError(f"Window must be positive, got {window}")
    if days <= 0:
        raise ValueError(f"Days must be positive, got {days}")

    fetch_days = max(days, window)
    history = await get_price_history(ticker, fetch_days)

    closes = [point.close for point in history]
    if not closes:
        raise ValueError(f"No price history for {ticker}")

    size = min(window, len(closes))
    return sum(closes[-size:]) / size


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8000)