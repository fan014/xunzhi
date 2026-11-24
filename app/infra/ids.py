from __future__ import annotations

from ulid import ULID

PREFIXES = {
    "source": "src_",
    "evidence": "ev_",
    "asset": "ast_",
    "pack": "pack_",
    "job": "job_",
    "section": "sec_",
    "question": "q_",
    "citation": "cit_",
    "attempt": "att_",
    "review": "rf_",
    "knowledge_point": "kp_",
    "learning_evidence": "lev_",
    "plan": "plan_",
    "plan_version": "pv_",
    "schedule_task": "sch_",
    "diagnosis_session": "dgs_",
    "diagnosis_round": "dgr_",
    "diagnosis_question": "dgq_",
    "diagnosis_answer": "dga_",
    "self_assessment": "sa_",
    "profile": "lps_",
    "knowledge_base": "kb_",
    "kb_item": "kbi_",
    "batch_cache": "bch_",
    "user": "user_",
    "switch": "sw_",
}


def new_id(kind: str) -> str:
    prefix = PREFIXES[kind]
    return f"{prefix}{ULID()}"