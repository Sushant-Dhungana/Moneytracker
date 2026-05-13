from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = ROOT_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(DEFAULT_ENV_FILE), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = Field(default="arthaX Backend", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    api_prefix: str = Field(default="/api/v1", alias="API_PREFIX")

    database_url: str = Field(alias="DATABASE_URL")
    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_jwt_aud: str = Field(default="authenticated", alias="SUPABASE_JWT_AUD")
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")
    auth_require_email_confirmation: bool = Field(
        default=True, alias="AUTH_REQUIRE_EMAIL_CONFIRMATION"
    )
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")
    db_connect_timeout_sec: int = Field(default=5, alias="DB_CONNECT_TIMEOUT_SEC")
    db_pool_acquire_timeout_sec: int = Field(default=8, alias="DB_POOL_ACQUIRE_TIMEOUT_SEC")
    supabase_auth_timeout_sec: int = Field(default=8, alias="SUPABASE_AUTH_TIMEOUT_SEC")
    ai_timeout_sec: int = Field(default=25, alias="AI_TIMEOUT_SEC")

    chat_api_endpoint: str | None = Field(default=None, alias="CHAT_API_ENDPOINT")
    chat_api_key: str | None = Field(default=None, alias="CHAT_API_KEY")
    chat_api_key_header: str | None = Field(default="x-api-key", alias="CHAT_API_KEY_HEADER")

    personal_chat_api_endpoint: str | None = Field(default=None, alias="PERSONAL_CHAT_API_ENDPOINT")
    personal_chat_api_key: str | None = Field(default=None, alias="PERSONAL_CHAT_API_KEY")
    personal_chat_api_key_header: str | None = Field(
        default="x-api-key", alias="PERSONAL_CHAT_API_KEY_HEADER"
    )
    personal_chat_api_fallback_endpoint: str | None = Field(
        default=None, alias="PERSONAL_CHAT_API_FALLBACK_ENDPOINT"
    )
    personal_chat_match_threshold: float = Field(default=0.55, alias="PERSONAL_CHAT_MATCH_THRESHOLD")
    personal_chat_match_count: int = Field(default=8, alias="PERSONAL_CHAT_MATCH_COUNT")

    business_chat_api_endpoint: str | None = Field(default=None, alias="BUSINESS_CHAT_API_ENDPOINT")
    business_chat_api_key: str | None = Field(default=None, alias="BUSINESS_CHAT_API_KEY")
    business_chat_api_key_header: str | None = Field(
        default="x-api-key", alias="BUSINESS_CHAT_API_KEY_HEADER"
    )
    business_chat_api_fallback_endpoint: str | None = Field(
        default=None, alias="BUSINESS_CHAT_API_FALLBACK_ENDPOINT"
    )
    business_chat_match_threshold: float = Field(default=0.55, alias="BUSINESS_CHAT_MATCH_THRESHOLD")
    business_chat_match_count: int = Field(default=8, alias="BUSINESS_CHAT_MATCH_COUNT")

    embedding_api_endpoint: str | None = Field(default=None, alias="EMBEDDING_API_ENDPOINT")
    embedding_api_key: str | None = Field(default=None, alias="EMBEDDING_API_KEY")
    embedding_api_key_header: str | None = Field(
        default="x-api-key", alias="EMBEDDING_API_KEY_HEADER"
    )
    embedding_model_id: str = Field(default="amazon.titan-embed-text-v2:0", alias="EMBEDDING_MODEL_ID")
    embedding_dim: int = Field(default=1024, alias="EMBEDDING_DIM")
    receipt_aws_access_key_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RECEIPT_AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    )
    receipt_aws_secret_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RECEIPT_AWS_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    )
    receipt_aws_session_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RECEIPT_AWS_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
    )
    receipt_aws_region: str = Field(
        default="us-east-1",
        validation_alias=AliasChoices("RECEIPT_AWS_REGION", "AWS_REGION"),
    )
    receipt_textract_endpoint: str | None = Field(
        default=None,
        alias="RECEIPT_TEXTRACT_ENDPOINT",
    )
    receipt_timeout_sec: int = Field(default=60, alias="RECEIPT_TIMEOUT_SEC")

    @property
    def supabase_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def supabase_jwks_url(self) -> str:
        return f"{self.supabase_issuer}/.well-known/jwks.json"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
