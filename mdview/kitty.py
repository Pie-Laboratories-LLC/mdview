"""Kitty graphics protocol using Unicode placeholders.

Images are transmitted once with a *virtual* placement (``U=1``). They are then
displayed by printing U+10EEEE cells whose foreground colour encodes the image
id and whose combining diacritics encode the row/column. Because the image is
"just text", it scrolls, clips and redraws with the rest of the TUI.
"""

from __future__ import annotations

import base64
import fcntl
import io
import itertools
import random
import struct
import sys
import termios
from typing import Callable

from PIL import Image

from ._diacritics import DIACRITICS

PLACEHOLDER = "\U0010EEEE"
MAX_CELLS = len(DIACRITICS)


def cell_pixel_size(default: tuple[int, int] = (10, 20)) -> tuple[int, int]:
    """(width, height) of one terminal cell in pixels."""
    for stream in (sys.__stdout__, sys.__stdin__, sys.__stderr__):
        try:
            if stream is None or not stream.isatty():
                continue
            buf = fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols, xpix, ypix = struct.unpack("HHHH", buf)
            if rows and cols and xpix and ypix:
                return max(1, xpix // cols), max(1, ypix // rows)
        except OSError:
            continue
    return default


def _apc(control: str, payload: str = "") -> str:
    return f"\x1b_G{control};{payload}\x1b\\" if payload else f"\x1b_G{control}\x1b\\"


def transmit_sequence(image_id: int, png: bytes, cols: int, rows: int) -> str:
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[i:i + 4096] for i in range(0, len(data), 4096)] or [""]
    out = []
    for n, chunk in enumerate(chunks):
        more = 1 if n < len(chunks) - 1 else 0
        if n == 0:
            ctl = f"a=T,U=1,f=100,t=d,i={image_id},c={cols},r={rows},q=2,m={more}"
        else:
            ctl = f"m={more},q=2"
        out.append(_apc(ctl, chunk))
    return "".join(out)


def delete_sequence(image_id: int) -> str:
    return _apc(f"a=d,d=I,i={image_id},q=2")


def placeholder_row(row: int, cols: int) -> str:
    r = chr(DIACRITICS[row])
    return "".join(PLACEHOLDER + r + chr(DIACRITICS[c]) for c in range(cols))


class KittyImages:
    """Tracks which (image, size) pairs have been sent to the terminal."""

    def __init__(self, write: Callable[[str], None]):
        self._write = write
        self._ids: dict[tuple, int] = {}
        # Random high base so we don't collide with other programs' images.
        self._counter = itertools.count(random.randint(0x200000, 0x900000))
        self.cell = cell_pixel_size()

    def refresh_cell_size(self) -> None:
        self.cell = cell_pixel_size(self.cell)

    def fit(self, img: Image.Image, max_cols: int, max_rows: int, upscale: float = 1.0) -> tuple[int, int]:
        """Cell box for an image: native size (times ``upscale``), shrunk to fit."""
        cw, ch = self.cell
        w = img.width * upscale / cw
        h = img.height * upscale / ch
        max_cols = max(1, min(max_cols, MAX_CELLS))
        max_rows = max(1, min(max_rows, MAX_CELLS))
        scale = min(1.0, max_cols / w if w else 1.0, max_rows / h if h else 1.0)
        return max(1, round(w * scale)), max(1, round(h * scale))

    def place(self, key: tuple, img: Image.Image, cols: int, rows: int) -> int:
        """Ensure an image is transmitted for a cols x rows box; return its id."""
        k = (key, cols, rows, self.cell)
        image_id = self._ids.get(k)
        if image_id is None:
            image_id = next(self._counter) & 0xFFFFFF
            cw, ch = self.cell
            target = (max(1, cols * cw), max(1, rows * ch))
            fitted = img.copy()
            fitted.thumbnail(target, Image.Resampling.LANCZOS)  # only ever shrinks
            if fitted.width < target[0] and fitted.height < target[1]:
                s = min(target[0] / fitted.width, target[1] / fitted.height)
                fitted = fitted.resize(
                    (max(1, round(fitted.width * s)), max(1, round(fitted.height * s))),
                    Image.Resampling.LANCZOS,
                )
            buf = io.BytesIO()
            fitted.save(buf, "PNG", compress_level=1)
            self._write(transmit_sequence(image_id, buf.getvalue(), cols, rows))
            self._ids[k] = image_id
        return image_id

    def clear(self) -> None:
        if self._ids:
            self._write("".join(delete_sequence(i) for i in self._ids.values()))
        self._ids.clear()
