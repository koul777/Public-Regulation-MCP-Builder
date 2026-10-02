"""Careless first-time-user journey through the real Streamlit operator UI.

A first-time operator clicks whatever is on screen and types anything into any
empty field.  This module replays such click sequences against the real
``frontend/streamlit_app.py`` with ``streamlit.testing.v1.AppTest`` and asserts
safety and usability invariants on every screen that is reached:

* I1  no uncaught exception element
* I2  no leaked Python internals in error/warning/info/success text
* I3  no dead end (something enabled can always be clicked or typed into)
* I4  no two enabled buttons share a label (except a ratcheted allowlist)
* I5  no approval/index action is offered before any document exists

Everything is deterministic and synthetic.  Each replayed sequence gets its own
temporary directory, settings object and patch set, so state never leaks from
one sequence to the next.  Networking is denied, and so is every way the UI
could start a child process, open a browser or open a file with the shell:
the explorer never clicks buttons that are known to do that, and the guards
below turn any attempt into a recorded failure instead of a real side effect.

This file is the safety net for later UI simplification.  If a label is renamed
on purpose, update the constants in the "Scope" section together with the UI
change instead of weakening an invariant.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import webbrowser
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

try:
    from streamlit.testing.v1 import AppTest
except Exception:  # pragma: no cover - optional in minimal environments
    AppTest = None

from app.core import config as config_module
from app.core.config import Settings
from frontend.authoring_page import AUTHORING_NAV_LABEL


REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = REPO_ROOT / "frontend" / "streamlit_app.py"

Action = tuple[str, int]  # (button label, ordinal among enabled buttons with that label)


# ---------------------------------------------------------------------------
# Scope: what is explored and what is tolerated
# ---------------------------------------------------------------------------

# What a careless user types into every empty text field before each click.
CARELESS_NAME = "테스트 기관"

# Breadth-first exploration depth from the first screen (clicks per sequence).
BFS_DEPTH = 2

# Sequences that are expanded one click deeper (depth 3).
DEEP_BRANCHES: tuple[tuple[Action, ...], ...] = (
    (("초보자 안내 시작", 0), ("기관 생성", 0)),
    (("기관 생성", 0), ("일반 모드로 계속", 0)),
)

# Hard ceiling on replayed sequences.  The explorer is deterministic, so this is
# not a time budget: if the UI grows enough to hit it, the sanity test fails and
# the ceiling has to be raised on purpose.
MAX_REPLAYS = 70

# Exploration must stay meaningful: the manual exploration this was modelled on
# found 14 distinct states.
MIN_UNIQUE_STATES = 6

# I2: text that must never reach an operator-visible alert.
FORBIDDEN_INTERNAL_TEXT = (
    "Traceback",
    "KeyError",
    "TypeError",
    "ValueError",
    "AttributeError",
    "NameError",
)

# I4 ratchet: labels that are currently shared by more than one enabled button
# on a single screen.  Every entry is a known UI defect to be fixed by a later
# UI task; once a label is no longer duplicated anywhere in the explored states
# ``test_known_duplicate_allowlist_has_no_stale_entries`` forces its removal.
KNOWN_DUPLICATE_BUTTON_LABELS = frozenset(
    {
        # The Home page shows one "이동" button per workflow step card, so the
        # buttons cannot be told apart by their visible label.
        "이동",
        # With regulation authoring enabled (the local default) the sidebar
        # ("sidebar-open-authoring") and the Home page ("home-open-authoring")
        # both render this same label in non-beginner mode.
        AUTHORING_NAV_LABEL,
    }
)

# I5: an approval or indexing action must not be offered without a document.
APPROVAL_OR_INDEX_ACTION = re.compile(r"승인|색인")

# Buttons that start real processes, open browsers/Explorer/native dialogs or
# install software.  A careless user may click them, but a test must not.  The
# launch guards below record any attempt that still slips through.
UNSAFE_LABEL_FRAGMENTS = (
    "챗봇 실행",    # starts a local Qwen server process and opens a browser
    "탐색기",      # Windows Explorer or the native folder picker
    "열기",        # shell-opens files or folders
    "설치",        # Kordoc installer (PowerShell/npm) and indexing pip install
    "모델 준비",   # indexing runtime preparation (pip + model probe)
)

# Streamlit widget kinds that accept input.
_INPUT_KINDS = (
    "text_input",
    "text_area",
    "selectbox",
    "radio",
    "checkbox",
    "number_input",
    "toggle",
    "multiselect",
    "slider",
    "select_slider",
    "date_input",
    "time_input",
    "color_picker",
)


def _is_unsafe_to_click(label: str) -> bool:
    return any(fragment in label for fragment in UNSAFE_LABEL_FRAGMENTS)


def _describe_sequence(sequence: tuple[Action, ...]) -> str:
    if not sequence:
        return "(첫 화면)"
    return " > ".join(
        label if ordinal == 0 else f"{label}#{ordinal + 1}"
        for label, ordinal in sequence
    )


# ---------------------------------------------------------------------------
# Isolation: fresh settings, denied network, denied process/browser launches
# ---------------------------------------------------------------------------


@dataclass
class _LaunchLog:
    """Everything the UI tried to do outside the sandbox."""

    network: list[str] = field(default_factory=list)
    processes: list[str] = field(default_factory=list)
    shell_opens: list[str] = field(default_factory=list)

    def problems(self) -> dict[str, list[str]]:
        found = {
            "network": self.network,
            "child process": self.processes,
            "browser/shell open": self.shell_opens,
        }
        return {name: list(items) for name, items in found.items() if items}


@contextlib.contextmanager
def _denied_network(log: _LaunchLog) -> Iterator[None]:
    # Windows asyncio implements its internal wake-up socketpair using a
    # loopback connect.  Permit only connections made inside socketpair; every
    # other connection is recorded and refused.
    socketpair_scope = threading.local()
    original_connect = socket.socket.connect
    original_socketpair = socket.socketpair

    def socketpair(*args, **kwargs):
        socketpair_scope.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            socketpair_scope.active = False

    def connect(sock, address):
        if getattr(socketpair_scope, "active", False):
            return original_connect(sock, address)
        log.network.append(str(address)[:80])
        raise AssertionError("The careless-user journey must not use networking")

    with patch.object(socket, "socketpair", socketpair), patch.object(
        socket.socket, "connect", connect
    ):
        yield


@contextlib.contextmanager
def _denied_launches(log: _LaunchLog) -> Iterator[None]:
    def refuse_popen(self, *args, **kwargs):
        self._child_created = False  # keep Popen.__del__ quiet after the refusal
        command = args[0] if args else kwargs.get("args", "")
        try:
            first = (
                command
                if isinstance(command, (str, bytes, os.PathLike))
                else list(command)[0]
            )
            executable = Path(os.fsdecode(first)).name
        except Exception:
            executable = ""
        log.processes.append(executable or "<unknown>")
        raise OSError("child process launches are blocked in the careless-user journey")

    def refuse_browser(*args, **kwargs):
        log.shell_opens.append("webbrowser.open")
        return False

    def refuse_startfile(*args, **kwargs):
        log.shell_opens.append("os.startfile")
        raise OSError("opening files is blocked in the careless-user journey")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(subprocess.Popen, "__init__", refuse_popen))
        stack.enter_context(patch.object(webbrowser, "open", refuse_browser))
        stack.enter_context(patch.object(os, "startfile", refuse_startfile, create=True))
        yield


@dataclass
class _Sandbox:
    app: "AppTest"
    settings: Settings
    root: Path

    def run(self) -> None:
        self.app.run()

    def fill_empty_text_inputs(self) -> None:
        """Type a generic name into every empty, enabled text field."""

        for text_input in self.app.text_input:
            if getattr(text_input, "disabled", False):
                continue
            if not str(text_input.value or "").strip():
                text_input.input(CARELESS_NAME)

    def click(self, action: Action) -> None:
        button = _find_button(self.app, action)
        if button is None:
            raise AssertionError(
                f"버튼 {action!r}을(를) 찾지 못했습니다. 같은 순서를 다시 실행했는데 "
                "화면이 달라졌습니다(비결정적 화면)."
            )
        button.click()
        self.app.run()

    def session_value(self, key: str, default: object = None) -> object:
        try:
            if key in self.app.session_state:
                return self.app.session_state[key]
        except Exception:
            pass
        return default

    @property
    def institution_created(self) -> bool:
        return Path(self.settings.institution_profiles_path).is_file()


@contextlib.contextmanager
def _isolated_app(log: _LaunchLog) -> Iterator[_Sandbox]:
    """Build a brand-new app, settings, data directory and patch set."""

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw_root:
        root = Path(raw_root)
        settings = Settings(
            app_env="test",
            data_dir=root / "data",
            artifact_root=root,
            institution_profiles_path=str(root / "profiles.json"),
            quality_profiles_path="",
            api_auth_required=False,
            tenant_storage_isolation=False,
            api_default_tenant_id="default",
            # Explicit so the I4 ratchet does not depend on the developer's
            # ENABLE_REGULATION_AUTHORING environment variable.
            enable_regulation_authoring=True,
            enable_agent_review=False,
            local_structure_review_enabled=False,
            enable_kordoc_table_parser=False,
            kordoc_table_command="",
            pdf_ocr_backend="",
            rag_llm_backend="extractive",
            openai_api_key="",
            openai_compatible_api_key="",
            azure_openai_api_key="",
            anthropic_api_key="",
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(config_module, "_runtime_overrides", {}))
            stack.enter_context(
                patch.object(config_module, "_base_settings", return_value=settings)
            )
            stack.enter_context(_denied_network(log))
            stack.enter_context(_denied_launches(log))
            app = AppTest.from_file(str(APP_PATH), default_timeout=45)
            app.session_state["ai_connection_overrides"] = vars(settings).copy()
            yield _Sandbox(app=app, settings=settings, root=root)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def _find_button(app: "AppTest", action: Action):
    label, ordinal = action
    seen = 0
    for button in app.button:
        if str(button.label) != label or getattr(button, "disabled", False):
            continue
        if seen == ordinal:
            return button
        seen += 1
    return None


def _element_texts(elements) -> tuple[str, ...]:
    texts = []
    for element in elements:
        value = getattr(element, "value", None)
        if value is None:
            value = getattr(element, "message", "")
        texts.append(" ".join(str(value or "").split()))
    return tuple(texts)


@dataclass(frozen=True)
class _Snapshot:
    """What a careless user can see and do on one reached screen."""

    sequence: tuple[Action, ...]
    exceptions: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    infos: tuple[str, ...]
    successes: tuple[str, ...]
    enabled_buttons: tuple[str, ...]
    disabled_buttons: tuple[str, ...]
    actions: tuple[Action, ...]
    enabled_inputs: tuple[tuple[str, int], ...]
    institution_created: bool
    has_document: bool

    @property
    def key(self) -> tuple[object, ...]:
        """State identity used to dedupe screens during exploration."""

        return (tuple(sorted(self.enabled_buttons)), self.errors, self.exceptions)

    @property
    def where(self) -> str:
        return _describe_sequence(self.sequence)

    @property
    def enabled_input_count(self) -> int:
        return sum(count for _kind, count in self.enabled_inputs)

    @property
    def alert_texts(self) -> tuple[str, ...]:
        return self.errors + self.warnings + self.infos + self.successes

    @property
    def safe_actions(self) -> tuple[Action, ...]:
        return tuple(a for a in self.actions if not _is_unsafe_to_click(a[0]))

    @property
    def duplicate_labels(self) -> dict[str, int]:
        return {
            label: count
            for label, count in Counter(self.enabled_buttons).items()
            if count > 1
        }


def _take_snapshot(sequence: tuple[Action, ...], sandbox: _Sandbox) -> _Snapshot:
    app = sandbox.app
    enabled: list[str] = []
    disabled: list[str] = []
    actions: list[Action] = []
    ordinals: Counter[str] = Counter()
    for button in app.button:
        label = str(button.label)
        if getattr(button, "disabled", False):
            disabled.append(label)
            continue
        enabled.append(label)
        actions.append((label, ordinals[label]))
        ordinals[label] += 1

    input_counts: list[tuple[str, int]] = []
    for kind in _INPUT_KINDS:
        widgets = getattr(app, kind, None)
        if widgets is None:
            continue
        count = sum(1 for widget in widgets if not getattr(widget, "disabled", False))
        if count:
            input_counts.append((kind, count))

    return _Snapshot(
        sequence=sequence,
        exceptions=_element_texts(app.exception),
        errors=_element_texts(app.error),
        warnings=_element_texts(app.warning),
        infos=_element_texts(app.info),
        successes=_element_texts(app.success),
        enabled_buttons=tuple(enabled),
        disabled_buttons=tuple(disabled),
        actions=tuple(actions),
        enabled_inputs=tuple(input_counts),
        institution_created=sandbox.institution_created,
        has_document=bool(sandbox.session_value("document_id")),
    )


# ---------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------


def _replay(sequence: tuple[Action, ...], log: _LaunchLog) -> _Snapshot:
    """Run one click sequence from a fresh, isolated app."""

    with _isolated_app(log) as sandbox:
        sandbox.run()
        for action in sequence:
            sandbox.fill_empty_text_inputs()
            sandbox.click(action)
        return _take_snapshot(sequence, sandbox)


@dataclass
class _Exploration:
    snapshots: list[_Snapshot]
    replays: int
    truncated: bool
    missing_branches: list[tuple[Action, ...]]
    seconds: float
    log: _LaunchLog

    @property
    def unique_states(self) -> dict[tuple[object, ...], _Snapshot]:
        states: dict[tuple[object, ...], _Snapshot] = {}
        for snapshot in self.snapshots:
            states.setdefault(snapshot.key, snapshot)
        return states

    @property
    def skipped_unsafe_labels(self) -> set[str]:
        return {
            label
            for snapshot in self.snapshots
            for label, _ordinal in snapshot.actions
            if _is_unsafe_to_click(label)
        }


def _explore() -> _Exploration:
    started = time.monotonic()
    log = _LaunchLog()
    snapshots: list[_Snapshot] = []
    by_sequence: dict[tuple[Action, ...], _Snapshot] = {}
    state = {"replays": 0, "truncated": False}

    def visit(sequence: tuple[Action, ...]) -> _Snapshot | None:
        if state["replays"] >= MAX_REPLAYS:
            state["truncated"] = True
            return None
        state["replays"] += 1
        snapshot = _replay(sequence, log)
        snapshots.append(snapshot)
        by_sequence[sequence] = snapshot
        return snapshot

    root = visit(())
    assert root is not None
    seen = {root.key}
    frontier = [root]
    for _depth in range(BFS_DEPTH):
        next_frontier: list[_Snapshot] = []
        for parent in frontier:
            for action in parent.safe_actions:
                child = visit(parent.sequence + (action,))
                if child is None:
                    break
                if child.key not in seen:
                    seen.add(child.key)
                    next_frontier.append(child)
        frontier = next_frontier

    missing: list[tuple[Action, ...]] = []
    for prefix in DEEP_BRANCHES:
        parent = by_sequence.get(prefix)
        if parent is None:
            missing.append(prefix)
            continue
        for action in parent.safe_actions:
            if visit(prefix + (action,)) is None:
                break

    return _Exploration(
        snapshots=snapshots,
        replays=state["replays"],
        truncated=bool(state["truncated"]),
        missing_branches=missing,
        seconds=time.monotonic() - started,
        log=log,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class UnsafeClickPolicyTests(unittest.TestCase):
    """The deny-list that keeps the explorer from starting real side effects."""

    def test_process_launching_buttons_are_never_clicked(self) -> None:
        for label in (
            "💬 독립 Qwen 챗봇 실행",
            "Kordoc 설치·검증 시작",
            "Kordoc 설치·검증 다시 시도",
            "검색 기능 설치·준비",
            "검색 모델 준비·점검",
            "Windows 탐색기에서 저장 폴더 선택",
            "Windows 탐색기에서 현재 폴더 열기",
            "원본 문서 열기 (Open source file)",
        ):
            with self.subTest(label=label):
                self.assertTrue(_is_unsafe_to_click(label))

    def test_ordinary_navigation_buttons_stay_in_scope(self) -> None:
        for label in (
            "초보자 안내 시작",
            "일반 모드로 계속",
            "기관 생성",
            "이동",
            "⚙️ 관리자 설정",
            "합성 샘플로 바로 시작",
            "Kordoc 준비 상태 다시 확인",
        ):
            with self.subTest(label=label):
                self.assertFalse(_is_unsafe_to_click(label))

    def test_sequence_description_numbers_repeated_labels(self) -> None:
        self.assertEqual("(첫 화면)", _describe_sequence(()))
        self.assertEqual(
            "기관 생성 > 이동#2",
            _describe_sequence((("기관 생성", 0), ("이동", 1))),
        )


@unittest.skipIf(AppTest is None, "streamlit.testing.v1.AppTest is not available")
class CarelessUserJourneyTests(unittest.TestCase):
    """Invariants on every screen a click-happy first-time user can reach."""

    exploration: _Exploration

    @classmethod
    def setUpClass(cls) -> None:
        cls.exploration = _explore()
        print(
            "[careless-user journey] "
            f"replays={cls.exploration.replays} "
            f"screens={len(cls.exploration.snapshots)} "
            f"unique_states={len(cls.exploration.unique_states)} "
            f"seconds={cls.exploration.seconds:.1f}",
            file=sys.stderr,
        )

    def test_exploration_covers_the_documented_scope(self) -> None:
        exploration = self.exploration
        self.assertFalse(
            exploration.truncated,
            f"탐색이 MAX_REPLAYS={MAX_REPLAYS}에 도달해 잘렸습니다. 화면이 늘었다면 "
            "상한을 의도적으로 올리세요.",
        )
        self.assertEqual(
            [],
            exploration.missing_branches,
            "깊이 3 확장 대상 화면에 도달하지 못했습니다. 버튼 이름을 바꿨다면 "
            "DEEP_BRANCHES를 함께 고치세요.",
        )
        root = exploration.snapshots[0]
        self.assertEqual((), root.sequence)
        self.assertIn("기관 생성", root.enabled_buttons)
        self.assertGreaterEqual(len(exploration.unique_states), MIN_UNIQUE_STATES)
        self.assertTrue(
            any(snapshot.institution_created for snapshot in exploration.snapshots),
            "기관 생성 이후 화면에 도달하지 못했습니다.",
        )
        for prefix in DEEP_BRANCHES:
            with self.subTest(branch=_describe_sequence(prefix)):
                deeper = [
                    snapshot
                    for snapshot in exploration.snapshots
                    if len(snapshot.sequence) == len(prefix) + 1
                    and snapshot.sequence[: len(prefix)] == prefix
                ]
                self.assertTrue(deeper)

    def test_no_exception_on_any_screen(self) -> None:  # I1
        failures = [
            f"{snapshot.where}: {snapshot.exceptions[0][:300]}"
            for snapshot in self.exploration.snapshots
            if snapshot.exceptions
        ]
        self.assertEqual([], failures)

    def test_no_internal_details_in_visible_messages(self) -> None:  # I2
        failures = [
            f"{snapshot.where}: '{token}' in {text[:200]!r}"
            for snapshot in self.exploration.snapshots
            for text in snapshot.alert_texts
            for token in FORBIDDEN_INTERNAL_TEXT
            if token in text
        ]
        self.assertEqual([], failures)

    def test_no_dead_end_screen(self) -> None:  # I3
        failures = [
            snapshot.where
            for snapshot in self.exploration.snapshots
            if not snapshot.enabled_buttons and snapshot.enabled_input_count == 0
        ]
        self.assertEqual(
            [],
            failures,
            "활성화된 버튼도 입력칸도 없는 막다른 화면입니다.",
        )

    def test_duplicate_enabled_button_labels_are_only_the_known_ones(self) -> None:  # I4
        failures = [
            f"{snapshot.where}: {label!r} x{count}"
            for snapshot in self.exploration.snapshots
            for label, count in sorted(snapshot.duplicate_labels.items())
            if label not in KNOWN_DUPLICATE_BUTTON_LABELS
        ]
        self.assertEqual(
            [],
            failures,
            "한 화면에 이름이 같은 활성 버튼이 둘 이상 있습니다. 사용자는 둘을 구별할 수 "
            "없습니다. 새 중복은 허용 목록에 넣지 말고 버튼 이름을 고치세요.",
        )

    def test_known_duplicate_allowlist_has_no_stale_entries(self) -> None:  # I4 ratchet
        observed = {
            label
            for snapshot in self.exploration.snapshots
            for label in snapshot.duplicate_labels
        }
        stale = sorted(KNOWN_DUPLICATE_BUTTON_LABELS - observed)
        self.assertEqual(
            [],
            stale,
            "탐색한 어느 화면에서도 더 이상 중복되지 않는 라벨이 KNOWN_DUPLICATE_BUTTON_LABELS에 "
            "남아 있습니다. 고쳐졌다면 목록에서 지우세요.",
        )

    def test_no_approval_or_index_action_before_a_document_exists(self) -> None:  # I5
        empty_workspaces = [
            snapshot
            for snapshot in self.exploration.snapshots
            if snapshot.institution_created and not snapshot.has_document
        ]
        self.assertTrue(
            empty_workspaces,
            "문서가 없는 기관 화면을 하나도 검사하지 못했습니다.",
        )
        offending = [
            f"{snapshot.where}: {label!r}"
            for snapshot in empty_workspaces
            for label in snapshot.enabled_buttons
            if APPROVAL_OR_INDEX_ACTION.search(label)
        ]
        self.assertEqual([], offending)

    def test_exploration_never_launched_processes_browsers_or_network(self) -> None:
        self.assertEqual({}, self.exploration.log.problems())


@unittest.skipIf(AppTest is None, "streamlit.testing.v1.AppTest is not available")
class CarelessInstitutionNameFuzzTests(unittest.TestCase):
    """Type anything into the institution-name field and press '기관 생성'."""

    BLANK_NAMES = ("", "   ")
    ODD_NAMES = (
        "a" * 300,
        "../../windows/system32",
        "<script>alert(1)</script>",
        "기관/이름:*?",
        "😀기관",
        "CON",
    )

    def setUp(self) -> None:
        self.log = _LaunchLog()
        self.addCleanup(self._assert_nothing_launched)

    def _assert_nothing_launched(self) -> None:
        self.assertEqual({}, self.log.problems())

    def _submit(self, value: str) -> dict[str, object]:
        with _isolated_app(self.log) as sandbox:
            sandbox.run()
            name_input = next(
                item for item in sandbox.app.text_input if item.label == "기관명"
            )
            name_input.input(value)
            sandbox.click(("기관 생성", 0))
            snapshot = _take_snapshot((("기관 생성", 0),), sandbox)
            app = sandbox.app
            names_on_disk = {path.name.lower() for path in sandbox.root.rglob("*")}
            return {
                "snapshot": snapshot,
                "created": sandbox.institution_created,
                "selected_profile": sandbox.session_value("selected_institution_profile_id"),
                "shows_current_institution": any(
                    "현재 기관" in text for text in _element_texts(app.info)
                ) or any(
                    "**현재 기관**" in text for text in _element_texts(app.markdown)
                ),
                "path_escape": sorted(
                    names_on_disk & {"system32", "windows", "..", "con"}
                ),
            }

    def _assert_safe(self, value: str, outcome: dict[str, object]) -> None:
        snapshot = outcome["snapshot"]
        assert isinstance(snapshot, _Snapshot)
        self.assertEqual((), snapshot.exceptions, f"I1 {value[:40]!r}")  # I1
        leaks = [
            token
            for text in snapshot.alert_texts
            for token in FORBIDDEN_INTERNAL_TEXT
            if token in text
        ]
        self.assertEqual([], leaks, f"I2 {value[:40]!r}")  # I2

    def test_blank_names_are_rejected_and_create_nothing(self) -> None:
        for value in self.BLANK_NAMES:
            with self.subTest(value=repr(value)):
                outcome = self._submit(value)
                self._assert_safe(value, outcome)
                snapshot = outcome["snapshot"]
                assert isinstance(snapshot, _Snapshot)
                self.assertTrue(snapshot.errors, "빈 이름인데 오류 안내가 없습니다.")
                self.assertFalse(outcome["created"], "빈 이름으로 기관이 저장됐습니다.")
                self.assertFalse(outcome["selected_profile"])
                self.assertFalse(outcome["shows_current_institution"])

    def test_odd_names_either_fail_with_a_message_or_create_safely(self) -> None:
        for value in self.ODD_NAMES:
            with self.subTest(value=repr(value)[:40]):
                outcome = self._submit(value)
                self._assert_safe(value, outcome)
                snapshot = outcome["snapshot"]
                assert isinstance(snapshot, _Snapshot)
                self.assertTrue(
                    outcome["created"] or snapshot.errors,
                    "기관도 만들어지지 않았고 오류 안내도 없습니다(조용한 실패).",
                )
                # The name must never become a path component.
                self.assertEqual([], outcome["path_escape"])


if __name__ == "__main__":
    unittest.main()
