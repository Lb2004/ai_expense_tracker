import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.mcp import MCPToolset, FastMCPClient

from config import Settings, get_settings
from schemas import ChatMessage, Role


@dataclass
class Deps:
    """Dependencies injected into the agent's RunContext."""
    user_id: int
    username: str


def _build_agent(settings: Settings) -> Agent[Deps, str]:
    if not settings.llm_api_key or settings.llm_api_key == "your-google-api-key":
        raise ValueError(
            "LLM_API_KEY is not set. Please add your Gemini API key to the .env file."
        )

    model = GoogleModel(
        settings.llm_model,
        provider=GoogleProvider(api_key=settings.llm_api_key),
    )

    # Connects to expense_mcp_server.py running as its own HTTP process on port 8001.
    client = FastMCPClient("http://127.0.0.1:8001/mcp")
    toolset = MCPToolset(client)

    def dynamic_instructions(ctx: RunContext[Deps]) -> str:
        today_str = date.today().isoformat()
        return (
            f"You are a personal expense tracking assistant for user '{ctx.deps.username}' (user_id={ctx.deps.user_id}).\n\n"
            f"CURRENT SYSTEM DATE: {today_str} (YYYY-MM-DD).\n\n"
            f"CRITICAL RULES:\n"
            f"- The active user's ID is EXACTLY {ctx.deps.user_id}.\n"
            f"- CURRENCY: All amounts are in Indian Rupees (₹). Always format currency using the ₹ symbol (e.g. ₹50, ₹1,200.00). Never use dollar signs ($).\n"
            f"- Whenever calling ANY tool (`add_expense`, `list_expenses`, `delete_expense`), you MUST ALWAYS pass user_id={ctx.deps.user_id}. NEVER pass any other user_id.\n"
            f"- When asked about expenses, spending, or purchases, you MUST call `list_expenses` with user_id={ctx.deps.user_id}.\n"
            f"- If `list_expenses` returns an empty list, state clearly that user '{ctx.deps.username}' has no recorded expenses yet. NEVER invent, assume, or display fake expenses or demo data.\n"
            f"- When the user asks to add an expense, extract the amount, category, description, and date from their message.\n"
            f"  * DATE HANDLING: If the user specifies a date (e.g. 'yesterday' or a specific date), resolve it relative to today ({today_str}). If no date is mentioned, you MUST use today's date ({today_str}). NEVER invent dates from 2024 or 2025.\n"
            f"- When listing expenses, present them in a readable markdown table format with amounts in ₹.\n"
            f"- When deleting, confirm which expense was removed."
        )

    return Agent(
        model,
        instructions=dynamic_instructions,
        toolsets=[toolset],
        deps_type=Deps,
    )


@lru_cache(maxsize=1)
def get_agent() -> Agent[Deps, str]:
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
    *,
    user_id: int,
    username: str,
) -> Iterator[str]:
    """Yield text deltas from the LLM. Isolated from UI and persistence."""
    agent = get_agent()
    prior = _to_model_messages(history)
    deps = Deps(user_id=user_id, username=username)
    with agent.run_stream_sync(
        user_message,
        deps=deps,
        # The API wants None for "no prior turns", not an empty list.
        message_history=prior or None,
    ) as result:
        # debounce_by=None streams each token instead of batching them.
        yield from result.stream_text(delta=True, debounce_by=None)