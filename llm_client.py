import os
from collections.abc import Iterator, Sequence
from functools import lru_cache

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

import stock_data
from config import Settings, get_settings
from schemas import ChatMessage, PricePoint, Role


def _build_agent(settings: Settings) -> Agent:
    if not settings.llm_api_key or settings.llm_api_key == "your-google-api-key":
        raise ValueError(
            "LLM_API_KEY is not set. Please add your Gemini API key to the .env file."
        )

    model = GoogleModel(
        settings.llm_model,
        provider=GoogleProvider(api_key=settings.llm_api_key),
    )
    agent = Agent(
        model,
        instructions=(
            "You are a stock market assistant. You have tools available to fetch "
            "real, current stock prices and historical data — always use them "
            "instead of saying you lack access to real-time data. "
            "If a tool returns an error message (for example, if a symbol is not found or delisted), "
            "explain the issue politely to the user and suggest checking the ticker symbol."
        ),
    )

    # tool_plain: no RunContext needed. Bodies call stock_data today;
    # swap those calls for an MCP client later.

    @agent.tool_plain
    def get_stock_price(ticker: str) -> str:
        """Get the latest daily closing price for a stock ticker symbol."""
        try:
            price = stock_data.get_stock_price(ticker)
            return f"{price:.2f}"
        except Exception as e:
            return f"Error fetching price for {ticker}: {e}"

    @agent.tool_plain
    def get_price_history(ticker: str, days: int) -> list[PricePoint] | str:
        """Get historical closing prices for a ticker over the last N days."""
        try:
            return stock_data.get_price_history(ticker, days)
        except Exception as e:
            return f"Error fetching price history for {ticker}: {e}"

    @agent.tool_plain
    def compare_stocks(ticker_a: str, ticker_b: str) -> str:
        """Compare current prices of two tickers and say which is higher."""
        try:
            return stock_data.compare_stocks(ticker_a, ticker_b)
        except Exception as e:
            return f"Error comparing {ticker_a} and {ticker_b}: {e}"

    @agent.tool_plain
    def moving_average(ticker: str, days: int, window: int) -> str:
        """Simple moving average of closing prices over a window of days."""
        try:
            ma = stock_data.moving_average(ticker, days, window)
            return f"{ma:.2f}"
        except Exception as e:
            return f"Error calculating moving average for {ticker}: {e}"

    return agent


@lru_cache(maxsize=1)
def get_agent() -> Agent:
    # Cache so Streamlit does not rebuild the Gemini agent on every rerun.
    return _build_agent(get_settings())


def _to_model_messages(history: Sequence[ChatMessage]) -> list[ModelMessage]:
    # Pydantic AI stores user turns as ModelRequest and assistant turns as ModelResponse.
    messages: list[ModelMessage] = []
    for item in history:
        if item.role == Role.user:
            if messages and isinstance(messages[-1], ModelRequest):
                messages[-1].parts.append(UserPromptPart(content=item.content))
            else:
                messages.append(ModelRequest(parts=[UserPromptPart(content=item.content)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=item.content)]))
    return messages


def stream_chat_response(
    user_message: str,
    history: Sequence[ChatMessage] = (),
) -> Iterator[str]:
    """Yield text deltas from the LLM. Isolated from UI and persistence."""
    agent = get_agent()
    prior = _to_model_messages(history)
    with agent.run_stream_sync(
        user_message,
        # The API wants None for "no prior turns", not an empty list.
        message_history=prior or None,
    ) as result:
        # debounce_by=None streams each token instead of batching them.
        yield from result.stream_text(delta=True, debounce_by=None)
