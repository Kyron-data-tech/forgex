from pathlib import Path
import secrets
from dotenv import load_dotenv
from pydantic_settings import BaseSettings
from pydantic import Field, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

class Settings(BaseSettings):
    HOST: str = Field(default="127.0.0.1", description="Strictly bind to 127.0.0.1")
    PORT: int = Field(default=8000)
    DEBUG: bool = Field(default=False)
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_hex(32))
    
    # Path configuration
    BASE_DIR: Path = PROJECT_ROOT
    PROJECT_DIR: Path = PROJECT_ROOT
    CHROME_PROFILE_DIR: Path = PROJECT_ROOT / ".chrome-profile"
    BROWSER_PROFILE_DIR: Path = PROJECT_ROOT / ".chrome-profile"  # backward compat
    DEFAULT_DATA_DIR: Path = PROJECT_ROOT / "data_storage"
    DATABASE_PATH: Path = PROJECT_ROOT / "ForgeX.sqlite3"
    
    # Browser / Operator configuration
    BROWSER_CHANNEL: str = Field(default="chrome", description="Must be Google Chrome")
    BROWSER_HEADLESS: bool = Field(default=False, description="Default visible for Operator style")
    SLOW_MO_MS: int = Field(default=0, description="Pacing handled by pointer")
    
    # Cursor / Pointer configuration
    CURSOR_ENABLED: bool = Field(default=True)
    CURSOR_SPEED: float = Field(default=1.0)
    CURSOR_COLOR: str = Field(default="#FF3366")
    CURSOR_LABEL: bool = Field(default=True)
    TYPE_DELAY_MS: int = Field(default=45)
    OS_CURSOR: bool = Field(default=False, description="Optional system mouse sync (pyautogui)")
    
    # Research playbook settings
    SEARCHES_PER_SUBJECT: int = Field(default=4)
    MAX_PASTE_CHARS: int = Field(default=12000)
    ROW_DELAY_S: int = Field(default=15)
    MAX_STEPS: int = Field(default=30)
    
    # ChatGPT integration settings
    CHATGPT_OPEN_MODE: str = Field(default="search", description="search | direct")
    CHATGPT_TEMP_CHAT: bool = Field(default=True)
    CHATGPT_TIMEOUT_S: int = Field(default=120)
    CHATGPT_MODE: str = Field(default="web", description="web | api")
    
    # LLM provider settings (for fallback & api mode)
    LLM_PROVIDER: str = Field(default="mock", description="mock | openai | anthropic | gemini | ollama")
    LLM_MODEL: str = Field(default="gpt-4o")
    LLM_API_KEY: str = Field(default="")
    
    # Credentials (Stored securely in .env, never hardcoded)
    WH_EMAIL: str = Field(default="")
    WH_PASSWORD: str = Field(default="")

    # WorldHeroes Admin Configuration
    WH_ADMIN_USERNAME: str = Field(default="", description="WorldHeroes admin username")
    WH_ADMIN_PASSWORD: str = Field(default="", description="WorldHeroes admin password")
    WH_ADMIN_URL: str = Field(default="https://worldheros.com/admin", description="WorldHeroes admin dashboard URL")
    WH_ADMIN_MODE: bool = Field(default=True, description="When true, directly publishes Hero/Story/Blog via admin control")

    # Safety Limits
    MAX_IMAGE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB per image
    MIN_FREE_DISK_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GB minimum free disk
    MAX_JOBS_PER_HOUR: int = 30
    DEFAULT_TIMEOUT_MS: int = 25000  # 25s timeout for visible browser
    
    # Security
    ALLOWED_HOSTS: list[str] = ["127.0.0.1", "localhost", "::1", "testserver", "test"]
    
    model_config = ConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()


def get_admin_credentials() -> tuple[str, str, bool]:
    """
    Retrieves (username, password, admin_mode) across:
    1. SQLite app_settings table (wh_admin_username, wh_admin_password, wh_admin_mode)
    2. os.environ
    3. settings (.env / defaults)
    Syncs os.environ and returns (username, password, admin_mode).
    """
    import os
    import sqlite3
    u = ""
    p = ""
    m = None

    db_path = PROJECT_ROOT / "ForgeX.sqlite3"
    if db_path.exists():
        try:
            con = sqlite3.connect(str(db_path))
            cur = con.cursor()
            cur.execute("SELECT key, value FROM app_settings WHERE key IN ('wh_admin_username', 'wh_admin_password', 'wh_admin_mode')")
            rows = dict(cur.fetchall())
            con.close()
            u = rows.get("wh_admin_username", "") or ""
            p = rows.get("wh_admin_password", "") or ""
            if "wh_admin_mode" in rows:
                m = rows["wh_admin_mode"].lower() in ("true", "1", "yes")
        except Exception:
            pass

    if not u:
        u = os.environ.get("WH_ADMIN_USERNAME") or getattr(settings, "WH_ADMIN_USERNAME", "") or ""
    if not p:
        p = os.environ.get("WH_ADMIN_PASSWORD") or getattr(settings, "WH_ADMIN_PASSWORD", "") or ""
    if m is None:
        env_mode = os.environ.get("WH_ADMIN_MODE")
        if env_mode is not None:
            m = env_mode.lower() in ("true", "1", "yes")
        else:
            m = getattr(settings, "WH_ADMIN_MODE", True)

    # Sync into os.environ
    if u:
        os.environ["WH_ADMIN_USERNAME"] = u
    if p:
        os.environ["WH_ADMIN_PASSWORD"] = p
    os.environ["WH_ADMIN_MODE"] = "true" if m else "false"

    return u, p, bool(m)
