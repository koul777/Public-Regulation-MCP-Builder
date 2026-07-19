from __future__ import annotations

from app.parsers.base import ParserError


_UNSAFE_XML_DECLARATIONS = (b"<!doctype", b"<!entity")


def reject_unsafe_xml_declarations(payload: bytes, *, format_name: str) -> None:
    """Reject DTD/entity declarations before stdlib XML parsing."""

    lowered = bytes(payload).lower()
    if any(marker in lowered for marker in _UNSAFE_XML_DECLARATIONS):
        raise ParserError(
            f"Unsafe {format_name} XML: DTD and entity declarations are not supported."
        )
