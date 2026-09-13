from collections.abc import Iterator, Sequence
from functools import lru_cache

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from config import Settings, get_settings
from schemas import ChatMessage, Role


def _build_agent(settings: Settings) -> Agent:
    model = GoogleModel(
        settings.llm_model,
        provider=GoogleProvider(api_key=settings.llm_api_key),
    )
    return Agent(
        model,
        instructions="You are a helpful chatbot. Reply clearly and concisely.",
        output_type=str,
    )


@lru_cache(maxsize=1)
def get_agent() -> Agent:
    return _build_agent(get_settings())


def _to_model_messages(history: Sequence[ChatMessage]) -> list[ModelMessage]:
    messages: list[ModelMessage] = []
    for item in history:
        if item.role == Role.user:
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
        message_history=prior or None,
    ) as result:
        yield from result.stream_text(delta=True, debounce_by=None)
