from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PublicGithubReleaseChecklistTests(unittest.TestCase):
    def test_checklist_keeps_mcp_bundle_zip_handoff_command(self) -> None:
        text = (REPO_ROOT / "docs" / "public_github_release_checklist_ko.md").read_text(encoding="utf-8")

        self.assertIn("--out-dir reports/mcp_connection_bundle", text)
        self.assertIn("--zip-out reports/mcp_connection_bundle.zip", text)
        self.assertIn("--include-wheel", text)
        self.assertIn("--bundle-dir reports/mcp_connection_bundle", text)
        self.assertIn("reg-rag-check-console-scripts", text)
        self.assertIn("reg-rag-public-release-gate", text)
        self.assertIn("reg-rag-fresh-clone-rehearsal", text)
        self.assertIn("--execute-harness", text)
        self.assertIn("reports/mcp_connection_readiness_current.json", text)
        self.assertIn("mcp_connection_readiness=reports/mcp_connection_readiness_current.json", text)
        self.assertIn("--authority-manifest reports/mcp_readiness_authority_current.json", text)
        self.assertIn("--evidence-verification-report reports/mcp_product_readiness_release_evidence_verification_current.json", text)
        self.assertIn("approval_journal", text)
        self.assertIn("reg-rag-release-evidence-index --profile mcp-product-readiness", text)
        self.assertIn("--probe-public-url", text)
        self.assertIn("ChatGPT HTTPS/tunnel doctor", text)
        self.assertIn("--client-profile chatgpt-remote", text)

    def test_readme_keeps_authority_backed_product_release_chain(self) -> None:
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("사람에게 승인되지 않은 청크", text)
        self.assertIn("승인된 규정만 MCP 데이터로 생성", text)
        self.assertIn("MCP로 쓸 파일 묶음 만들기", text)
        self.assertIn("MCP HTTP - URL로 연결", text)
        self.assertIn("OpenAI Secure MCP Tunnel", text)
        self.assertIn("run_openai_secure_tunnel.ps1", text)
        self.assertNotIn("Cloudflare Named Tunnel", text)


if __name__ == "__main__":
    unittest.main()
