"""mdview: a NERDTree-style markdown browser for kitty-graphics terminals."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import DirectoryTree, Static, Tree
from textual.widgets._directory_tree import DirEntry

from .config import Config, load_config
from .images import ImageStore
from .kitty import PLACEHOLDER, KittyImages
from .render import GUTTER, DocumentRenderer, Link, Rendered
from .resolver import LinkTarget, Resolver, is_markdown, slugify

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".avif", ".ico"}
TEXT_LIMIT = 2 * 1024 * 1024


def _code_lang(path: Path) -> str:
    return path.suffix.lstrip(".").lower() or "text"


def document_source(path: Path) -> str:
    """Markdown source to display for any file in the tree."""
    if is_markdown(path):
        return path.read_text(errors="replace")
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return f"# {path.name}\n\n![{path.name}](<{path.name}>)\n"
    data = path.read_bytes()[:TEXT_LIMIT]
    if b"\0" in data[:8192]:
        return f"# {path.name}\n\n*Binary file, {path.stat().st_size:,} bytes.* Press `o` to open it externally.\n"
    text = data.decode(errors="replace")
    fence = "````" if "```" in text else "```"
    return f"# {path.name}\n\n{fence}{_code_lang(path)}\n{text}\n{fence}\n"


def open_external(target: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(target)


# ---------------------------------------------------------------------- tree
class MDTree(DirectoryTree):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l", "app.focus_doc", "Doc", show=False),
        Binding("h", "collapse", "Collapse", show=False),
        Binding("o", "select_cursor", "Open", show=False),
        Binding("g", "first", "Top", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("O", "open_external", "Open externally", show=False),
    ]

    def __init__(self, path: Path, show_all: bool, **kw):
        self.show_all = show_all
        self._md_cache: dict[Path, bool] = {}
        super().__init__(path, **kw)

    def _has_markdown(self, d: Path, depth: int = 0) -> bool:
        if d in self._md_cache:
            return self._md_cache[d]
        found = False
        if depth <= 8:
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: e.is_dir())  # files first
                for e in entries:
                    if e.name.startswith(".") or e.name in SKIP_DIRS:
                        continue
                    if e.is_file() and is_markdown(Path(e.name)):
                        found = True
                        break
                    if e.is_dir(follow_symlinks=False) and self._has_markdown(Path(e.path), depth + 1):
                        found = True
                        break
            except OSError:
                pass
        self._md_cache[d] = found
        return found

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        out = []
        for p in paths:
            if p.name.startswith(".") or p.name in SKIP_DIRS:
                continue
            if self.show_all:
                out.append(p)
            elif p.is_dir():
                if self._has_markdown(p):
                    out.append(p)
            elif is_markdown(p):
                out.append(p)
        return out

    def action_collapse(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            node.parent.collapse()
            self.move_cursor(node.parent)

    def action_first(self) -> None:
        self.cursor_line = 0

    def action_last(self) -> None:
        self.cursor_line = self.last_line

    def action_open_external(self) -> None:
        node = self.cursor_node
        if node and node.data:
            open_external(str(node.data.path))

    async def reveal(self, path: Path) -> None:
        """Expand ancestors of `path` and put the cursor on it."""
        root_path = Path(self.path).resolve()
        try:
            rel = path.resolve().relative_to(root_path)
        except ValueError:
            return
        node = self.root
        for part in rel.parts:
            if not node.is_expanded:
                node.expand()
            for _ in range(100):  # children load in a worker
                child = next((c for c in node.children if c.data and c.data.path.name == part), None)
                if child is not None or (node.is_expanded and node.children and _ > 20):
                    break
                await asyncio.sleep(0.02)
            if child is None:
                return
            node = child
        self.move_cursor(node)
        self.scroll_to_node(node, animate=False)


# ----------------------------------------------------------------- doc view
@dataclass
class HistoryEntry:
    path: Path
    scroll_y: int
    selected: int | None


class DocView(ScrollView, can_focus=True):
    DEFAULT_CSS = """
    DocView { overflow-x: hidden; }
    """
    BINDINGS = [
        Binding("j", "next_link", "Next link", show=False),
        Binding("k", "prev_link", "Prev link", show=False),
        Binding("h", "app.focus_tree", "Tree", show=False),
        Binding("enter", "follow", "Follow", show=False),
        Binding("H,backspace,ctrl+o", "back", "Back", show=False),
        Binding("L", "forward", "Forward", show=False),
        Binding("space,ctrl+f,pagedown", "page(1)", "Page down", show=False),
        Binding("ctrl+b,pageup", "page(-1)", "Page up", show=False),
        Binding("ctrl+d", "half(1)", show=False),
        Binding("ctrl+u", "half(-1)", show=False),
        Binding("ctrl+e,down", "line(1)", show=False),
        Binding("ctrl+y,up", "line(-1)", show=False),
        Binding("g,home", "top", show=False),
        Binding("G,end", "bottom", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("O", "open_external", show=False),
    ]

    def __init__(self, config: Config, store: ImageStore, kitty: KittyImages, **kw):
        super().__init__(**kw)
        self.config = config
        self.store = store
        self.kitty = kitty
        self.path: Path | None = None
        self.rendered: Rendered | None = None
        self.resolver: Resolver | None = None
        self.selected: int | None = None  # Link.id
        self.history: list[HistoryEntry] = []
        self.future: list[HistoryEntry] = []
        self._source = ""
        self._relayout_timer: Timer | None = None

    # ------------------------------------------------------------ loading
    def load(self, path: Path, anchor: str = "", push: bool = True, scroll_y: int = 0,
             selected: int | None = None) -> None:
        path = path.resolve()
        if push and self.path is not None:
            self.history.append(HistoryEntry(self.path, round(self.scroll_y), self.selected))
            self.future.clear()
        if path != self.path:
            self.kitty.clear()
        self.path = path
        try:
            self._source = document_source(path)
        except OSError as e:
            self._source = f"# Error\n\nCould not read `{path}`: {e}\n"
        self.selected = selected
        self.relayout()
        if anchor:
            self.goto_anchor(anchor)
        else:
            self.scroll_to(y=scroll_y, animate=False, immediate=True)
        self.app.post_status()

    def relayout(self) -> None:
        if self.path is None:
            return
        width = self.scrollable_content_region.width or self.size.width or 80
        height = self.scrollable_content_region.height or self.size.height or 24
        renderer = DocumentRenderer(self.config, self.path, self.store, self.kitty,
                                    width - 1, max_image_rows=max(4, height - 2))
        self.rendered = renderer.render(self._source)
        self.resolver = renderer.resolver
        self.virtual_size = Size(width, len(self.rendered.lines))
        if self.selected is not None and self._link(self.selected) is None:
            self.selected = None
        self.refresh()

    def schedule_relayout(self, delay: float = 0.05) -> None:
        if self._relayout_timer is not None:
            self._relayout_timer.stop()
        self._relayout_timer = self.set_timer(delay, self._do_relayout)

    def _do_relayout(self) -> None:
        self._relayout_timer = None
        y = round(self.scroll_y)
        self.relayout()
        self.scroll_to(y=y, animate=False, immediate=True)
        self.app.post_status()

    def on_resize(self, event: events.Resize) -> None:
        self.kitty.refresh_cell_size()
        self.schedule_relayout(0.1)

    # ---------------------------------------------------------- rendering
    def render_line(self, y: int) -> Strip:
        width = self.size.width
        idx = round(self.scroll_y) + y
        if self.rendered is None or idx >= len(self.rendered.lines):
            return Strip.blank(width, self.rich_style)
        strip = self.rendered.lines[idx]
        sel = self._link(self.selected) if self.selected is not None else None
        if sel and sel.first_line <= idx <= sel.last_line:
            strip = self._highlight(strip, sel.id)
        return strip.crop_extend(0, width, None).apply_style(self.rich_style)

    @staticmethod
    def _highlight(strip: Strip, link_id: int) -> Strip:
        segs = list(strip)
        out = [Segment("▶" + " " * (GUTTER - 1), Style(color="#e5c07b", bold=True))]
        rest = segs[1:] if segs and segs[0].text == " " * GUTTER else segs
        for seg in rest:
            meta = seg.style.meta if seg.style else None
            if meta and meta.get("link") == link_id and not seg.text.startswith(PLACEHOLDER):
                seg = Segment(seg.text, seg.style + Style(reverse=True), seg.control)
            out.append(seg)
        return Strip(out)

    # --------------------------------------------------------- navigation
    @property
    def view_height(self) -> int:
        return max(1, self.scrollable_content_region.height)

    @property
    def max_scroll(self) -> int:
        n = len(self.rendered.lines) if self.rendered else 0
        return max(0, n - self.view_height)

    def _link(self, link_id: int | None) -> Link | None:
        if self.rendered is None or link_id is None:
            return None
        return next((l for l in self.rendered.links if l.id == link_id), None)

    def _visible(self, link: Link, top: int | None = None) -> bool:
        top = round(self.scroll_y) if top is None else top
        bottom = top + self.view_height - 1
        return link.last_line >= top and link.first_line <= bottom

    def _select(self, link: Link) -> None:
        self.selected = link.id
        top = round(self.scroll_y)
        bottom = top + self.view_height - 1
        if link.last_line > bottom:
            self._scroll_top(min(link.first_line, link.last_line - self.view_height + 1))
        elif link.first_line < top:
            self._scroll_top(max(link.first_line, 0))
        self.refresh()
        self.app.post_status()

    def _scroll_top(self, y: int) -> int:
        y = max(0, min(y, self.max_scroll))
        self.scroll_to(y=y, animate=False, immediate=True)
        return y

    def action_next_link(self) -> None:
        if not self.rendered:
            return
        links = self.rendered.links
        top = round(self.scroll_y)
        cur = self._link(self.selected)
        if cur is not None and self._visible(cur):
            i = links.index(cur)
            nxt = links[i + 1] if i + 1 < len(links) else None
        else:
            nxt = next((l for l in links if l.last_line >= top), None)
        if nxt is not None and self._visible(nxt):
            self._select(nxt)
            return
        # No link on screen: page down, stopping when the next link appears.
        new_top = self._scroll_top(top + self.view_height - 1)
        if nxt is not None and self._visible(nxt, new_top):
            self.selected = nxt.id
        else:
            self.selected = None
        self.refresh()
        self.app.post_status()

    def action_prev_link(self) -> None:
        if not self.rendered:
            return
        links = self.rendered.links
        top = round(self.scroll_y)
        bottom = top + self.view_height - 1
        cur = self._link(self.selected)
        if cur is not None and self._visible(cur):
            i = links.index(cur)
            prv = links[i - 1] if i > 0 else None
        else:
            prv = next((l for l in reversed(links) if l.first_line <= bottom), None)
        if prv is not None and self._visible(prv):
            self._select(prv)
            return
        new_top = self._scroll_top(top - (self.view_height - 1))
        if prv is not None and self._visible(prv, new_top):
            self.selected = prv.id
        else:
            self.selected = None
        self.refresh()
        self.app.post_status()

    def action_page(self, direction: int) -> None:
        self._scroll_top(round(self.scroll_y) + direction * (self.view_height - 1))
        self._drop_offscreen_selection()

    def action_half(self, direction: int) -> None:
        self._scroll_top(round(self.scroll_y) + direction * max(1, self.view_height // 2))
        self._drop_offscreen_selection()

    def action_line(self, direction: int) -> None:
        self._scroll_top(round(self.scroll_y) + direction)

    def action_top(self) -> None:
        self._scroll_top(0)
        self._drop_offscreen_selection()

    def action_bottom(self) -> None:
        self._scroll_top(self.max_scroll)
        self._drop_offscreen_selection()

    def _drop_offscreen_selection(self) -> None:
        cur = self._link(self.selected)
        if cur is not None and not self._visible(cur):
            self.selected = None
            self.refresh()
            self.app.post_status()

    def goto_anchor(self, anchor: str) -> bool:
        if not self.rendered:
            return False
        for key in (anchor, slugify(anchor), anchor.lower()):
            if key in self.rendered.anchors:
                self._scroll_top(self.rendered.anchors[key])
                self.selected = None
                self.refresh()
                return True
        self.app.post_status(f"anchor #{anchor} not found", error=True)
        return False

    # ------------------------------------------------------------ actions
    def selected_link(self) -> Link | None:
        return self._link(self.selected)

    def action_follow(self) -> None:
        link = self.selected_link()
        if link is None or self.resolver is None:
            return
        self.app.follow(self.resolver.resolve_link(link.href), link.href)

    def action_back(self) -> None:
        if not self.history or self.path is None:
            return
        self.future.append(HistoryEntry(self.path, round(self.scroll_y), self.selected))
        e = self.history.pop()
        self.load(e.path, push=False, scroll_y=e.scroll_y, selected=e.selected)
        self.app.reveal(e.path)

    def action_forward(self) -> None:
        if not self.future or self.path is None:
            return
        self.history.append(HistoryEntry(self.path, round(self.scroll_y), self.selected))
        e = self.future.pop()
        self.load(e.path, push=False, scroll_y=e.scroll_y, selected=e.selected)
        self.app.reveal(e.path)

    def action_reload(self) -> None:
        if self.path:
            self.store.forget_files()
            self.kitty.clear()
            self.load(self.path, push=False, scroll_y=round(self.scroll_y), selected=self.selected)

    def action_open_external(self) -> None:
        link = self.selected_link()
        if link is not None and self.resolver is not None:
            t = self.resolver.resolve_link(link.href)
            open_external(t.url if t.kind == "url" else str(t.path or link.href))
        elif self.path:
            open_external(str(self.path))

    def image_ready(self) -> None:
        self.schedule_relayout()


# ----------------------------------------------------------------------- app
HELP = """\
[b]mdview[/b]   h/l: tree ⇄ doc   j/k: next/prev link (pages when none visible)   \
enter: follow   H/L: back/fwd   space/^b: page   g/G: top/bottom   t: tree   r: reload   q: quit"""


class MDViewApp(App):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    MDTree { height: 1fr; border-right: vkey $panel-lighten-1; padding-right: 1; }
    MDTree:focus { border-right: vkey $accent; }
    DocView { width: 1fr; height: 1fr; }
    #status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    #status.error { color: $error; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "toggle_tree", "Toggle tree"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.kitty = KittyImages(self._term_write)
        self.store = ImageStore(on_ready=self._image_ready_threadsafe)
        self._preview_timer: Timer | None = None
        self._status_msg: tuple[str, bool] | None = None

    # Raw writes go through the driver so they're ordered with screen updates.
    def _term_write(self, data: str) -> None:
        driver = getattr(self, "_driver", None)
        if driver is not None:
            driver.write(data)
            driver.flush()
        elif sys.__stdout__:
            sys.__stdout__.write(data)
            sys.__stdout__.flush()

    def _image_ready_threadsafe(self, key: tuple) -> None:
        try:
            self.call_from_thread(self.doc.image_ready)
        except Exception:  # app shutting down
            pass

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            tree = MDTree(self.config.root, self.config.show_all_files, id="tree")
            tree.styles.width = self.config.tree_width
            yield tree
            yield DocView(self.config, self.store, self.kitty, id="doc")
        yield Static(id="status")

    @property
    def doc(self) -> DocView:
        return self.query_one(DocView)

    @property
    def tree(self) -> MDTree:
        return self.query_one(MDTree)

    def on_mount(self) -> None:
        self.title = "mdview"
        start = self.config.start_file or self._default_doc()
        if start is not None:
            self.call_after_refresh(self._open_start, start)
        else:
            self.tree.focus()
            self.post_status()

    def _open_start(self, start: Path) -> None:
        self.doc.load(start, push=False)
        self.reveal(start)
        self.doc.focus() if self.config.start_file else self.tree.focus()

    def _default_doc(self) -> Path | None:
        for name in ("README.md", "readme.md", "index.md", "_index.md"):
            p = self.config.root / name
            if p.is_file():
                return p
        return None

    async def action_quit(self) -> None:
        self.kitty.clear()  # free image memory in the terminal while the driver is alive
        self.exit()

    def on_unmount(self) -> None:
        self.kitty.clear()
        self.store.shutdown()

    # ---------------------------------------------------------- tree events
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        if not isinstance(data, DirEntry) or not data.path.is_file():
            return
        if self._preview_timer is not None:
            self._preview_timer.stop()
        path = data.path
        self._preview_timer = self.set_timer(0.15, lambda: self._preview(path))

    def _preview(self, path: Path) -> None:
        if self.doc.path != path.resolve():
            self.doc.load(path)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if self.doc.path != event.path.resolve():
            self.doc.load(event.path)
        self.doc.focus()

    # ------------------------------------------------------------- actions
    def action_focus_doc(self) -> None:
        node = self.tree.cursor_node
        if node and isinstance(node.data, DirEntry) and node.data.path.is_file() \
                and self.doc.path != node.data.path.resolve():
            self.doc.load(node.data.path)
        self.doc.focus()

    def action_focus_tree(self) -> None:
        if self.tree.display:
            self.tree.focus()

    def action_toggle_tree(self) -> None:
        self.tree.display = not self.tree.display
        if not self.tree.display:
            self.doc.focus()

    def action_help(self) -> None:
        self.post_status(HELP)

    def reveal(self, path: Path) -> None:
        self.run_worker(self.tree.reveal(path), exclusive=True, group="reveal")

    def follow(self, target: LinkTarget, href: str) -> None:
        if target.kind == "url":
            open_external(target.url)
            self.post_status(f"opened {target.url}")
        elif target.kind == "anchor":
            self.doc.goto_anchor(target.anchor)
        elif target.kind in ("markdown", "file") and target.path is not None:
            if target.kind == "file" and target.path.suffix.lower() not in IMAGE_SUFFIXES \
                    and not _looks_text(target.path):
                open_external(str(target.path))
                self.post_status(f"opened {target.path.name} externally")
                return
            self.doc.load(target.path, anchor=target.anchor)
            self.reveal(target.path)
        elif target.kind == "dir" and target.path is not None:
            self.reveal(target.path)
            self.tree.focus()
        else:
            self.post_status(f"link target not found: {href}", error=True)

    def post_status(self, message: str | None = None, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.set_class(error, "error")
        if message:
            status.update(message)
            return
        doc = self.doc
        parts = []
        if doc.path:
            try:
                parts.append(str(doc.path.relative_to(self.config.root)))
            except ValueError:
                parts.append(str(doc.path))
        link = doc.selected_link()
        if link is not None:
            parts.append(f"→ {link.href}")
        if doc.rendered and doc.rendered.pending:
            parts.append(f"⏳ {doc.rendered.pending} image(s)")
        parts.append("? help")
        status.update("   ".join(parts))


def _looks_text(path: Path) -> bool:
    try:
        return b"\0" not in path.read_bytes()[:8192]
    except OSError:
        return False


def main(argv: list[str] | None = None) -> None:
    config = load_config(argv)
    MDViewApp(config).run()


if __name__ == "__main__":
    main()
