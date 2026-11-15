"""Resolve image sources and link targets for a markdown document.

Images are looked up using three modes, tried in the configured order:

* ``relative`` - relative to the markdown file (``![x](./pic.png)``)
* ``document`` - in the document's own assets folder: the front-matter
  ``assets:`` key if present, else ``doc_assets_pattern`` (default ``{stem}``,
  i.e. ``post.md`` -> ``post/``)
* ``global``   - in the shared images folder for the whole tree

Root-absolute paths (``/images/a.png``) are tried against the global images
folder, then the tree root, then the real filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Config

URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
FRONT_ASSETS_RE = re.compile(r"^\s*assets\s*:\s*(.+?)\s*$", re.MULTILINE)
MD_SUFFIXES = {".md", ".markdown", ".mdown", ".mkd", ".mdx"}


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MD_SUFFIXES


def is_url(target: str) -> bool:
    # A Windows-ish "C:" is not relevant here; anything with a scheme is a URL.
    return bool(URL_RE.match(target))


def front_matter_assets(front_matter: str | None) -> str | None:
    if not front_matter:
        return None
    m = FRONT_ASSETS_RE.search(front_matter)
    if not m:
        return None
    return m.group(1).strip().strip("'\"") or None


@dataclass
class LinkTarget:
    kind: str  # "url" | "anchor" | "markdown" | "dir" | "file" | "missing"
    url: str = ""
    path: Path | None = None
    anchor: str = ""


class Resolver:
    def __init__(self, config: Config, doc_path: Path, front_matter: str | None = None):
        self.config = config
        self.doc_path = doc_path
        self.doc_dir = doc_path.parent
        self.doc_assets = self._doc_assets_dir(front_matter)

    def _doc_assets_dir(self, front_matter: str | None) -> Path | None:
        explicit = front_matter_assets(front_matter)
        if explicit:
            p = Path(explicit).expanduser()
            return p if p.is_absolute() else (self.doc_dir / p)
        pattern = self.config.doc_assets_pattern
        if not pattern:
            return None
        return self.doc_dir / pattern.format(stem=self.doc_path.stem, name=self.doc_path.name)

    # ------------------------------------------------------------------ images
    def image_candidates(self, src: str) -> list[Path]:
        src = unquote(src.split("#", 1)[0].split("?", 1)[0])
        if not src:
            return []
        cands: list[Path] = []
        gi = self.config.global_images
        if src.startswith("/"):
            rel = src.lstrip("/")
            if gi:
                cands.append(gi / rel)
                cands.append(gi / Path(rel).name)
            cands.append(self.config.root / rel)
            cands.append(Path(src))
            return cands
        rel = Path(src)
        for mode in self.config.image_modes:
            if mode == "relative":
                cands.append(self.doc_dir / rel)
            elif mode == "document" and self.doc_assets:
                cands.append(self.doc_assets / rel)
                cands.append(self.doc_assets / rel.name)
            elif mode == "global" and gi:
                cands.append(gi / rel)
                cands.append(gi / rel.name)
        return cands

    def resolve_image(self, src: str) -> tuple[str, str | Path]:
        """Return ("url", url) | ("file", path) | ("missing", src)."""
        if src.startswith("data:"):
            return ("url", src)
        if is_url(src):
            scheme = urlsplit(src).scheme.lower()
            if scheme == "file":
                return self._first_existing([Path(unquote(urlsplit(src).path))], src)
            return ("url", src)
        return self._first_existing(self.image_candidates(src), src)

    @staticmethod
    def _first_existing(cands: list[Path], src: str) -> tuple[str, str | Path]:
        for c in cands:
            try:
                if c.is_file():
                    return ("file", c.resolve())
            except OSError:
                continue
        return ("missing", src)

    # ------------------------------------------------------------------- links
    def resolve_link(self, href: str) -> LinkTarget:
        href = href.strip()
        if not href:
            return LinkTarget("missing")
        if href.startswith("#"):
            return LinkTarget("anchor", anchor=unquote(href[1:]))
        if is_url(href):
            parts = urlsplit(href)
            if parts.scheme.lower() != "file":
                return LinkTarget("url", url=href)
            href = parts.path + (f"#{parts.fragment}" if parts.fragment else "")

        path_part, _, anchor = href.partition("#")
        path_part = unquote(path_part.split("?", 1)[0])
        anchor = unquote(anchor)

        cands: list[Path] = []
        if path_part.startswith("/"):
            rel = path_part.lstrip("/")
            cands += [self.config.root / rel]
            if self.config.global_images:
                cands.append(self.config.global_images / rel)
            cands.append(Path(path_part))
        else:
            cands.append(self.doc_dir / path_part)
            # Fall back to the tree root (the "global directory") for
            # site-style links like `docs/intro.md`.
            cands.append(self.config.root / path_part)

        expanded: list[Path] = []
        for c in cands:
            expanded.append(c)
            # Doc generators often link `foo` or `foo/` meaning `foo.md`.
            if not c.suffix:
                expanded += [c.with_suffix(".md"), c / "index.md", c / "README.md", c / "_index.md"]

        for c in expanded:
            try:
                if c.is_file():
                    kind = "markdown" if is_markdown(c) else "file"
                    return LinkTarget(kind, path=c.resolve(), anchor=anchor)
            except OSError:
                continue
        for c in cands:
            if c.is_dir():
                return LinkTarget("dir", path=c.resolve(), anchor=anchor)
        return LinkTarget("missing", url=href)


def slugify(text: str) -> str:
    """GitHub-style heading anchor."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")
