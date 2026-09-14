# StockBot

Streamlit chatbot using Pydantic AI (Google Gemini), SQLAlchemy, and SQLite. Yahoo Finance calls live in `stock_data.py` so they can later be wrapped by an MCP server; `llm_client.py` can then call that server instead of importing `stock_data` directly.

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate`.

2. Copy the example env file and set your Gemini API key. Do not commit `.env`.

```bash
copy .env.example .env
```

On macOS/Linux: `cp .env.example .env`

```
LLM_API_KEY=your-google-api-key
LLM_MODEL=gemini-3.8-flash
DATABASE_URL=sqlite:///./chatbot.db
```

## Run

```bash
streamlit run app.py
```

The app creates `chatbot.db` on first launch. Use the sidebar to open past conversations or start a new one. Replies stream in the main pane.

## Layout

| File | Role |
| --- | --- |
| `app.py` | Streamlit UI |
| `config.py` | Settings from `.env` |
| `database.py` | Engine, `db_session`, `Base` |
| `models.py` | Conversation and Message tables |
| `schemas.py` | Pydantic models shared by UI, DB, and LLM |
| `repository.py` | Conversation and message persistence |
| `stock_data.py` | Yahoo Finance (future MCP server) |
| `llm_client.py` | Pydantic AI agent + streaming (future MCP client) |
