#!/usr/bin/env python3
"""Check the static landing for broken local links, anchors and import deep links.

Run from the landing repository root:

    python scripts/check_landing_links.py

Only the standard library is used, so CI does not need a requirements file.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
BOT_HOST = "t.me"
BOT_PATH = "/smartfavoritebot"
IMPORT_SOURCES = {
    "instagram",
    "x",
    "reddit",
    "youtube",
    "bookmarks",
    "file",
    "links",
}
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "javascript:", "data:")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.anchors: set[str] = set()
        self.hreflang: list[str] = []
        self.canonical: str | None = None
        self.lang: str | None = None
        self.title = False
        self.h1 = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "html":
            self.lang = values.get("lang")
        elif tag == "title":
            self.title = True
        elif tag == "h1":
            self.h1 += 1
        elif tag == "a":
            if values.get("id"):
                self.anchors.add(values["id"])
            if values.get("name"):
                self.anchors.add(values["name"])
            if values.get("href"):
                self.links.append(("href", values["href"]))
        elif tag in {"img", "script", "link"}:
            key = "src" if tag in {"img", "script"} else "href"
            if values.get(key):
                self.links.append((key, values[key]))
            if tag == "link" and values.get("rel") == "canonical":
                self.canonical = values.get("href")
            if tag == "link" and values.get("rel") == "alternate" and values.get("hreflang"):
                self.hreflang.append(values["hreflang"])
        for key in ("id", "name"):
            if values.get(key) and tag not in {"a", "link", "meta"}:
                self.anchors.add(values[key])


def local_target(source: Path, raw: str) -> tuple[Path, str] | None:
    """Return the file a local URL points at and its fragment, or None for remote links."""
    value = raw.strip()
    if not value or value.startswith("#") or value.lower().startswith(SKIP_SCHEMES):
        return None
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if not path:
        return None
    candidate = (source.parent / path).resolve() if not path.startswith("/") else (ROOT / path.lstrip("/")).resolve()
    if candidate.is_dir():
        candidate = candidate / "index.html"
    return candidate, parsed.fragment


def check_deep_link(raw: str) -> str | None:
    parsed = urlparse(raw)
    if parsed.netloc.lower() not in {BOT_HOST, "telegram.me"}:
        return None
    if parsed.path.rstrip("/").lower() != BOT_PATH:
        return None
    query = parse_qs(parsed.query)
    start = (query.get("start") or [""])[0]
    if not start:
        return None
    if start == "import":
        return None
    if start.startswith("import_"):
        source = start[len("import_") :]
        if source in IMPORT_SOURCES:
            return None
        return f"unknown import source {source!r}"
    if start.startswith("review_"):
        value = start[len("review_") :]
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
            return None
        return f"malformed review id {value!r}"
    return f"unknown start parameter {start!r}"


def main() -> int:
    errors: list[str] = []
    pages = sorted(ROOT.rglob("*.html"))
    if not pages:
        print("no HTML pages found", file=sys.stderr)
        return 1

    parsed_pages: dict[Path, PageParser] = {}
    for page in pages:
        if any(part.startswith(".") for part in page.relative_to(ROOT).parts):
            continue
        parser = PageParser()
        parser.feed(page.read_text(encoding="utf-8"))
        parsed_pages[page] = parser
        rel = page.relative_to(ROOT)
        if not parser.lang:
            errors.append(f"{rel}: <html> has no lang")
        if not parser.title:
            errors.append(f"{rel}: missing <title>")
        if parser.h1 != 1:
            errors.append(f"{rel}: expected exactly one <h1>, found {parser.h1}")
        if not parser.canonical:
            errors.append(f"{rel}: missing canonical link")
        counterpart = None
        if rel.parts[:2] == ("en", "import") and rel.name != "index.html":
            counterpart = ROOT / "import" / rel.name
        elif rel.parts[:1] == ("import",) and rel.name != "index.html":
            counterpart = ROOT / "en" / "import" / rel.name
        if counterpart is not None and counterpart.exists():
            if "ru" not in parser.hreflang or "en" not in parser.hreflang:
                errors.append(f"{rel}: missing ru/en hreflang")

    for page, parser in parsed_pages.items():
        rel = page.relative_to(ROOT)
        for kind, raw in parser.links:
            deep_link_error = check_deep_link(raw)
            if deep_link_error:
                errors.append(f"{rel}: {raw}: {deep_link_error}")
            target = local_target(page, raw)
            if target is None:
                continue
            path, fragment = target
            try:
                rel_path = path.relative_to(ROOT)
            except ValueError:
                errors.append(f"{rel}: {kind}={raw!r} -> path leaves the landing root")
                continue
            if not path.exists():
                errors.append(f"{rel}: {kind}={raw!r} -> missing {rel_path}")
                continue
            if fragment and path.suffix == ".html":
                anchors = parsed_pages.get(path)
                if anchors is None:
                    anchors = PageParser()
                    anchors.feed(path.read_text(encoding="utf-8"))
                    parsed_pages[path] = anchors
                if fragment not in anchors.anchors:
                    errors.append(f"{rel}: {kind}={raw!r} -> missing anchor #{fragment}")

    sitemap = ROOT / "sitemap.xml"
    if sitemap.exists():
        try:
            tree = ET.parse(sitemap)
        except ET.ParseError as exc:
            errors.append(f"sitemap.xml: {exc}")
        else:
            namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
            for loc in tree.iter(f"{namespace}loc"):
                url = (loc.text or "").strip()
                parsed = urlparse(url)
                path = unquote(parsed.path)
                base = "/smart-favorite-landing"
                if path.startswith(base):
                    path = path[len(base) :]
                path = path.strip("/")
                candidate = ROOT / path
                if candidate.is_dir():
                    candidate = candidate / "index.html"
                if not path:
                    candidate = ROOT / "index.html"
                if not candidate.exists():
                    errors.append(f"sitemap.xml: {url} -> missing {candidate.relative_to(ROOT)}")
    else:
        errors.append("sitemap.xml is missing")

    if errors:
        print(f"landing link check failed: {len(errors)} problem(s)", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"landing link check passed: {len(parsed_pages)} pages, {len(pages)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
