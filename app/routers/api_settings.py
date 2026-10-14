import os
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.job import AppSetting
from app.config import settings
from app.services.storage import StorageService
from app.services.browser import browser_manager

router = APIRouter(prefix="/api/settings", tags=["settings"])

class StoragePathRequest(BaseModel):
    path: str

class StorageValidationResponse(BaseModel):
    path: str
    exists: bool
    is_writable: bool
    free_gb: float
    sufficient: bool
    min_required_gb: float

class CredentialRequest(BaseModel):
    email: str
    password: str

class AdminCredentialRequest(BaseModel):
    username: str
    password: str
    admin_mode: bool = True


# ─────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────

@router.get("/storage")
async def get_storage_settings(db: AsyncSession = Depends(get_db)):
    stmt = select(AppSetting).where(AppSetting.key == "user_data_dir")
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    current_path = setting.value if setting else str(settings.DEFAULT_DATA_DIR.resolve())
    storage = StorageService()
    disk_info = storage.check_disk_space(Path(current_path))
    return {"current_path": current_path, "disk_info": disk_info}

@router.post("/storage/validate", response_model=StorageValidationResponse)
async def validate_storage_path(req: StoragePathRequest):
    target = Path(req.path).resolve()
    storage = StorageService()
    try:
        exists = target.exists()
        is_writable = os.access(target if exists else target.parent, os.W_OK)
        disk_info = storage.check_disk_space(target)
        return StorageValidationResponse(
            path=str(target),
            exists=exists,
            is_writable=is_writable,
            free_gb=disk_info["free_gb"],
            sufficient=disk_info["sufficient"],
            min_required_gb=disk_info["min_required_gb"]
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid storage path: {str(e)}")

@router.post("/storage/save")
async def save_storage_path(req: StoragePathRequest, db: AsyncSession = Depends(get_db)):
    target = Path(req.path).resolve()
    target.mkdir(parents=True, exist_ok=True)
    storage = StorageService()
    disk_info = storage.check_disk_space(target)
    if not disk_info["sufficient"]:
        raise HTTPException(
            status_code=400,
            detail=f"Disk space too low ({disk_info['free_gb']} GB available). At least {disk_info['min_required_gb']} GB required."
        )
    stmt = select(AppSetting).where(AppSetting.key == "user_data_dir")
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting:
        setting.value = str(target)
    else:
        setting = AppSetting(key="user_data_dir", value=str(target))
        db.add(setting)
    await db.commit()
    return {"success": True, "saved_path": str(target)}


# ─────────────────────────────────────────────────────────────
# Browser / Session
# ─────────────────────────────────────────────────────────────

@router.get("/browser/status")
async def check_browser_status():
    """Check whether the profile has a valid authenticated session."""
    try:
        state = await browser_manager.check_login_state()
        # Also tell the frontend whether credentials are saved
        has_creds = bool(os.environ.get("WH_EMAIL") and os.environ.get("WH_PASSWORD"))
        state["has_saved_credentials"] = has_creds
        return state
    except Exception as e:
        return {"is_logged_in": False, "error": str(e), "has_saved_credentials": False}

@router.post("/browser/save-credentials")
async def save_credentials(req: CredentialRequest):
    """
    Saves WorldHeroes email + password to .env for automatic headless login.
    The password is stored only in the local .env file (never in the DB, never logged).
    """
    # Basic validation
    email = req.email.strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password too short.")

    env_path = settings.BASE_DIR / ".env"

    # Read existing .env content
    env_lines = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    # Remove old WH_EMAIL / WH_PASSWORD lines
    env_lines = [
        line for line in env_lines
        if not line.startswith("WH_EMAIL=") and not line.startswith("WH_PASSWORD=")
    ]

    # Append new credentials
    env_lines.append(f'WH_EMAIL={email}')
    env_lines.append(f'WH_PASSWORD={req.password}')

    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    # Also inject into current process environment so we don't need a restart
    os.environ["WH_EMAIL"] = email
    os.environ["WH_PASSWORD"] = req.password

    return {"success": True, "message": "Credentials saved. Click 'Login Now' to verify."}

@router.post("/browser/auto-login")
async def auto_login():
    """
    Uses stored credentials to log into worldheros.com headlessly.
    No browser window is ever shown to the user.
    """
    email = os.environ.get("WH_EMAIL", "")
    password = os.environ.get("WH_PASSWORD", "")
    if not email or not password:
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": "No credentials saved yet. Enter your email and password first."
            }
        )
    try:
        result = await browser_manager.auto_login_headless(email, password)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Browser engine error: {str(exc)}"
            }
        )

    if not result.get("success"):
        return JSONResponse(
            status_code=401,
            content={
                "success": False,
                "error": result.get("error", "Login failed — please check email and password.")
            }
        )
    return result

@router.delete("/browser/clear-credentials")
async def clear_credentials():
    """Removes saved credentials from .env and environment."""
    env_path = settings.BASE_DIR / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        lines = [l for l in lines if not l.startswith("WH_EMAIL=") and not l.startswith("WH_PASSWORD=")]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ.pop("WH_EMAIL", None)
    os.environ.pop("WH_PASSWORD", None)
    return {"success": True, "message": "Credentials cleared."}

# Kept for backward compat / manual troubleshooting
@router.post("/browser/close-login")
async def close_browser_login():
    return browser_manager.close_interactive_browser()


# ─────────────────────────────────────────────────────────────
# WorldHeroes Admin Configuration
# ─────────────────────────────────────────────────────────────

@router.get("/admin/status")
async def get_admin_status(db: AsyncSession = Depends(get_db)):
    """Check admin direct publishing mode and credentials status."""
    # Check SQLite AppSetting first, fallback to os.environ / settings
    u_res = await db.execute(select(AppSetting).where(AppSetting.key == "wh_admin_username"))
    u_row = u_res.scalar_one_or_none()
    username = (u_row.value if u_row else "") or os.environ.get("WH_ADMIN_USERNAME") or getattr(settings, "WH_ADMIN_USERNAME", "")

    p_res = await db.execute(select(AppSetting).where(AppSetting.key == "wh_admin_password"))
    p_row = p_res.scalar_one_or_none()
    has_password = bool((p_row.value if p_row else "") or os.environ.get("WH_ADMIN_PASSWORD") or getattr(settings, "WH_ADMIN_PASSWORD", ""))

    m_res = await db.execute(select(AppSetting).where(AppSetting.key == "wh_admin_mode"))
    m_row = m_res.scalar_one_or_none()
    admin_mode = (m_row.value.lower() in ("true", "1")) if m_row else getattr(settings, "WH_ADMIN_MODE", True)

    admin_url = getattr(settings, "WH_ADMIN_URL", "https://worldheros.com/admin")
    return {
        "admin_mode": admin_mode,
        "has_saved_credentials": bool(username and has_password),
        "admin_username": username,
        "admin_url": admin_url,
    }


@router.post("/admin/save-credentials")
async def save_admin_credentials(req: AdminCredentialRequest, db: AsyncSession = Depends(get_db)):
    """Saves WorldHeroes admin username & password to SQLite DB and .env for direct publishing."""
    username = req.username.strip()
    password = req.password.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Admin username is required.")
    if not password:
        raise HTTPException(status_code=400, detail="Admin password is required.")

    # 1. Dual-persist to SQLite AppSetting table
    settings_dict = {
        "wh_admin_username": username,
        "wh_admin_password": password,
        "wh_admin_mode": "true" if req.admin_mode else "false",
    }
    for k, v in settings_dict.items():
        res = await db.execute(select(AppSetting).where(AppSetting.key == k))
        entry = res.scalar_one_or_none()
        if entry:
            entry.value = v
        else:
            db.add(AppSetting(key=k, value=v))
    await db.commit()

    # 2. Persist to .env with proper JSON escaping to protect special characters
    try:
        env_path = settings.BASE_DIR / ".env"
        env_lines = []
        if env_path.exists():
            env_lines = env_path.read_text(encoding="utf-8").splitlines()

        env_lines = [
            l for l in env_lines
            if not l.startswith("WH_ADMIN_USERNAME=")
            and not l.startswith("WH_ADMIN_PASSWORD=")
            and not l.startswith("WH_ADMIN_MODE=")
        ]

        safe_pass = json.dumps(password)
        safe_user = json.dumps(username)
        env_lines.append(f"WH_ADMIN_USERNAME={safe_user}")
        env_lines.append(f"WH_ADMIN_PASSWORD={safe_pass}")
        env_lines.append(f"WH_ADMIN_MODE={'true' if req.admin_mode else 'false'}")

        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    except Exception as e:
        pass

    # 3. Inject into current process environment and runtime settings
    os.environ["WH_ADMIN_USERNAME"] = username
    os.environ["WH_ADMIN_PASSWORD"] = password
    os.environ["WH_ADMIN_MODE"] = "true" if req.admin_mode else "false"
    settings.WH_ADMIN_USERNAME = username
    settings.WH_ADMIN_PASSWORD = password
    settings.WH_ADMIN_MODE = req.admin_mode

    return {
        "success": True,
        "message": f"Admin credentials saved for '{username}'! Direct publishing is now active for Hero, Blog, and Story.",
        "admin_username": username,
        "admin_mode": req.admin_mode,
    }


@router.delete("/admin/clear-credentials")
async def clear_admin_credentials(db: AsyncSession = Depends(get_db)):
    """Clears WorldHeroes admin credentials from DB, .env, and memory."""
    # 1. Clear from SQLite
    for k in ["wh_admin_username", "wh_admin_password"]:
        res = await db.execute(select(AppSetting).where(AppSetting.key == k))
        entry = res.scalar_one_or_none()
        if entry:
            await db.delete(entry)
    await db.commit()

    # 2. Clear from .env
    try:
        env_path = settings.BASE_DIR / ".env"
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
            lines = [
                l for l in lines
                if not l.startswith("WH_ADMIN_USERNAME=")
                and not l.startswith("WH_ADMIN_PASSWORD=")
            ]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        pass

    os.environ.pop("WH_ADMIN_USERNAME", None)
    os.environ.pop("WH_ADMIN_PASSWORD", None)
    settings.WH_ADMIN_USERNAME = ""
    settings.WH_ADMIN_PASSWORD = ""

    return {"success": True, "message": "Admin credentials cleared successfully."}
