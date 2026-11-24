from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger("xunzhi.model")

ENDPOINT = "https://api.deepseek.com/chat/completions"


class ModelError(Exception):
    def __init__(self, code: str, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class ImagePayload:
    ref: str
    description: str
    data: bytes
    mime: str


@dataclass
class TextPayload:
    ref: str
    description: str


@dataclass
class ModelRequest:
    system_prompt: str
    task_text: str
    text_items: list[TextPayload] = field(default_factory=list)
    images: list[ImagePayload] = field(default_factory=list)
    question_count: int = 5


@dataclass
class ModelResult:
    content_text: str
    finish_reason: str
    usage: dict
    latency_ms: int
    request_stats: dict = field(default_factory=dict)


@dataclass
class FitStats:
    text_items: int = 0
    images: int = 0
    text_bytes: int = 0
    image_bytes: int = 0
    request_bytes: int = 0
    limit_bytes: int = 0
    dropped_text_items: int = 0
    dropped_images: int = 0

    def as_dict(self) -> dict:
        return {
            "text_items": self.text_items,
            "images": self.images,
            "text_bytes": self.text_bytes,
            "image_bytes": self.image_bytes,
            "request_bytes": self.request_bytes,
            "limit_bytes": self.limit_bytes,
            "dropped_text_items": self.dropped_text_items,
            "dropped_images": self.dropped_images,
        }


def build_payload_for(request: ModelRequest, model: str) -> dict:
    blocks: list[dict] = []
    text_lines = "\n".join(item.description for item in request.text_items)
    if text_lines:
        blocks.append({"type": "text", "text": text_lines})
    for image in request.images:
        blocks.append({"type": "text", "text": image.description})
        encoded = base64.b64encode(image.data).decode("ascii")
        blocks.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime};base64,{encoded}",
                    "detail": "original",
                },
            }
        )
    blocks.append({"type": "text", "text": request.task_text})
    max_tokens = {3: 8000, 5: 12000, 8: 16000}.get(request.question_count, 12000)
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": blocks},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "thinking": {"type": "disabled"},
    }


def encode_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def fit_request(
    request: ModelRequest,
    *,
    model: str | None = None,
    max_bytes: int | None = None,
    min_text_items: int = 1,
    min_images: int = 0,
) -> tuple[ModelRequest, FitStats]:
    model = model or settings.model
    limit = max_bytes if max_bytes is not None else settings.model_max_request_mb * 1024 * 1024
    text_items = list(request.text_items)
    images = list(request.images)
    need_text = min_text_items if request.text_items else 0
    need_images = min_images if request.images else 0
    stats = FitStats(limit_bytes=limit)
    while True:
        candidate = ModelRequest(
            system_prompt=request.system_prompt,
            task_text=request.task_text,
            text_items=text_items,
            images=images,
            question_count=request.question_count,
        )
        payload = build_payload_for(candidate, model)
        size = len(encode_payload(payload))
        if size <= limit and len(text_items) >= need_text and len(images) >= need_images:
            stats.request_bytes = size
            break
        if len(images) > min_images:
            images.pop()
            stats.dropped_images += 1
            continue
        if len(text_items) > min_text_items:
            text_items.pop()
            stats.dropped_text_items += 1
            continue
        stats.request_bytes = size
        raise ModelError(
            "request_too_large",
            f"必要证据超出单次请求 {limit / (1024 * 1024):.1f} MB 上限，请缩小页码范围或减少资料后重试",
        )
    stats.text_items = len(text_items)
    stats.images = len(images)
    stats.text_bytes = sum(len(item.description.encode("utf-8")) for item in text_items)
    stats.image_bytes = sum(len(item.data) for item in images)
    return ModelRequest(
        system_prompt=request.system_prompt,
        task_text=request.task_text,
        text_items=text_items,
        images=images,
        question_count=request.question_count,
    ), stats


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key or settings.resolved_api_key
        self.model = model or settings.model
        self.transport = transport

    def build_payload(self, request: ModelRequest) -> dict:
        return build_payload_for(request, self.model)

    def generate(self, request: ModelRequest) -> ModelResult:
        if not self.api_key:
            raise ModelError("no_api_key", "服务端未配置 DEEPSEEK_API_KEY")
        request, fit = fit_request(request, model=self.model)
        payload = self.build_payload(request)
        if fit.dropped_images or fit.dropped_text_items:
            logger.info(
                "model_request trimmed model=%s %s",
                self.model,
                fit.as_dict(),
            )
        started = time.monotonic()
        budget = max(1, min(settings.generate_timeout, 180))
        deadline = started + budget
        response = None
        attempts = 0
        while attempts < 2:
            attempts += 1
            remaining = max(1, int(deadline - time.monotonic()))
            try:
                client_kwargs = {"timeout": httpx.Timeout(remaining)}
                if self.transport is not None:
                    client_kwargs["transport"] = self.transport
                with httpx.Client(**client_kwargs) as client:
                    response = client.post(
                        ENDPOINT,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
            except httpx.TimeoutException as exc:
                raise ModelError("generation_timeout", "模型服务响应超时") from exc
            except httpx.HTTPError as exc:
                raise ModelError("network_error", f"模型服务连接失败：{type(exc).__name__}") from exc
            if response.status_code != 429 and response.status_code < 500:
                break
            if attempts == 1:
                delay = min(2.0, max(0.0, deadline - time.monotonic() - 1))
                if delay > 0:
                    time.sleep(delay)
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code == 401:
            raise ModelError("auth_error", "模型服务鉴权失败，请检查服务端 Key", response.status_code)
        if response.status_code == 402:
            raise ModelError("quota_error", "模型服务额度不足", response.status_code)
        if response.status_code == 429:
            raise ModelError("upstream_rate_limited", "模型服务限流，请稍后重试", response.status_code)
        if response.status_code >= 500:
            raise ModelError("upstream_error", "模型服务暂时不可用", response.status_code)
        if response.status_code >= 400:
            raise ModelError(
                "upstream_error",
                f"模型服务返回 {response.status_code}",
                response.status_code,
            )
        body = response.json()
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish_reason = choice.get("finish_reason") or "stop"
        logger.info(
            "model_call ok model=%s latency_ms=%s finish_reason=%s usage=%s request=%s",
            self.model,
            latency,
            finish_reason,
            body.get("usage"),
            fit.as_dict(),
        )
        return ModelResult(
            content_text=content,
            finish_reason=finish_reason,
            usage=body.get("usage") or {},
            latency_ms=latency,
            request_stats=fit.as_dict(),
        )


class FakeModelClient:
    """Deterministic test double. Enabled only with XUNZHI_ALLOW_FAKE=1 or test injection."""

    def __init__(self, mode: str | None = None, fixture_dir: Path | None = None):
        self.mode = mode or settings.fake_mode
        self.fixture_dir = fixture_dir or Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "model_output"

    def _request_stats(self, request: ModelRequest) -> dict:
        payload = build_payload_for(request, "fake")
        return {
            "text_items": len(request.text_items),
            "images": len(request.images),
            "text_bytes": sum(len(item.description.encode("utf-8")) for item in request.text_items),
            "image_bytes": sum(len(item.data) for item in request.images),
            "request_bytes": len(encode_payload(payload)),
            "limit_bytes": settings.model_max_request_mb * 1024 * 1024,
            "dropped_text_items": 0,
            "dropped_images": 0,
            "is_test_double": True,
        }

    def generate(self, request: ModelRequest) -> ModelResult:
        if self.mode == "timeout":
            raise ModelError("generation_timeout", "模型服务响应超时（测试替身）")
        stats = self._request_stats(request)
        if "【连接测试】" in request.task_text:
            return ModelResult(
                content_text='{"ok": true}',
                finish_reason="stop",
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "fake": True},
                latency_ms=1,
                request_stats=stats,
            )
        if "【个人学习测评】" in request.task_text:
            result = self._assessment(request)
            result.request_stats = stats
            return result
        if "【模块计划】" in request.task_text:
            result = self._module_output(request)
            result.request_stats = stats
            return result
        retry_mode = "retry" if "【再练约束】" in request.task_text else self.mode
        path = self.fixture_dir / f"generate_{request.question_count}q_{retry_mode}.json"
        if not path.exists() and retry_mode != self.mode:
            path = self.fixture_dir / f"generate_{request.question_count}q_{self.mode}.json"
        if not path.exists():
            path = self.fixture_dir / f"generate_{request.question_count}q_ok.json"
        if not path.exists():
            raise ModelError("upstream_error", "测试替身缺少回放样例")
        content = path.read_text(encoding="utf-8")
        return ModelResult(
            content_text=content,
            finish_reason="length" if self.mode == "truncated" else "stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "fake": True},
            latency_ms=1,
            request_stats=stats,
        )

    def _module_output(self, request: ModelRequest) -> ModelResult:
        marker = "【模块计划】\n"
        raw = request.task_text.split(marker, 1)[1] if marker in request.task_text else "[]"
        try:
            modules, _ = json.JSONDecoder().raw_decode(raw.strip())
        except json.JSONDecodeError:
            modules = []
        text_ref = next((item.ref for item in request.text_items), None)
        image_ref = request.images[0].ref if request.images else None
        ref = text_ref or image_ref or "T1"
        counts: dict[str, int] = {}
        for module in modules:
            points = module.get("core_points") or []
            planned = int(module.get("question_count") or 0)
            if points and planned:
                base, extra = divmod(planned, len(points))
                for index, point in enumerate(points):
                    counts[point] = counts.get(point, 0) + base + (1 if index < extra else 0)
        if not counts:
            match = re.search(r"每考点题量：(.+?)；排除考点", request.task_text)
            if match:
                for part in match.group(1).split("；"):
                    name, _, number = part.rpartition("=")
                    if name.strip() and number.strip().isdigit():
                        counts[name.strip()] = int(number.strip())
        if not counts:
            for module in modules:
                for point in module.get("core_points") or []:
                    counts.setdefault(point, 1)
        module_of: dict[str, str] = {}
        for module in modules:
            for point in module.get("core_points") or []:
                module_of[point] = module.get("module_id")
        for point in counts:
            module_of.setdefault(point, modules[0].get("module_id") if modules else "mod_core")

        def block(heading: str, body: str) -> dict:
            return {
                "heading": heading,
                "body": body,
                "role": "weak",
                "citations": [{"ref": ref, "claim": heading}],
            }

        module_output = []
        for module in modules:
            title = module.get("title") or module.get("module_id")
            core = module.get("core_points") or []
            core_text = "、".join(core) if core else title
            key_points = [
                block(
                    "核心概念",
                    f"学习「{title}」时，先把资料里的判断条件拆成三件事：设备处在哪一侧、双方是否属于同一范围、是否必须交给其他设备继续转发。"
                    f"本模块覆盖{core_text}；结论必须回到资料中的标签、连线和文字依据，不能只凭设备名称或直觉。",
                ),
                block(
                    "如何应用",
                    f"遇到新的情境，先定位题目问的是哪一步，再对照「{core_text}」逐项检查，最后说明为什么另一条路径不成立。"
                    "如果资料没有给出某个机制，就不要把它当作作答依据；把已学概念和易混点放在一起比较。",
                ),
            ]
            pitfalls = [
                block(
                    "容易混淆的地方",
                    f"常见错误是把「{core_text}」中的某一个条件当作全部条件，或把资料里没有出现的机制补进来。"
                    "判断时先写清已知条件，再检查结论是否被原文直接支持。",
                )
            ]
            visual = (
                [block("图文依据", f"对照资料图，找到与「{title}」直接相关的设备标签、连线或区域，再用一句话说明它们如何支持本模块结论。")]
                if image_ref
                else []
            )
            point_ids = module.get("knowledge_point_ids") or []
            first_point_id = point_ids[0] if point_ids else ""
            pages = [
                {
                    "page_id": "core-mechanism",
                    "knowledge_point_ids": [first_point_id] if first_point_id else [],
                    "title": f"机制：{title}",
                    "explanation": (
                        f"学习「{title}」时，先列出资料给出的条件，再说明按什么顺序判断，"
                        f"最后核对结论是否由原文标签、连线或文字直接支持。"
                        f"本模块覆盖{core_text}；遇到新情境时，先判断条件落在哪一侧，再套用对应规则。"
                    ),
                    "visual": None,
                    "related_sources": [{"ref": ref, "claim": f"{title}的判断条件"}],
                }
            ]
            if len(core) >= 2:
                pages.append(
                    {
                        "page_id": "compare-cases",
                        "knowledge_point_ids": point_ids[:2],
                        "title": f"对照：{core[0]} 与 {core[1]}",
                        "explanation": (
                            f"左卡说明「{core[0]}」的条件与结果，右卡说明「{core[1]}」的条件与结果。"
                            "遇到新案例时，先判断它属于哪一侧，再套用对应规则。"
                        ),
                        "visual": {
                            "type": "comparison",
                            "title": "条件对比",
                            "caption": "教学示意：用两个具体例子说明条件变化带来的结果差异",
                            "cards": [
                                {
                                    "title": core[0],
                                    "condition": f"满足「{core[0]}」的条件",
                                    "example": "教学示例：一个符合该条件的具体情境",
                                    "result": f"按资料得到「{core[0]}」的结果",
                                },
                                {
                                    "title": core[1],
                                    "condition": f"满足「{core[1]}」的条件",
                                    "example": "教学示例：一个落在另一侧的情境",
                                    "result": f"按资料得到「{core[1]}」的结果",
                                },
                            ],
                        },
                        "related_sources": [{"ref": ref, "claim": "资料说明两个概念的区别"}],
                    }
                )
            for image_index, image in enumerate(request.images[:4], start=1):
                pages.append(
                    {
                        "page_id": f"source-figure-{image_index}",
                        "knowledge_point_ids": [first_point_id] if first_point_id else [],
                        "title": f"对照资料原图 {image_index}",
                        "explanation": (
                            "这一页展示本次实际发送的资料原图，并说明图中可见的标签、连线或区域"
                            "如何支持本页结论；右栏逐条对应图中的条件与结果。"
                        ),
                        "visual": {
                            "type": "source_asset",
                            "source_ref": image.ref,
                            "caption": "本页引用资料中的原始图像",
                            "alt_text": "资料原图中标出当前条件与对应结果",
                        },
                        "related_sources": [
                            {"ref": image.ref, "claim": "资料原图对应本页讲解的条件"}
                        ],
                    }
                )
            module_output.append(
                {
                    "module_id": module.get("module_id"),
                    "title": title,
                    "objective": module.get("objective") or f"掌握{core_text}",
                    "core_points": core,
                    "key_points": key_points,
                    "pitfalls": pitfalls,
                    "visual": visual,
                    "pages": pages,
                    "recap": {
                        "summary": f"本模块用条件判断「{core_text}」的结果。",
                        "rules": [
                            {
                                "condition": f"满足「{core[0]}」的条件" if core else f"满足「{title}」的条件",
                                "result": f"得到与「{core[0]}」一致的结果" if core else "按资料得到对应结论",
                                "pitfall": "不要把条件当作结论，或补入资料没有展开的机制",
                            }
                        ],
                        "cross_module": "与其他模块的区别：先确认本模块的判断条件，再与另一模块的过程或对象区分。",
                    },
                    "completion": module.get("completion") or "能独立完成本模块练习。",
                }
            )

        flat_key_points = [
            block("先看题目条件", "先确认资料给出的设备位置、地址范围和直接连线，再决定是否需要其他设备参与；不要把直觉当成原文依据。"),
            block("再找直接依据", "每个结论都要能指回资料中的标签、连线或文字；没有依据的推断只能作为待验证的猜测，不能直接出题。"),
            block("最后做情境迁移", "把同一个判断放到新的主机组合或不同网段里，检查规则是否仍然成立，避免只记住上一题答案。"),
        ]
        flat_pitfalls = [
            block("只看设备名称", "设备名称相同不代表处在同一范围，仍要看它们在资料中的位置和连接关系。"),
            block("补入未学机制", "资料没有展开的协议细节不能拿来做题或解释；遇到边界外内容只保留为未覆盖说明。"),
        ]
        questions = []
        ordinal = 1
        module_question_counts: dict[str, int] = {}
        for point, count in counts.items():
            module_id = module_of.get(point)
            for index in range(count):
                stem = f"关于「{point}」，第 {ordinal} 个新情境下哪项判断符合资料依据？"
                questions.append(
                    {
                        "ordinal": ordinal,
                        "stem": stem,
                        "options": [
                            "按资料条件与设备角色判断",
                            "因为设备名称相同就判定为同一范围",
                            "忽略资料中的网段与连接关系",
                            "套用资料之外的路由协议细节",
                        ],
                        "answer_index": 0,
                        "explanation": f"正确答案依据资料中与「{point}」直接相关的条件；其余选项要么混淆条件，要么引入未学机制。",
                        "knowledge_point": point,
                        "module_id": module_id,
                        "difficulty_tag": "consolidate",
                        "is_image_based": False,
                        "citations": [{"ref": ref, "claim": point}],
                    }
                )
                module_question_counts[module_id] = module_question_counts.get(module_id, 0) + 1
                ordinal += 1
        review_required = "【本批综合复习】" in request.task_text
        if len(modules) >= 2 or review_required:
            review_target = next(
                (mid for mid, count in module_question_counts.items() if count >= 2),
                None,
            )
            if review_target:
                for question in reversed(questions):
                    if question["module_id"] == review_target:
                        question["module_id"] = "review"
                        break
        content = json.dumps(
            {
                "progress_reading": {
                    "learned": [],
                    "unlearned": [],
                    "weak_points": [module.get("title") for module in modules],
                    "intent": [],
                    "insufficient": False,
                },
                "handout": {
                    "scope": {
                        "covered": [module.get("title") for module in modules],
                        "not_covered": ["资料之外的扩展机制"],
                    },
                    "key_points": flat_key_points,
                    "pitfalls": flat_pitfalls,
                    "visual": [block("图文解释", "对照资料图说明设备位置、连接与判断条件之间的关系。")] if image_ref else [],
                },
                "modules": module_output,
                "questions": questions,
                "insufficient_evidence": None,
            },
            ensure_ascii=False,
        )
        return ModelResult(
            content_text=content,
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "fake": True},
            latency_ms=1,
        )

    def _assessment(self, request: ModelRequest) -> ModelResult:
        marker = "【测评输入】\n"
        raw = request.task_text.split(marker, 1)[1] if marker in request.task_text else "{}"
        try:
            data, _ = json.JSONDecoder().raw_decode(raw.strip())
        except json.JSONDecodeError:
            data = {}
        points = data.get("points") or []
        evidence = data.get("evidence") or []
        findings = []
        steps = []
        unknowns = []
        if points and evidence:
            point = points[0]
            point_id = point["knowledge_point_id"]
            items = [item for item in evidence if item.get("knowledge_point_id") == point_id]
            wrong = [item for item in items if item.get("result") == "wrong"]
            basis = wrong[0] if wrong else (items[0] if items else None)
            if basis is not None:
                if wrong:
                    findings.append(
                        {
                            "knowledge_point_id": point_id,
                            "kind": "observation",
                            "judgement": f"{point.get('display_name', '该考点')}最近出现答错。",
                            "evidence_ids": [item["evidence_id"] for item in wrong[:2]],
                            "note": "可从已保存的作答记录核对。",
                        }
                    )
                    steps.extend(
                        [
                            {
                                "knowledge_point_id": point_id,
                                "action": "compare_explanation",
                                "instruction": "先回看资料中的对照说明。",
                                "basis_evidence_ids": [basis["evidence_id"]],
                                "completion": "能说出正确判断依据。",
                            },
                            {
                                "knowledge_point_id": point_id,
                                "action": "new_question",
                                "instruction": "做两道不同情境的同考点新题。",
                                "basis_evidence_ids": [basis["evidence_id"]],
                                "completion": "两道新题都能独立答对。",
                            },
                        ]
                    )
                else:
                    findings.append(
                        {
                            "knowledge_point_id": point_id,
                            "kind": "observation",
                            "judgement": "当前证据显示该考点作答正确。",
                            "evidence_ids": [basis["evidence_id"]],
                            "note": "仍需不同情境的新题确认。",
                        }
                    )
                    steps.append(
                        {
                            "knowledge_point_id": point_id,
                            "action": "new_question",
                            "instruction": "用一道不同情境的新题确认。",
                            "basis_evidence_ids": [basis["evidence_id"]],
                            "completion": "新题能独立答对。",
                        }
                    )
        content = json.dumps(
            {"findings": findings, "route_steps": steps, "unknowns": unknowns},
            ensure_ascii=False,
        )
        return ModelResult(
            content_text=content,
            finish_reason="stop",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "fake": True},
            latency_ms=1,
        )


def get_model_client():
    if settings.allow_fake:
        return FakeModelClient()
    return DeepSeekClient()


def is_fake_client(client) -> bool:
    return isinstance(client, FakeModelClient)