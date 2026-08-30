from __future__ import annotations

import unittest

from app.parsers.base import ParserError
from app.parsers.xml_safety import elementtree_xml_input, reject_unsafe_xml_declarations


class XmlSafetyTests(unittest.TestCase):
    def test_allows_benign_xml_in_supported_encodings(self) -> None:
        xml = '<?xml version="1.0"?><root><item>안전</item></root>'
        for encoding in ("utf-8", "utf-16", "utf-16-be", "utf-32", "utf-32-be"):
            with self.subTest(encoding=encoding):
                payload = xml.encode(encoding)
                reject_unsafe_xml_declarations(payload, format_name="test")
                normalized = elementtree_xml_input(payload)
                self.assertNotIn("\ufffd", normalized if isinstance(normalized, str) else "")

    def test_rejects_utf16_doctype_and_utf32_entity(self) -> None:
        payloads = (
            '<!DOCTYPE root SYSTEM "blocked"><root/>'.encode("utf-16"),
            '<!DOCTYPE root SYSTEM "blocked"><root/>'.encode("utf-16-be"),
            '<!ENTITY injected "blocked"><root/>'.encode("utf-32"),
            '<!ENTITY injected "blocked"><root/>'.encode("utf-32-be"),
        )
        for payload in payloads:
            with self.assertRaisesRegex(ParserError, "DTD and entity declarations"):
                reject_unsafe_xml_declarations(payload, format_name="test")


if __name__ == "__main__":
    unittest.main()
