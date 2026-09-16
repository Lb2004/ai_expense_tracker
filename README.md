# Expense Tracker

Streamlit chatbot using Pydantic AI (Google Gemini), Model Context Protocol (MCP), SQLAlchemy, and SQLite. Expense data is served via a local MCP server (`expense_mcp_server.py`) with session-token authentication.

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
DATABASE_URL=sqlite:///./expenses.db 
```

## Running the Application

This project runs with a decoupled MCP architecture:

1. **Start the MCP Expense Server** (Terminal 1):
```bash
python expense_mcp_server.py
```
This runs the MCP server on `http://127.0.0.1:8001/mcp`.

2. **Start the Streamlit Web UI** (Terminal 2):
```bash
streamlit run app.py
```

The app automatically initializes `expenses.db` on first launch.

## Running Tests

```bash
python -m pytest test_isolation.py -v
```

All 8 tests cover session auth, multi-user isolation, SQL safety, and adversarial scenarios.

## Layout

| File | Role |
| --- | --- |
| `expense_mcp_server.py` | Standalone MCP server: expense CRUD, budget, safe NL-to-SQL, affordability analysis |
| `llm_client.py` | Pydantic AI agent connected to MCP tools via session tokens |
| `app.py` | Streamlit UI with session-token login and streaming chat |
| `config.py` | Environment settings and config loading |
| `database.py` | SQLAlchemy engine, session management, and SQLite pragmas |
| `models.py` | SQLAlchemy models: User, Expense, UserSession, Budget |
| `schemas.py` | Pydantic models shared across layers |
| `test_isolation.py` | Pytest suite: 8 tests proving isolation and safety guarantees |

## Architecture & Design Decisions

### Why MCP (Model Context Protocol)?

The expense tracker uses MCP to **decouple the LLM's tool layer from the application UI**:

- **Independent deployment**: `expense_mcp_server.py` runs as its own HTTP process. It can be restarted, scaled, or replaced without touching the Streamlit app.
- **Transport-agnostic**: Tools are defined once and served over streamable HTTP. The same tools could be consumed by any MCP-compatible client (CLI, desktop app, another LLM framework).
- **Clear trust boundary**: The MCP server owns all database access. The LLM and UI never touch the DB directly for expense operations — they go through validated tool calls.

### Session-Token Authentication

**Problem**: In the rough prototype, tools accepted a raw `user_id` parameter. The LLM was instructed to always pass the correct ID, but nothing *enforced* this — a prompt injection or model hallucination could pass any user's ID.

**Solution**: All tools now accept a `session_token` (UUID) instead of `user_id`. The flow:

1. User logs in → app creates a `UserSession` row (token + expiry) → stores token in `st.session_state`
2. LLM instructions include the token → LLM passes it to every tool call
3. MCP server validates the token server-side (`resolve_session`) → extracts `user_id` internally
4. Expired or invalid tokens are rejected with an error

**Why this matters**: Even if the LLM hallucinates or an attacker manipulates the prompt, they can only operate within the scope of their own valid session. They cannot guess another user's UUID token.

### Safe NL-to-SQL (query_expenses)

**Problem**: Users want analytical queries ("What's my total food spending?") that don't map to a simple list/filter.

**Solution**: The `query_expenses` tool accepts a SQL `SELECT` query from the LLM, validated with multiple safety layers:

1. **Parse with sqlparse** → reject non-SELECT statements
2. **Keyword blocklist** → reject `DROP`, `DELETE`, `INSERT`, `ALTER`, `PRAGMA`, etc.
3. **Column allowlist** → only `id`, `amount`, `category`, `description`, `date` are permitted. `user_id`, `created_at`, and table names like `users`/`sessions` are blocked.
4. **Auto-injected WHERE clause** → `WHERE user_id = :uid` is injected server-side so the LLM never sees or controls the user filter
5. **Bound parameters** → prevents SQL injection via the user_id value
6. **Row cap (200) & read-only** → limits resource abuse

### Per-User Budget & Affordability

The `set_budget` tool persists a monthly budget to the DB. `can_i_afford` computes remaining budget vs. item cost and returns structured data (not prose) so the LLM can present the analysis conversationally.
