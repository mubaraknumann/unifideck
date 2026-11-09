"""launcher/xbox_pc/xml_lite.py — a small XML reader for Xbox PC manifests.

Decky's frozen Python does not ship the stdlib ``xml`` package, so a plugin
module that imports ``xml.etree`` fails to load and takes the whole plugin
down with it. This module reads the two documents we need, the game's
``MicrosoftGame.config`` and the update service's reply, with ``re`` only.

It covers elements, attributes, text, comments, processing instructions,
CDATA and the five named entities plus numeric references. A DOCTYPE is
refused, so no entity can be declared or expanded. Namespace prefixes are
dropped from tag names (``gdk:Game`` reads as ``Game``).

Stdlib only, Python 3.10 safe. It runs in the plugin and in the launcher.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

_NAME = r"[A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?"
_TOKEN_RE = re.compile(
    r"<!--.*?-->"
    r"|<\?.*?\?>"
    r"|<!\[CDATA\[(?P<cdata>.*?)\]\]>"
    r"|<!(?P<decl>[A-Za-z])"
    rf"|</(?P<close>{_NAME})\s*>"
    rf"|<(?P<open>{_NAME})(?P<attrs>(?:\s+{_NAME}\s*=\s*(?:\"[^\"<]*\"|'[^'<]*'))*)\s*(?P<empty>/?)>"
    r"|<",
    re.DOTALL,
)
_ATTR_RE = re.compile(rf"({_NAME})\s*=\s*(?:\"([^\"<]*)\"|'([^'<]*)')")
_ENTITY_RE = re.compile(r"&(?:#x([0-9A-Fa-f]+)|#([0-9]+)|(amp|lt|gt|quot|apos));")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


class ParseError(ValueError):
    """The text is not XML this reader accepts."""


class Element:
    """One element: local tag name, attributes, text and children."""

    __slots__ = ("attrib", "children", "tag", "text")

    def __init__(self, tag: str, attrib: dict[str, str]) -> None:
        self.tag = tag
        self.attrib = attrib
        self.text = ""
        self.children: list[Element] = []

    def get(self, name: str, default: str | None = None) -> str | None:
        """An attribute's value, or ``default``."""
        return self.attrib.get(name, default)

    def iter(self) -> Iterator[Element]:
        """This element and every descendant, in document order."""
        yield self
        for child in self.children:
            yield from child.iter()

    def find(self, tag: str) -> Element | None:
        """The first direct child named ``tag``, or None."""
        return next((c for c in self.children if c.tag == tag), None)


def _local(name: str) -> str:
    """A tag or attribute name without its namespace prefix."""
    return name.rsplit(":", 1)[-1]


def _unescape(text: str) -> str:
    """``text`` with entity references replaced. Raises on a bare ``&``."""
    if "&" not in _ENTITY_RE.sub("", text):
        return _ENTITY_RE.sub(_entity, text)
    raise ParseError("undefined entity or bare '&'")


def _entity(match: re.Match[str]) -> str:
    hex_code, dec_code, named = match.groups()
    if named:
        return _NAMED[named]
    code = int(hex_code, 16) if hex_code else int(dec_code)
    try:
        return chr(code)
    except (ValueError, OverflowError) as err:
        raise ParseError(f"bad character reference {match.group(0)}") from err


def _attributes(raw: str) -> dict[str, str]:
    """The attributes of one start tag, prefixes dropped."""
    attrib: dict[str, str] = {}
    for name, double, single in _ATTR_RE.findall(raw):
        attrib[_local(name)] = _unescape(double or single)
    return attrib


def _add_text(stack: list[Element], text: str) -> None:
    """Append character data to the open element (whitespace outside is fine)."""
    if stack:
        stack[-1].text += text
    elif text.strip():
        raise ParseError("text outside the root element")


def _open(stack: list[Element], root: Element | None, match: re.Match[str]) -> Element | None:
    """Start an element. Returns the root once it is known."""
    if root is not None and not stack:
        raise ParseError("more than one root element")
    element = Element(_local(match.group("open")), _attributes(match.group("attrs")))
    if stack:
        stack[-1].children.append(element)
    if not match.group("empty"):
        stack.append(element)
    return root or element


def _close(stack: list[Element], name: str) -> None:
    """End the open element, which must be named ``name``."""
    if not stack or stack[-1].tag != _local(name):
        raise ParseError(f"unexpected </{name}>")
    stack.pop()


def _consume(stack: list[Element], root: Element | None, match: re.Match[str]) -> Element | None:
    """Apply one markup token. Comments and processing instructions are skipped."""
    if match.group("decl"):
        raise ParseError("DOCTYPE and other declarations are not accepted")
    if match.group("open"):
        return _open(stack, root, match)
    if match.group("close"):
        _close(stack, match.group("close"))
    elif match.group("cdata") is not None:
        _add_text(stack, match.group("cdata"))
    elif match.group(0) == "<":
        raise ParseError(f"malformed markup at offset {match.start()}")
    return root


def fromstring(text: str) -> Element:
    """The root element of ``text``. Raises :class:`ParseError`."""
    stack: list[Element] = []
    root: Element | None = None
    pos = 0
    for match in _TOKEN_RE.finditer(text):
        _add_text(stack, _unescape(text[pos:match.start()]))
        pos = match.end()
        root = _consume(stack, root, match)
    _add_text(stack, _unescape(text[pos:]))
    if root is None or stack:
        raise ParseError("no root element" if root is None else f"<{stack[-1].tag}> is not closed")
    return root


def _decode(data: bytes) -> str:
    """Bytes as text: a UTF-16 BOM wins, else UTF-8 (with or without BOM)."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise ParseError(f"not UTF-8 or UTF-16: {err}") from err


def parse_file(path: Path) -> Element:
    """The root element of the file at ``path``. Raises ``OSError`` or :class:`ParseError`."""
    return fromstring(_decode(path.read_bytes()))
