from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PublicRepositoryHistoryPolicyTests(unittest.TestCase):
    def test_history_policy_requires_clean_history_publication(self):
        policy = (PROJECT_ROOT / "docs" / "public_repository_history_policy_ko.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("orphan", policy.lower())
        self.assertIn("private", policy.lower())
        self.assertIn("visibility", policy.lower())

    def test_readme_links_history_policy(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/public_repository_history_policy_ko.md", readme)


if __name__ == "__main__":
    unittest.main()
