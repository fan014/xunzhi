from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes import get_db
from app.domain import offline

router = APIRouter(prefix="/api/offline")


class OfflineAnswer(BaseModel):
    question_id: str
    selected_index: int | None = None


class OfflineSubmit(BaseModel):
    answers: list[OfflineAnswer] = Field(default_factory=list)


class OfflineRetry(BaseModel):
    focus_ids: list[str] = Field(default_factory=list)


class OfflineDiagnosisAnswer(BaseModel):
    branch: str


def fail(status: int, code: str, message: str):
    return HTTPException(status_code=status, detail={"error": {"code": code, "message": message, "actions": []}})


@router.get("/sample")
def get_sample(db: Session = Depends(get_db)):
    return offline.sample_payload(db)


@router.post("/attempts")
def submit_offline(payload: OfflineSubmit, db: Session = Depends(get_db)):
    try:
        return offline.submit(db, [item.model_dump() for item in payload.answers])
    except offline.OfflineError as exc:
        raise fail(exc.status, exc.code, exc.message)


@router.post("/retry")
def retry_offline(payload: OfflineRetry, db: Session = Depends(get_db)):
    try:
        return offline.retry(db, payload.focus_ids)
    except offline.OfflineError as exc:
        raise fail(exc.status, exc.code, exc.message)


@router.get("/diagnosis/sample")
def get_diagnosis_sample(db: Session = Depends(get_db)):
    return offline.diagnosis_sample(db)


@router.post("/diagnosis/answer")
def answer_diagnosis_offline(payload: OfflineDiagnosisAnswer, db: Session = Depends(get_db)):
    try:
        return offline.diagnosis_answer(db, payload.branch)
    except offline.OfflineError as exc:
        raise fail(exc.status, exc.code, exc.message)


@router.post("/diagnosis/reset")
def reset_diagnosis_offline(db: Session = Depends(get_db)):
    return offline.diagnosis_reset(db)


@router.post("/reset")
def reset_offline(db: Session = Depends(get_db)):
    return offline.reset(db)