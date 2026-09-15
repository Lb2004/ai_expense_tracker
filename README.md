# StockBot

Streamlit chatbot using Pydantic AI (Google Gemini), Model Context Protocol (MCP), SQLAlchemy, and SQLite. Stock market data is served via a local MCP server (`mcpserver.py`) with support for optional Alpha Vantage integration.

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate`.

2. Copy the example env file and set your API keys. Do not commit `.env`.

```bash
copy .env.example .env
```

On macOS/Linux: `cp .env.example .env`

```
LLM_API_KEY=your-google-api-key
LLM_MODEL=gemini-3.8-flash
DATABASE_URL=sqlite:///./chatbot.db
ALPHAVANTAGE_API_KEY=your-alphavantage-api-key  # Optional
```

## Running the Application

This project runs with a decoupled MCP architecture:

1. **Start the MCP Stock Server** (Terminal 1):
```bash
python mcpserver.py
```
This runs the MCP server on `http://127.0.0.1:8000/mcp`.

2. **Start the Streamlit Web UI** (Terminal 2):
```bash
streamlit run app.py
```

The app automatically initializes `chatbot.db` on first launch. Use the sidebar to switch conversations or create a new one.

## Layout

| File | Role |
| --- | --- |
| `mcpserver.py` | Local FastMCP server exposing Yahoo Finance stock tools over streamable HTTP |
| `llm_client.py` | Pydantic AI agent connected to MCP tools and optional Alpha Vantage discovery |
| `app.py` | Streamlit UI with streaming chat and conversation sidebar |
| `config.py` | Environment settings and config loading |
| `database.py` | SQLAlchemy engine, session management, and SQLite pragmas |
| `models.py` | SQLAlchemy models for conversations and messages |
| `repository.py` | CRUD operations for conversations and chat history |
| `schemas.py` | Pydantic models shared across layers |
