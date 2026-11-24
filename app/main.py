from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.offline import router as offline_router
from app.api.routes import router, templates
from app.api.settings import router as settings_router
from app.config import PROJECT_ROOT, settings
from app.infra import storage
from app.infra.db import init_db, recover_interrupted_jobs
from app.infra.migrations import MigrationError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def error_body(code: str, message: str, field: str | None = None, actions: list[str] | None = None):
    payload = {"error": {"code": code, "message": message, "actions": actions or []}}
    if field:
        payload["error"]["field"] = field
    return payload


def _backfill_text_indexes() -> None:
    try:
        from app.domain.text_index import backfill_all
        from app.infra.db import SessionLocal

        with SessionLocal() as db:
            results = backfill_all(db)
        logging.getLogger("xunzhi").info("text index backfill done results=%s", results)
    except Exception:
        logging.getLogger("xunzhi").warning("text index backfill failed", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_dirs()
    try:
        init_db()
    except MigrationError as exc:
        logging.getLogger("xunzhi").error(
            "startup aborted code=%s table=generation_job column=launch_state",
            getattr(exc, "code", "migration_failed"),
        )
        raise
    recover_interrupted_jobs()
    storage.cleanup_tmp()
    threading.Thread(target=_backfill_text_indexes, name="text-index-backfill", daemon=True).start()
    try:
        from app.domain.deletion import cleanup_pending_sources
        from app.infra.db import SessionLocal

        with SessionLocal() as db:
            cleanup_pending_sources(db)
    except Exception:
        logging.getLogger("xunzhi").warning("cleanup pending sources failed", exc_info=True)
    yield


app = FastAPI(title="循知 Xunzhi", version="0.4.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")), name="static")


@app.exception_handler(StarletteHTTPException)
async def http_error(_request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content=error_body("http_error", str(detail)))


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", [])[1:])
    return JSONResponse(
        status_code=422,
        content=error_body("invalid_request", str(first.get("msg", "请求参数不合法")), field=field),
    )


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception):
    logging.getLogger("xunzhi").exception("unhandled error")
    return JSONResponse(status_code=500, content=error_body("internal_error", "服务内部错误，请稍后重试"))


app.include_router(router)
app.include_router(offline_router)
app.include_router(settings_router)


@app.get("/")
def home():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("index.html").render())


@app.get("/packs/{pack_id}")
def pack_page(pack_id: str):
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("pack.html").render(pack_id=pack_id))


@app.get("/diagnosis/{session_id}")
def diagnosis_page(session_id: str):
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("diagnosis.html").render(session_id=session_id))


@app.get("/records")
def records_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("records.html").render())


@app.get("/calendar")
def calendar_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("calendar.html").render())


@app.get("/profile")
def profile_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("profile.html").render())


@app.get("/knowledge-bases")
def knowledge_bases_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("knowledge_bases.html").render())


@app.get("/knowledge-bases/{kb_id}")
def knowledge_base_page(kb_id: str):
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("knowledge_base.html").render(kb_id=kb_id))


@app.get("/offline")
def offline_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("offline.html").render())

@app.get("/settings")
def settings_page():
    from fastapi.responses import HTMLResponse

    return HTMLResponse(templates.get_template("settings.html").render())