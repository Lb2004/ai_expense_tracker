# Expense Tracker

Streamlit chatbot using Pydantic AI (Google Gemini), Model Context Protocol (MCP), SQLAlchemy, and SQLite. Expense data is served via a local MCP server (`expense_mcp_server.py`) with bcrypt password authentication and session-token authorization. A second MCP server (`financial_insights_server.py`) provides live exchange rates and gold prices.

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

2. **Start the Financial Insights Server** (Terminal 2):
```bash
python financial_insights_server.py
```
This runs the financial insights MCP server on `http://127.0.0.1:8002/mcp`.

3. **Start the Streamlit Web UI** (Terminal 3):
```bash
streamlit run app.py
```

The app automatically initializes `expenses.db` on first launch.

## Running Tests

```bash
python -m pytest test_isolation.py -v
```

18 tests cover session auth, password hashing, multi-user isolation, SQL safety, pagination, amount validation, expiry enforcement, and adversarial scenarios. Three tests are explicitly regression-proof (see test docstrings for revert conditions).

## Layout

| File | Role |
| --- | --- |
| `shared_models.py` | Canonical SQLAlchemy models: User, Expense, UserSession, Budget (single source of truth) |
| `expense_mcp_server.py` | Standalone MCP server: expense CRUD, budget, safe NL-to-SQL, affordability analysis, monthly summary resource |
| `financial_insights_server.py` | Standalone MCP server: exchange rates and gold prices via Frankfurter API |
| `llm_client.py` | Pydantic AI agent connected to both MCP servers via session tokens |
| `app.py` | Streamlit UI with bcrypt password auth and streaming chat |
| `config.py` | Environment settings, MCP server URLs, and config loading |
| `database.py` | SQLAlchemy engine, session management, and SQLite pragmas |
| `models.py` | Re-exports from `shared_models.py` for backward compatibility |
| `schemas.py` | Pydantic models shared across layers |
| `test_isolation.py` | Pytest suite: 18 tests proving isolation and safety guarantees |

## Architecture & Design Decisions

### Why Standalone MCP Servers?

The expense tracker uses MCP to **decouple the LLM's tool layer from the application UI**:

- **Independent deployment**: `expense_mcp_server.py` and `financial_insights_server.py` run as their own HTTP processes. They can be restarted, scaled, or replaced without touching the Streamlit app.
- **Reusability across any MCP-compatible client**: The same tools could be consumed by a CLI, desktop app, another LLM framework, or a completely different agent. This is not theoretical — in a prior stock-chatbot project, Alpha Vantage financial data was *only* reachable via an MCP server, which proved that MCP provides real value beyond in-process tool-calling: it makes external API integrations reusable across clients without duplicating adapter code.
- **Clear trust boundary**: The MCP server owns all database access. The LLM and UI never touch the DB directly for expense operations — they go through validated tool calls.
- **Multi-server composition**: A single Pydantic AI agent connects to *both* the expense server and the financial insights server simultaneously, enabling questions like "What's the gold price in INR, and can I afford a 10g gold coin?" that span multiple data sources in a single response.

### Session-Token Authentication (Simplified OAuth Stand-In)

The `session_token` pattern used throughout the MCP tools is a **simplified stand-in for OAuth-based per-user MCP authentication** in production. In this POC:

1. User signs up/logs in with bcrypt-hashed password → app calls internal `signup_user`/`login_user` → creates a `UserSession` row with a `secrets.token_urlsafe(32)` token and 24-hour expiry
2. Token is embedded in the LLM's system prompt → LLM passes it to every tool call
3. MCP server validates the token server-side (`resolve_session`) → extracts `user_id` internally, rejects expired tokens
4. The session-creation functions (`signup_user`, `login_user`) are explicitly *not* MCP-exposed — they are internal functions called only from the Streamlit login flow

A production system would replace this with OAuth 2.0 / OIDC at the transport level (HTTP Authorization headers), eliminating the need to pass tokens through the LLM's instruction context. The MCP spec's built-in `AuthSettings` and `TokenVerifier` support this directly.

### Why "Can I Afford X?" Needs the LLM + Multi-Server Layer

A fixed UI could show a budget dashboard, but it cannot answer open-ended affordability questions that require *composing* data from multiple sources in real time. When a user asks "I want to buy a 10g gold coin — what's the current gold price in INR, and can I afford it based on my recent spending?", the system needs to: (1) call the financial insights server for the live gold price, (2) compute the total cost, (3) call the expense server's `can_i_afford` with the computed cost, and (4) synthesize both results into a conversational answer. This multi-step reasoning, data composition across heterogeneous APIs, and natural-language presentation is precisely what the LLM/NL-to-SQL/multi-server architecture enables — and it cannot be replicated by a static UI without hardcoding every possible question pattern.

### Safe NL-to-SQL (query_expenses)

**Problem**: Users want analytical queries ("What's my total food spending?") that don't map to a simple list/filter.

**Solution**: The `query_expenses` tool accepts a SQL `SELECT` query from the LLM, validated with multiple safety layers:

1. **Parse with sqlparse** → reject non-SELECT statements
2. **Keyword blocklist** → reject `DROP`, `DELETE`, `INSERT`, `ALTER`, `PRAGMA`, etc.
3. **Column allowlist** → only `id`, `amount`, `category`, `description`, `date` are permitted. `user_id`, `created_at`, and table names like `users`/`sessions` are blocked.
4. **Temporary VIEW** (`my_expenses`) → replaces the fragile regex-based WHERE injection. A per-request temp VIEW scoped to the authenticated user's data is created before the query runs. This is immune to table-alias problems (e.g. `FROM expenses e`) that broke the old regex approach.
5. **Row cap (200) & read-only** → limits resource abuse

### Per-User Budget & Affordability

The `set_budget` tool persists a monthly budget to the DB. `can_i_afford` computes remaining budget vs. item cost and returns structured data (not prose) so the LLM can present the analysis conversationally.

### MCP Resource: Monthly Summary

The `expense://summary/{session_token}/monthly` MCP Resource provides a JSON summary of the user's monthly spending (total spent, top category, last-month comparison, remaining budget). The Streamlit sidebar reads this to display a live spending dashboard.

## Known Limitations

These are accepted limitations in this POC, documented for technical review:

| # | Limitation | File(s) | Notes |
|---|-----------|---------|-------|
| 12 | **Session token in system prompt** carries a theoretical prompt-injection risk — a crafted message could try to get the model to reveal its system prompt. | `llm_client.py` (dynamic_instructions) | Production: use transport-level auth (HTTP headers) instead. |
| 13 | **LLM compliance with session_token** is not structurally enforced — the system prompt *instructs* the LLM to pass the token, but if it fails, the server correctly rejects the call (graceful failure, not a security hole). | `llm_client.py` (dynamic_instructions) | A reliability limitation, not a security one. |
| 14 | **`created_at` uses an app-level default** (`datetime.now(timezone.utc)`) rather than a DB DDL default (`server_default=func.now()`). Rows created outside the ORM won't get automatic timestamps. | `shared_models.py` (User, Expense models) | Production: add DDL-level defaults. |
| 15 | **Tests call MCP tool functions directly** (Python function calls, not HTTP requests), so HTTP serialization and network failure paths are untested. | `test_isolation.py` (module docstring) | Add HTTP-level integration tests for production. |
| 16 | **`date.today()` in test assertions** carries a theoretical midnight-rollover flakiness risk. | `test_isolation.py` (module docstring) | Low-priority; use frozen time fixtures if needed. |
| 17 | **Circular-import workaround** in `database.py` was resolved by extracting `shared_models.py`. The old local import inside `init_db()` is no longer needed. | `database.py` | Resolved. |
| 18 | **MCP server binds to `127.0.0.1`** (localhost only) — this is intentional for the POC. | `expense_mcp_server.py` (line 695), `financial_insights_server.py` | Do not change to `0.0.0.0` without adding transport-level auth. |
