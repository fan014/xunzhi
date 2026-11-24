from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domain import diagnosis  # noqa: E402


class DiagnosisStub:
    def __init__(self) -> None:
        self.calls = 0
        self.mode = os.getenv("XUNZHI_STUB_MODE", "retry")

    def _ref(self, task_text: str) -> str:
        for line in task_text.splitlines():
            text = line.strip()
            if len(text) > 1 and text[0] in "TPIF" and text[1].isdigit():
                return text.split("|", 1)[0].strip()
        return "T1"

    def generate(self, request):
        self.calls += 1
        if self.mode == "dual_broken":
            return SimpleNamespace(
                content_text=(
                    '{"question_type":"scope","stem":"第一道完整","options":["学过","没学过"],"citation_ref":"T1"}'
                    '\n{"question_type":"probe_question","stem":"第二道残缺'
                ),
                usage={},
                finish_reason="stop",
                request_stats={},
            )
        ref = self._ref(request.task_text)
        if self.calls == 1:
            payload = {
                "question_type": "scope",
                "stem": "第一轮：你学过交换机转发吗？",
                "options": ["学过", "没学过"],
                "citation_ref": "[T99]",
            }
        else:
            payload = {
                "question_type": "scope",
                "stem": "第二轮：你学过跨网段转发吗？",
                "options": ["学过", "没学过"],
                "citation_ref": f"[{ref}]",
            }
        return SimpleNamespace(
            content_text=json.dumps(payload, ensure_ascii=False),
            usage={},
            finish_reason="stop",
            request_stats={},
        )


stub = DiagnosisStub()
diagnosis.get_model_client = lambda: stub


def main() -> int:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.getenv("XUNZHI_PORT", "8023")),
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())