import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./reservas.db")
    agent_api_key: str = os.getenv("AGENT_API_KEY", "dev-key")
    trytn_mode: str = os.getenv("TRYTN_MODE", "mock")
    trytn_base_url: str = os.getenv("TRYTN_BASE_URL", "https://api.trytn.com")
    trytn_api_key: str = os.getenv("TRYTN_API_KEY", "")
    trytn_webhook_secret: str = os.getenv("TRYTN_WEBHOOK_SECRET", "dev-secret")
    mirlo_notify_url: str = os.getenv("MIRLO_NOTIFY_URL", "")
    mirlo_api_key: str = os.getenv("MIRLO_API_KEY", "")
    hold_minutes: int = int(os.getenv("HOLD_MINUTES", "10"))
    payment_minutes: int = int(os.getenv("PAYMENT_MINUTES", "30"))


def get_settings() -> Settings:
    return Settings()
