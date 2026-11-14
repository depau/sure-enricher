import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    """Server configuration (env only). Everything per user lives in the database."""

    sure_url: str  # how this server reaches Sure, e.g. http://web:3000 inside compose
    secret_key: str = field(repr=False)  # seals tokens and provider sessions at rest (spe.vault), signs cookies
    sure_public_url: str = ""  # where browsers reach Sure (sign-in, links); defaults to sure_url
    public_url: str = "http://127.0.0.1:8790"  # where browsers reach this UI; its path is the mount prefix
    database_url: str = "sqlite:///spe.db"
    login_browser_cdp: str | None = None  # CDP URL of the login browser, for importing provider sessions
    disconnected_wipe_days: int = 30
    receipts_dir: str | None = None  # Esselunga receipt PDFs are kept here, per user and connection  # provider sessions of users Sure no longer accepts are erased after this

    @property
    def base_path(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Settings":
        missing = [k for k in ("SURE_URL", "SPE_SECRET_KEY") if not env.get(k)]
        if missing:
            raise SystemExit(f"missing required environment variables: {', '.join(missing)}")
        return cls(
            sure_url=env["SURE_URL"].rstrip("/"),
            secret_key=env["SPE_SECRET_KEY"],
            sure_public_url=(env.get("SURE_PUBLIC_URL") or env["SURE_URL"]).rstrip("/"),
            public_url=(env.get("SPE_PUBLIC_URL") or cls.public_url).rstrip("/"),
            database_url=env.get("DATABASE_URL", cls.database_url),
            login_browser_cdp=env.get("SPE_LOGIN_BROWSER_CDP") or None,
            disconnected_wipe_days=int(env.get("SPE_DISCONNECTED_WIPE_DAYS", cls.disconnected_wipe_days)),
            receipts_dir=env.get("SPE_RECEIPTS_DIR") or None,
        )
