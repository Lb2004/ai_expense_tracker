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

    # Fix #6: MCP server URL from config instead of hardcoded string.
    expense_client = FastMCPClient(settings.mcp_server_url)
    expense_toolset = MCPToolset(expense_client)

    # Feature #20: Second MCP server for financial insights (exchange rates, gold prices).
    financial_client = FastMCPClient(settings.financial_insights_url)
    financial_toolset = MCPToolset(financial_client)

    def dynamic_instructions(ctx: RunContext[Deps]) -> str:
        today_str = date.today().isoformat()
        return (
            f"You are a personal expense tracking assistant for user '{ctx.deps.username}'.\n\n"
            f"CURRENT SYSTEM DATE: {today_str} (YYYY-MM-DD).\n\n"
            # Known limitation (#12): The session token is embedded directly
            # in this system prompt string.  This carries a theoretical
            # prompt-injection risk — a crafted user message could try to
            # get the model to reveal its system prompt (and thus the token).
            # This is an accepted limitation of prompt-embedded credentials
            # in current function-calling APIs; a production system would
            # use transport-level auth (HTTP headers, OAuth) instead of
            # passing the token through the LLM's instruction context.
            f"SESSION TOKEN: {ctx.deps.session_token}\n\n"
            f"CRITICAL RULES:\n"
            # Known limitation (#13): The system prompt *instructs* the LLM
            # to include session_token on every tool call, but there is no
            # structural enforcement if it fails to comply.  The server
            # correctly rejects calls with missing/wrong session_token
            # (a graceful failure, not a security hole), so the worst case
            # is the LLM retrying after an error response.
            f"- AUTHENTICATION: Your session token is '{ctx.deps.session_token}'. You MUST pass this as the `session_token` argument to EVERY expense tool call. NEVER invent, modify, or omit the token.\n"
            f"- CURRENCY: All amounts are in Indian Rupees (₹). Always format currency using the ₹ symbol (e.g. ₹50, ₹1,200.00). Never use dollar signs ($).\n\n"
            f"AVAILABLE EXPENSE TOOLS & WHEN TO USE THEM:\n"
            f"- `add_expense`: When the user wants to add/record a new expense.\n"
            f"- `list_expenses`: When the user asks to see, list, or view their expenses.\n"
            f"- `delete_expense`: When the user wants to remove an expense by ID.\n"
            f"- `set_budget`: When the user wants to set or change their monthly budget.\n"
            f"- `can_i_afford`: When the user asks 'can I afford X?' or similar affordability questions. Extract item_description and item_cost from the message. IMPORTANT: If the tool returns an error with 'no_budget_set', ask the user to set their monthly budget first (e.g. 'What is your monthly budget? You can say something like: Set my budget to ₹30,000').\n"
            f"- `get_expense_schema`: Call this BEFORE `query_expenses` to learn the available columns.\n"
            f"- `query_expenses`: For analytical questions like 'total spending on food', 'average expense last month', etc. Write a SELECT query using ONLY the columns from get_expense_schema against the 'my_expenses' table. Do NOT include user_id in your SQL — it is auto-injected via a VIEW.\n\n"
            f"FINANCIAL INSIGHTS TOOLS (no session_token needed):\n"
            f"- `get_exchange_rate`: Convert between currencies. Use when the user asks about exchange rates or currency conversion.\n"
            f"- `get_gold_price`: Get the current gold price in a given currency. Use when the user asks about gold prices.\n\n"
            f"DATE HANDLING:\n"
            f"- If the user specifies a date, resolve it relative to today ({today_str}).\n"
            f"- If no date is mentioned for add_expense, use today's date ({today_str}).\n"
            f"- NEVER invent dates from 2024 or 2025.\n\n"
            f"DISPLAY RULES:\n"
            f"- When listing expenses, present them in a readable markdown table format with amounts in ₹.\n"
            f"- If list_expenses returns empty, state clearly the user has no recorded expenses. NEVER invent fake data.\n"
            f"- When showing can_i_afford results, present the analysis conversationally using the structured data.\n"
            f"- When deleting, confirm which expense was removed.\n"
            f"- NEVER provide investment advice or recommendations. Only present factual data from financial tools."
        )

    return Agent(
        model,
        instructions=dynamic_instructions,
        toolsets=[expense_toolset, financial_toolset],
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