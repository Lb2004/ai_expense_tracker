from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Runtime config loaded from environment / .env. Never hardcode secrets."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore unknown env vars instead of failing startup.
    )

    llm_api_key: str = ""
    llm_model: str = "gemini-3.8-flash"
    database_url: str = "sqlite:///./expenses.db"

    # Fix #6: MCP server URLs are now configurable rather than hardcoded.
    mcp_server_url: str = "http://127.0.0.1:8001/mcp"
    financial_insights_url: str = "http://127.0.0.1:8002/mcp"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
