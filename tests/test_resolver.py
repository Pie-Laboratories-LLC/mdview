from pathlib import Path

import pytest

from mdview.config import Config
from mdview.resolver import Resolver, slugify


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "shared.png").write_bytes(b"x")
    (tmp_path / "blog").mkdir()
    (tmp_path / "blog" / "post.md").write_text("# Post\n")
    (tmp_path / "blog" / "post").mkdir()
    (tmp_path / "blog" / "post" / "pic.png").write_bytes(b"x")
    (tmp_path / "blog" / "local.png").write_bytes(b"x")
    (tmp_path / "blog" / "custom").mkdir()
    (tmp_path / "blog" / "custom" / "c.png").write_bytes(b"x")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "intro.md").write_text("# Intro\n")
    (tmp_path / "docs" / "guide").mkdir()
    (tmp_path / "docs" / "guide" / "README.md").write_text("# Guide\n")
    return tmp_path


def cfg(root: Path, **kw) -> Config:
    return Config(root=root, global_images=root / "assets", **kw)


def test_relative_mode(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md")
    assert r.resolve_image("./local.png") == ("file", (tree / "blog" / "local.png").resolve())


def test_document_assets_mode(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md")
    assert r.resolve_image("./pic.png") == ("file", (tree / "blog" / "post" / "pic.png").resolve())


def test_front_matter_assets_override(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md", front_matter="title: x\nassets: custom\n")
    assert r.resolve_image("c.png") == ("file", (tree / "blog" / "custom" / "c.png").resolve())


def test_global_mode(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md")
    assert r.resolve_image("shared.png") == ("file", (tree / "assets" / "shared.png").resolve())
    assert r.resolve_image("/shared.png") == ("file", (tree / "assets" / "shared.png").resolve())
    assert r.resolve_image("../images/shared.png")[0] == "file"  # basename fallback


def test_mode_order(tree):
    (tree / "assets" / "local.png").write_bytes(b"x")
    r = Resolver(cfg(tree, image_modes=["global", "relative"]), tree / "blog" / "post.md")
    assert r.resolve_image("local.png") == ("file", (tree / "assets" / "local.png").resolve())


def test_missing_and_urls(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md")
    assert r.resolve_image("nope.png") == ("missing", "nope.png")
    assert r.resolve_image("https://x.y/a.png") == ("url", "https://x.y/a.png")


def test_links(tree):
    r = Resolver(cfg(tree), tree / "blog" / "post.md")
    t = r.resolve_link("../docs/intro.md#setup")
    assert (t.kind, t.path, t.anchor) == ("markdown", (tree / "docs" / "intro.md").resolve(), "setup")
    assert r.resolve_link("/docs/intro.md").path == (tree / "docs" / "intro.md").resolve()
    assert r.resolve_link("docs/intro").path == (tree / "docs" / "intro.md").resolve()  # root fallback
    assert r.resolve_link("/docs/guide/").path == (tree / "docs" / "guide" / "README.md").resolve()
    assert r.resolve_link("#heading").kind == "anchor"
    assert r.resolve_link("https://example.com").kind == "url"
    assert r.resolve_link("mailto:a@b.c").kind == "url"
    assert r.resolve_link("missing.md").kind == "missing"


def test_slugify():
    assert slugify("Hello, World! 2") == "hello-world-2"
    assert slugify("To boldly go...") == "to-boldly-go"
