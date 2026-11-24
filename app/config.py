from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    root_dir: Path
    user_id: str
    desktop_mode: bool
    api_key: str
    model: str
    generate_timeout: int
    stall_hint: int
    max_sources: int
    max_pages: int
    max_total_mb: int
    max_progress_chars: int
    kb_max_sources: int
    kb_max_pages: int
    model_max_text_items: int
    model_max_images: int
    model_max_request_mb: int
    generation_max_parallel: int
    host: str
    port: int
    allow_fake: bool
    fake_mode: str

    @property
    def resolved_api_key(self) -> str:
        from app.infra import credentials

        default_workspace = self.data_dir.resolve() == self.root_dir.resolve()
        return credentials.effective_api_key(
            self.data_dir, self.api_key, inherit_env=default_workspace
        )

    @property
    def key_configured(self) -> bool:
        return bool(self.resolved_api_key)

    @property
    def can_generate(self) -> bool:
        return self.key_configured or self.allow_fake

    @property
    def is_fake(self) -> bool:
        return not self.key_configured and self.allow_fake

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.root_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "xunzhi.sqlite3"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = Path(os.getenv("XUNZHI_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    root_dir = Path(os.getenv("XUNZHI_ROOT_DIR", str(data_dir)))
    if not root_dir.is_absolute():
        root_dir = PROJECT_ROOT / root_dir
    return Settings(
        data_dir=data_dir,
        root_dir=root_dir,
        user_id=(os.getenv("XUNZHI_USER_ID") or "").strip(),
        desktop_mode=_bool(os.getenv("XUNZHI_DESKTOP"), False),
        api_key=(os.getenv("DEEPSEEK_API_KEY") or "").strip(),
        model=os.getenv("XUNZHI_MODEL", "deepseek-flash"),
        generate_timeout=_int(os.getenv("XUNZHI_GENERATE_TIMEOUT"), 180),
        stall_hint=_int(os.getenv("XUNZHI_STALL_HINT"), 60),
        max_sources=_int(os.getenv("XUNZHI_MAX_SOURCES"), 10),
        max_pages=_int(os.getenv("XUNZHI_MAX_PAGES"), 40),
        max_total_mb=_int(os.getenv("XUNZHI_MAX_TOTAL_MB"), 150),
        max_progress_chars=_int(os.getenv("XUNZHI_MAX_PROGRESS_CHARS"), 500),
        kb_max_sources=_int(os.getenv("XUNZHI_KB_MAX_SOURCES"), 100),
        kb_max_pages=_int(os.getenv("XUNZHI_KB_MAX_PAGES"), 2000),
        model_max_text_items=_int(os.getenv("XUNZHI_MODEL_MAX_TEXT_ITEMS"), 80),
        model_max_images=_int(os.getenv("XUNZHI_MODEL_MAX_IMAGES"), 12),
        model_max_request_mb=_int(os.getenv("XUNZHI_MODEL_MAX_REQUEST_MB"), 24),
        generation_max_parallel=_int(os.getenv("XUNZHI_GENERATION_MAX_PARALLEL"), 2),
        host=os.getenv("XUNZHI_HOST", "127.0.0.1"),
        port=_int(os.getenv("XUNZHI_PORT"), 8000),
        allow_fake=_bool(os.getenv("XUNZHI_ALLOW_FAKE"), False),
        fake_mode=os.getenv("XUNZHI_FAKE_MODE", "ok"),
    )


settings = load_settings()