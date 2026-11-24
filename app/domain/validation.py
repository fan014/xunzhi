from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.domain.schemas import (
    PAGE_TYPES,
    ModelOutput,
    PageVisual,
    RefMap,
    stem_hash,
    word_count,
)


@dataclass
class ValidationError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _require_refs(owner: str, citations, ref_map: RefMap) -> None:
    if not citations:
        raise ValidationError("missing_citation", f"{owner} 没有资料依据")
    for citation in citations:
        target = ref_map.get(citation.ref)
        if target is None:
            raise ValidationError(
                "citation_not_found", f"{owner} 引用了本次证据清单中不存在的编号 {citation.ref}"
            )
        if target.kind in {"page_image", "image_region"} and target.confidence != "high":
            raise ValidationError("low_confidence_image", f"{owner} 仅依据低置信度图像")


_UNLEARNED_PROTOCOLS = ("OSPF", "BGP", "RIP")
_UNLEARNED_MECHANISM_TERMS = ("路由表",)
_MECHANISM_OPERATION_CUES = (
    "计算",
    "构建",
    "生成",
    "收敛",
    "查找过程",
    "构建过程",
    "工作机制",
    "原理",
    "如何",
    "为什么",
    "步骤",
    "算法",
    "下一跳",
)
_EXCLUSION_CUES = (
    "不需要",
    "无需",
    "不涉及",
    "不依赖",
    "不查",
    "不用",
    "不采用",
    "而不是",
    "而非",
    "不是",
    "不作为",
    "不包含",
    "不负责",
)


def _near_mentions(text: str, term: str, radius: int = 16) -> list[str]:
    windows: list[str] = []
    index = text.find(term)
    while index != -1:
        windows.append(text[max(0, index - radius) : index + len(term) + radius])
        index = text.find(term, index + 1)
    return windows


def _requires_unlearned_mechanism(stem: str, term: str, known: str) -> bool:
    if term not in stem:
        return False
    if term in known or term.lower() in known.lower():
        return False
    if term not in _UNLEARNED_MECHANISM_TERMS:
        return True
    for window in _near_mentions(stem, term):
        if any(cue in window for cue in _EXCLUSION_CUES):
            continue
        if any(cue in window for cue in _MECHANISM_OPERATION_CUES):
            return True
    return False


def _step_refs_ok(step, node_ids: set[str], edge_ids: set[str]) -> bool:
    if any(node_id not in node_ids for node_id in step.active_node_ids):
        return False
    if any(edge_id not in edge_ids for edge_id in step.active_edge_ids):
        return False
    if any(node_id not in node_ids for node_id in step.node_states):
        return False
    return True


def _page_visual_ok(
    visual: PageVisual,
    ref_map: RefMap,
    sent_image_refs: set[str] | None,
) -> tuple[bool, str]:
    if visual.type == "text":
        return True, ""
    if visual.type == "source_asset":
        ref = (visual.source_ref or "").strip()
        target = ref_map.get(ref) if ref else None
        if target is None:
            return False, "本页资料原图引用不在本次可用范围内"
        if target.kind not in {"page_image", "image_region"}:
            return False, "本页引用的不是图像证据"
        if target.confidence != "high":
            return False, "本页资料原图置信度不足"
        if sent_image_refs is not None and ref not in sent_image_refs:
            return False, "本页资料原图未被本次模型调用实际发送"
        return True, ""
    if visual.type == "comparison":
        usable = [
            card
            for card in visual.cards
            if (card.title or card.condition or card.result or card.example).strip()
        ]
        if len(usable) < 2:
            return False, "对照卡片不足两张"
        return True, ""
    if visual.type == "step_sequence":
        if visual.preset_id:
            return True, ""
        node_ids = [item.id for item in visual.nodes if (item.id or "").strip()]
        if not node_ids or len(set(node_ids)) != len(node_ids):
            return False, "分步演示的节点为空或 ID 重复"
        edge_ids = [item.id for item in visual.edges if (item.id or "").strip()]
        if len(set(edge_ids)) != len(edge_ids):
            return False, "分步演示的连线 ID 重复"
        node_set = set(node_ids)
        for edge in visual.edges:
            if edge.source not in node_set or edge.target not in node_set:
                return False, "分步演示的连线端点不存在"
        edge_set = set(edge_ids)
        steps = list(visual.steps)
        for mode in visual.modes:
            steps.extend(mode.steps)
        if not steps:
            return False, "分步演示缺少步骤"
        if any(not _step_refs_ok(step, node_set, edge_set) for step in steps):
            return False, "分步演示的步骤引用了不存在的节点或连线"
        option_ids = [item.id for item in visual.options if (item.id or "").strip()]
        if len(set(option_ids)) != len(option_ids):
            return False, "演示选项 ID 重复"
        option_set = set(option_ids)
        if visual.correct_option_id and visual.correct_option_id not in option_set:
            return False, "演示的正确答案不属于当前场景选项"
        for mode in visual.modes:
            if mode.correct_option_id and mode.correct_option_id not in option_set:
                return False, "演示的正确答案不属于当前场景选项"
        return True, ""
    return False, "当前图示类型不受支持"


def sanitize_module_pages(
    output: ModelOutput,
    ref_map: RefMap,
    sent_image_refs: set[str] | None = None,
) -> list[str]:
    """校验并降级学习页图示。无效图示保留正文，仅记录提示，不阻断翻页。"""
    warnings: list[str] = []
    for module in output.modules:
        seen: set[str] = set()
        for index, page in enumerate(module.pages):
            label = module.title or module.module_id
            if not page.explanation.strip():
                page.explanation = module.objective or "本页暂无详细讲解，可继续查看相关资料。"
                warnings.append(f"模块「{label}」第 {index + 1} 页正文为空，已使用模块目标兜底")
            if not page.title.strip():
                page.title = f"{label} · 学习页 {index + 1}"
                warnings.append(f"模块「{label}」第 {index + 1} 页缺少标题，已自动补全")
            if not page.page_id.strip() or page.page_id in seen:
                page.page_id = f"page_{module.module_id}_{index + 1}"
                warnings.append(f"模块「{label}」第 {index + 1} 页 page_id 为空或重复，已重建")
            seen.add(page.page_id)
            valid_sources = []
            for citation in page.related_sources:
                if ref_map.get(citation.ref) is None:
                    warnings.append(
                        f"模块「{label}」第 {index + 1} 页相关内容引用了不存在的编号 {citation.ref}，已忽略"
                    )
                    continue
                valid_sources.append(citation)
            page.related_sources = valid_sources
            if page.visual is None:
                continue
            if page.visual.type not in PAGE_TYPES:
                page.visual = PageVisual(
                    type="text",
                    title=page.visual.title,
                    notice="当前演示类型不受支持，已保留本页正文。",
                )
                warnings.append(f"模块「{label}」第 {index + 1} 页图示类型非法，已降级为文字")
                continue
            ok, reason = _page_visual_ok(page.visual, ref_map, sent_image_refs)
            if not ok:
                page.visual = PageVisual(
                    type="text",
                    title=page.visual.title,
                    caption=page.visual.caption,
                    alt_text=page.visual.alt_text,
                    notice=f"当前演示暂不可用：{reason}。可阅读本页正文并继续翻页。",
                )
                warnings.append(f"模块「{label}」第 {index + 1} 页图示已降级：{reason}")
    return warnings


def validate_output(
    output: ModelOutput,
    expected_count: int,
    ref_map: RefMap,
    has_image: bool,
    prompt_max_words: int = 1800,
    scope_text: str = "",
    planned_points: set[str] | None = None,
    sent_image_refs: set[str] | None = None,
    require_pages: bool = False,
) -> list[str]:
    warnings: list[str] = []
    if output.insufficient_evidence is not None:
        raise ValidationError(
            "insufficient_evidence",
            json.dumps(output.insufficient_evidence.model_dump(), ensure_ascii=False),
        )
    warnings.extend(sanitize_module_pages(output, ref_map, sent_image_refs))
    handout = output.handout
    if not handout.scope.covered or not handout.scope.not_covered:
        raise ValidationError("handout_incomplete", "范围声明必须同时包含覆盖与不覆盖内容")
    if not 3 <= len(handout.key_points) <= 6:
        raise ValidationError("handout_incomplete", "知识要点需为 3–6 条")
    if not 2 <= len(handout.pitfalls) <= 4:
        raise ValidationError("handout_incomplete", "易错处需为 2–4 条")
    if has_image and not handout.visual:
        raise ValidationError("handout_incomplete", "含图资料必须提供图文解释")
    for index, block in enumerate(handout.key_points + handout.pitfalls + handout.visual, start=1):
        if not block.body.strip():
            raise ValidationError("handout_incomplete", "讲义段落正文为空")
        if not block.citations:
            raise ValidationError("missing_citation", f"讲义第 {index} 段没有资料依据")
        _require_refs(f"讲义第 {index} 段", block.citations, ref_map)

    for module in output.modules:
        blocks = module.key_points + module.pitfalls + module.visual
        label = module.title or module.module_id
        if require_pages and not module.pages:
            raise ValidationError(
                "module_pages_missing",
                f"模块「{label}」缺少有序学习页 pages，需按结构化契约输出",
            )
        if not blocks:
            raise ValidationError("module_handout_missing", f"模块「{label}」缺少讲义内容")
        if sum(word_count(block.body) for block in blocks) < 80:
            raise ValidationError("module_handout_missing", f"模块「{label}」讲义过短，需讲清概念、易混点与应用")
        for index, block in enumerate(blocks, start=1):
            if not block.body.strip():
                raise ValidationError("module_handout_missing", f"模块「{label}」第 {index} 段正文为空")
            if not block.citations:
                raise ValidationError("missing_citation", f"模块「{label}」第 {index} 段没有资料依据")
            _require_refs(f"模块「{label}」第 {index} 段", block.citations, ref_map)

    if len(output.questions) != expected_count:
        raise ValidationError(
            "question_count_mismatch",
            f"需要 {expected_count} 道题，实际收到 {len(output.questions)} 道",
        )
    seen: set[str] = set()
    for question in output.questions:
        if question.ordinal < 1:
            raise ValidationError("invalid_structure", "题号必须从 1 开始")
        if not question.stem.strip():
            raise ValidationError("invalid_structure", "题干不能为空")
        if not 0 <= question.answer_index <= 3:
            raise ValidationError("invalid_answer_index", f"第 {question.ordinal} 题答案索引越界")
        if not question.explanation.strip() or "见教材第" in question.explanation:
            raise ValidationError("invalid_explanation", f"第 {question.ordinal} 题解析不合格")
        digest = stem_hash(question.stem)
        if digest in seen:
            raise ValidationError("duplicate_stem", f"第 {question.ordinal} 题与同包题目重复")
        seen.add(digest)
        _require_refs(f"第 {question.ordinal} 题", question.citations, ref_map)
        if question.is_image_based:
            has_image_reference = any(
                ref_map.get(citation.ref) is not None
                and ref_map.get(citation.ref).kind in {"page_image", "image_region"}
                and ref_map.get(citation.ref).confidence == "high"
                and (sent_image_refs is None or citation.ref in sent_image_refs)
                for citation in question.citations
            )
            if not has_image_reference:
                raise ValidationError(
                    "missing_image_citation",
                    f"第 {question.ordinal} 题标记为读图题，但没有引用本次实际发送的高置信图像证据",
                )

    ordinals = [item.ordinal for item in output.questions]
    if ordinals != list(range(1, expected_count + 1)):
        raise ValidationError("invalid_structure", "题号必须从 1 连续递增")

    planned = {value for value in planned_points or set() if value}
    planned_blob = " ".join(sorted(planned))
    unlearned = [
        item.strip()
        for item in output.progress_reading.unlearned
        if item.strip()
        and not any(item.strip() in value or value in item.strip() for value in planned)
    ]
    known = " ".join(
        (target.quote_text or "") + (target.label_text or "")
        for target in ref_map.targets.values()
    )
    scope_known = known + " " + (scope_text or "") + " " + planned_blob
    for item in output.questions:
        for term in unlearned:
            if term and term in item.stem:
                raise ValidationError("out_of_scope", f"第 {item.ordinal} 题涉及未学内容：{term}")
        for word in _UNLEARNED_PROTOCOLS + _UNLEARNED_MECHANISM_TERMS:
            if _requires_unlearned_mechanism(item.stem, word, scope_known):
                raise ValidationError(
                    "out_of_scope", f"第 {item.ordinal} 题涉及未学的机制细节：{word}"
                )
            if word in item.explanation and any("路由" in term for term in unlearned):
                raise ValidationError(
                    "out_of_scope", f"第 {item.ordinal} 题涉及未学的机制细节：{word}"
                )

    total = word_count(
        "".join(block.body for block in handout.key_points + handout.pitfalls + handout.visual)
    )
    if not 800 <= total <= prompt_max_words:
        warnings.append(f"handout_word_count={total}，建议 800–{prompt_max_words}")

    if known.strip():
        for item in output.questions:
            for token in set(
                re.findall(r"PC-[A-Z]|SW\d+|R\d+|192\.168\.\d+\.\d+/\d+", item.stem)
            ):
                if token not in known:
                    warnings.append(f"第 {item.ordinal} 题的设备或网段未出现在证据标签中：{token}")
    return warnings