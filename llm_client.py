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
    session_token: str
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
            f"You are a personal expense tracking assistant for user '{ctx.deps.username}'.\n\n"
            f"CURRENT SYSTEM DATE: {today_str} (YYYY-MM-DD).\n\n"
            f"SESSION TOKEN: {ctx.deps.session_token}\n\n"
            f"CRITICAL RULES:\n"
            f"- AUTHENTICATION: Your session token is '{ctx.deps.session_token}'. You MUST pass this as the `session_token` argument to EVERY tool call. NEVER invent, modify, or omit the token.\n"
            f"- CURRENCY: All amounts are in Indian Rupees (₹). Always format currency using the ₹ symbol (e.g. ₹50, ₹1,200.00). Never use dollar signs ($).\n"
            f"- NEVER call `create_user_session` — that tool is for the login system only.\n\n"
            f"AVAILABLE TOOLS & WHEN TO USE THEM:\n"
            f"- `add_expense`: When the user wants to add/record a new expense.\n"
            f"- `list_expenses`: When the user asks to see, list, or view their expenses.\n"
            f"- `delete_expense`: When the user wants to remove an expense by ID.\n"
            f"- `set_budget`: When the user wants to set or change their monthly budget.\n"
            f"- `can_i_afford`: When the user asks 'can I afford X?' or similar affordability questions. Extract item_description and item_cost from the message. IMPORTANT: If the tool returns an error with 'no_budget_set', ask the user to set their monthly budget first (e.g. 'What is your monthly budget? You can say something like: Set my budget to ₹30,000').\n"
            f"- `get_expense_schema`: Call this BEFORE `query_expenses` to learn the available columns.\n"
            f"- `query_expenses`: For analytical questions like 'total spending on food', 'average expense last month', etc. Write a SELECT query using ONLY the columns from get_expense_schema. Do NOT include user_id in your SQL — it is auto-injected.\n\n"
            f"DATE HANDLING:\n"
            f"- If the user specifies a date, resolve it relative to today ({today_str}).\n"
            f"- If no date is mentioned for add_expense, use today's date ({today_str}).\n"
            f"- NEVER invent dates from 2024 or 2025.\n\n"
            f"DISPLAY RULES:\n"
            f"- When listing expenses, present them in a readable markdown table format with amounts in ₹.\n"
            f"- If list_expenses returns empty, state clearly the user has no recorded expenses. NEVER invent fake data.\n"
            f"- When showing can_i_afford results, present the analysis conversationally using the structured data.\n"
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
    session_token: str,
    username: str,
) -> Iterator[str]:
    """Yield text deltas from the LLM. Isolated from UI and persistence."""
    agent = get_agent()
    prior = _to_model_messages(history)
    deps = Deps(session_token=session_token, username=username)
    with agent.run_stream_sync(
        user_message,
        deps=deps,
        # The API wants None for "no prior turns", not an empty list.
        message_history=prior or None,
    ) as result:
        # debounce_by=None streams each token instead of batching them.
        yield from result.stream_text(delta=True, debounce_by=None)