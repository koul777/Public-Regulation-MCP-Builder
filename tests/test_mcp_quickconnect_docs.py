from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class McpQuickConnectDocsTests(unittest.TestCase):
    def test_quickconnect_doc_lists_common_client_paths(self) -> None:
        text = (REPO_ROOT / "docs" / "mcp_quickconnect_ko.md").read_text(encoding="utf-8")

        self.assertIn("claude_desktop_config.json", text)
        self.assertIn("codex_config_snippet.toml", text)
        self.assertIn("chatgpt_desktop_local_mcp.json", text)
        self.assertIn("README.ko.md", text)
        self.assertIn("connect_mcp_client.ps1", text)
        self.assertIn("MCP 사용 시작하기.txt", text)
        self.assertIn("설치 후 MCP 사용 방법 보기.bat", text)
        self.assertIn("Codex 플러그인 MCP 입력값.txt", text)
        self.assertIn("ChatGPT Desktop에 연결하기.bat", text)
        self.assertIn("Codex에 연결하기.bat", text)
        self.assertIn("Claude Desktop에 연결하기.bat", text)
        self.assertIn("Claude Code에 연결하기.bat", text)
        self.assertIn("연결 상태 확인하기.bat", text)
        self.assertIn("@aks_mcp MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", text)
        self.assertIn("같은 이름으로 번들을 다시 생성", text)
        self.assertIn("install_local_package.ps1", text)
        self.assertIn("Write MCP setup bundle now", text)
        self.assertIn("--zip-out reports/mcp_connection_bundle.zip", text)
        self.assertIn("--include-wheel", text)
        self.assertIn("--bundle-dir reports/mcp_connection_bundle", text)
        self.assertIn("--probe-public-url", text)
        self.assertIn("approval_provenance_coverage", text)
        self.assertIn("approval_journal_coverage", text)
        self.assertIn("append-only approval journal coverage", text)
        self.assertIn("claude_code_add_stdio.ps1", text)
        self.assertIn("run_chatgpt_data_server.ps1", text)
        self.assertIn("run_openai_secure_tunnel.ps1", text)
        self.assertIn("validate_client_config_smoke.ps1", text)
        self.assertIn("run_mcp_client_config_smoke.py", text)
        self.assertIn("claude_api_fragment.json", text)
        self.assertIn("--connection-mode openai-tunnel", text)
        self.assertIn("`search`는 질문과 관련된 승인 규정 조항을 찾는 도구", text)
        self.assertIn("`fetch`는 `search` 결과의 `id`로 원문 근거", text)
        self.assertIn('Unexpected token "{", "m"', text)
        self.assertIn("-ValidateClaudeDesktop", text)
        self.assertIn("-InstallClaudeDesktop", text)
        self.assertIn("--codex-config", text)
        self.assertIn("$HOME\\.codex\\config.toml", text)
        self.assertIn("--flat-storage", text)
        self.assertIn("ready_for_local_claude_desktop_mvp", text)
        self.assertIn("--no-warm-cache", text)
        self.assertIn("full_profile_search_timing_ms", text)
        self.assertIn("smoke-test 문서만 보이면", text)
        self.assertIn("MCP-visible records", text)
        self.assertIn("Reindex approved chunks", text)
        self.assertIn("reg-rag-mcp-index-visibility", text)
        self.assertIn("approved chunks, indexed status, MCP-visible records, stale vector count", text)
        self.assertIn("draft command", text)
        self.assertIn("연결 전 게이트", text)
        self.assertIn("approved chunk 인덱싱", text)
        self.assertIn("UNREVIEWED_PREVIEW", text)
        self.assertIn("UNREVIEWED_POC_REVIEW", text)
        self.assertIn("approved vector", text)
        self.assertIn("정식 MCP handoff", text)
        self.assertIn("approval_review_batch_manifest", text)
        self.assertIn("Review batch ID to load", text)
        self.assertIn("approval_request_template.chunk_ids", text)
        self.assertIn("Approve selected review batch for RAG", text)
        self.assertIn("review_flags_acknowledged", text)
        self.assertIn("예열 후 AKS 첫 `search`는 약 143ms", text)

    def test_client_config_doc_explains_warm_cache_and_latency_fields(self) -> None:
        text = (REPO_ROOT / "docs" / "mcp_client_config_examples_ko.md").read_text(encoding="utf-8")

        self.assertIn("승인 Vector DB, 승인 스냅샷, BM25 인덱스", text)
        self.assertIn("--no-warm-cache", text)
        self.assertIn("codex_config_snippet.toml", text)
        self.assertIn("chatgpt_desktop_local_mcp.json", text)
        self.assertIn("Codex에 연결하기.bat", text)
        self.assertIn("ChatGPT Desktop에 연결하기.bat", text)
        self.assertIn("Claude Desktop에 연결하기.bat", text)
        self.assertIn("Claude Code에 연결하기.bat", text)
        self.assertIn("연결 상태 확인하기.bat", text)
        self.assertIn("@aksmcp MCP 연결 상태와 사용 가능한 규정 도구를 보여줘.", text)
        self.assertIn("--client-profile chatgpt-remote", text)
        self.assertIn("추가·개정 청크", text)
        self.assertIn("--codex-config", text)
        self.assertIn("--flat-storage", text)
        self.assertIn("search_elapsed_ms", text)
        self.assertIn("warm_search_elapsed_ms", text)
        self.assertIn("-ValidateClaudeDesktop", text)
        self.assertIn("smoke-test 문서만 보이거나", text)
        self.assertIn("reg-rag-mcp-index-visibility", text)
        self.assertIn("전처리, 승인, 인덱싱 때 사용한 tenant", text)
        self.assertIn("잘못된 data-dir 또는 tenant", text)

    def test_readme_links_quickconnect_doc(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("docs/mcp_quickconnect_ko.md", text)


if __name__ == "__main__":
    unittest.main()
