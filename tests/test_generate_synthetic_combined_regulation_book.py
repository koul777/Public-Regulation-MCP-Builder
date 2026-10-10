from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from app.parsers.factory import get_parser
from app.processors.normalizer import TextNormalizer
from app.processors.structure_detector import (
    STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY,
    StructureDetector,
)
from scripts import generate_synthetic_combined_regulation_book as gen

SMALL = 6
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _docx_text(data: bytes) -> str:
    """Paragraph text of a DOCX (table cells included), one paragraph per line."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p"))


def _truth(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


@pytest.fixture(scope="module")
def default_docx() -> tuple[bytes, dict]:
    _book, outputs = gen.generate(gen.DEFAULT_SEED, gen.DEFAULT_REGULATIONS, ("docx",))
    data, truth = outputs["docx"]
    return data, _truth(truth)


# --------------------------------------------------------------------------- determinism


def test_same_seed_gives_identical_docx_text_and_ground_truth() -> None:
    _b1, first = gen.generate(123, 8, ("docx",))
    _b2, second = gen.generate(123, 8, ("docx",))

    assert _docx_text(first["docx"][0]) == _docx_text(second["docx"][0])
    assert first["docx"][0] == second["docx"][0]
    assert first["docx"][1] == second["docx"][1]
    assert _truth(first["docx"][1]) == _truth(second["docx"][1])


def test_different_seed_changes_text_and_ground_truth() -> None:
    _b1, first = gen.generate(1, 8, ("docx",))
    _b2, second = gen.generate(2, 8, ("docx",))

    assert _docx_text(first["docx"][0]) != _docx_text(second["docx"][0])
    assert first["docx"][1] != second["docx"][1]


def test_pdf_and_hwpx_are_byte_deterministic() -> None:
    _b1, first = gen.generate(7, 3, ("pdf", "hwpx"))
    _b2, second = gen.generate(7, 3, ("pdf", "hwpx"))

    assert first["pdf"][0] == second["pdf"][0]
    assert first["hwpx"][0] == second["hwpx"][0]


def test_formats_share_one_logical_ground_truth() -> None:
    _book, outputs = gen.generate(5, SMALL, ("docx", "pdf", "hwpx"))
    docx, pdf, hwpx = (_truth(outputs[f][1]) for f in ("docx", "pdf", "hwpx"))

    for truth in (docx, pdf, hwpx):
        truth.pop("format")
        truth.pop("source_file")
    assert docx == pdf == hwpx


# --------------------------------------------------------------------------- requested counts


@pytest.mark.parametrize("count", [1, 5, 12])
def test_requested_regulation_count_is_honored(count: int) -> None:
    _book, outputs = gen.generate(11, count, ("docx",))
    data, raw = outputs["docx"]
    truth = _truth(raw)
    text = _docx_text(data)

    assert truth["totals"]["regulations"] == count
    assert len(truth["regulations"]) == count
    assert [r["order"] for r in truth["regulations"]] == list(range(1, count + 1))
    assert len({r["title"] for r in truth["regulations"]}) == count
    for reg in truth["regulations"]:
        assert f"{reg['number']}. {reg['title']}" in text
    numbered = [line for line in text.splitlines() if re.match(r"\d-\d+-\d+\. ", line)]
    assert len([line for line in numbered if "....." in line]) == count  # contents entries (dot leaders)
    assert len([line for line in numbered if "....." not in line]) == count  # regulation headings
    assert len(re.findall(r"(?m)^제1조\(목적\)", text)) == count


def test_default_book_matches_requested_scale(default_docx: tuple[bytes, dict]) -> None:
    _data, truth = default_docx
    totals = truth["totals"]

    assert totals["regulations"] == gen.DEFAULT_REGULATIONS == 48
    assert 2000 <= totals["articles"] <= 3000
    assert totals["deleted_articles"] > 0
    assert totals["branch_articles"] > 0
    assert 12 <= totals["queries"] <= 20
    assert totals["appendices"] > 0 and totals["forms"] > 0
    assert truth["layout"]["pdf_page_count"] >= 400
    sizes = sorted(r["article_count"] for r in truth["regulations"])
    assert sizes[-1] >= 80, "a few regulations must be very long"
    assert sizes[len(sizes) // 2] <= 50, "most regulations must be short or medium"
    assert all(5 <= r["counts"]["addenda"] <= 15 for r in truth["regulations"] if r["article_count"] >= 15)


@pytest.mark.parametrize("bad", [0, gen.MAX_REGULATIONS + 1])
def test_cli_rejects_out_of_range_regulation_count(bad: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = gen.main(["--out-dir", str(tmp_path), "--regulations", str(bad), "--formats", "docx"])

    assert code == 2
    assert "--regulations" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_cli_rejects_unknown_format(tmp_path: Path) -> None:
    assert gen.main(["--out-dir", str(tmp_path), "--formats", "txt"]) == 2


# --------------------------------------------------------------------------- ground truth content


def test_ground_truth_describes_the_book(default_docx: tuple[bytes, dict]) -> None:
    data, truth = default_docx
    text = _docx_text(data)
    compact_text = _compact(text)
    by_title = {r["title"]: r for r in truth["regulations"]}

    deleted = branch = 0
    for reg in truth["regulations"]:
        labels = [a["label"] for a in reg["articles"]]
        assert len(labels) == len(set(labels)), reg["title"]
        assert labels[0] == "제1조"
        assert reg["chapters"] and all(c["title"] for c in reg["chapters"])
        assert reg["counts"] == {
            "addenda": len(reg["addenda"]),
            "appendices": len(reg["appendices"]),
            "forms": len(reg["forms"]),
        }
        assert reg["revision_lines"][0].startswith("제정 ")
        assert len(reg["sample_articles"]) == 3
        for art in reg["articles"]:
            if art["deleted"]:
                deleted += 1
                assert art["deleted_on"]
                assert re.search(rf"(?m)^{art['label']} 삭제 <\d{{4}}\. \d{{1,2}}\. \d{{1,2}}\.>", text)
            if art["branch"]:
                branch += 1
                assert re.fullmatch(r"제\d+조의\d+", art["label"])
        for sample in reg["sample_articles"]:
            assert sample["text_compact"] == _compact(sample["text"])
            assert sample["text_compact"] in compact_text
            assert sample["text"].splitlines() == sample["lines"]
    assert deleted == truth["totals"]["deleted_articles"]
    assert branch == truth["totals"]["branch_articles"]

    kinds = {ref["kind"] for ref in truth["cross_references"]}
    assert {"external", "internal"} <= kinds
    for ref in truth["cross_references"]:
        assert ref["source_regulation"] in by_title and ref["target_regulation"] in by_title
        source = by_title[ref["source_regulation"]]
        assert ref["source_article"] in {a["label"] for a in source["articles"]}
        if ref["target_article"] is not None:
            target = by_title[ref["target_regulation"]]
            assert ref["target_article"] in {a["label"] for a in target["articles"]}
        if ref["kind"] == "external":
            assert ref["source_regulation"] != ref["target_regulation"]
            assert f"「{ref['target_regulation']}」" in ref["text"]


def test_queries_have_exactly_one_answering_article(default_docx: tuple[bytes, dict]) -> None:
    data, truth = default_docx
    text = _docx_text(data)
    compact_text = _compact(text)
    by_title = {r["title"]: r for r in truth["regulations"]}

    assert len({q["id"] for q in truth["queries"]}) == len(truth["queries"])
    assert len({q["query"] for q in truth["queries"]}) == len(truth["queries"])
    assert len({(q["expected"]["regulation"], q["expected"]["article"]) for q in truth["queries"]}) == len(truth["queries"])
    for query in truth["queries"]:
        expected = query["expected"]
        reg = by_title[expected["regulation"]]
        assert expected["article"] in {a["label"] for a in reg["articles"]}
        assert re.search(r"[가-힣]", query["query"]) and query["query"].endswith("?")
        compact_article = _compact(query["expected_text"])
        assert compact_article.startswith(_compact(f"{expected['article']}({expected['article_title']})"))
        assert text.count(query["expected_text"]) == 1, query["id"]
        for must in query["must_contain"]:
            assert _compact(must) in compact_article
        assert compact_text.count(_compact(max(query["must_contain"], key=len))) == 1, query["id"]


def test_book_has_no_local_paths_or_real_identifiers(default_docx: tuple[bytes, dict]) -> None:
    data, raw_truth = default_docx
    source = Path(gen.__file__).read_text(encoding="utf-8")
    forbidden = ["/" + "home/", "/" + "Users/", "C:" + "\\", "/" + "tmp/", "@" + "gmail", "https" + "://"]
    blob = source + json.dumps(raw_truth, ensure_ascii=False) + _docx_text(data)

    for token in forbidden:
        assert token not in blob, token
    assert gen.INSTITUTION in blob


# --------------------------------------------------------------------------- real parsers and detector


def test_cli_files_parse_and_split_into_every_regulation(tmp_path: Path) -> None:
    code = gen.main([
        "--out-dir", str(tmp_path), "--regulations", str(SMALL), "--seed", "31", "--formats", "docx,pdf,hwpx",
    ])
    assert code == 0

    manifest = _truth((tmp_path / "manifest.json").read_bytes())
    assert manifest["regulations"] == SMALL
    assert {f["format"] for f in manifest["files"]} == {"docx", "pdf", "hwpx"}

    for item in manifest["files"]:
        fmt = item["format"]
        path = tmp_path / item["path"]
        truth_path = tmp_path / item["ground_truth"]
        assert path.name == f"combined_book.{fmt}"
        assert gen.sha256_bytes(path.read_bytes()) == item["sha256"]
        assert gen.sha256_bytes(truth_path.read_bytes()) == item["ground_truth_sha256"]
        truth = _truth(truth_path.read_bytes())
        assert truth["format"] == fmt and truth["source_file"] == path.name

        parsed = get_parser(path).parse(path, f"doc_{fmt}")
        normalized = TextNormalizer().normalize_document(parsed)
        compact_text = _compact("\n".join(b.text for p in normalized.pages for b in p.blocks))
        if fmt == "pdf":
            assert len(parsed.pages) == truth["layout"]["pdf_page_count"] > 1
        for reg in truth["regulations"]:
            assert _compact(f"{reg['number']}. {reg['title']}") in compact_text
            for sample in reg["sample_articles"]:
                assert sample["text_compact"] in compact_text, (fmt, reg["title"], sample["label"])

        nodes = StructureDetector().detect(normalized)
        assert normalized.metadata.get(STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY) is None
        assert [n.title for n in nodes if n.node_type == "regulation"] == [r["title"] for r in truth["regulations"]]


def test_default_docx_splits_into_48_regulations_with_every_article(tmp_path: Path, default_docx: tuple[bytes, dict]) -> None:
    data, truth = default_docx
    path = tmp_path / "combined_book.docx"
    path.write_bytes(data)

    normalized = TextNormalizer().normalize_document(get_parser(path).parse(path, "doc_default"))
    nodes = StructureDetector().detect(normalized)
    by_id = {n.node_id: n for n in nodes}

    def owner(node):
        in_addendum = False
        while node is not None and node.node_type != "regulation":
            in_addendum = in_addendum or node.node_type == "supplementary"
            node = by_id.get(node.parent_id)
        return (node, in_addendum)

    detected: dict[str, list[str]] = {}
    for node in nodes:
        if node.node_type != "article":
            continue
        regulation, in_addendum = owner(node)
        if regulation is not None and not in_addendum:
            detected.setdefault(regulation.title, []).append(node.number)

    assert normalized.metadata.get(STRUCTURE_BOUNDARY_DIAGNOSTIC_METADATA_KEY) is None
    assert [n.title for n in nodes if n.node_type == "regulation"] == [r["title"] for r in truth["regulations"]]
    for reg in truth["regulations"]:
        assert detected[reg["title"]] == [a["label"] for a in reg["articles"]], reg["title"]
