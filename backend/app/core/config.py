from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "NetSuite Ecommerce Ops Suite"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite"
    DATABASE_URL_SYNC: str = "postgresql://postgres:postgres@localhost:5432/ecom_netsuite"

    REDIS_URL: str = "redis://localhost:6379/0"

    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    ENCRYPTION_KEY: str = "change-me-generate-a-real-fernet-key"
    ENCRYPTION_KEY_VERSION: int = 1

    CORS_ORIGINS: str = "http://localhost:3000"

    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    MCP_SERVER_HOST: str = "0.0.0.0"
    MCP_SERVER_PORT: int = 8001
    MCP_RATE_LIMIT_PER_MINUTE: int = 60

    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"
    DEFAULT_AI_PROVIDER: str = "anthropic"
    VOYAGE_API_KEY: str = ""
    VOYAGE_EMBED_MODEL: str = "voyage-3-lite"
    CHAT_MAX_HISTORY_TURNS: int = 20
    CHAT_MAX_TOOL_CALLS_PER_TURN: int = 5
    CHAT_RAG_TOP_K: int = 5

    NETSUITE_SUITEQL_MAX_ROWS: int = 1000
    NETSUITE_SUITEQL_TIMEOUT: int = 30
    NETSUITE_SUITEQL_ALLOWED_TABLES: str = "transaction,customer,item,account,subsidiary,department,location,currency,employee,vendor"

    NETSUITE_OAUTH_CLIENT_ID: str = ""
    NETSUITE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/connections/netsuite/callback"
    NETSUITE_OAUTH_SCOPE: str = "mcp"
    NETSUITE_ACCOUNT_ID: str = ""
    NETSUITE_MCP_TRANSPORT: str = "http"

    AUDIT_RETENTION_DAYS: int = 90

    model_config = {"env_file": ".env", "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]


settings = Settings()
