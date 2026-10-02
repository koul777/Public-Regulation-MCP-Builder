from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import unittest
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

from frontend import beginner_tour


REPO_ROOT = Path(__file__).resolve().parents[1]


class _HTMLCapture(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


class BeginnerTourTests(unittest.TestCase):
    def test_marker_round_trips_copy_without_injecting_markup_or_attributes(self) -> None:
        payload = {
            "title": '\"><img src=x onerror="alert(1)"> 한글 & 설명',
            "description": "</script><script>alert('synthetic')</script>\n줄바꿈",
            "selectors": ['[data-label="one & two"]', ".synthetic-'control"],
            "step": 3,
            "substep": 2,
        }
        attributes = beginner_tour.marker_attributes(**payload)
        parsed = _HTMLCapture()
        parsed.feed(f"<div {attributes}></div>")

        self.assertEqual(1, len(parsed.tags))
        tag, attrs = parsed.tags[0]
        self.assertEqual("div", tag)
        self.assertEqual({"data-rr-tour"}, set(attrs))
        self.assertEqual(payload, json.loads(attrs["data-rr-tour"]))

    def test_marker_context_selectors_are_optional_and_round_trip(self) -> None:
        base = {"title": "확인", "description": "표를 보세요", "selectors": ["div.a"],
                "step": 1, "substep": 4}
        plain = _HTMLCapture()
        plain.feed(f"<div {beginner_tour.marker_attributes(**base)}></div>")
        # Existing markers keep their exact payload: no empty "context" key.
        self.assertEqual(base, json.loads(plain.tags[0][1]["data-rr-tour"]))

        for empty in (None, []):
            self.assertNotIn("context", beginner_tour.marker_attributes(**base, context_selectors=empty))
        context = ['div[class~="st-key-detected"]', '[data-label="a & b"]']
        lit = _HTMLCapture()
        lit.feed(f"<div {beginner_tour.marker_attributes(**base, context_selectors=context)}></div>")
        self.assertEqual({**base, "context": context}, json.loads(lit.tags[0][1]["data-rr-tour"]))

    def test_controller_lights_context_regions_but_keeps_one_action_target(self) -> None:
        script = (beginner_tour.ASSETS / "beginner_tour.js").read_text(encoding="utf-8")

        # Context regions come from the marker, are skipped when hidden, and take part
        # in the dimming cut-outs, but never replace the click target.
        self.assertIn("marker.context", script)
        self.assertIn("const contextElements = marker =>", script)
        self.assertIn("elements.find(visible)", script)
        self.assertIn("const lit = r && usable(r) ? [r, ...extra] : [];", script)
        self.assertIn("target?.contains(event.target)", script)
        self.assertNotIn("companions.some", script)
        self.assertNotIn("companions.includes", script)

    def _journey(self, **kwargs: object) -> tuple[str, list[dict[str, str | None]]]:
        with patch.object(beginner_tour.st, "markdown") as markdown:
            beginner_tour.render_journey(**kwargs)
        markup = markdown.call_args.args[0]
        self.assertTrue(markdown.call_args.kwargs["unsafe_allow_html"])
        parsed = _HTMLCapture()
        parsed.feed(markup)
        return markup, [attrs for tag, attrs in parsed.tags if tag == "li"]

    def test_visiting_later_step_does_not_count_unfinished_work_as_complete(self) -> None:
        markup, cards = self._journey(
            active_step=4, completed=(True, False, False, False),
            uses_results=True, mcp=True,
        )
        self.assertIn("1 / 4 완료", markup)
        self.assertEqual(4, len(cards))
        self.assertEqual(1, sum("done" in card["class"].split() for card in cards))
        self.assertIn("waiting", cards[2]["class"].split())
        self.assertIn("current", cards[3]["class"].split())
        self.assertEqual("step", cards[3]["aria-current"])

    def test_optional_results_step_is_excluded_from_count_and_visible_numbering(self) -> None:
        for skipped_result_complete in (False, True):
            with self.subTest(skipped_result_complete=skipped_result_complete):
                markup, cards = self._journey(
                    active_step=3,
                    completed=(True, skipped_result_complete, False, False),
                    uses_results=False, mcp=False,
                )
                self.assertIn("1 / 3 완료", markup)
                self.assertNotIn("결과 살펴보기", markup)
                self.assertIn("Qwen 앱으로 이어가기", markup)
                self.assertIn("규정 선택·질문·근거 확인은 Qwen 창에서", markup)
                self.assertNotIn("AI에 연결하기", markup)
                self.assertEqual(3, len(cards))
                self.assertEqual("step", cards[1]["aria-current"])
                self.assertEqual(
                    ["✓", "2", "3"],
                    re.findall(r'class="rr-journey-number">([^<]+)', markup),
                )

    def test_completed_journey_uses_real_visible_stage_count(self) -> None:
        for uses_results, total in ((True, 4), (False, 3)):
            with self.subTest(uses_results=uses_results):
                markup, cards = self._journey(
                    active_step=4, completed=(True,) * 4,
                    uses_results=uses_results, mcp=True,
                )
                self.assertIn(f"{total} / {total} 완료", markup)
                self.assertTrue(all("done" in card["class"].split() for card in cards))

    def test_completed_qwen_journey_describes_handoff_without_claiming_answer_review(self) -> None:
        markup, cards = self._journey(
            active_step=4, completed=(True,) * 4,
            uses_results=True, mcp=False,
        )
        self.assertTrue(all("done" in card["class"].split() for card in cards))
        self.assertIn("빌더의 Qwen 인계 준비를 마쳤어요", markup)
        self.assertIn("기관·규정 선택, 질문, 답변의 근거 인용 확인은 Qwen 창에서 계속하세요", markup)
        self.assertNotIn("답변과 근거 함께 확인", markup)

    def test_tour_render_is_read_only_and_script_config_cannot_escape(self) -> None:
        page = '</script><img src=x onerror="alert(1)"> 한글'
        state = {
            beginner_tour.TOUR_REQUEST_KEY: 7,
            "approval:synthetic": False,
            "indexed:synthetic": False,
        }
        for native in (False, True):
            for enabled in (False, True):
                host_render = Mock()

                def native_html(body: str, *, unsafe_allow_javascript: bool = False) -> None:
                    host_render(body, unsafe_allow_javascript=unsafe_allow_javascript)

                def legacy_html(body: str) -> None:
                    self.fail("Legacy st.html strips scripts; use the iframe fallback")

                with self.subTest(native=native, enabled=enabled), patch.object(
                    beginner_tour, "st", SimpleNamespace(
                        session_state=MappingProxyType(state),
                        html=native_html if native else legacy_html,
                        iframe=host_render if native else None,
                    )
                ), patch.object(beginner_tour.components, "html") as iframe_render:
                    beginner_tour.render_tour(enabled=enabled, page=page)
                    render = host_render if native else iframe_render
                    markup = render.call_args.args[0]
                    parsed = _HTMLCapture()
                    parsed.feed(markup)
                    self.assertEqual(["span", "script"], [tag for tag, _ in parsed.tags])
                    match = re.search(r"const config = (.*?);\n", markup)
                    self.assertIsNotNone(match)
                    config = json.loads(match.group(1))
                    self.assertEqual(enabled, config["enabled"])
                    self.assertEqual(page, config["page"])
                    self.assertEqual(7, config["request"])
                    self.assertFalse(config["native"])
                    self.assertEqual(config["mount"], parsed.tags[0][1]["data-rr-tour-mount"])
                    if native:
                        self.assertEqual({"height": 1, "tab_index": -1}, render.call_args.kwargs)
                        iframe_render.assert_not_called()
                    else:
                        self.assertEqual(
                            {"height": 0, "scrolling": False, "tab_index": -1}, render.call_args.kwargs,
                        )
                        host_render.assert_not_called()
        self.assertEqual(
            {beginner_tour.TOUR_REQUEST_KEY: 7, "approval:synthetic": False,
             "indexed:synthetic": False},
            state,
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed to execute the disabled controller")
    def test_disabled_controller_only_disposes_previous_overlay(self) -> None:
        # A disabled controller must clean up the previous overlay before exiting,
        # without touching the DOM, preferences, network, or workflow controls.
        script = (beginner_tour.ASSETS / "beginner_tour.js").read_text(encoding="utf-8")
        harness = r"""
const vm = require('node:vm');
const fs = require('node:fs');
let disposed = 0;
const deny = new Proxy({}, {get(_target, key) {throw new Error('Unexpected access: ' + String(key));}});
const parent = new Proxy({
    document: deny,
    __regulationBeginnerTour: {dispose() {disposed += 1;}}
}, {get(target, key) {if (!(key in target)) throw new Error('Unexpected parent access: ' + String(key)); return target[key];}});
vm.runInNewContext(fs.readFileSync(0, 'utf8'), {
    window: {parent}, config: {enabled: false},
    fetch: deny, XMLHttpRequest: deny, WebSocket: deny
}, {timeout: 1000});
if (disposed !== 1) throw new Error('Previous controller must be disposed exactly once');
process.stdout.write('disabled-controller-clean');
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness], input=script,
            text=True, encoding="utf-8", capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("disabled-controller-clean", result.stdout)

    def test_assets_are_available_as_package_resources_and_declared_for_wheels(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = project["tool"]["setuptools"]["package-data"]["frontend"]
        for name in ("beginner_tour.js", "beginner_tour.css"):
            with self.subTest(name=name):
                self.assertIn(f"assets/{name}", declared)
                resource = files("frontend").joinpath("assets", name)
                self.assertTrue(resource.is_file())
                self.assertTrue(resource.read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
