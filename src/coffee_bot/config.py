from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    model: str
    data_dir: Path
    output_dir: Path
    timezone: ZoneInfo
    http_host: str
    http_port: int
    public_base_url: str | None
    image_format: str
    low_battery_volts: float
    project_root: Path = PROJECT_ROOT
    templates_dir: Path = TEMPLATES_DIR


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")

    raw_ids = os.environ.get("ALLOWED_USER_IDS", "")
    try:
        allowed = frozenset(int(x) for x in raw_ids.replace(" ", "").split(",") if x)
    except ValueError:
        raise SystemExit("ALLOWED_USER_IDS must be comma-separated numeric Telegram user IDs")
    if not allowed:
        raise SystemExit("ALLOWED_USER_IDS is empty; the bot would ignore everyone")

    # The Anthropic client reads ANTHROPIC_API_KEY itself; fail early if it's missing.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (see .env.example)")

    image_format = os.environ.get("IMAGE_FORMAT", "auto").strip().lower()
    if image_format not in ("auto", "2bit", "1bit", "bmp"):
        raise SystemExit("IMAGE_FORMAT must be one of: auto, 2bit, 1bit, bmp")

    return Config(
        telegram_token=token,
        allowed_user_ids=allowed,
        model=os.environ.get("MODEL", "claude-opus-5-5"),
        data_dir=_resolve(os.environ.get("DATA_DIR", "./data")),
        output_dir=_resolve(os.environ.get("OUTPUT_DIR", "./output")),
        timezone=ZoneInfo(os.environ.get("TIMEZONE", "UTC")),
        http_host=os.environ.get("HTTP_HOST", "0.0.0.0"),
        http_port=int(os.environ.get("HTTP_PORT", "9157")),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip() or None,
        image_format=image_format,
        low_battery_volts=float(os.environ.get("LOW_BATTERY_VOLTS", "3.5")),
    )
