"""Lucide icons (assets/icons, ISC licence) as reportlab drawings, coloured by importance."""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from reportlab.graphics.shapes import Circle, Drawing, Group
from reportlab.lib.colors import HexColor
from svglib.svglib import svg2rlg

from .taxonomy import IMPORTANCE_COLOR

ASSETS = Path(__file__).parent / "assets"
WHITE = "#FFFFFF"


@lru_cache(maxsize=None)
def _shape(name: str, stroke: str) -> tuple[Group, float]:
    """Parse once per (icon, colour); every placement wraps the same shapes."""
    svg = (ASSETS / "icons" / f"{name}.svg").read_text().replace("currentColor", stroke)
    drawing = svg2rlg(io.BytesIO(svg.encode()))
    return Group(*drawing.contents), drawing.width


def icon(name: str, size: float, stroke: str, disc: str | None = None) -> Drawing:
    """An icon `size` points square; with `disc`, drawn small inside a filled circle."""
    shape, native = _shape(name, stroke)
    out = Drawing(size, size)
    inner = size
    if disc:
        out.add(Circle(size / 2, size / 2, size / 2, fillColor=HexColor(disc), strokeColor=None))
        inner = size * 0.62
    scale = inner / native
    offset = (size - inner) / 2
    out.add(Group(shape, transform=(scale, 0, 0, scale, offset, offset)))
    return out


def importance_icon(name: str, importance: str, size: float) -> Drawing:
    """Major: white icon on red. Notable: black icon on amber. Standard: black. Minor: grey."""
    if importance == "major":
        return icon(name, size, WHITE, IMPORTANCE_COLOR["major"])
    if importance == "notable":
        return icon(name, size, IMPORTANCE_COLOR["standard"], IMPORTANCE_COLOR["notable"])
    return icon(name, size, IMPORTANCE_COLOR[importance])
