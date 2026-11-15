"""Configuration: CLI flags merged over an optional `.mdview.toml` in the root."""

from __future__ import annotations

import argparse
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_MODES = ("relative", "document", "global")
CONFIG_NAME = ".mdview.toml"


@dataclass
class Config:
    root: Path
    start_file: Path | None = None
    # Mode 1: a shared images/assets folder for the whole tree.
    global_images: Path | None = None
    # Mode 2: per-document assets folder. `{stem}` is the markdown filename
    # without extension; the pattern is resolved relative to the document's
    # directory. A front-matter `assets:` key overrides it per document.
    doc_assets_pattern: str = "{stem}"
    # Order in which the three image modes are tried.
    image_modes: list[str] = field(default_factory=lambda: list(IMAGE_MODES))
    show_all_files: bool = False
    tree_width: int = 32
    math: bool = True
    diagrams: bool = True
    remote_images: bool = True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="mdview",
        description="Browse a directory tree of markdown with inline kitty-protocol images.",
    )
    p.add_argument("path", nargs="?", default=".", help="root directory or a markdown file (default: .)")
    p.add_argument("-i", "--images", metavar="DIR",
                   help="global images/assets folder (relative to root if not absolute)")
    p.add_argument("-a", "--assets-pattern", metavar="PATTERN",
                   help="per-document assets folder pattern, relative to the document; "
                        "{stem} = filename without .md (default: {stem})")
    p.add_argument("-m", "--image-modes", metavar="MODES",
                   help="comma-separated lookup order of relative,document,global "
                        "(default: relative,document,global)")
    p.add_argument("--all-files", action="store_true", help="show non-markdown files in the tree")
    p.add_argument("--tree-width", type=int, metavar="COLS")
    p.add_argument("--no-math", action="store_true", help="do not render TeX math as images")
    p.add_argument("--no-diagrams", action="store_true", help="do not render mermaid diagrams")
    p.add_argument("--no-remote", action="store_true", help="do not fetch http(s) images")
    return p.parse_args(argv)


def load_config(argv: list[str] | None = None) -> Config:
    args = _parse_args(argv)
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"mdview: {args.path}: no such file or directory")

    start_file = None
    if path.is_file():
        start_file = path
        root = path.parent
    else:
        root = path

    # Walk upward for a config file so opening a subfolder still finds it.
    file_cfg: dict = {}
    for d in [root, *root.parents]:
        cand = d / CONFIG_NAME
        if cand.is_file():
            with cand.open("rb") as fh:
                file_cfg = tomllib.load(fh)
            file_cfg["_dir"] = d
            break

    cfg = Config(root=root, start_file=start_file)
    cfg_dir: Path = file_cfg.get("_dir", root)

    images = args.images or file_cfg.get("images")
    if images:
        base = root if args.images else cfg_dir
        gp = Path(images).expanduser()
        cfg.global_images = (gp if gp.is_absolute() else base / gp).resolve()

    cfg.doc_assets_pattern = args.assets_pattern or file_cfg.get("assets_pattern", cfg.doc_assets_pattern)

    modes = args.image_modes or file_cfg.get("image_modes")
    if modes:
        if isinstance(modes, str):
            modes = [m.strip() for m in modes.split(",") if m.strip()]
        bad = [m for m in modes if m not in IMAGE_MODES]
        if bad:
            raise SystemExit(f"mdview: unknown image mode(s): {', '.join(bad)}; choose from {', '.join(IMAGE_MODES)}")
        cfg.image_modes = list(modes)

    cfg.show_all_files = args.all_files or bool(file_cfg.get("all_files", False))
    cfg.tree_width = args.tree_width or int(file_cfg.get("tree_width", cfg.tree_width))
    cfg.math = not args.no_math and bool(file_cfg.get("math", True))
    cfg.diagrams = not args.no_diagrams and bool(file_cfg.get("diagrams", True))
    cfg.remote_images = not args.no_remote and bool(file_cfg.get("remote_images", True))
    return cfg
