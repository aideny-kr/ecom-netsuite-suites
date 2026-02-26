from pathlib import Path

from pydantic_settings import BaseSettings

# Resolve .env from project root (one level above backend/)
_env_file = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    APP_NAME: str = "NetSuite Ecommerce Ops Suite"
    APP_ENV: str = "development"
    APP_DEBUG: bool = True

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite"
    DATABASE_URL_SYNC: str = "postgresql://postgres:postgres@localhost:5432/ecom_netsuite"
    DATABASE_URL_DIRECT: str = ""  # Direct Supabase connection for migrations
    DATABASE_URL_DIRECT_SYNC: str = ""  # Direct sync connection for Alembic

    SUPABASE_URL: str = ""
    SUPABASE_PUBLISHABLE_KEY: str = ""

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
    VOYAGE_EMBED_MODEL: str = "voyage-3"
    OPENAI_EMBEDDING_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_DIMENSIONS: int = 1536
    DOMAIN_KNOWLEDGE_TOP_K: int = 3
    CHAT_MAX_HISTORY_TURNS: int = 20
    CHAT_MAX_TOOL_CALLS_PER_TURN: int = 5
    CHAT_RAG_TOP_K: int = 5

    NETSUITE_SUITEQL_MAX_ROWS: int = 1000
    NETSUITE_SUITEQL_TIMEOUT: int = 30
    NETSUITE_SUITEQL_ALLOWED_TABLES: str = (
        "transaction,transactionline,customer,item,account,subsidiary,"
        "department,location,classification,currency,employee,vendor,"
        "customtransactionbodyfield,customtransactioncolumnfield,"
        "entitycustomfield,itemcustomfield,crmcustomfield,othercustomfield,"
        "customrecordtype,customlist"
    )

    # OAuth for regular API connection (SuiteQL, file sync, metadata)
    NETSUITE_OAUTH_CLIENT_ID: str = ""
    NETSUITE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/connections/netsuite/callback"
    NETSUITE_OAUTH_SCOPE: str = "rest_webservices,restlets"

    # OAuth for MCP connector (AI tools)
    NETSUITE_MCP_OAUTH_CLIENT_ID: str = ""
    NETSUITE_MCP_OAUTH_SCOPE: str = "mcp"

    NETSUITE_ACCOUNT_ID: str = ""
    NETSUITE_MCP_TRANSPORT: str = "http"

    # Multi-agent orchestration
    MULTI_AGENT_ENABLED: bool = True
    MULTI_AGENT_SPECIALIST_PROVIDER: str = "anthropic"
    MULTI_AGENT_SPECIALIST_MODEL: str = "claude-sonnet-4-5-20250929"
    # SuiteQL agent uses a stronger model for SQL reasoning and huge NetSuite context
    MULTI_AGENT_SQL_MODEL: str = "claude-sonnet-4-5-20250929"
    # Synthesis model — used to compose final user-facing answer from agent results
    MULTI_AGENT_SYNTHESIS_MODEL: str = "claude-sonnet-4-5-20250929"
    MULTI_AGENT_MAX_BUDGET_TOKENS: int = 50000
    MULTI_AGENT_MAX_RETRIES: int = 2

    # Web search
    WEB_SEARCH_PROVIDER: str = "duckduckgo"  # Options: "duckduckgo", "tavily"
    TAVILY_API_KEY: str = ""  # For future Tavily upgrade
    WEB_SEARCH_MAX_RESULTS: int = 5

    # Stripe billing
    STRIPE_API_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    BILLING_BASE_CREDITS: int = 500

    AUDIT_RETENTION_DAYS: int = 90

    model_config = {"env_file": str(_env_file), "extra": "ignore"}

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]


settings = Settings()
