"""Lightweight HTML fixture helpers for provider regression tests."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


class _FixtureHTMLParser(HTMLParser):
    """Parse HTML into a small searchable representation."""

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str]]] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes.append((tag.lower(), {name.lower(): value or "" for name, value in attrs}))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.text_parts.append(data.strip())


class FixtureLocator:
    """Minimal async locator used by provider tests."""

    def __init__(self, count: int):
        self._count = count

    async def count(self) -> int:
        return self._count


class FixturePage:
    """Simple page/frame stand-in that can answer locator.count() against fixture HTML."""

    def __init__(self, html: str, *, title: str = "", url: str = "https://example.test/fixture"):
        parser = _FixtureHTMLParser()
        parser.feed(html)
        parser.close()
        self._nodes = parser.nodes
        self._text = " ".join(parser.text_parts)
        self._title = title
        self.url = url

    @classmethod
    def from_file(cls, path: Path, *, title: str = "", url: str = "https://example.test/fixture") -> FixturePage:
        return cls(path.read_text(encoding="utf-8"), title=title, url=url)

    def locator(self, selector: str) -> FixtureLocator:
        return FixtureLocator(self._count_selector(selector.strip()))

    async def title(self) -> str:
        return self._title

    def _count_selector(self, selector: str) -> int:
        if selector.startswith('text="') and selector.endswith('"'):
            needle = selector[len('text="') : -1]
            return 1 if needle.lower() in self._text.lower() else 0

        if selector.startswith(':text("') and selector.endswith('")'):
            needle = selector[len(':text("') : -2]
            return 1 if needle.lower() in self._text.lower() else 0

        if selector.startswith("#"):
            needle = selector[1:]
            return sum(1 for _tag, attrs in self._nodes if attrs.get("id") == needle)

        if selector.startswith("."):
            class_name = selector[1:]
            return sum(1 for _tag, attrs in self._nodes if class_name in attrs.get("class", "").split())

        return sum(1 for tag, attrs in self._nodes if self._matches_compound_selector(tag, attrs, selector))

    def _matches_compound_selector(self, tag: str, attrs: dict[str, str], selector: str) -> bool:
        tag_name: str | None = None
        if selector and selector[0].isalnum():
            match = re.match(r"^[a-zA-Z0-9_-]+", selector)
            if not match:
                return False
            tag_name = match.group(0).lower()
            selector = selector[match.end() :]

        attr_segments = re.findall(r"\[[^\]]+\]", selector)
        if tag_name and tag != tag_name:
            return False

        for segment in attr_segments:
            body = segment[1:-1].strip()
            contains_match = re.match(r'^([a-zA-Z0-9:_-]+)\*="([^"]+)"(?:\s+i)?$', body)
            if contains_match:
                attr_name, expected = contains_match.groups()
                actual = attrs.get(attr_name.lower(), "")
                if expected.lower() not in actual.lower():
                    return False
                continue

            exact_match = re.match(r'^([a-zA-Z0-9:_-]+)="([^"]+)"$', body)
            if exact_match:
                attr_name, expected = exact_match.groups()
                if attrs.get(attr_name.lower()) != expected:
                    return False
                continue

            presence_match = re.match(r"^([a-zA-Z0-9:_-]+)$", body)
            if presence_match:
                if presence_match.group(1).lower() not in attrs:
                    return False
                continue

            return False

        return bool(tag_name or attr_segments)
