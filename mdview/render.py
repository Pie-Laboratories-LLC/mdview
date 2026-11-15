"""Markdown -> list of terminal lines (Textual Strips).

Links and heading anchors are tagged through Rich ``Style.meta`` so that after
wrapping we can recover exactly which screen lines each link occupies. Images
become rows of kitty Unicode placeholders.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from PIL import Image
from rich import box
from rich.color import Color
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.strip import Strip

from .config import Config
from .images import ImageError, ImageRequest, ImageStore
from .kitty import KittyImages, placeholder_row
from .resolver import Resolver, slugify

GUTTER = 2

MATH_LANGS = {"math", "latex", "tex", "katex"}
DIAGRAM_LANGS = {"mermaid"}

S_LINK = Style(color="#61afef", underline=True)
S_CODE = Style(color="#e5c07b", bgcolor="#2c313a")
S_DIM = Style(color="#7f848e")
S_WARN = Style(color="#e06c75")
S_QUOTE = Style(color="#56b6c2")
HEADING_STYLES = {
    1: Style(color="#c678dd", bold=True),
    2: Style(color="#61afef", bold=True),
    3: Style(color="#98c379", bold=True),
    4: Style(color="#e5c07b", bold=True),
    5: Style(color="#d19a66", bold=True),
    6: Style(color="#abb2bf", bold=True, italic=True),
}

_IMG_TAG_RE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*>", re.I)
_ALT_RE = re.compile(r"\balt\s*=\s*[\"']([^\"']*)[\"']", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def make_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True})
    md.enable(["table", "strikethrough"])
    md.use(front_matter_plugin)
    md.use(dollarmath_plugin, double_inline=True)
    md.use(tasklists_plugin)
    return md


_PARSER = make_parser()


@dataclass
class Link:
    id: int
    href: str
    text: str
    first_line: int = -1
    last_line: int = -1


@dataclass
class Rendered:
    lines: list[Strip]
    links: list[Link]
    anchors: dict[str, int]
    pending: int = 0  # images still loading
    width: int = 0


@dataclass
class _ImagePart:
    src: str
    alt: str
    link: int | None
    request: ImageRequest | None = None  # pre-built (diagrams/math)
    upscale: float = 1.0


def front_matter_of(source: str) -> str | None:
    tokens = _PARSER.parse(source)
    for t in tokens:
        if t.type == "front_matter":
            return t.content
    return None


def _image_color(image_id: int) -> Color:
    return Color.from_rgb((image_id >> 16) & 255, (image_id >> 8) & 255, image_id & 255)


def _plain(node: SyntaxTreeNode) -> str:
    if node.type in ("text", "code_inline", "math_inline", "math_inline_double"):
        return node.content
    if node.type in ("softbreak", "hardbreak"):
        return " "
    if node.type == "image":
        return "".join(_plain(c) for c in node.children) or node.content
    return "".join(_plain(c) for c in node.children)


class DocumentRenderer:
    def __init__(
        self,
        config: Config,
        doc_path: Path,
        store: ImageStore,
        kitty: KittyImages,
        width: int,
        max_image_rows: int,
    ):
        self.config = config
        self.doc_path = doc_path
        self.store = store
        self.kitty = kitty
        self.total_width = max(20, width)
        self.width = self.total_width - GUTTER
        self.max_image_rows = max(3, max_image_rows)
        self.console = Console(
            width=self.width, color_system="truecolor", force_terminal=True,
            legacy_windows=False, emoji=False, highlight=False, markup=False,
        )
        self.links: list[Link] = []
        self.pending = 0
        self.resolver: Resolver | None = None

    # ------------------------------------------------------------- entry point
    def render(self, source: str) -> Rendered:
        tokens = _PARSER.parse(source)
        fm = next((t.content for t in tokens if t.type == "front_matter"), None)
        self.resolver = Resolver(self.config, self.doc_path, fm)
        tree = SyntaxTreeNode(tokens)
        lines = self._blocks(tree.children, self.width)
        return self._finish(lines)

    def _finish(self, lines: list[Text]) -> Rendered:
        strips: list[Strip] = []
        anchors: dict[str, int] = {}
        by_id = {l.id: l for l in self.links}
        pad = Segment(" " * GUTTER)
        for n, line in enumerate(lines):
            # Text.render() omits the Text's base style; apply it underneath.
            base = self.console.get_style(line.style) if line.style else None
            segs = [pad, *Segment.apply_style(line.render(self.console, end=""), base)]
            for seg in segs:
                meta = seg.style.meta if seg.style else None
                if not meta:
                    continue
                if (lid := meta.get("link")) is not None and lid in by_id:
                    lk = by_id[lid]
                    if lk.first_line < 0:
                        lk.first_line = n
                    lk.last_line = n
                if (anc := meta.get("anchor")) and anc not in anchors:
                    anchors[anc] = n
            strips.append(Strip(segs))
        links = [l for l in self.links if l.first_line >= 0]
        links.sort(key=lambda l: (l.first_line, l.id))
        return Rendered(strips, links, anchors, self.pending, self.total_width)

    # ------------------------------------------------------------------ blocks
    def _blocks(self, nodes: list[SyntaxTreeNode], width: int, tight: bool = False) -> list[Text]:
        out: list[Text] = []
        for node in nodes:
            lines = self._block(node, width)
            if not lines:
                continue
            if out and not tight:
                out.append(Text())
            out.extend(lines)
        return out

    def _block(self, node: SyntaxTreeNode, width: int) -> list[Text]:
        t = node.type
        if t == "heading":
            return self._heading(node, width)
        if t == "paragraph":
            return self._paragraph(node.children[0] if node.children else None, width)
        if t in ("bullet_list", "ordered_list"):
            return self._list(node, width)
        if t == "blockquote":
            inner = self._blocks(node.children, max(10, width - 2))
            bar = Text("▎ ", style=S_QUOTE)
            return [Text.assemble(bar, l) for l in inner]
        if t in ("fence", "code_block"):
            return self._code(node, width)
        if t in ("math_block", "math_block_label"):
            if self.config.math:
                return self._image_lines(_ImagePart(
                    "", "", None,
                    request=ImageRequest("math", node.content.strip(), True),
                    upscale=self._math_upscale()), width, fallback=node.content.strip())
            return self._code_text(node.content.strip(), "latex", width)
        if t == "hr":
            return [Text("─" * width, style=S_DIM)]
        if t == "table":
            return self._table(node, width)
        if t == "html_block":
            return self._html_block(node.content, width)
        if t == "front_matter":
            return []
        if node.children:
            return self._blocks(node.children, width)
        if node.content:
            return self._wrap(Text(node.content, style=S_DIM), width)
        return []

    def _heading(self, node: SyntaxTreeNode, width: int) -> list[Text]:
        level = int(node.tag[1]) if node.tag[1:].isdigit() else 6
        inline = node.children[0] if node.children else None
        plain = _plain(inline) if inline else ""
        anchor = Style(meta={"anchor": slugify(plain)})
        style = HEADING_STYLES.get(level, HEADING_STYLES[6])
        text = Text()
        if level >= 3:
            text.append("#" * level + " ", style=S_DIM)
        if inline:
            for part in self._inline(inline.children, Style(), None):
                if isinstance(part, Text):
                    text.append_text(part)
        # Spans (not the base style) survive rendering with their meta intact.
        text.stylize_before(style + anchor)
        lines = self._wrap(text, width)
        if level == 1:
            lines.append(Text("━" * min(width, max(l.cell_len for l in lines)), style=style))
        elif level == 2:
            lines.append(Text("─" * min(width, max(l.cell_len for l in lines)), style=S_DIM))
        return lines

    def _list(self, node: SyntaxTreeNode, width: int) -> list[Text]:
        ordered = node.type == "ordered_list"
        start = int(node.attrs.get("start", 1)) if ordered else 1
        items = node.children
        markers = [f"{start + i}." if ordered else "•" for i in range(len(items))]
        mw = max((len(m) for m in markers), default=1) + 1
        tight = all(
            c.hidden for item in items for c in item.children if c.type == "paragraph"
        )
        out: list[Text] = []
        for item, marker in zip(items, markers):
            inner = self._blocks(item.children, max(10, width - mw), tight=tight)
            if not inner:
                inner = [Text()]
            if out and not tight:
                out.append(Text())
            for i, line in enumerate(inner):
                prefix = Text(marker.rjust(mw - 1) + " ", style=S_DIM) if i == 0 else Text(" " * mw)
                out.append(Text.assemble(prefix, line))
        return out

    def _code(self, node: SyntaxTreeNode, width: int) -> list[Text]:
        lang = (node.info or "").strip().split()[0].lower() if node.info else ""
        code = node.content.rstrip("\n")
        if lang in DIAGRAM_LANGS and self.config.diagrams:
            return self._image_lines(_ImagePart(
                "", f"{lang} diagram", None,
                request=ImageRequest("mermaid", code),
                upscale=self._diagram_upscale()), width, fallback=code)
        if lang in MATH_LANGS and self.config.math:
            return self._image_lines(_ImagePart(
                "", "", None,
                request=ImageRequest("math", code, True),
                upscale=self._math_upscale()), width, fallback=code)
        return self._code_text(code, lang, width)

    def _code_text(self, code: str, lang: str, width: int) -> list[Text]:
        syntax = Syntax(
            code, lang or "text", theme="monokai", word_wrap=True,
            background_color="#23272e", padding=(0, 1),
        )
        return self._renderable_lines(syntax, width)

    def _table(self, node: SyntaxTreeNode, width: int) -> list[Text]:
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold", border_style="#5c6370",
                      expand=False, pad_edge=True)
        header_done = False
        for section in node.children:
            for row in section.children:
                cells = []
                for cell in row.children:
                    txt = Text()
                    if cell.children:
                        for part in self._inline(cell.children[0].children, Style(), None):
                            if isinstance(part, Text):
                                txt.append_text(part)
                            else:
                                txt.append(f"[{part.alt or 'image'}]", style=S_DIM)
                    cells.append((txt, cell.attrs.get("style", "")))
                if section.type == "thead" and not header_done:
                    for txt, st in cells:
                        just = "center" if "center" in st else "right" if "right" in st else "left"
                        table.add_column(txt, justify=just)
                    header_done = True
                else:
                    table.add_row(*[c for c, _ in cells])
        return self._renderable_lines(table, width)

    def _html_block(self, content: str, width: int) -> list[Text]:
        stripped = content.strip()
        if stripped.startswith("<!--"):
            return []
        out: list[Text] = []
        for m in _IMG_TAG_RE.finditer(content):
            alt = _ALT_RE.search(m.group(0))
            out.extend(self._image_lines(_ImagePart(m.group(1), alt.group(1) if alt else "", None), width))
        text = htmlmod.unescape(_TAG_RE.sub("", content)).strip()
        if text:
            if out:
                out.append(Text())
            for para in text.split("\n"):
                out.extend(self._wrap(Text(para.strip()), width))
        return out

    # ------------------------------------------------------------------ inline
    def _paragraph(self, inline: SyntaxTreeNode | None, width: int) -> list[Text]:
        if inline is None:
            return []
        parts = self._inline(inline.children, Style(), None)
        out: list[Text] = []
        buf = Text()

        def flush() -> None:
            nonlocal buf
            if buf.plain.strip():
                out.extend(self._wrap(buf, width))
            buf = Text()

        for part in parts:
            if isinstance(part, Text):
                buf.append_text(part)
            else:
                flush()
                out.extend(self._image_lines(part, width))
        flush()
        return out

    def _new_link(self, href: str, text: str) -> int:
        lid = len(self.links)
        self.links.append(Link(lid, href, text))
        return lid

    def _inline(self, nodes: list[SyntaxTreeNode], style: Style, link: int | None) -> list[Text | _ImagePart]:
        out: list[Text | _ImagePart] = []
        for n in nodes:
            t = n.type
            if t == "text":
                out.append(Text(n.content, style=style))
            elif t == "softbreak":
                out.append(Text(" ", style=style))
            elif t == "hardbreak":
                out.append(Text("\n", style=style))
            elif t == "strong":
                out += self._inline(n.children, style + Style(bold=True), link)
            elif t == "em":
                out += self._inline(n.children, style + Style(italic=True), link)
            elif t == "s":
                out += self._inline(n.children, style + Style(strike=True), link)
            elif t == "code_inline":
                out.append(Text(n.content, style=style + S_CODE))
            elif t == "link":
                href = n.attrs.get("href", "")
                lid = self._new_link(str(href), _plain(n))
                out += self._inline(n.children, style + S_LINK + Style(meta={"link": lid}), lid)
            elif t == "image":
                out.append(_ImagePart(str(n.attrs.get("src", "")), _plain(n), link))
            elif t in ("math_inline", "math_inline_double"):
                out.append(self._inline_math(n.content, style, t == "math_inline_double"))
            elif t == "html_inline":
                out += self._html_inline(n.content, style, link)
            elif n.children:
                out += self._inline(n.children, style, link)
            elif n.content:
                out.append(Text(n.content, style=style))
        return out

    def _html_inline(self, content: str, style: Style, link: int | None) -> list[Text | _ImagePart]:
        low = content.lower()
        if low.startswith("<br"):
            return [Text("\n")]
        if low.startswith("<input") and "checkbox" in low:
            return [Text("☑ " if "checked" in low else "☐ ", style=style + S_DIM)]
        m = _IMG_TAG_RE.search(content)
        if m:
            alt = _ALT_RE.search(content)
            return [_ImagePart(m.group(1), alt.group(1) if alt else "", link)]
        return []

    def _inline_math(self, tex: str, style: Style, display: bool) -> Text | _ImagePart:
        fallback = Text(tex, style=style + Style(italic=True, color="#d19a66"))
        if not self.config.math:
            return fallback
        req = ImageRequest("math", tex, display)
        if display:
            return _ImagePart("", "", None, request=req, upscale=self._math_upscale())
        img = self.store.get(req)
        if img is None:
            self.pending += 1
            return fallback
        if isinstance(img, ImageError):
            return fallback
        cols, rows = self.kitty.fit(img, self.width, 1, upscale=self._math_upscale())
        image_id = self.kitty.place(req.key, img, cols, 1)
        return Text(placeholder_row(0, cols), style=style + Style(color=_image_color(image_id)))

    # ------------------------------------------------------------------ images
    def _math_upscale(self) -> float:
        # KaTeX pages are rendered at 40px; make that roughly one text row.
        return self.kitty.cell[1] / 40 * 0.95

    def _diagram_upscale(self) -> float:
        # Mermaid SVGs use ~14px text and are rasterised at SVG_SCALE.
        from .images import SVG_SCALE

        return self.kitty.cell[1] / (14 * SVG_SCALE) * 0.9

    def _image_lines(self, part: _ImagePart, width: int, fallback: str | None = None) -> list[Text]:
        link_style = (S_LINK + Style(meta={"link": part.link})) if part.link is not None else Style()
        label = part.alt or part.src or "image"
        req = part.request
        if req is None:
            assert self.resolver is not None
            kind, value = self.resolver.resolve_image(part.src)
            if kind == "missing":
                return [Text(f"⚠ image not found: {part.src}", style=S_WARN + link_style)]
            if kind == "url":
                if not self.config.remote_images and not str(value).startswith("data:"):
                    return [Text(f"🖼  {label} (remote image disabled)", style=S_DIM + link_style)]
                req = ImageRequest("url", str(value))
            else:
                p = Path(value)
                req = ImageRequest("file", str(p), stamp=p.stat().st_mtime)

        img = self.store.get(req)
        if img is None:
            self.pending += 1
            what = "rendering" if req.kind in ("math", "mermaid") else "loading"
            return [Text(f"⏳ {what} {label}…", style=S_DIM + link_style)]
        if isinstance(img, ImageError):
            lines = [Text(f"⚠ {label}: {img}", style=S_WARN + link_style)]
            if fallback:
                lines += self._code_text(fallback, "text", width)
            return lines
        return self._placeholder_lines(req, img, width, part, link_style)

    def _placeholder_lines(self, req: ImageRequest, img: Image.Image, width: int,
                           part: _ImagePart, link_style: Style) -> list[Text]:
        cols, rows = self.kitty.fit(img, width, self.max_image_rows, upscale=part.upscale)
        image_id = self.kitty.place(req.key, img, cols, rows)
        style = Style(color=_image_color(image_id))
        if part.link is not None:
            style = style + Style(meta={"link": part.link})
        lines = [Text(placeholder_row(r, cols), style=style) for r in range(rows)]
        if part.alt and req.kind in ("file", "url"):
            cap = Text(part.alt, style=S_DIM + Style(italic=True))
            if part.link is not None:
                cap = Text.assemble(cap, Text("  ↗", style=link_style))
            lines.extend(self._wrap(cap, width))
        return lines

    # ----------------------------------------------------------------- helpers
    def _wrap(self, text: Text, width: int) -> list[Text]:
        out: list[Text] = []
        for piece in text.split("\n", allow_blank=True):
            if not piece.plain:
                out.append(Text())
                continue
            out.extend(piece.wrap(self.console, width, overflow="fold"))
        return out

    def _renderable_lines(self, renderable, width: int) -> list[Text]:
        opts = self.console.options.update(width=width, height=None)
        out = []
        for segs in self.console.render_lines(renderable, opts, pad=False):
            t = Text(end="")
            for seg in segs:
                if seg.control:
                    continue
                t.append(seg.text, seg.style)
            out.append(t)
        return out
