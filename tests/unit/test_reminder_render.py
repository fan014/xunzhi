from __future__ import annotations

from app.domain import reminder


def test_render_email_contains_tasks_minutes_and_entry_without_private_results():
    digest = {
        "date": "2026-11-24",
        "task_count": 2,
        "minutes": 25,
        "summary": "今天大概需要 25 分钟。先读一节「边际收益」，再完成其余任务。",
        "groups": [
            {
                "plan_id": "plan_1",
                "title": "经济学学习",
                "task_count": 2,
                "minutes": 25,
                "tasks": [
                    {"title": "边际收益", "task_type": "reading", "type_label": "阅读", "minutes": 15},
                    {"title": "练习：4 道", "task_type": "practice", "type_label": "练习", "minutes": 10},
                ],
            }
        ],
    }
    message = reminder.render_email(digest)
    assert "25 分钟" in message["subject"] or "2 项" in message["subject"]
    assert "25 分钟" in message["text"]
    assert "阅读：边际收益" in message["text"]
    assert "练习：练习：4 道" in message["text"]
    assert "打开循知 → 学习日历" in message["text"]
    assert "正确率" not in message["text"]
    assert "错题" not in message["text"]
    assert "<li>" in message["html"]


def test_summary_is_deterministic_and_no_tasks_is_empty():
    assert reminder._summary([], 0) == "今天没有待完成的学习任务。"
    summary = reminder._summary(
        [
            {
                "task_count": 1,
                "tasks": [
                    {"title": "机会成本", "task_type": "reading", "type_label": "阅读", "minutes": 12}
                ],
            }
        ],
        12,
    )
    assert summary.startswith("今天大概需要 12 分钟。")
    assert "机会成本" in summary
