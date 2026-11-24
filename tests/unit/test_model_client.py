from __future__ import annotations

from app.infra.model_client import DeepSeekClient, ImagePayload, ModelRequest, TextPayload


def make_request() -> ModelRequest:
    return ModelRequest(
        system_prompt="只可使用证据，输出 json",
        task_text="【任务】难度=巩固；题量=5",
        text_items=[TextPayload(ref="T1", description="[T1] 计算机网络.pdf｜第16页｜交换机按 MAC 转发")],
        images=[
            ImagePayload(
                ref="F1",
                description="【图 F1】网络拓扑.png",
                data=b"\x89PNG\r\n\x1a\nfake",
                mime="image/png",
            )
        ],
        question_count=5,
    )


def test_payload_uses_single_model_json_mode_and_disabled_thinking():
    payload = DeepSeekClient(api_key="test-key", model="deepseek-flash").build_payload(make_request())
    assert payload["model"] == "deepseek-flash"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 12000


def test_image_block_is_immediately_preceded_by_its_label():
    payload = DeepSeekClient(api_key="test-key").build_payload(make_request())
    blocks = payload["messages"][1]["content"]
    image_index = next(index for index, block in enumerate(blocks) if block["type"] == "image_url")
    label = blocks[image_index - 1]
    assert label["type"] == "text"
    assert "F1" in label["text"]
    image = blocks[image_index]["image_url"]
    assert image["url"].startswith("data:image/png;base64,")
    assert image["detail"] == "original"


def test_text_evidence_precedes_images():
    payload = DeepSeekClient(api_key="test-key").build_payload(make_request())
    blocks = payload["messages"][1]["content"]
    assert "[T1]" in blocks[0]["text"]
    assert blocks[-1]["type"] == "text"
    assert "【任务】" in blocks[-1]["text"]


def test_key_is_never_embedded_in_payload():
    payload = DeepSeekClient(api_key="sk-secret-key", model="deepseek-flash").build_payload(make_request())
    assert "sk-secret-key" not in str(payload)