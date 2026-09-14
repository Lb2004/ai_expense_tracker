"""Yahoo Finance helpers. This is the layer to wrap in an MCP server later."""

import yfinance as yf

from schemas import PricePoint


def get_stock_price(ticker: str) -> float:
    """Last closing price on the latest daily bar (not a live tick)."""
    ticker = ticker.strip().upper()
    history = yf.Ticker(ticker).history(period="1d")
    if history.empty:
        raise ValueError(f"No price data for ticker {ticker!r}. Check the symbol.")
    return float(history["Close"].iloc[-1])


def get_price_history(ticker: str, days: int) -> list[PricePoint]:
    """Daily closing prices for the past `days` days."""
    ticker = ticker.strip().upper()
    if days <= 0:
        raise ValueError(f"Days must be positive, got {days}")
    history = yf.Ticker(ticker).history(period=f"{days}d")
    if history.empty:
        raise ValueError(f"No price data for ticker {ticker!r}. Check the symbol.")
    return [
        PricePoint(date=str(index.date()), close=float(row["Close"]))
        for index, row in history.iterrows()
    ]


def compare_stocks(ticker_a: str, ticker_b: str) -> str:
    ticker_a = ticker_a.strip().upper()
    ticker_b = ticker_b.strip().upper()
    price_a = get_stock_price(ticker_a)
    price_b = get_stock_price(ticker_b)
    if price_a > price_b:
        return f"{ticker_a} (${price_a:.2f}) is higher than {ticker_b} (${price_b:.2f})"
    if price_b > price_a:
        return f"{ticker_b} (${price_b:.2f}) is higher than {ticker_a} (${price_a:.2f})"
    return f"{ticker_a} and {ticker_b} are equal at ${price_a:.2f}"


def moving_average(ticker: str, days: int, window: int) -> float:
    if window <= 0:
        raise ValueError(f"Window must be positive, got {window}")
    if days <= 0:
        raise ValueError(f"Days must be positive, got {days}")
    fetch_days = max(days, window)
    closes = [point.close for point in get_price_history(ticker, fetch_days)]
    if not closes:
        raise ValueError(f"No price history for {ticker}")
    # If fewer bars exist than `window`, average all of the bars we have.
    size = min(window, len(closes))
    return sum(closes[-size:]) / size

