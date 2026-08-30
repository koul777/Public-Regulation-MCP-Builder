from __future__ import annotations

from uuid import UUID, uuid5

from app.schemas.authoring import (
    AuthoringTemplate,
    AuthoringTemplateNode,
    ClauseDraft,
    DraftNodeType,
)


class AuthoringTemplateNotFoundError(KeyError):
    pass


class AuthoringTemplateService:
    """Provide public, institution-neutral skeletons with beginner guidance."""

    def list_templates(self) -> list[AuthoringTemplate]:
        return [template.model_copy(deep=True) for template in _TEMPLATES]

    def get_template(self, template_id: str) -> AuthoringTemplate:
        normalized = template_id.strip().lower()
        for template in _TEMPLATES:
            if template.template_id == normalized:
                return template.model_copy(deep=True)
        raise AuthoringTemplateNotFoundError(f"Unknown authoring template: {normalized}")

    def instantiate_clauses(self, template_id: str, *, project_id: UUID) -> list[ClauseDraft]:
        template = self.get_template(template_id)
        identifiers = {node.node_key: uuid5(project_id, node.node_key) for node in template.nodes}
        return [
            ClauseDraft(
                clause_id=identifiers[node.node_key],
                node_type=node.node_type,
                parent_id=identifiers.get(node.parent_key) if node.parent_key else None,
                order=node.order,
                article_number=node.article_number,
                title=node.title,
                body="",
                beginner_guidance=node.beginner_guidance,
                required=node.required,
            )
            for node in template.nodes
        ]


def _node(
    node_key: str,
    node_type: DraftNodeType,
    order: int,
    article_number: str,
    title: str,
    guidance: str,
    *,
    parent_key: str | None = None,
    required: bool = True,
) -> AuthoringTemplateNode:
    return AuthoringTemplateNode(
        node_key=node_key,
        node_type=node_type,
        parent_key=parent_key,
        order=order,
        article_number=article_number,
        title=title,
        beginner_guidance=guidance,
        required=required,
    )


_TEMPLATES = (
    AuthoringTemplate(
        template_id="general-regulation",
        version="1.0",
        name_ko="범용 규정",
        description_ko="목적, 적용 범위, 역할, 업무 절차와 시행일을 차례로 정리하는 기본 골격입니다.",
        recommended_for_ko="처음 규정을 만들거나 어떤 골격을 선택할지 확실하지 않을 때 사용하세요.",
        first_action_ko="먼저 제1조에 이 규정으로 해결하려는 문제를 한 문장으로 적으세요.",
        nodes=[
            _node("chapter-general", DraftNodeType.CHAPTER, 1, "제1장", "총칙", "총칙 아래의 목적·범위·용어를 확인하세요."),
            _node("purpose", DraftNodeType.ARTICLE, 1, "제1조", "목적", "이 규정으로 해결하려는 문제와 기대 결과를 한두 문장으로 적으세요.", parent_key="chapter-general"),
            _node("scope", DraftNodeType.ARTICLE, 2, "제2조", "적용 범위", "적용 대상인 사람, 조직, 업무와 예외를 구체적으로 적으세요.", parent_key="chapter-general"),
            _node("definitions", DraftNodeType.ARTICLE, 3, "제3조", "용어의 정의", "여러 뜻으로 해석될 수 있는 핵심 용어만 정의하세요.", parent_key="chapter-general", required=False),
            _node("chapter-operation", DraftNodeType.CHAPTER, 2, "제2장", "운영", "담당자와 실제 업무 흐름을 순서대로 확인하세요."),
            _node("roles", DraftNodeType.ARTICLE, 1, "제4조", "책임과 역할", "담당부서와 관련자의 책임, 권한, 협조 사항을 구분해 적으세요.", parent_key="chapter-operation"),
            _node("procedure", DraftNodeType.ARTICLE, 2, "제5조", "업무 절차", "신청부터 처리와 결과 통지까지 누가 무엇을 하는지 시간 순서로 적으세요.", parent_key="chapter-operation"),
            _node("records", DraftNodeType.ARTICLE, 3, "제6조", "기록과 관리", "남겨야 할 기록, 보관 주체, 보관 기간과 접근 범위를 적으세요.", parent_key="chapter-operation"),
            _node("supplementary", DraftNodeType.SUPPLEMENTARY, 3, "부칙", "시행일", "언제부터 시행하는지 적고 필요한 경과조치를 함께 확인하세요."),
        ],
    ),
    AuthoringTemplate(
        template_id="committee-operation",
        version="1.0",
        name_ko="위원회 운영 규정",
        description_ko="위원회의 구성, 회의, 의결과 회의록 관리에 필요한 기본 골격입니다.",
        recommended_for_ko="상설 또는 한시 위원회의 운영 기준을 처음 정할 때 사용하세요.",
        first_action_ko="먼저 제1조에 위원회를 두는 이유와 다룰 사항을 적으세요.",
        nodes=[
            _node("purpose", DraftNodeType.ARTICLE, 1, "제1조", "목적", "위원회를 두는 이유와 다룰 사항을 적으세요."),
            _node("functions", DraftNodeType.ARTICLE, 2, "제2조", "기능", "심의, 자문, 의결 등 위원회가 맡는 일을 구분해 적으세요."),
            _node("composition", DraftNodeType.ARTICLE, 3, "제3조", "구성", "위원 수, 자격, 임기, 위촉과 해촉 기준을 적으세요."),
            _node("chair", DraftNodeType.ARTICLE, 4, "제4조", "위원장의 직무", "위원장과 직무대행자의 역할을 적으세요."),
            _node("meetings", DraftNodeType.ARTICLE, 5, "제5조", "회의", "소집권자, 통지 기한, 정족수와 비대면 회의 가능 여부를 적으세요."),
            _node("decisions", DraftNodeType.ARTICLE, 6, "제6조", "의결", "의결 정족수와 이해충돌이 있는 위원의 처리 방법을 적으세요."),
            _node("minutes", DraftNodeType.ARTICLE, 7, "제7조", "회의록", "기록 항목, 확인자, 보관 장소와 공개 범위를 적으세요."),
            _node("supplementary", DraftNodeType.SUPPLEMENTARY, 8, "부칙", "시행일", "시행일과 최초 위원 구성에 필요한 경과조치를 적으세요."),
        ],
    ),
    AuthoringTemplate(
        template_id="work-procedure",
        version="1.0",
        name_ko="업무 처리 절차 규정",
        description_ko="신청, 검토, 결정, 통지와 기록 보관으로 이어지는 업무 흐름의 기본 골격입니다.",
        recommended_for_ko="반복되는 내부 업무의 담당자와 처리 기준을 명확히 할 때 사용하세요.",
        first_action_ko="먼저 제2조에 이 절차가 적용되는 신청과 업무의 범위를 적으세요.",
        nodes=[
            _node("purpose", DraftNodeType.ARTICLE, 1, "제1조", "목적", "업무 절차를 정해 줄이려는 혼선이나 위험을 적으세요."),
            _node("scope", DraftNodeType.ARTICLE, 2, "제2조", "적용 범위", "대상 업무, 신청자, 담당 조직과 제외 대상을 적으세요."),
            _node("roles", DraftNodeType.ARTICLE, 3, "제3조", "담당자와 역할", "접수, 검토, 결정, 통지 담당자의 역할을 나누어 적으세요."),
            _node("application", DraftNodeType.ARTICLE, 4, "제4조", "신청과 접수", "필요 서류, 접수 방법, 보완 요청과 접수 시각 기준을 적으세요."),
            _node("review", DraftNodeType.ARTICLE, 5, "제5조", "검토", "검토 기준, 확인할 자료와 이해충돌 회피 방법을 적으세요."),
            _node("decision", DraftNodeType.ARTICLE, 6, "제6조", "결정과 통지", "결정권자, 처리 기한, 통지 방법과 이의 제기 안내를 적으세요."),
            _node("records", DraftNodeType.ARTICLE, 7, "제7조", "기록 관리", "처리 근거와 결과의 보관 기간, 접근 권한, 폐기 방법을 적으세요."),
            _node("supplementary", DraftNodeType.SUPPLEMENTARY, 8, "부칙", "시행일", "시행일과 진행 중인 업무에 적용할 경과조치를 적으세요."),
        ],
    ),
)
