"""Load and convert anything that becomes a picture in a document.

* raster files Pillow understands are opened directly
* everything else mcat understands (svg, pdf, html, video, ...) goes through
  ``mcat <file> -o image``, which uses mcat's own renderers / chromium
* ``mermaid`` fenced blocks are rendered by mcat's markdown->html pipeline
  (it inlines the diagram as SVG) and then rasterised by mcat
* TeX (``$...$``, ``$$...$$``, ```` ```math ````) is typeset with KaTeX in an
  HTML page that mcat screenshots through chromium

Work runs on a thread pool; results are cached in memory and generated PNGs on
disk (``$XDG_CACHE_HOME/mdview``).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageOps

MCAT = shutil.which("mcat")
KATEX_VERSION = "0.16.11"
MATH_FG = (225, 225, 225)
SVG_SCALE = 3.0
# mcat drives a single headless chromium profile; concurrent runs deadlock.
_CHROMIUM_LOCK = threading.Lock()


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "mdview"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class ImageRequest:
    kind: str  # "file" | "url" | "math" | "mermaid"
    value: str  # path / url / tex / mermaid source
    display: bool = True  # math only
    stamp: float = 0.0  # file mtime, so edits invalidate the cache

    @property
    def key(self) -> tuple:
        return (self.kind, self.value, self.display, self.stamp)


class ImageError(Exception):
    pass


# --------------------------------------------------------------------- helpers
def _digest(*parts: object) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode())
    return h.hexdigest()[:32]


def _run_mcat(path: Path, *extra: str, timeout: float = 90) -> bytes:
    if not MCAT:
        raise ImageError("mcat not found on PATH")
    try:
        proc = subprocess.run(
            [MCAT, "--silent", *extra, str(path)],
            capture_output=True, timeout=timeout,
            env={**os.environ, "MCAT_SILENT": "1"},
        )
    except subprocess.TimeoutExpired as e:
        raise ImageError(f"mcat timed out on {path.name}") from e
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        raise ImageError(f"mcat failed: {msg[-1] if msg else proc.returncode}")
    return proc.stdout


def _mcat_image(path: Path, chromium: bool = True) -> Image.Image:
    if chromium:
        with _CHROMIUM_LOCK:
            data = _run_mcat(path, "-o", "image", timeout=45)
    else:
        data = _run_mcat(path, "-o", "image")
    try:
        return Image.open(io.BytesIO(data))
    except Exception as e:
        raise ImageError(f"mcat produced no image for {path.name}") from e


_SVG_DIM_RE = re.compile(r'\b(width|height)\s*=\s*"([\d.]+)(px)?"')


def scale_svg(svg: str, factor: float) -> str:
    """Scale an SVG's intrinsic size so rasterisation is crisp."""
    m = re.search(r"<svg\b[^>]*>", svg)
    if not m:
        return svg
    tag = m.group(0)
    dims = {k: float(v) for k, v, _ in _SVG_DIM_RE.findall(tag)}
    if "width" not in dims or "height" not in dims:
        vb = re.search(r'viewBox\s*=\s*"[\d.\-]+[\s,]+[\d.\-]+[\s,]+([\d.]+)[\s,]+([\d.]+)"', tag)
        if not vb:
            return svg
        dims = {"width": float(vb.group(1)), "height": float(vb.group(2))}
        new_tag = tag[:-1] + f' width="{dims["width"] * factor:.2f}" height="{dims["height"] * factor:.2f}">'
    else:
        new_tag = _SVG_DIM_RE.sub(lambda mm: f'{mm.group(1)}="{dims[mm.group(1)] * factor:.2f}"', tag)
        if "viewBox" not in tag:
            new_tag = new_tag[:-1] + f' viewBox="0 0 {dims["width"]:.2f} {dims["height"]:.2f}">'
    return svg[: m.start()] + new_tag + svg[m.end():]


def _normalise(img: Image.Image) -> Image.Image:
    try:
        img.seek(0)  # first frame of animations
    except Exception:
        pass
    img = ImageOps.exif_transpose(img)
    return img.convert("RGBA")


def autocrop(img: Image.Image, pad: int = 6) -> Image.Image:
    """Crop to content, treating the top-left pixel colour as background."""
    rgba = img.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, rgba.getpixel((0, 0)))
    from PIL import ImageChops

    diff = ImageChops.difference(rgba, bg).convert("L").point(lambda v: 255 if v > 12 else 0)
    box = diff.getbbox()
    if not box:
        return rgba
    l, t, r, b = box
    return rgba.crop((max(0, l - pad), max(0, t - pad), min(rgba.width, r + pad), min(rgba.height, b + pad)))


# ------------------------------------------------------------------ converters
def load_file(path: Path) -> Image.Image:
    if path.suffix.lower() == ".svg":
        return _svg_file(path)
    try:
        img = Image.open(path)
        img.load()
        return _normalise(img)
    except Exception:
        pass
    return _normalise(_cached_png(("mcat", str(path), path.stat().st_mtime), lambda: _mcat_image(path)))


def _svg_file(path: Path) -> Image.Image:
    def make() -> Image.Image:
        svg = path.read_text(errors="replace")
        return _svg_text(svg)

    return _normalise(_cached_png(("svg", str(path), path.stat().st_mtime), make))


def _svg_text(svg: str) -> Image.Image:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "img.svg"
        p.write_text(scale_svg(svg, SVG_SCALE))
        return _mcat_image(p, chromium=False)


def load_url(url: str) -> Image.Image:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" in header:
            data = base64.b64decode(payload)
        else:
            from urllib.parse import unquote_to_bytes

            data = unquote_to_bytes(payload)
        if "svg" in header:
            return _normalise(_svg_text(data.decode(errors="replace")))
        return _normalise(Image.open(io.BytesIO(data)))

    remote = cache_dir() / "remote"
    remote.mkdir(exist_ok=True)
    dest = remote / _digest("url", url)
    if not dest.exists():
        req = urllib.request.Request(url, headers={"User-Agent": "mdview/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(50 * 1024 * 1024)
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
    data = dest.read_bytes()
    if data.lstrip()[:5] in (b"<svg ", b"<?xml") and b"<svg" in data[:2048]:
        return _normalise(_svg_text(data.decode(errors="replace")))
    try:
        img = Image.open(dest)
        img.load()
        return _normalise(img)
    except Exception:
        return _normalise(_mcat_image(dest))


def _cached_png(key: tuple, make: Callable[[], Image.Image]) -> Image.Image:
    path = cache_dir() / f"{_digest(*key)}.png"
    if path.exists():
        try:
            img = Image.open(path)
            img.load()
            return img
        except Exception:
            path.unlink(missing_ok=True)
    img = make()
    tmp = path.with_suffix(".tmp.png")
    img.save(tmp, "PNG")
    tmp.replace(path)
    return img


KATEX_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@{v}/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@{v}/dist/katex.min.js"></script>
<style>html,body{{margin:0;background:#fff;color:#000}}
#m{{display:inline-block;padding:12px;font-size:40px;white-space:nowrap}}
.katex-display{{margin:0}}</style></head>
<body><div id="m"></div><script>
try {{ katex.render({tex}, document.getElementById("m"), {{displayMode:{display},throwOnError:false}}); }}
catch (e) {{ document.getElementById("m").textContent = {tex}; }}
</script></body></html>"""


def render_math(tex: str, display: bool) -> Image.Image:
    def make() -> Image.Image:
        import json

        page = KATEX_HTML.format(v=KATEX_VERSION, tex=json.dumps(tex), display="true" if display else "false")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "math.html"
            p.write_text(page)
            shot = _mcat_image(p)
        # Black-on-white -> light glyphs on transparent so it sits on any
        # dark background.
        grey = autocrop(shot.convert("RGB"), pad=4).convert("L")
        alpha = ImageOps.invert(grey)
        out = Image.new("RGBA", grey.size, MATH_FG + (0,))
        out.putalpha(alpha)
        return out

    return _cached_png(("math", tex, display, KATEX_VERSION), make)


_SVG_DATA_RE = re.compile(r'data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)')


def render_mermaid(code: str) -> Image.Image:
    def make() -> Image.Image:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "diagram.md"
            p.write_text(f"```mermaid\n{code}\n```\n")
            out = _run_mcat(p, "-o", "html").decode(errors="replace")
        m = _SVG_DATA_RE.search(out)
        if not m:
            raise ImageError("mcat could not render the mermaid diagram")
        svg = base64.b64decode(m.group(1)).decode(errors="replace")
        return _svg_text(svg)

    return _cached_png(("mermaid", code), make)


def produce(req: ImageRequest) -> Image.Image:
    if req.kind == "file":
        return load_file(Path(req.value))
    if req.kind == "url":
        return load_url(req.value)
    if req.kind == "math":
        return _normalise(render_math(req.value, req.display))
    if req.kind == "mermaid":
        return _normalise(render_mermaid(req.value))
    raise ImageError(f"unknown image kind {req.kind}")


# ----------------------------------------------------------------------- store
class ImageStore:
    """Thread-pooled, memoised image loading.

    ``get`` returns the image if ready, an ``ImageError`` if loading failed, or
    ``None`` after queueing the work; ``on_ready(key)`` fires (from a worker
    thread) once a queued request settles.
    """

    def __init__(self, on_ready: Callable[[tuple], None] | None = None, workers: int = 4):
        self._done: dict[tuple, Image.Image | ImageError] = {}
        self._pending: set[tuple] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mdview-img")
        self.on_ready = on_ready

    def get(self, req: ImageRequest) -> Image.Image | ImageError | None:
        key = req.key
        with self._lock:
            if key in self._done:
                return self._done[key]
            if key in self._pending:
                return None
            self._pending.add(key)
        self._pool.submit(self._work, req)
        return None

    def _work(self, req: ImageRequest) -> None:
        try:
            result: Image.Image | ImageError = produce(req)
        except ImageError as e:
            result = e
        except Exception as e:  # noqa: BLE001 - surface any loader failure in the UI
            result = ImageError(f"{type(e).__name__}: {e}")
        with self._lock:
            self._done[req.key] = result
            self._pending.discard(req.key)
        if self.on_ready:
            self.on_ready(req.key)

    def forget_files(self) -> None:
        with self._lock:
            for k in [k for k in self._done if k[0] == "file"]:
                del self._done[k]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


