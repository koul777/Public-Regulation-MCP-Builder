from __future__ import annotations

from app.parsers.base import ParserError


_UNSAFE_XML_DECLARATION_TEXT = ("<!doctype", "<!entity")
_XML_DECLARATION_ENCODINGS = (
    "ascii",
    "utf-16-le",
    "utf-16-be",
    "utf-32-le",
    "utf-32-be",
)
_UNSAFE_XML_DECLARATIONS = tuple(
    marker.encode(encoding)
    for marker in _UNSAFE_XML_DECLARATION_TEXT
    for encoding in _XML_DECLARATION_ENCODINGS
)

_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF32_LE_XML_PREFIX = b"<\x00\x00\x00"
_UTF32_BE_XML_PREFIX = b"\x00\x00\x00<"
_UTF16_LE_XML_PREFIX = b"<\x00"
_UTF16_BE_XML_PREFIX = b"\x00<"


def elementtree_xml_input(payload: bytes) -> bytes | str:
    """Strictly decode XML encodings unsupported by ``ElementTree`` bytes input.

    CPython's Expat binding can reject otherwise-valid XML when an explicit
    endian-specific UTF-16/32 declaration is supplied as bytes.  Detect the
    XML-standard BOM or opening-byte signature and pass strict Unicode text to
    ``ElementTree`` instead.  UTF-8 and other byte streams remain under the XML
    parser's own declared-encoding validation.
    """

    if payload.startswith(_UTF32_LE_BOM) or payload.startswith(_UTF32_BE_BOM):
        return payload.decode("utf-32")
    if payload.startswith(_UTF16_LE_BOM) or payload.startswith(_UTF16_BE_BOM):
        return payload.decode("utf-16")
    if payload.startswith(_UTF32_LE_XML_PREFIX):
        return payload.decode("utf-32-le")
    if payload.startswith(_UTF32_BE_XML_PREFIX):
        return payload.decode("utf-32-be")
    if payload.startswith(_UTF16_LE_XML_PREFIX):
        return payload.decode("utf-16-le")
    if payload.startswith(_UTF16_BE_XML_PREFIX):
        return payload.decode("utf-16-be")
    return payload


def reject_unsafe_xml_declarations(payload: bytes, *, format_name: str) -> None:
    """Reject encoded DTD/entity declarations before stdlib XML parsing.

    XML processors can auto-detect UTF-16 and UTF-32 payloads.  Scan those
    representations as well as ASCII-compatible XML so a byte-level guard
    cannot be bypassed merely by changing the document encoding.
    """

    lowered = bytes(payload).lower()
    if any(marker in lowered for marker in _UNSAFE_XML_DECLARATIONS):
        raise ParserError(
            f"Unsafe {format_name} XML: DTD and entity declarations are not supported."
        )
