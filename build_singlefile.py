#!/usr/bin/env python3
"""Inline a site's HTML, local assets, and linked CDN CSS/JS into one HTML file.

Usage (from the repository root, with internet available for CDN downloads):
    python3 build_singlefile.py index.html -o Hype-single.html
    python3 build_singlefile.py unblock/index.html -o Hype-unblock-single.html
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def fetch(url: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "singlefile-builder/1.0"})
    with urlopen(request, timeout=30) as response:
        data = response.read()
        content_type = response.headers.get_content_type()
        return data, content_type


def local_file(url: str, document_dir: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        raise ValueError(f"Not a local URL: {url}")
    path = unquote(parsed.path)
    candidate = (ROOT / path.lstrip("/")) if path.startswith("/") else (document_dir / path)
    candidate = candidate.resolve()
    if candidate != ROOT and ROOT not in candidate.parents:
        raise ValueError(f"Local asset escapes repository: {url}")
    return candidate


def resource(url: str, base_url: str, document_dir: Path) -> tuple[bytes, str, str]:
    absolute = urljoin(base_url, url) if base_url else url
    parsed = urlparse(absolute)
    if parsed.scheme in ("http", "https"):
        data, content_type = fetch(absolute)
    elif parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    elif not parsed.scheme:
        path = local_file(absolute, document_dir)
        data = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    else:
        raise ValueError(f"Unsupported resource URL: {absolute}")
    return data, content_type, absolute


def data_uri(data: bytes, content_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
IMPORT_RE = re.compile(r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?\s*([^;]*);", re.IGNORECASE | re.DOTALL)


def inline_css(css: str, base_url: str, document_dir: Path, seen: set[str]) -> str:
    def import_replacer(match: re.Match[str]) -> str:
        url = match.group(2).strip()
        media = match.group(3).strip()
        try:
            _, _, absolute = resource(url, base_url, document_dir)
            if absolute in seen:
                return ""
            seen.add(absolute)
            raw, _, _ = resource(url, base_url, document_dir)
            imported = inline_css(raw.decode("utf-8", "replace"), absolute, document_dir, seen)
            return f"\n@media {media} {{\n{imported}\n}}\n" if media else f"\n{imported}\n"
        except Exception as exc:
            print(f"Warning: could not inline CSS import {url!r}: {exc}", file=sys.stderr)
            return match.group(0)

    css = IMPORT_RE.sub(import_replacer, css)

    def url_replacer(match: re.Match[str]) -> str:
        url = match.group(2).strip()
        if not url or url.startswith(("data:", "#", "blob:", "javascript:")):
            return match.group(0)
        try:
            raw, content_type, _ = resource(url, base_url, document_dir)
            return f"url('{data_uri(raw, content_type)}')"
        except Exception as exc:
            print(f"Warning: could not inline CSS URL {url!r}: {exc}", file=sys.stderr)
            return match.group(0)

    return URL_RE.sub(url_replacer, css)


ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", re.DOTALL)


def attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): next(v for v in m.groups()[1:] if v is not None) for m in ATTR_RE.finditer(tag)}


def replace_attr(tag: str, name: str, value: str | None) -> str:
    pattern = re.compile(rf"\s+{re.escape(name)}\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
    tag = pattern.sub("", tag)
    if value is not None:
        end = tag.rfind("/") if tag.rstrip().endswith("/>") else tag.rfind(">")
        tag = tag[:end] + f' {name}="{value}"' + tag[end:]
    return tag


def safe_script(text: str) -> str:
    # Prevent an embedded source file from prematurely closing its HTML script tag.
    return re.sub(r"</script", r"<\\/script", text, flags=re.IGNORECASE)


def build(source: Path, output: Path) -> None:
    source = source.resolve()
    if source != ROOT and ROOT not in source.parents:
        raise ValueError("Input HTML must be inside the repository")
    html = source.read_text(encoding="utf-8")
    base_url = source.as_uri()
    doc_dir = source.parent

    # Repair the premature </html> found in the repository pages.
    html = re.sub(r"</html\s*>\s*(?=<body\b)", "", html, flags=re.IGNORECASE)

    tag_re = re.compile(r"<(script|link|img|source)\b[^>]*>|<(\w+)\b[^>]*\sstyle\s*=\s*(?:\"[^\"]*\"|'[^']*')[^>]*>", re.IGNORECASE | re.DOTALL)

    def replace_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        name = (match.group(1) or match.group(2) or "").lower()
        attributes = attrs(tag)

        if name == "script" and attributes.get("src"):
            src = attributes["src"]
            try:
                raw, _, absolute = resource(src, base_url, doc_dir)
                code = raw.decode("utf-8", "replace")
                # Preserve the inline script's execution order; external-only attributes no longer apply.
                for attr in ("src", "async", "defer", "integrity", "crossorigin", "referrerpolicy"):
                    tag = replace_attr(tag, attr, None)
                opening = tag[:tag.find(">") + 1]
                return opening + safe_script(code) + "</script>"
            except Exception as exc:
                print(f"Warning: could not inline script {src!r}: {exc}", file=sys.stderr)
                return tag

        rel = set(attributes.get("rel", "").lower().split())
        if name == "link" and attributes.get("href") and ("stylesheet" in rel):
            href = attributes["href"]
            try:
                raw, _, absolute = resource(href, base_url, doc_dir)
                css = inline_css(raw.decode("utf-8", "replace"), absolute, doc_dir, {absolute})
                return "<style>\n" + css + "\n</style>"
            except Exception as exc:
                print(f"Warning: could not inline stylesheet {href!r}: {exc}", file=sys.stderr)
                return tag

        if name == "link" and attributes.get("href") and ("icon" in rel or "manifest" in rel):
            href = attributes["href"]
            try:
                raw, content_type, _ = resource(href, base_url, doc_dir)
                return replace_attr(tag, "href", data_uri(raw, content_type))
            except Exception as exc:
                print(f"Warning: could not inline linked asset {href!r}: {exc}", file=sys.stderr)
                return tag

        if name in ("img", "source") and attributes.get("src"):
            src = attributes["src"]
            try:
                raw, content_type, _ = resource(src, base_url, doc_dir)
                tag = replace_attr(tag, "src", data_uri(raw, content_type))
            except Exception as exc:
                print(f"Warning: could not inline image {src!r}: {exc}", file=sys.stderr)

        # Inline CSS URLs inside style attributes too.
        style_match = re.search(r"\sstyle\s*=\s*(\"[^\"]*\"|'[^']*')", tag, re.IGNORECASE | re.DOTALL)
        if style_match:
            quoted = style_match.group(1)
            css = quoted[1:-1]
            css = inline_css(css, base_url, doc_dir, set())
            tag = tag[:style_match.start(1)] + '"' + css.replace('"', '&quot;') + '"' + tag[style_match.end(1):]
        return tag

    html = tag_re.sub(replace_tag, html)
    if not re.search(r"</html\s*>", html, re.IGNORECASE):
        html = html.rstrip() + "\n</html>\n"
    output.write_text(html, encoding="utf-8")
    print(f"Wrote {output} ({output.stat().st_size:,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="index.html", help="input HTML page (default: index.html)")
    parser.add_argument("-o", "--output", default="Hype-single.html", help="output HTML filename")
    args = parser.parse_args()
    build((ROOT / args.input), (ROOT / args.output))


if __name__ == "__main__":
    main()
