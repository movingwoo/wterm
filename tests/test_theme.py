"""라이트 테마의 두 정의와 작은 글자 대비를 고정한다.

색은 화면에서 확인해야 하지만, 2:1에 가까운 ANSI white 같은 회귀는
숫자로도 막을 수 있다. 외부 의존성 없이 WCAG 대비비를 계산한다.
"""

import re
from pathlib import Path


CSS = (Path(__file__).parents[1] / "static" / "style.css").read_text()
MIN_TEXT_CONTRAST = 4.5


def _tokens(selector: str) -> dict[str, str]:
    match = re.search(rf"{selector}\s*\{{(?P<body>.*?)\n\s*\}}", CSS, re.DOTALL)
    assert match, selector
    return {
        name: value.strip()
        for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", match["body"])
    }


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_light_theme_definitions_stay_identical():
    """시스템 라이트와 사용자가 고른 라이트는 같은 팔레트여야 한다."""
    system_light = _tokens(r':root:not\(\[data-theme="dark"\]\)')
    explicit_light = _tokens(r':root\[data-theme="light"\]')
    assert system_light == explicit_light


def test_light_theme_small_text_has_readable_contrast():
    """각 전경을 실제로 쓰는 배경과 페어링한다."""
    colors = _tokens(r':root\[data-theme="light"\]')
    pairs = {
        "text on surface": ("text", "surface"),
        "dim text on surface": ("text-dim", "surface"),
        "faint text on background": ("text-faint", "bg"),
        "accent heading": ("accent", "bg-sunken"),
        "accent button": ("accent", "bg-deep"),
        "Claude label": ("accent-alt", "bg"),
        "live badge": ("ok", "bg"),
        "connected status": ("ok", "ok-bg"),
        "Codex badge": ("warn", "bg"),
        "connecting status": ("warn", "warn-bg"),
        "danger text": ("danger", "bg"),
        "disconnected status": ("danger", "danger-bg"),
        "shell badge": ("info", "bg"),
    }
    failures = {
        name: ratio
        for name, (foreground, background) in pairs.items()
        if (ratio := _contrast(colors[foreground], colors[background]))
        < MIN_TEXT_CONTRAST
    }
    assert not failures


def test_light_terminal_ansi_foregrounds_have_readable_contrast():
    colors = _tokens(r':root\[data-theme="light"\]')
    ansi = [
        "black",
        "red",
        "green",
        "yellow",
        "blue",
        "magenta",
        "cyan",
        "white",
        "bright-black",
        "bright-red",
        "bright-green",
        "bright-yellow",
        "bright-blue",
        "bright-magenta",
        "bright-cyan",
        "bright-white",
    ]
    failures = {
        name: ratio
        for name in ansi
        if (ratio := _contrast(colors[f"term-{name}"], colors["term-bg"]))
        < MIN_TEXT_CONTRAST
    }
    assert not failures
