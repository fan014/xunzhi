from __future__ import annotations

import pytest

from app.infra.model_client import (
    ImagePayload,
    ModelError,
    ModelRequest,
    TextPayload,
    build_payload_for,
    encode_payload,
    fit_request,
)


def _request(images=None, texts=None) -> ModelRequest:
    return ModelRequest(
        system_prompt="只可使用证据，输出 json",
        task_text="【任务】题量=5",
        text_items=texts
        or [
            TextPayload(ref="T1", description="[T1] 第一段证据"),
            TextPayload(ref="T2", description="[T2] 第二段证据"),
        ],
        images=images or [],
        question_count=5,
    )


def test_fit_request_drops_late_images_before_text():
    images = [
        ImagePayload(ref=f"I{index}", description=f"图 {index}", data=b"x" * 200_000, mime="image/png")
        for index in range(1, 4)
    ]
    fitted, stats = fit_request(_request(images=images), model="test-model", max_bytes=600_000)

    assert len(fitted.images) == 2
    assert [item.ref for item in fitted.images] == ["I1", "I2"]
    assert len(fitted.text_items) == 2
    assert stats.dropped_images == 1
    assert stats.dropped_text_items == 0
    assert stats.request_bytes == len(encode_payload(build_payload_for(fitted, "test-model")))
    assert stats.request_bytes <= 600_000


def test_fit_request_drops_text_when_images_are_gone():
    texts = [
        TextPayload(ref=f"T{index}", description="证" * 1000) for index in range(1, 4)
    ]
    fitted, stats = fit_request(_request(texts=texts), model="test-model", max_bytes=4_000)

    assert len(fitted.text_items) == 1
    assert fitted.text_items[0].ref == "T1"
    assert stats.dropped_text_items == 2
    assert stats.dropped_images == 0
    assert stats.request_bytes <= 4_000


def test_fit_request_reports_when_required_evidence_exceeds_limit():
    request = _request(texts=[TextPayload(ref="T1", description="证" * 5000)])

    with pytest.raises(ModelError) as excinfo:
        fit_request(request, model="test-model", max_bytes=1_000)

    assert excinfo.value.code == "request_too_large"
    assert "必要证据" in excinfo.value.message