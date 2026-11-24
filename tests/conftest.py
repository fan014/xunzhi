from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / ".pytest-data"

os.environ["XUNZHI_DATA_DIR"] = str(DATA_DIR)
os.environ["XUNZHI_ALLOW_FAKE"] = "1"
os.environ["XUNZHI_FAKE_MODE"] = "ok"
# 显式置空，阻止 load_dotenv 从本机 .env 注入真实 Key；测试仍强制替身。
os.environ["DEEPSEEK_API_KEY"] = ""

shutil.rmtree(DATA_DIR, ignore_errors=True)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from tests.fixtures import make_fixtures  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def prepare_fixtures():
    make_fixtures.main()
    yield


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def topology_png() -> Path:
    return ROOT / "fixtures" / "images" / "topology.png"


@pytest.fixture()
def sample_pdf() -> Path:
    return ROOT / "fixtures" / "pdf" / "sample_3pages.pdf"