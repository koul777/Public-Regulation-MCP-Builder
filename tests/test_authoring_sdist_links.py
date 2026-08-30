from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
README_DOC_LINK_RE = re.compile(r"\((docs/(?!assets/)[^)#?]+)\)")
AUTHORING_DOC_LINK_RE = re.compile(
    r"\((?!https?://|#)([^)#?]+\.md)(?:#[^)]*)?\)"
)
PUBLIC_ASSET_PREFIX = (
    "https://raw.githubusercontent.com/koul777/"
    "Public-Regulation-MCP-Builder/main/docs/assets/"
)
README_PUBLIC_ASSET_RE = re.compile(
    re.escape(PUBLIC_ASSET_PREFIX) + r"([A-Za-z0-9._-]+)"
)


class AuthoringSdistLinkTests(unittest.TestCase):
    def test_manifest_includes_every_relative_readme_doc_link(self) -> None:
        referenced_docs = sorted(
            {
                match.group(1)
                for match in README_DOC_LINK_RE.finditer(
                    (REPO_ROOT / "README.md").read_text(encoding="utf-8")
                )
            }
        )
        self.assertTrue(referenced_docs)
        manifest_lines = {
            line.strip()
            for line in (REPO_ROOT / "MANIFEST.in")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        missing = [
            path for path in referenced_docs if f"include {path}" not in manifest_lines
        ]
        self.assertEqual([], missing)

    def test_manifest_includes_every_relative_authoring_doc_link(self) -> None:
        manifest_lines = {
            line.strip()
            for line in (REPO_ROOT / "MANIFEST.in")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        missing_files: list[str] = []
        missing_manifest_entries: list[str] = []

        for source in sorted((REPO_ROOT / "docs").glob("authoring*.md")):
            for match in AUTHORING_DOC_LINK_RE.finditer(
                source.read_text(encoding="utf-8")
            ):
                target = (source.parent / match.group(1)).resolve(strict=False)
                try:
                    relative = target.relative_to(REPO_ROOT).as_posix()
                except ValueError:
                    missing_files.append(f"{source.name}:outside:{match.group(1)}")
                    continue
                if not target.is_file():
                    missing_files.append(f"{source.name}:{relative}")
                if f"include {relative}" not in manifest_lines:
                    missing_manifest_entries.append(f"{source.name}:{relative}")

        self.assertEqual([], missing_files)
        self.assertEqual([], missing_manifest_entries)

    def test_readme_media_uses_public_absolute_urls_without_bloating_sdist(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("docs/assets/", readme.replace(PUBLIC_ASSET_PREFIX, ""))
        linked_assets = sorted(set(README_PUBLIC_ASSET_RE.findall(readme)))
        self.assertTrue(linked_assets)
        missing = [
            filename
            for filename in linked_assets
            if not (REPO_ROOT / "docs" / "assets" / filename).is_file()
        ]
        self.assertEqual([], missing)

        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertNotIn("docs/assets", manifest)


if __name__ == "__main__":
    unittest.main()
