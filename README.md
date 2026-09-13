# Chatbot demo

Minimal Streamlit chatbot using Pydantic AI, SQLAlchemy, and SQLite. LLM calls are isolated in `llm_client.py` so an MCP client/server can be added later without changing the UI or database layers.

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate`.

2. Copy the example env file and set your LLM credentials. Do not commit `.env`.

```bash
copy .env.example .env
```

On macOS/Linux: `cp .env.example .env`

Edit `.env`:

```
LLM_API_KEY=sk-your-openai-api-key
LLM_MODEL=gpt-4o-mini
DATABASE_URL=sqlite:///./chatbot.db
```

`LLM_MODEL` is passed to Pydantic AI's OpenAI chat model (Chat Completions API).

## Run

```bash
streamlit run app.py
```

The app creates `chatbot.db` on first launch (unless you change `DATABASE_URL`). Use the sidebar to open past conversations or start a new one. User and assistant messages are stored in SQLite; the assistant reply is streamed token-by-token in the main pane.

## Layout

| File | Role |
| --- | --- |
| `app.py` | Streamlit UI |
| `config.py` | `pydantic-settings` config from `.env` |
| `database.py` | SQLAlchemy engine, session, `Base` |
| `models.py` | `Conversation` and `Message` ORM models |
| `schemas.py` | Pydantic message/request/response models |
| `llm_client.py` | Pydantic AI agent + streaming (no UI/DB imports) |
| `repository.py` | Conversation/message persistence |
