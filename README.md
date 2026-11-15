# mdview

A terminal markdown browser: a NERDTree-style directory tree on the left and
rendered markdown on the right. It draws images inline with the kitty graphics
protocol (Unicode placeholders, so images scroll with the text), and also
renders mermaid diagrams and TeX math.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/mdview ~/notes            # or a single file: mdview notes/README.md
```

You need a terminal that supports kitty Unicode placeholders (kitty, Ghostty).
Install [mcat](https://github.com/Skardyy/mcat) for anything Pillow can't open
(SVG, PDF, HTML, video, ...), mermaid, and TeX. TeX is typeset with KaTeX in
mcat's headless chromium (`mcat --fetch-chromium`), and KaTeX loads from the
jsDelivr CDN. Rendered output is cached in `~/.cache/mdview`.

## Image lookup modes

mdview tries each mode in order (`-m/--image-modes`, default
`relative,document,global`) and uses the first file it finds:

| mode       | where it looks                                                           |
|------------|--------------------------------------------------------------------------|
| `relative` | relative to the markdown file: `![x](./pic.png)`                         |
| `document` | the document's own assets folder. A front-matter `assets: dir` key sets it; otherwise `--assets-pattern` decides (default `{stem}`, so `post.md` → `post/`) |
| `global`   | the shared folder set with `-i/--images DIR`                             |

For paths that start at the root, like `/img/a.png`, mdview tries the global
folder, then the tree root, then the absolute path on disk.

You can save settings in a `.mdview.toml` in the root or any parent folder:

```toml
images = "static/images"      # relative to this file
assets_pattern = "{stem}"
image_modes = ["relative", "document", "global"]
all_files = false
tree_width = 32
```

## Links

* `#anchor` jumps to a heading (GitHub-style slugs).
* A relative path is resolved against the current document first, then the tree
  root. `/path` is resolved against the root. If a path has no extension,
  mdview also tries `.md`, `index.md` and `README.md`.
* Markdown files open in the viewer and the tree follows along. Images and text
  files open in the viewer too. Your OS opens URLs and other files.

## Keys

| key | tree | document |
|-----|------|----------|
| `h` / `l` | collapse / go to document | go to tree / – |
| `j` / `k` | down / up | next / previous link. If that link isn't on screen, scroll a page toward it instead |
| `enter`, `o` | expand folder or open file | follow selected link |
| `H` / `L` (`backspace`) | | history back / forward |
| `space` `^f` / `^b` | | page down / up |
| `^d` / `^u`, `^e` / `^y` | | half page / one line |
| `g` / `G` | top / bottom | top / bottom |
| `O` | open externally | open link or file externally |
| `r` | | reload (also re-reads images) |
| `t`, `?`, `q` | toggle tree, help, quit | |

## Tests

```sh
.venv/bin/pytest
```
