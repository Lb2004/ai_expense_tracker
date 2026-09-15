import os
from collections.abc import Iterator, Sequence
from functools import lru_cache

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.mcp import MCPToolset, FastMCPClient
from pydantic_ai.toolsets import FilteredToolset

from config import Settings, get_settings
from schemas import ChatMessage, Role

AllowedAlphavantageTools = {"TOOL_LIST", "TOOL_GET", "TOOL_CALL"}


def _only_discovery_tools(ctx, tool_def) -> bool:
    return tool_def.name in AllowedAlphavantageTools


def _build_agent(settings: Settings) -> Agent:
    if not settings.llm_api_key or settings.llm_api_key == "your-google-api-key":
        raise ValueError(
            "LLM_API_KEY is not set. Please add your Gemini API key to the .env file."
        )

    model = GoogleModel(
        settings.llm_model,
        provider=GoogleProvider(api_key=settings.llm_api_key),
    )

    # Connects to mcpserver.py running as its own HTTP process on port 8000.
    client = FastMCPClient("http://127.0.0.1:8000/mcp")
    toolset = MCPToolset(client)
    toolsets = [toolset]

    instructions = (
        "You are a stock market assistant. You have tools available to fetch "
        "real, current stock prices and historical data — always use them "
        "instead of saying you lack access to real-time data. "
    )

    if settings.alphavantage_api_key and settings.alphavantage_api_key.strip():
        alphavantage_client = FastMCPClient(
            f"https://mcp.alphavantage.co/mcp?apikey={settings.alphavantage_api_key.strip()}"
        )
        alphavantage_full_toolset = MCPToolset(alphavantage_client)
        alphavantage_toolset = FilteredToolset(alphavantage_full_toolset, _only_discovery_tools)
        toolsets.append(alphavantage_toolset)
        instructions += (
            "For Alpha Vantage data, use TOOL_LIST or TOOL_GET to discover the right "
            "tool name, then call it via TOOL_CALL with tool_name and arguments. "
        )

    instructions += (
        "If a tool returns an error message (for example, if a symbol is not found or delisted), "
        "explain the issue politely to the user and suggest checking the ticker symbol."
    )

    return Agent(
        model,
        instructions=instructions,
        toolsets=toolsets,
    )


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