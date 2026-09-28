"""Presentation-only guided tour. Workflow decisions stay in the Streamlit app."""

from __future__ import annotations

import html
import json
from pathlib import Path
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components


ASSETS = Path(__file__).with_name("assets")
TOUR_REQUEST_KEY = "beginner_tour_request"


def marker_attributes(
    title: str,
    description: str,
    *,
    selectors: list[str],
    step: int,
    substep: int,
) -> str:
    """Serialize trusted widget selectors and escaped copy into a DOM marker."""
    payload = json.dumps(
        {"title": title, "description": description, "selectors": selectors,
         "step": step, "substep": substep},
        ensure_ascii=False,
    )
    return f'data-rr-tour="{html.escape(payload, quote=True)}"'


def render_journey(
    *, active_step: int, completed: tuple[bool, ...], uses_results: bool, mcp: bool,
) -> None:
    """Show actual completion, never count navigation as completed work."""
    stages = [(1, "파일 올리기", "규정 파일을 읽고 정리"),
              (2, "결과 살펴보기", "AI 검수 결과 확인"),
              (3, "확인하고 승인", "원문 비교 후 검색에 등록"),
              (4, "AI에 연결하기" if mcp else "Qwen 앱으로 이어가기",
               "사용할 AI 앱에 연결" if mcp else "규정 선택·질문·근거 확인은 Qwen 창에서")]
    if not uses_results:
        stages = [stage for stage in stages if stage[0] != 2]
    cards = []
    for display_number, (step, title, description) in enumerate(stages, start=1):
        done = step <= len(completed) and completed[step - 1]
        state = "done" if done else "current" if step == active_step else "waiting"
        badge = "완료" if done else "지금" if state == "current" else "다음"
        current = ' aria-current="step"' if step == active_step else ""
        cards.append(
            f'<li class="rr-journey-card {state}"{current}>'
            f'<span class="rr-journey-number">{"✓" if done else display_number}</span>'
            f'<strong>{title}</strong><span class="rr-journey-badge">{badge}</span>'
            f'<p>{description}</p></li>'
        )
    done_count = sum(step <= len(completed) and completed[step - 1] for step, _, _ in stages)
    completion_title = "모든 준비를 마쳤어요" if mcp else "빌더의 Qwen 인계 준비를 마쳤어요"
    completion_description = (
        "승인한 규정을 AI에서 활용할 수 있어요. 새 문서도 같은 순서로 진행하세요."
        if mcp else "기관·규정 선택, 질문, 답변의 근거 인용 확인은 Qwen 창에서 계속하세요."
    )
    ribbon = (
        '<div class="rr-journey-complete" role="status">'
        f'<span aria-hidden="true">✓</span><div><strong>{completion_title}</strong>'
        f'<p>{completion_description}</p>'
        '</div></div>'
        if done_count == len(stages) else ""
    )
    st.markdown(
        f'<div class="rr-journey" data-rr-journey data-rr-complete="{str(done_count == len(stages)).lower()}">'
        '<div class="rr-journey-heading"><span>나의 규정을 AI와 연결하는 순서</span>'
        f'<span>{done_count} / {len(stages)} 완료</span></div>'
        '<ol class="rr-journey-track">' + "".join(cards) + '</ol>' + ribbon + '</div>',
        unsafe_allow_html=True,
    )


def render_tour(*, enabled: bool, page: str) -> None:
    """Attach one disposable tour controller after all page widgets exist.

    No external scripts, telemetry, automatic clicks, or approval writes. The
    browser remembers only tour preferences, scoped to this tab and app path.
    """
    request = int(st.session_state.get(TOUR_REQUEST_KEY, 0))
    # Use the component execution path consistently. Some Streamlit HTML
    # sanitization paths strip scripts despite accepting the unsafe JS flag.
    # The minimal-height frame executes only our bundled, trusted controller.
    mount = uuid4().hex
    config = json.dumps({
        "enabled": enabled, "page": page, "request": request,
        "native": False, "mount": mount,
    }).replace("<", "\\u003c")
    script = (ASSETS / "beginner_tour.js").read_text(encoding="utf-8")
    style = (ASSETS / "beginner_tour.css").read_text(encoding="utf-8")
    # A unique mount makes an otherwise identical rerun execute its bootstrap.
    # All variables stay in the IIFE; text travels as escaped JSON, never code.
    body = (
        f'<span data-rr-tour-mount="{mount}" hidden></span>'
        '<script>(() => { const config = ' + config + ';\nconst tourCSS = '
        + json.dumps(style).replace("<", "\\u003c") + ';\n' + script + '\n})();</script>'
    )
    iframe = getattr(st, "iframe", None)
    if callable(iframe):
        iframe(body, height=1, tab_index=-1)
    else:
        components.html(body, height=0, scrolling=False, tab_index=-1)


def render_beginner_style() -> None:
    st.markdown(
        "<style>" + (ASSETS / "beginner_tour.css").read_text(encoding="utf-8") + "</style>",
        unsafe_allow_html=True,
    )
