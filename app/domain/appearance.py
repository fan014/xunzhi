from __future__ import annotations

THEMES = {"system", "light", "dark"}
FONT_SIZES = {"small": 0.94, "standard": 1.0, "large": 1.14}
FONT_FAMILIES = {
    "system": '"Inter", "PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif',
    "reading": '"Noto Serif SC", "Source Han Serif SC", "Songti SC", "SimSun", serif',
}
DEFAULT_APPEARANCE = {
    "theme": "system",
    "font_size": "standard",
    "font_family": "system",
}

LIGHT = {
    "--bg": "#f7f8fa",
    "--surface": "#ffffff",
    "--surface-soft": "#fbfcfd",
    "--ink": "#1d2430",
    "--ink-soft": "#3c4656",
    "--muted": "#687182",
    "--line": "#e2e6ec",
    "--line-strong": "#d4dae3",
    "--accent": "#315c93",
    "--accent-strong": "#274b78",
    "--accent-weak": "#edf2f9",
    "--accent-soft": "#dce6f2",
    "--ok": "#2c6b4f",
    "--ok-bg": "#eaf3ee",
    "--danger": "#a33a2b",
    "--danger-bg": "#fbefec",
    "--warn": "#8a5a13",
    "--warn-bg": "#fbf3e4",
    "--shadow": "0 12px 36px rgba(23, 32, 48, .14)",
}

DARK = {
    "--bg": "#11161d",
    "--surface": "#161d26",
    "--surface-soft": "#121820",
    "--ink": "#e7ecf3",
    "--ink-soft": "#c1ccd9",
    "--muted": "#95a1b2",
    "--line": "#2a3543",
    "--line-strong": "#3b4858",
    "--accent": "#86aede",
    "--accent-strong": "#a5c5ec",
    "--accent-weak": "#1a293b",
    "--accent-soft": "#22364e",
    "--ok": "#85c7a6",
    "--ok-bg": "#16271f",
    "--danger": "#e79a8f",
    "--danger-bg": "#301d1a",
    "--warn": "#d8b277",
    "--warn-bg": "#2b2417",
    "--shadow": "0 18px 48px rgba(0, 0, 0, .42)",
}

DARK_OVERRIDES = """
body { color: var(--ink); background: var(--bg); }
.brand-mark { color: var(--bg); }
.primary, .primary:hover, .link-button.primary { color: var(--bg); }
.primary:disabled { background: #3b4858; border-color: #3b4858; color: var(--muted); }
.mobile-bar { background: rgba(17, 22, 29, .94); }
input, textarea, select { background: var(--surface); color: var(--ink); }
input:hover, textarea:hover, select:hover,
.chip:hover, .check-row:hover, .icon:hover { background: var(--surface-soft); border-color: var(--line-strong); }
.secondary, .link-button { background: var(--surface); border-color: var(--line-strong); color: var(--ink); }
.secondary:hover, .link-button:hover { background: var(--surface-soft); color: var(--ink); }
.badge.ok, .option.correct { border-color: #315844; }
.badge.warn { border-color: #5c4a27; }
.badge.danger, .option.wrong { border-color: #5f312b; }
.progress-track { background: #26313e; }
.calendar-cell.is-selected { background: var(--accent-weak); }
.calendar-cell.has-tasks { background: rgba(134, 174, 222, .07); }
.calendar-cell.has-overdue { box-shadow: inset 0 0 0 1px #5c4a27; }
.schedule-task.state-completed { border-color: #315844; background: #14221b; }
.schedule-task.state-overdue { border-color: #5c4a27; background: #221d14; }
#shift-panel { border-color: #2b4058; background: #151f2b; }
.shift-change { background: var(--surface); }
.visual-box { fill: var(--surface); stroke: var(--line-strong); }
.visual-node.switch .visual-box { fill: var(--accent-weak); }
.visual-label { fill: var(--ink); }
.visual-edge { stroke: var(--line-strong); }
.visual-edge-label { fill: var(--muted); }
.visual-node.active .visual-box { fill: var(--accent-soft); }
.visual-feedback.bad, .visual-notice { color: var(--danger); }
.reader-source-card, .recap-card, .page-sources, .profile-card,
.module-card, .result-panel, .assessment-panel, #mastery-changes {
  background: var(--surface);
}
.recap-cross { background: var(--surface-soft); }
.dialog-body { background: var(--surface); }
.dialog::backdrop { background: rgba(0, 0, 0, .55); }
.practice-scope-banner { border-color: var(--line-strong); background: var(--surface-soft); }
.practice-scope-banner.is-complete { border-color: #315844; background: #14221b; }
.practice-scope-banner.is-unavailable { border-color: #5c4a27; background: #221d14; }
#test-banner { background: var(--warn-bg); border-color: #5c4a27; color: var(--ink); }
"""

FONT_CSS = """
body {
  font-family: var(--font-body);
  font-size: calc(15.5px * var(--font-scale));
}
h1 { font-size: calc(30px * var(--font-scale)); }
h2 { font-size: calc(18px * var(--font-scale)); }
h3 { font-size: calc(15.5px * var(--font-scale)); }
.body, .explanation, .stem, .reader-text .body, .profile-finding p { font-size: calc(15.5px * var(--font-scale)); }
.meta, .hint, .calendar-feedback, .brief-stat, .brief-advice-text,
.assessment-source-name, .settings-card .field-label, .model-state p,
.schedule-task-body .hint { font-size: calc(13.5px * var(--font-scale)); }
.schedule-task-title { font-size: calc(14px * var(--font-scale)); }
"""


def normalize(values: dict | None) -> dict:
    raw = values or {}
    theme = str(raw.get("theme") or "").strip()
    size = str(raw.get("font_size") or "").strip()
    family = str(raw.get("font_family") or "").strip()
    if theme not in THEMES:
        theme = DEFAULT_APPEARANCE["theme"]
    if size not in FONT_SIZES:
        size = DEFAULT_APPEARANCE["font_size"]
    if family not in FONT_FAMILIES:
        family = DEFAULT_APPEARANCE["font_family"]
    return {"theme": theme, "font_size": size, "font_family": family}


def _vars(values: dict, *, scheme: str) -> str:
    lines = []
    for key, value in values.items():
        lines.append(f"  {key}: {value};")
    lines.append(f"  color-scheme: {scheme};")
    return ":root {\n" + "\n".join(lines) + "\n}"


def _font(values: dict) -> str:
    scale = FONT_SIZES[values["font_size"]]
    family = FONT_FAMILIES[values["font_family"]]
    return (
        f":root {{ --font-scale: {scale}; --font-body: {family}; }}\n"
        + FONT_CSS
    )


def css(values: dict | None) -> str:
    chosen = normalize(values)
    theme = chosen["theme"]
    if theme == "light":
        return "\n".join([_vars(LIGHT, scheme="light"), _font(chosen)])
    if theme == "dark":
        return "\n".join([_vars(DARK, scheme="dark"), _font(chosen), DARK_OVERRIDES])
    return "\n".join(
        [
            _vars(LIGHT, scheme="light dark"),
            _font(chosen),
            "@media (prefers-color-scheme: dark) {",
            _vars(DARK, scheme="dark"),
            DARK_OVERRIDES,
            "}",
        ]
    )