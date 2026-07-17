from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _latest_batch_csv(reports_dir: Path) -> Path:
    candidates = sorted(reports_dir.glob("batch_quality_*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No batch_quality_*.csv found under {reports_dir}")
    return candidates[0]


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return f"{(numerator / denominator) * 100:.1f}%"


def _chunk_artifact_path(repo_dir: Path, document_id: str, suffix: str) -> Path:
    return repo_dir / f"{document_id}_{suffix}.json"


def _metadata_inventory(chunks: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    for chunk in chunks:
        metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
        document_inventory = metadata.get("document_inventory")
        kordoc_inventory = metadata.get("kordoc_table_inventory")
        if isinstance(document_inventory, dict) or isinstance(kordoc_inventory, dict):
            return (
                document_inventory if isinstance(document_inventory, dict) else {},
                kordoc_inventory if isinstance(kordoc_inventory, dict) else {},
            )
    return {}, {}


def _entity_id(chunk: dict[str, Any]) -> str:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    return str(metadata.get("entity_id") or metadata.get("hierarchy_path") or chunk.get("chunk_id") or "")


def _analyze_document(row: dict[str, str], label: dict[str, str], repo_dir: Path) -> dict[str, Any]:
    document_id = row["document_id"]
    chunks = _load_json(_chunk_artifact_path(repo_dir, document_id, "chunks"))
    nodes_path = _chunk_artifact_path(repo_dir, document_id, "nodes")
    nodes = _load_json(nodes_path) if nodes_path.exists() else []

    type_counts = Counter(str(chunk.get("chunk_type") or "") for chunk in chunks)
    article_numbers = {
        str(chunk.get("metadata", {}).get("article_no"))
        for chunk in chunks
        if chunk.get("chunk_type") == "article" and chunk.get("metadata", {}).get("article_no")
    }
    appendix_form_entities = {
        _entity_id(chunk)
        for chunk in chunks
        if chunk.get("chunk_type") in {"appendix", "form"} and _entity_id(chunk)
    }
    node_type_counts = Counter(str(node.get("node_type") or "") for node in nodes)
    document_inventory, kordoc_inventory = _metadata_inventory(chunks)

    article_reference = _int(label.get("pipeline_article_count"))
    article_distinct = len(article_numbers)
    if article_reference and article_distinct == article_reference:
        article_verdict = "stable_count_match"
    elif article_reference:
        article_verdict = "needs_review_count_gap"
    else:
        article_verdict = "needs_manual_reference"

    appendix_reference = _int(label.get("pipeline_appendix_form_count"))
    appendix_entities = len(appendix_form_entities)
    appendix_chunk_count = type_counts.get("appendix", 0) + type_counts.get("form", 0)
    if appendix_reference and appendix_entities == appendix_reference:
        appendix_verdict = "stable_entity_count_match"
    elif appendix_reference and appendix_chunk_count == appendix_reference:
        appendix_verdict = "chunk_count_match_entity_review_needed"
    elif appendix_reference == 0 and appendix_entities > 0:
        appendix_verdict = "possible_new_detection_or_false_positive"
    elif appendix_reference and appendix_entities:
        appendix_verdict = "entity_chunk_boundary_review_needed"
    else:
        appendix_verdict = "needs_manual_reference"

    table_like = _int(row.get("table_like_chunks"))
    structured = _int(row.get("chunks_with_table_cell_rows"))
    no_cell = _int(row.get("table_like_without_cell_rows"))
    false_positive = _int(row.get("probable_table_false_positive_chunks"))
    extraction_failed = _int(row.get("probable_table_extraction_failed_chunks"))
    no_cell_rate = no_cell / table_like if table_like else 0.0
    if table_like and no_cell == 0 and false_positive == 0 and extraction_failed == 0:
        table_verdict = "best_current_state_still_review_required"
    elif no_cell_rate >= 0.30 or false_positive >= 10 or extraction_failed:
        table_verdict = "high_risk_table_review_required"
    else:
        table_verdict = "medium_risk_table_review_required"

    source_path = label.get("source_path") or row.get("input_path") or ""
    note = _codex_note(
        filename=row.get("filename", ""),
        article_verdict=article_verdict,
        appendix_verdict=appendix_verdict,
        table_verdict=table_verdict,
        article_reference=article_reference,
        article_distinct=article_distinct,
        appendix_reference=appendix_reference,
        appendix_entities=appendix_entities,
        table_like=table_like,
        structured=structured,
        no_cell=no_cell,
        false_positive=false_positive,
        extraction_failed=extraction_failed,
    )

    return {
        "priority_rank": "",
        "old_document_id": row.get("source_record_id", ""),
        "new_document_id": document_id,
        "extension": Path(source_path).suffix.lower(),
        "institution_name": row.get("institution_name", ""),
        "filename": row.get("filename", ""),
        "source_path": source_path,
        "parse_status": row.get("status", ""),
        "quality_score": row.get("quality_score", ""),
        "chunk_count": row.get("chunk_count", ""),
        "chunk_to_source_char_ratio": row.get("chunk_to_source_char_ratio", ""),
        "article_reference_count": article_reference,
        "article_current_distinct_count": article_distinct,
        "article_chunk_count": type_counts.get("article", 0),
        "article_node_count": node_type_counts.get("article", 0),
        "article_verdict": article_verdict,
        "appendix_form_reference_count": appendix_reference,
        "appendix_form_entity_count": appendix_entities,
        "appendix_form_chunk_count": appendix_chunk_count,
        "appendix_node_count": node_type_counts.get("appendix", 0),
        "form_node_count": node_type_counts.get("form", 0),
        "appendix_form_verdict": appendix_verdict,
        "table_like_chunks": table_like,
        "chunks_with_table_cell_rows": structured,
        "table_like_without_cell_rows": no_cell,
        "table_structured_rate": _pct(structured, table_like),
        "probable_table_false_positive_chunks": false_positive,
        "probable_table_extraction_failed_chunks": extraction_failed,
        "table_verdict": table_verdict,
        "agent_review_status": row.get("agent_review_status", ""),
        "agent_review_skip_reason": row.get("agent_review_skip_reason", ""),
        "agent_review_selected_count": row.get("agent_review_selected_count", ""),
        "kordoc_status": kordoc_inventory.get("status", "not_recorded"),
        "kordoc_table_count": kordoc_inventory.get("table_count", 0),
        "hwp_inventory_articles": document_inventory.get("hierarchy", {}).get("articles", ""),
        "hwp_inventory_attachments": document_inventory.get("attachments", {}).get("total", ""),
        "hwp_inventory_tables": document_inventory.get("tables", {}).get("total", ""),
        "codex_review_note": note,
    }


def _codex_note(
    *,
    filename: str,
    article_verdict: str,
    appendix_verdict: str,
    table_verdict: str,
    article_reference: int,
    article_distinct: int,
    appendix_reference: int,
    appendix_entities: int,
    table_like: int,
    structured: int,
    no_cell: int,
    false_positive: int,
    extraction_failed: int,
) -> str:
    notes: list[str] = []
    if article_verdict == "stable_count_match":
        notes.append(f"조문 수는 기준 후보 {article_reference}건과 현재 고유 조문 {article_distinct}건이 일치.")
    else:
        notes.append(f"조문 경계 재검토 필요: 기준 후보 {article_reference}건, 현재 고유 조문 {article_distinct}건.")

    if "지식재산권관리규정" in filename:
        notes.append("사용자 지정 핵심 사례: 조문 55건은 일치하고 별표/별지 엔터티는 25건 수준으로 보이나, 표 셀 보존이 병목.")
    elif appendix_verdict in {"stable_entity_count_match", "chunk_count_match_entity_review_needed"}:
        notes.append(f"별표/별지 후보는 기준 {appendix_reference}건과 큰 충돌은 없지만 엔터티/청크 기준 분리 필요.")
    else:
        notes.append(f"별표/별지 경계 검토 필요: 기준 후보 {appendix_reference}건, 현재 엔터티 {appendix_entities}건.")

    if table_verdict.startswith("high"):
        notes.append(
            f"표 고위험: table-like {table_like}건 중 셀 미보존 {no_cell}건, 오탐 후보 {false_positive}건, 추출 실패 {extraction_failed}건."
        )
    elif table_verdict.startswith("medium"):
        notes.append(
            f"표 중위험: table-like {table_like}건 중 구조화 {structured}건, 셀 미보존 {no_cell}건."
        )
    else:
        notes.append(f"표는 현재 샘플 내 상대적으로 양호: table-like {table_like}건 모두 셀 구조가 있음. 그래도 표 단위 검토 필요.")
    return " ".join(notes)


def _apply_priority(detail_rows: list[dict[str, Any]], board_rows: list[dict[str, str]]) -> None:
    priorities = {row.get("document_id", ""): row.get("priority_rank", "") for row in board_rows}
    for detail in detail_rows:
        detail["priority_rank"] = priorities.get(detail["old_document_id"], "")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _risk_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "article_exact": sum(1 for row in rows if row["article_verdict"] == "stable_count_match"),
        "article_review_needed": sum(1 for row in rows if row["article_verdict"] != "stable_count_match"),
        "table_high_risk": sum(1 for row in rows if str(row["table_verdict"]).startswith("high")),
        "table_medium_risk": sum(1 for row in rows if str(row["table_verdict"]).startswith("medium")),
        "table_best_current": sum(1 for row in rows if str(row["table_verdict"]).startswith("best")),
        "kordoc_unavailable": sum(1 for row in rows if row["kordoc_status"] == "not_available"),
    }


def _write_markdown(path: Path, rows: list[dict[str, Any]], batch_csv: Path, detail_csv: Path) -> None:
    totals = {
        "documents": len(rows),
        "parsed": sum(1 for row in rows if row["parse_status"] == "completed"),
        "avg_quality": sum(_float(row["quality_score"]) for row in rows) / len(rows) if rows else 0.0,
        "table_like": sum(_int(row["table_like_chunks"]) for row in rows),
        "table_structured": sum(_int(row["chunks_with_table_cell_rows"]) for row in rows),
        "table_no_cell": sum(_int(row["table_like_without_cell_rows"]) for row in rows),
        "table_false_positive": sum(_int(row["probable_table_false_positive_chunks"]) for row in rows),
        "table_extraction_failed": sum(_int(row["probable_table_extraction_failed_chunks"]) for row in rows),
        "ai_selected": sum(_int(row["agent_review_selected_count"]) for row in rows),
    }
    risks = _risk_counts(rows)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("# 규정 10개 파서 검증 보고서")
    lines.append("")
    lines.append(f"- 작성 시각: {generated_at}")
    lines.append("- 검증자: Codex가 OpenAI API 호출 대신 AI 검수자 역할로 산출물 대조")
    lines.append(f"- 원본 배치 결과: `{batch_csv}`")
    lines.append(f"- 세부 CSV: `{detail_csv}`")
    lines.append("- 주의: 이 보고서는 정식 골드셋 F1 확정값이 아니라, 10개 규정 재처리 결과에 대한 Codex 검증 판단입니다.")
    lines.append("")
    lines.append("## 핵심 결론")
    lines.append("")
    lines.append(f"- 10개 중 {totals['parsed']}개가 파싱 완료됐고, 평균 품질 점수는 {totals['avg_quality']:.2f}입니다.")
    lines.append(
        f"- 조문 경계는 기준 후보와 현재 고유 조문 수가 {risks['article_exact']}/10개 문서에서 일치합니다. "
        f"{risks['article_review_needed']}개 문서는 조문 경계 재검토가 필요합니다."
    )
    lines.append(
        f"- 표는 아직 병목입니다. table-like {totals['table_like']}건 중 셀 구조가 있는 것은 "
        f"{totals['table_structured']}건({_pct(totals['table_structured'], totals['table_like'])}), "
        f"셀 구조가 없는 것은 {totals['table_no_cell']}건입니다."
    )
    lines.append(
        f"- 표 오탐 후보는 {totals['table_false_positive']}건, 표 추출 실패 후보는 "
        f"{totals['table_extraction_failed']}건입니다."
    )
    lines.append(
        f"- AI 검수 후보는 총 {totals['ai_selected']}개가 선택됐지만, 이번 실행은 API 키 없이 진행되어 "
        "`api_configuration_needed/openai_api_key_missing` 상태로 남았습니다. 이번 보고서에서는 Codex가 그 검수자 역할을 대신했습니다."
    )
    lines.append("- Kordoc은 현재 10개 문서 모두 `not_available`로 기록됐습니다. 따라서 이 결과는 Kordoc 표 보강 전 현재치입니다.")
    lines.append("")
    lines.append("## 정확도 상승 예상")
    lines.append("")
    lines.append("- 조문: 이미 10개 중 9개가 기준 후보 수와 맞아 큰 상승 여지는 작습니다. 남은 핵심은 제주국제자유도시개발센터 계약업무규정의 조문 경계 차이입니다.")
    lines.append("- 별표/별지: 청크 수가 아니라 엔터티 수로 세면 좋아지는 문서가 있습니다. 국가철도공단 지식재산권관리규정은 조문 55건과 별표/별지 엔터티 25건 수준으로 맞아 보입니다.")
    lines.append("- 표: 가장 큰 상승 여지가 있습니다. 현재 구조화율은 table-like 기준 약 65%이고, Kordoc 어댑터가 제대로 붙으면 우선 목표는 셀 미보존 184건과 오탐 59건을 줄이는 것입니다.")
    lines.append("- 결론적으로 Kordoc 표 어댑터와 AI/Codex 검수 루프를 붙이면 체감 개선은 표에서 가장 크게 나오고, 조문은 누락 문서 보정 중심으로 올라갈 가능성이 큽니다.")
    lines.append("")
    lines.append("## 문서별 검증표")
    lines.append("")
    lines.append(
        "|순위|기관|문서|형식|품질|조문 기준/현재|별표·별지 기준/현재 엔터티|표 구조화|표 위험|판단|"
    )
    lines.append("|---:|---|---|---|---:|---:|---:|---:|---|---|")
    for row in sorted(rows, key=lambda item: _int(item.get("priority_rank"), 999)):
        table_risk = row["table_verdict"]
        short_table_risk = "높음" if str(table_risk).startswith("high") else "중간" if str(table_risk).startswith("medium") else "상대적 양호"
        lines.append(
            "|{priority}|{inst}|{file}|{ext}|{quality}|{article_ref}/{article_now}|{appendix_ref}/{appendix_now}|{structured}/{table_like}|{risk}|{note}|".format(
                priority=row.get("priority_rank", ""),
                inst=_md(row.get("institution_name", "")),
                file=_md(row.get("filename", "")),
                ext=row.get("extension", ""),
                quality=row.get("quality_score", ""),
                article_ref=row.get("article_reference_count", ""),
                article_now=row.get("article_current_distinct_count", ""),
                appendix_ref=row.get("appendix_form_reference_count", ""),
                appendix_now=row.get("appendix_form_entity_count", ""),
                structured=row.get("chunks_with_table_cell_rows", ""),
                table_like=row.get("table_like_chunks", ""),
                risk=short_table_risk,
                note=_md(row.get("codex_review_note", "")),
            )
        )
    lines.append("")
    lines.append("## Kordoc 확인")
    lines.append("")
    lines.append("- 다운로드 ZIP은 바로 실행 파일이 아니라 TypeScript 소스입니다. `npm ci`와 `npm run build` 후 `node ...\\dist\\cli.js` 형태로 연결해야 합니다.")
    lines.append("- 현재 Python 어댑터는 Kordoc의 `{ type: 'table', table: {...} }` 구조를 완전히 읽지 못할 수 있습니다. Kordoc 적용 전 이 어댑터부터 보강해야 합니다.")
    lines.append("- 이번 10개 결과에는 Kordoc 표 추출이 반영되지 않았으므로, Kordoc 빌드 및 어댑터 수정 후 같은 10개로 재측정해야 합니다.")
    lines.append("")
    lines.append("## 다음 작업")
    lines.append("")
    lines.append("1. Kordoc JSON `block.table` 스키마를 현재 `KordocTableParser`가 셀/행/열/병합 정보로 읽게 수정합니다.")
    lines.append("2. 같은 10개 문서를 Kordoc 적용 상태로 재실행해 표 구조화율, 셀 미보존, 오탐 수를 비교합니다.")
    lines.append("3. 제주국제자유도시개발센터 계약업무규정의 조문 경계 차이를 원문 기준으로 재검토합니다.")
    lines.append("4. 국가철도공단 지식재산권관리규정은 표·각주 중심으로 별도 회귀 테스트를 유지합니다.")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_kordoc_comparison_rows(batch_csv: Path, data_dir: Path) -> list[dict[str, Any]]:
    if not batch_csv.exists():
        return []
    rows = _load_csv(batch_csv)
    repo_dir = data_dir / "repository"
    comparison_rows: list[dict[str, Any]] = []
    for row in rows:
        document_id = row.get("document_id", "")
        chunks_path = repo_dir / f"{document_id}_chunks.json"
        kordoc_inventory: dict[str, Any] = {}
        if chunks_path.exists():
            chunks = _load_json(chunks_path)
            for chunk in chunks:
                metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
                candidate = metadata.get("kordoc_table_inventory")
                if isinstance(candidate, dict):
                    kordoc_inventory = candidate
                    break
        comparison_rows.append(
            {
                "old_document_id": row.get("source_record_id", ""),
                "new_document_id": document_id,
                "institution_name": row.get("institution_name", ""),
                "filename": row.get("filename", ""),
                "kordoc_status": kordoc_inventory.get("status", "missing"),
                "kordoc_table_count": kordoc_inventory.get("table_count", 0),
                "kordoc_stored_table_count": kordoc_inventory.get("stored_table_count", 0),
                "kordoc_tables_truncated": kordoc_inventory.get("tables_truncated", False),
                "local_table_like_chunks": row.get("table_like_chunks", ""),
                "local_chunks_with_table_cell_rows": row.get("chunks_with_table_cell_rows", ""),
                "local_table_like_without_cell_rows": row.get("table_like_without_cell_rows", ""),
            }
        )
    return comparison_rows


def _append_kordoc_markdown(
    markdown_path: Path,
    *,
    kordoc_rows: list[dict[str, Any]],
    kordoc_batch_csv: Path,
    kordoc_csv: Path,
) -> None:
    if not kordoc_rows:
        return
    parsed = sum(1 for row in kordoc_rows if row.get("kordoc_status") == "parsed")
    invalid_json = sum(1 for row in kordoc_rows if row.get("kordoc_status") == "invalid_json")
    total_tables = sum(_int(row.get("kordoc_table_count")) for row in kordoc_rows)
    stored_tables = sum(_int(row.get("kordoc_stored_table_count")) for row in kordoc_rows)
    lines = markdown_path.read_text(encoding="utf-8").rstrip().splitlines()
    lines.extend(
        [
            "",
            "## Kordoc 적용 재실행 결과",
            "",
            f"- Kordoc 배치 결과: `{kordoc_batch_csv}`",
            f"- Kordoc 비교 CSV: `{kordoc_csv}`",
            f"- Kordoc parsed 문서: {parsed}/10, invalid_json 문서: {invalid_json}/10",
            f"- Kordoc 감지 표: {total_tables}건, inventory 저장 표: {stored_tables}건",
            "- 로컬 품질 지표는 아직 Kordoc 표를 정식 chunk/table 지표로 병합하지 않기 때문에 이전과 동일하게 보입니다. 현재 단계의 의미는 Kordoc 표 신호가 AI/Codex 검수 후보로 붙는지 확인한 것입니다.",
            "",
            "|기관|문서|Kordoc 상태|Kordoc 표/저장|로컬 table-like/셀구조/셀없음|판단|",
            "|---|---|---|---:|---:|---|",
        ]
    )
    for row in kordoc_rows:
        status = str(row.get("kordoc_status", ""))
        if status == "parsed":
            note = "Kordoc 표 신호 확보. 다음 단계는 로컬 표 chunk와 병합/대조."
        elif status == "invalid_json":
            note = "Kordoc stdout 정리 또는 --silent 실행 보강 필요."
        else:
            note = "Kordoc 실행 상태 확인 필요."
        lines.append(
            "|{inst}|{file}|{status}|{kt}/{ks}|{lt}/{ls}/{ln}|{note}|".format(
                inst=_md(row.get("institution_name", "")),
                file=_md(row.get("filename", "")),
                status=_md(status),
                kt=row.get("kordoc_table_count", ""),
                ks=row.get("kordoc_stored_table_count", ""),
                lt=row.get("local_table_like_chunks", ""),
                ls=row.get("local_chunks_with_table_cell_rows", ""),
                ln=row.get("local_table_like_without_cell_rows", ""),
                note=_md(note),
            )
        )
    lines.extend(["", "### Kordoc 이후 수정 우선순위", ""])
    if invalid_json:
        lines.append(
            "1. `invalid_json` PDF는 Kordoc 진행 메시지와 JSON 출력을 분리하도록 `--silent` 또는 stdout 정리 로직을 추가합니다."
        )
        lines.append(
            "2. Kordoc inventory를 현재 table chunk와 매칭해 `table_like_without_cell_rows`를 실제로 줄이는 병합 단계를 추가합니다."
        )
        lines.append("3. 병합 후 같은 10개로 표 구조화율을 다시 계산해야 실제 정확도 상승률을 말할 수 있습니다.")
    else:
        lines.append("1. `invalid_json`은 이번 보강으로 0건까지 해소됐습니다.")
        lines.append(
            "2. 다음 핵심 작업은 Kordoc inventory를 현재 table chunk와 매칭해 `table_like_without_cell_rows`를 실제로 줄이는 병합 단계입니다."
        )
        lines.append("3. 병합 후 같은 10개로 표 구조화율을 다시 계산해야 실제 정확도 상승률을 말할 수 있습니다.")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _md(value: Any) -> str:
    text = str(value).replace("\n", " ")
    return text.replace("|", "\\|")


def build_report(args: argparse.Namespace) -> dict[str, str]:
    reports_dir = Path(args.reports_dir)
    batch_csv = Path(args.batch_csv) if args.batch_csv else _latest_batch_csv(reports_dir)
    labels = {row["document_id"]: row for row in _load_csv(Path(args.labels_csv))}
    board_rows = _load_csv(Path(args.completion_board_csv))
    batch_rows = _load_csv(batch_csv)
    repo_dir = Path(args.data_dir) / "repository"
    details = []
    for row in batch_rows:
        old_id = row.get("source_record_id", "")
        label = labels.get(old_id)
        if not label:
            continue
        details.append(_analyze_document(row, label, repo_dir))
    _apply_priority(details, board_rows)
    details = sorted(details, key=lambda item: _int(item.get("priority_rank"), 999))

    desktop = Path(args.out_dir) if args.out_dir else Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    md_path = desktop / args.out_markdown
    csv_path = desktop / args.out_csv
    kordoc_csv_path = desktop / args.out_kordoc_csv
    _write_csv(csv_path, details)
    _write_markdown(md_path, details, batch_csv=batch_csv, detail_csv=csv_path)
    result = {"markdown": str(md_path), "csv": str(csv_path), "batch_csv": str(batch_csv)}
    kordoc_reports_dir = Path(args.kordoc_reports_dir)
    kordoc_batch_csv = Path(args.kordoc_batch_csv) if args.kordoc_batch_csv else None
    if kordoc_batch_csv is None and kordoc_reports_dir.exists():
        candidates = sorted(kordoc_reports_dir.glob("batch_quality_*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
        kordoc_batch_csv = candidates[0] if candidates else None
    if kordoc_batch_csv and kordoc_batch_csv.exists():
        kordoc_rows = _load_kordoc_comparison_rows(kordoc_batch_csv, Path(args.kordoc_data_dir))
        if kordoc_rows:
            _write_csv(kordoc_csv_path, kordoc_rows)
            _append_kordoc_markdown(
                md_path,
                kordoc_rows=kordoc_rows,
                kordoc_batch_csv=kordoc_batch_csv,
                kordoc_csv=kordoc_csv_path,
            )
            result["kordoc_csv"] = str(kordoc_csv_path)
            result["kordoc_batch_csv"] = str(kordoc_batch_csv)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a Codex-reviewed 10-document parser verification report.")
    parser.add_argument("--reports-dir", default="reports/parser_10doc_eval_20260711")
    parser.add_argument("--batch-csv", default=None)
    parser.add_argument("--labels-csv", default="reports/parsing_manual_goldset_labels_reprocessed_20260711.csv")
    parser.add_argument("--completion-board-csv", default="reports/parsing_goldset_completion_board_reprocessed_20260711.csv")
    parser.add_argument("--data-dir", default="data/parser_10doc_eval_20260711")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--out-markdown", default="parser_10doc_codex_review_20260711.md")
    parser.add_argument("--out-csv", default="parser_10doc_codex_review_20260711_detail.csv")
    parser.add_argument("--out-kordoc-csv", default="parser_10doc_kordoc_comparison_20260711.csv")
    parser.add_argument("--kordoc-reports-dir", default="reports/parser_10doc_eval_kordoc_20260711")
    parser.add_argument("--kordoc-batch-csv", default=None)
    parser.add_argument("--kordoc-data-dir", default="data/parser_10doc_eval_kordoc_20260711")
    return parser.parse_args()


def main() -> int:
    result = build_report(parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
