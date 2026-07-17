from __future__ import annotations

import ast
import unittest
from pathlib import Path


class FrontendUploadTypesTests(unittest.TestCase):
    def test_streamlit_uploader_accepts_public_institution_hwp_formats(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        upload_types: set[str] = set()
        accepts_multiple = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "file_uploader":
                continue
            if (
                not node.args
                or not isinstance(node.args[0], ast.Constant)
                or node.args[0].value not in {"Upload a regulation document", "문서 업로드"}
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg == "type" and isinstance(keyword.value, ast.List):
                    upload_types.update(
                        item.value
                        for item in keyword.value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
                if keyword.arg == "accept_multiple_files" and isinstance(keyword.value, ast.Constant):
                    accepts_multiple = bool(keyword.value.value)

        self.assertEqual({"pdf", "docx", "hwpx", "hwp"}, upload_types)
        self.assertTrue(accepts_multiple)

    def test_streamlit_upload_documents_large_batch_limits(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")

        self.assertIn("_uploaded_file_size", source)
        self.assertIn("selected_upload_bytes", source)
        self.assertIn("max_batch_upload_mb", source)
        self.assertIn("max_batch_upload_files", source)
        self.assertIn("progress_callback=_upload_progress", source)
        self.assertIn("_render_upload_file_progress", source)
        self.assertIn("file_status_rows", source)
        self.assertIn("status_label=\"탑재 중\"", source)
        self.assertIn("status_label=\"전처리 중\"", source)
        self.assertIn("status_label=\"완료\"", source)
        self.assertIn('data-testid="stFileUploader"', source)
        self.assertIn("_render_selected_upload_files", source)
        self.assertIn("드롭이 성공하면 아래에 파일명이 바로 표시됩니다", source)
        self.assertIn("_persist_pending_upload", source)
        self.assertIn("pending_uploads", source)
        self.assertIn("저장된 대기 파일", source)
        self.assertIn("selected_pending_paths", source)

        config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
        self.assertIn("maxUploadSize = 1000", config)

        env_example = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("MAX_UPLOAD_MB=1000", env_example)
        self.assertIn("MAX_BATCH_UPLOAD_MB=1000", env_example)
        self.assertIn("MAX_BATCH_UPLOAD_FILES=100", env_example)

    def test_streamlit_upload_uses_streaming_path(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        called_attrs = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        self.assertIn("upload_stream", called_attrs)
        self.assertNotIn("getvalue", called_attrs)

    def test_streamlit_upload_sets_local_operator_tenant(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        tenant_keyword_found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "upload_stream":
                continue
            for keyword in node.keywords:
                if keyword.arg != "tenant_id":
                    continue
                tenant_keyword_found = (
                    isinstance(keyword.value, ast.Call)
                    and isinstance(keyword.value.func, ast.Name)
                    and keyword.value.func.id == "_local_operator_tenant_id"
                )

        self.assertTrue(tenant_keyword_found)
        self.assertIn('document.model_copy(update={"tenant_id": document_tenant_id})', source)
        self.assertIn("JsonRepository(settings).get_document(document_id) is None", source)

    def test_streamlit_upload_exposes_institution_profile_metadata_controls(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported_names: set[str] = set()
        control_labels: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "app.core.institution_profiles":
                imported_names.update(alias.name for alias in node.names)
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"selectbox", "text_input"}:
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                control_labels.add(node.args[0].value)

        self.assertIn("load_institution_profile_registry", imported_names)
        self.assertIn("apply_institution_profile_to_metadata", imported_names)
        self.assertIn("load_institution_profile_registry_from_bytes", imported_names)
        self.assertIn("save_institution_profile_registry", imported_names)
        self.assertIn("upsert_institution_profile", imported_names)
        expected_label_sets = [
            {
                "profile_id",
                "institution_name",
                "source_system",
                "source_url",
                "source_record_id",
                "source_file_id",
                "source_disclosure_date",
                "source_posted_date",
            },
            {
                "프로필 ID",
                "기관 프로필 ID",
                "기관명",
                "출처 시스템",
                "출처 URL",
                "출처 레코드 ID",
                "출처 파일 ID",
                "공개일",
                "게시일",
            },
        ]
        self.assertTrue(any(expected.issubset(control_labels) for expected in expected_label_sets))

    def test_streamlit_can_upload_institution_profile_registry_json(self) -> None:
        source = Path("frontend/streamlit_app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        uploader_keys: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "file_uploader":
                continue
            for keyword in node.keywords:
                if keyword.arg == "key" and isinstance(keyword.value, ast.Constant):
                    uploader_keys.add(str(keyword.value.value))

        self.assertIn("institution_profile_registry_upload", uploader_keys)


if __name__ == "__main__":
    unittest.main()
