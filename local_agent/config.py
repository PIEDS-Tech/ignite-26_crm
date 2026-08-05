import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


class Settings:
    # The hosted Django CRM. No DATABASE_URL here on purpose -- the agent has
    # no database access at all.
    api_base_url: str = os.getenv("AGENT_API_BASE_URL", "").rstrip("/")
    api_token: str = os.getenv("AGENT_API_TOKEN", "")

    member_email: str = os.getenv("AGENT_MEMBER_EMAIL", "")

    client_secrets_path: Path = Path(
        os.getenv("GOOGLE_CLIENT_SECRETS_PATH", REPO_ROOT / "client_secret.json")
    ).expanduser()
    token_dir: Path = Path(os.getenv("AGENT_TOKEN_DIR", "~/.ignite_crm")).expanduser()

    # Paces sends within a batch. The hard daily cap is enforced server-side so
    # it counts across every device a member uses.
    send_delay_seconds: float = float(os.getenv("AGENT_SEND_DELAY_SECONDS", "2"))

    static_dir: Path = REPO_ROOT / "shared" / "static"


settings = Settings()
