from pathlib import Path

from PIL import Image

from mdview.config import Config
from mdview.images import ImageStore
from mdview.kitty import PLACEHOLDER, KittyImages
from mdview.render import DocumentRenderer


def render(tmp_path: Path, md: str, **cfg):
    doc = tmp_path / "doc.md"
    doc.write_text(md)
    sent: list[str] = []
    kitty = KittyImages(sent.append)
    kitty.cell = (10, 20)
    store = ImageStore()
    for _ in range(200):
        r = DocumentRenderer(Config(root=tmp_path, **cfg), doc, store, kitty, 60, 20).render(md)
        if not r.pending:
            break
        import time; time.sleep(0.02)
    store.shutdown()
    return r, sent


def test_image_placeholders_carry_image_id(tmp_path):
    Image.new("RGB", (100, 40), "red").save(tmp_path / "a.png")
    r, sent = render(tmp_path, "[![alt](a.png)](https://x.y)\n")
    assert len(sent) == 1 and "U=1" in sent[0]
    image_id = int(sent[0].split(",i=")[1].split(",")[0])
    cells = [seg for strip in r.lines for seg in strip if seg.text.startswith(PLACEHOLDER)]
    assert cells
    for seg in cells:
        t = seg.style.color.triplet
        assert (t.red << 16 | t.green << 8 | t.blue) == image_id
        assert seg.style.meta.get("link") == r.links[0].id  # image is part of the link
    assert r.links[0].first_line < r.links[0].last_line


def test_heading_anchors_and_links(tmp_path):
    r, _ = render(tmp_path, "# One\n\n## Two `x`\n\n[a](#two-x) [b](b.md)\n", math=False)
    assert r.anchors == {"one": 0, "two-x": 3}
    assert [l.href for l in r.links] == ["#two-x", "b.md"]
