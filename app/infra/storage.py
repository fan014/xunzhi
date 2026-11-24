from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from app.config import settings


def ensure_dirs() -> None:
    for path in (
        settings.data_dir,
        settings.sources_dir,
        settings.derived_dir,
        settings.tmp_dir,
        settings.logs_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


def source_dir(source_id: str) -> Path:
    return settings.sources_dir / source_id


def original_path(source_id: str, ext: str) -> Path:
    return source_dir(source_id) / f"original.{ext}"


def derived_dir(source_id: str) -> Path:
    return settings.derived_dir / source_id


def pages_dir(source_id: str) -> Path:
    return derived_dir(source_id) / "pages"


def thumbs_dir(source_id: str) -> Path:
    return derived_dir(source_id) / "thumbs"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def relative(path: Path) -> str:
    return path.relative_to(settings.data_dir).as_posix()


def absolute(rel_path: str) -> Path:
    target = (settings.data_dir / rel_path).resolve()
    root = settings.data_dir.resolve()
    if root != target and root not in target.parents:
        raise ValueError("path escapes data directory")
    return target


def cleanup_tmp() -> None:
    if not settings.tmp_dir.exists():
        return
    for child in settings.tmp_dir.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)