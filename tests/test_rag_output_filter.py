from __future__ import annotations

import unittest

from app.rag.output_filter import sanitize_rag_answer


class RagOutputFilterTests(unittest.TestCase):
    def test_redacts_local_paths_and_secret_like_values(self) -> None:
        windows_path = "C:" + "\\Users\\dd\\secret.pdf"
        unc_path = "\\\\" + "server\\share\\secret.docx"
        app_path = "/app" + "/data/private/result.json"
        workspace_path = "/workspace" + "/Rag/data/private.csv"
        answer = (
            f"windows {windows_path} "
            f"unc {unc_path} "
            f"{app_path} "
            f"{workspace_path} "
            "API_KEY=abc123 bearer abcdefghijklmnop"
        )

        sanitized = sanitize_rag_answer(answer)

        self.assertIn("[local-path-redacted]", sanitized)
        self.assertIn("[secret-redacted]", sanitized)
        self.assertNotIn("C:" + "\\Users", sanitized)
        self.assertNotIn("\\\\" + "server\\share", sanitized)
        self.assertNotIn("/app" + "/data", sanitized)
        self.assertNotIn("/workspace" + "/Rag", sanitized)
        self.assertNotIn("abc123", sanitized)
        self.assertNotIn("abcdefghijklmnop", sanitized)

    def test_redacts_structured_secret_like_values(self) -> None:
        answer = (
            '{"api_key":"json-secret","access_token": "token-secret"} '
            "password: 'yaml-secret' secret = \"config-secret\" refresh_token: refresh-secret"
        )

        sanitized = sanitize_rag_answer(answer)

        self.assertIn("[secret-redacted]", sanitized)
        self.assertNotIn("json-secret", sanitized)
        self.assertNotIn("token-secret", sanitized)
        self.assertNotIn("yaml-secret", sanitized)
        self.assertNotIn("config-secret", sanitized)
        self.assertNotIn("refresh-secret", sanitized)

    def test_redacts_common_unix_sensitive_paths(self) -> None:
        sanitized = sanitize_rag_answer("see /etc/passwd and ~/.ssh/id_rsa")

        self.assertIn("[local-path-redacted]", sanitized)
        self.assertNotIn("/etc/passwd", sanitized)
        self.assertNotIn("~/.ssh/id_rsa", sanitized)


if __name__ == "__main__":
    unittest.main()
