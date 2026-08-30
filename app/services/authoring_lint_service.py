from __future__ import annotations

import re
from uuid import UUID

from app.schemas.authoring import (
    ARTICLE_REFERENCE_RE,
    AuthoringLintFinding,
    AuthoringLintReport,
    AuthoringLintSeverity,
    AuthoringMode,
    AuthoringProject,
    ClauseDraft,
    DraftNodeType,
)


_PLACEHOLDER_RE = re.compile(r"(?:TODO|TBD|XXX|\[\s*\]|추후\s*작성|검토\s*필요)", re.IGNORECASE)
_EXTERNAL_REFERENCE_PREFIX_RE = re.compile(
    r"(?:」|법|법률|시행령|시행규칙|정관|조례|훈령|예규|고시|시행세칙)\s*$"
)


class AuthoringLintService:
    """Run pure, deterministic checks over an isolated authoring project."""

    def lint(self, project: AuthoringProject) -> AuthoringLintReport:
        findings: list[AuthoringLintFinding] = []
        findings.extend(self._lint_required_metadata(project))
        findings.extend(self._lint_structure(project))
        findings.extend(self._lint_references(project))
        findings.extend(self._lint_checklist(project))
        return AuthoringLintReport(
            project_id=project.project_id,
            revision=project.revision,
            findings=findings,
        )

    def _lint_required_metadata(self, project: AuthoringProject) -> list[AuthoringLintFinding]:
        findings: list[AuthoringLintFinding] = []
        required_text = (
            ("title", project.title, "regulation_title_missing", "규정명을 입력하세요."),
            ("purpose", project.purpose, "purpose_missing", "이 규정으로 해결하려는 문제를 한두 문장으로 적으세요."),
            ("scope", project.scope, "scope_missing", "누구와 어떤 업무에 적용되는지 적으세요."),
            (
                "responsible_department",
                project.responsible_department,
                "responsible_department_missing",
                "규정을 관리하고 질문을 받을 담당부서를 적으세요.",
            ),
        )
        for field_path, value, code, suggestion in required_text:
            if not value.strip():
                findings.append(
                    _finding(
                        code,
                        AuthoringLintSeverity.ERROR,
                        "필수 정보가 비어 있습니다.",
                        field_path,
                        suggestion,
                    )
                )
        if not project.legal_bases:
            findings.append(
                _finding(
                    "legal_basis_missing",
                    AuthoringLintSeverity.ERROR,
                    "법적 또는 내부 근거가 비어 있습니다.",
                    "legal_bases",
                    "관련 법령, 정관, 상위 규정 또는 내부 방침을 확인해 하나 이상 적으세요.",
                )
            )
        if project.planned_effective_date is None:
            findings.append(
                _finding(
                    "effective_date_missing",
                    AuthoringLintSeverity.ERROR,
                    "시행 예정일이 비어 있습니다.",
                    "planned_effective_date",
                    "검토와 안내에 필요한 시간을 고려해 시행 예정일을 선택하세요.",
                )
            )
        if project.authoring_mode != AuthoringMode.ENACTMENT:
            if not (project.revision_reason or "").strip():
                findings.append(
                    _finding(
                        "revision_reason_missing",
                        AuthoringLintSeverity.ERROR,
                        "개정안에 개정 사유가 없습니다.",
                        "revision_reason",
                        "무엇이 왜 달라져야 하는지 적으세요.",
                    )
                )
            if not (project.predecessor_reference or "").strip():
                findings.append(
                    _finding(
                        "predecessor_reference_missing",
                        AuthoringLintSeverity.ERROR,
                        "개정 대상 규정을 식별할 정보가 없습니다.",
                        "predecessor_reference",
                        "기존 규정명과 버전 또는 시행일을 적으세요.",
                    )
                )
        return findings

    def _lint_structure(self, project: AuthoringProject) -> list[AuthoringLintFinding]:
        if not project.clauses:
            return [
                _finding(
                    "clauses_missing",
                    AuthoringLintSeverity.ERROR,
                    "작성된 조문이 없습니다.",
                    "clauses",
                    "범용 템플릿을 적용한 뒤 각 조문의 안내에 따라 내용을 채우세요.",
                )
            ]

        findings: list[AuthoringLintFinding] = []
        clauses_by_id = {clause.clause_id: clause for clause in project.clauses}
        known_ids = set(clauses_by_id)
        order_keys: set[tuple[UUID | None, int]] = set()
        article_numbers: dict[str, ClauseDraft] = {}
        for index, clause in enumerate(project.clauses):
            field_prefix = f"clauses[{index}]"
            if clause.parent_id is not None and clause.parent_id not in known_ids:
                findings.append(
                    _clause_finding(
                        "parent_node_missing",
                        AuthoringLintSeverity.ERROR,
                        "상위 항목을 찾을 수 없습니다.",
                        f"{field_prefix}.parent_id",
                        "상위 장·절·조를 다시 선택하거나 상위 항목 연결을 지우세요.",
                        clause,
                    )
                )
            elif clause.parent_id is not None and _has_parent_cycle(
                clause,
                clauses_by_id=clauses_by_id,
            ):
                findings.append(
                    _clause_finding(
                        "parent_cycle",
                        AuthoringLintSeverity.ERROR,
                        "상위 항목 연결이 순환합니다.",
                        f"{field_prefix}.parent_id",
                        "이 항목의 상위 장·절·조를 다시 선택해 순환 연결을 끊으세요.",
                        clause,
                    )
                )
            order_key = (clause.parent_id, clause.order)
            if order_key in order_keys:
                findings.append(
                    _clause_finding(
                        "duplicate_sibling_order",
                        AuthoringLintSeverity.WARNING,
                        "같은 상위 항목 안에서 표시 순서가 겹칩니다.",
                        f"{field_prefix}.order",
                        "항목을 읽을 순서대로 서로 다른 순번을 지정하세요.",
                        clause,
                    )
                )
            order_keys.add(order_key)

            if clause.required and clause.node_type not in {DraftNodeType.CHAPTER, DraftNodeType.SECTION}:
                if not clause.body.strip():
                    findings.append(
                        _clause_finding(
                            "clause_body_empty",
                            AuthoringLintSeverity.ERROR,
                            "필수 조문의 내용이 비어 있습니다.",
                            f"{field_prefix}.body",
                            clause.beginner_guidance,
                            clause,
                        )
                    )
                elif _PLACEHOLDER_RE.search(clause.body):
                    findings.append(
                        _clause_finding(
                            "placeholder_remaining",
                            AuthoringLintSeverity.ERROR,
                            "완성되지 않은 자리표시 문구가 남아 있습니다.",
                            f"{field_prefix}.body",
                            "TODO, 검토 필요 같은 문구를 실제 내용으로 바꾸세요.",
                            clause,
                        )
                    )

            if clause.node_type == DraftNodeType.ARTICLE:
                normalized = _normalize_article_number(clause.article_number)
                if normalized is None:
                    findings.append(
                        _clause_finding(
                            "article_number_invalid",
                            AuthoringLintSeverity.ERROR,
                            "조 번호 형식을 확인할 수 없습니다.",
                            f"{field_prefix}.article_number",
                            "제1조 또는 제3조의2처럼 조 번호를 적으세요.",
                            clause,
                        )
                    )
                elif normalized in article_numbers:
                    findings.append(
                        _clause_finding(
                            "duplicate_article_number",
                            AuthoringLintSeverity.ERROR,
                            "같은 조 번호가 두 번 사용되었습니다.",
                            f"{field_prefix}.article_number",
                            "각 조에 겹치지 않는 번호를 지정하세요.",
                            clause,
                        )
                    )
                else:
                    article_numbers[normalized] = clause
        return findings

    def _lint_references(self, project: AuthoringProject) -> list[AuthoringLintFinding]:
        findings: list[AuthoringLintFinding] = []
        reference_ids = {reference.reference_id for reference in project.references}
        article_numbers = {
            normalized
            for clause in project.clauses
            if clause.node_type == DraftNodeType.ARTICLE
            if (normalized := _normalize_article_number(clause.article_number)) is not None
        }
        for index, clause in enumerate(project.clauses):
            field_prefix = f"clauses[{index}]"
            for reference_id in clause.reference_ids:
                if reference_id not in reference_ids:
                    findings.append(
                        _clause_finding(
                            "reference_snapshot_missing",
                            AuthoringLintSeverity.ERROR,
                            "조문에 연결된 근거 기록을 찾을 수 없습니다.",
                            f"{field_prefix}.reference_ids",
                            "유효한 근거 기록을 다시 연결하거나 잘못된 연결을 지우세요.",
                            clause,
                        )
                    )
            text = "\n".join(part for part in (clause.title or "", clause.body) if part)
            missing_numbers: set[str] = set()
            for match in ARTICLE_REFERENCE_RE.finditer(text):
                if _looks_like_external_reference(text, match.start()):
                    continue
                normalized = _normalized_reference_match(match)
                if normalized not in article_numbers:
                    missing_numbers.add(normalized)
            for missing_number in sorted(missing_numbers, key=_article_sort_key):
                findings.append(
                    _clause_finding(
                        "internal_article_reference_missing",
                        AuthoringLintSeverity.ERROR,
                        f"본문이 가리키는 {missing_number}를 찾을 수 없습니다.",
                        f"{field_prefix}.body",
                        "조 번호를 바로잡거나 빠진 조문을 추가하세요. 외부 법령 인용이면 법령명을 함께 적으세요.",
                        clause,
                    )
                )
        return findings

    def _lint_checklist(self, project: AuthoringProject) -> list[AuthoringLintFinding]:
        if not project.checklist:
            return [
                _finding(
                    "checklist_missing",
                    AuthoringLintSeverity.ERROR,
                    "초보자 확인 목록을 찾을 수 없습니다.",
                    "checklist",
                    "프로젝트 확인 목록을 복원한 뒤 각 항목을 확인하세요.",
                )
            ]
        incomplete = [item for item in project.checklist if not item.completed]
        if not incomplete:
            return []
        return [
            _finding(
                "checklist_incomplete",
                AuthoringLintSeverity.ERROR,
                f"초보자 확인 목록 {len(incomplete)}개가 남아 있습니다.",
                "checklist",
                "내용 확인을 요청하기 전에 남은 항목을 차례로 확인하세요.",
            )
        ]


def lint_authoring_project(project: AuthoringProject) -> AuthoringLintReport:
    return AuthoringLintService().lint(project)


def _finding(
    code: str,
    severity: AuthoringLintSeverity,
    message: str,
    field_path: str,
    suggestion: str,
) -> AuthoringLintFinding:
    return AuthoringLintFinding(
        code=code,
        severity=severity,
        message=message,
        field_path=field_path,
        suggestion=suggestion,
    )


def _clause_finding(
    code: str,
    severity: AuthoringLintSeverity,
    message: str,
    field_path: str,
    suggestion: str,
    clause: ClauseDraft,
) -> AuthoringLintFinding:
    return AuthoringLintFinding(
        code=code,
        severity=severity,
        message=message,
        field_path=field_path,
        suggestion=suggestion,
        clause_id=clause.clause_id,
        article_number=clause.article_number,
    )


def _normalize_article_number(value: str) -> str | None:
    cleaned = re.sub(r"\s+", "", value)
    if cleaned.isdigit():
        return f"제{int(cleaned)}조"
    match = re.fullmatch(r"제(\d+)조(?:의(\d+))?", cleaned)
    if match is None:
        return None
    suffix = f"의{int(match.group(2))}" if match.group(2) else ""
    return f"제{int(match.group(1))}조{suffix}"


def _normalized_reference_match(match: re.Match[str]) -> str:
    suffix = f"의{int(match.group(2))}" if match.group(2) else ""
    return f"제{int(match.group(1))}조{suffix}"


def _looks_like_external_reference(text: str, match_start: int) -> bool:
    prefix = text[max(0, match_start - 40) : match_start]
    return _EXTERNAL_REFERENCE_PREFIX_RE.search(prefix) is not None


def _has_parent_cycle(
    clause: ClauseDraft,
    *,
    clauses_by_id: dict[UUID, ClauseDraft],
) -> bool:
    visited: set[UUID] = set()
    parent_id = clause.parent_id
    while parent_id is not None:
        if parent_id == clause.clause_id:
            return True
        if parent_id in visited:
            # The clause depends on a broken branch but is not itself one of
            # the nodes that must change to break the cycle.
            return False
        visited.add(parent_id)
        parent = clauses_by_id.get(parent_id)
        if parent is None:
            return False
        parent_id = parent.parent_id
    return False


def _article_sort_key(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"제(\d+)조(?:의(\d+))?", value)
    if match is None:
        return (10**9, 10**9)
    return (int(match.group(1)), int(match.group(2) or 0))
