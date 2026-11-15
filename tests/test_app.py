"""Headless UI tests for pane switching, link hopping and link following."""

from pathlib import Path

import pytest

from mdview.app import MDViewApp
from mdview.config import Config

FILLER = "\n\n".join(f"Paragraph {i} of filler text." for i in range(30))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    doc = (
        "# Top\n\n[first](other.md) and [second](#end)\n\n"
        + FILLER
        + "\n\n[third](https://example.com)\n\n"
        + FILLER
        + "\n\n## End\n\nlast line\n"
    )
    (tmp_path / "README.md").write_text(doc)
    (tmp_path / "other.md").write_text("# Other\n\n[back](README.md)\n")
    return tmp_path


def make_app(root: Path) -> MDViewApp:
    return MDViewApp(Config(root=root, start_file=root / "README.md", math=False, diagrams=False))


async def test_link_hopping_and_paging(root):
    app = make_app(root)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        doc = app.doc
        assert app.focused is doc
        links = doc.rendered.links
        assert [l.href for l in links] == ["other.md", "#end", "https://example.com"]

        await pilot.press("j")
        assert doc.selected == links[0].id and doc.scroll_y == 0
        await pilot.press("j")
        assert doc.selected == links[1].id and doc.scroll_y == 0

        # Next link is off-screen: j pages down instead of jumping to it.
        third = links[2]
        presses = 0
        while doc.selected != third.id:
            before = doc.scroll_y
            await pilot.press("j")
            presses += 1
            assert doc.scroll_y > before or doc.selected == third.id
            assert presses < 10
        assert presses >= 2  # needed at least one plain page-down
        assert doc._visible(third)

        # No more links: j keeps paging to the end of the document.
        for _ in range(10):
            await pilot.press("j")
        assert doc.scroll_y == doc.max_scroll
        assert doc.selected is None

        # k pages back up until the third link reappears, then hops links.
        for _ in range(10):
            await pilot.press("k")
            if doc.selected == third.id:
                break
        assert doc.selected == third.id
        await pilot.press("k")
        assert doc.selected in (None, links[1].id)  # pages if the previous link is off-screen
        for _ in range(10):
            if doc.selected == links[0].id:
                break
            await pilot.press("k")
        assert doc.selected == links[0].id
        assert doc.scroll_y == 0


async def test_follow_anchor_relative_and_back(root):
    app = make_app(root)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        doc = app.doc
        await pilot.press("j", "j", "enter")  # #end anchor
        assert doc.scroll_y == min(doc.rendered.anchors["end"], doc.max_scroll)

        await pilot.press("g", "j", "enter")  # other.md
        await pilot.pause()
        assert doc.path == (root / "other.md").resolve()

        await pilot.press("H")
        assert doc.path == (root / "README.md").resolve()
        await pilot.press("L")
        assert doc.path == (root / "other.md").resolve()


async def test_h_and_l_switch_panes(root):
    app = make_app(root)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        await pilot.press("h")
        assert app.focused is app.tree
        await pilot.press("l")
        assert app.focused is app.doc
