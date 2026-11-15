"""Classical CV plot detector calibrated from a single game screenshot.

No game automation, network access or model download is performed here.
The 'blocked' label means a plot with visible gray rocks, not a verified
statement about the game's actual planting rules.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np

LABELS = {
    "empty": "Empty / 可種",
    "water": "Water / 水珠",
    "blocked": "Rocks / 石塊",
}
# BGR, also used to draw OpenCV preview images.
COLORS = {
    "empty": (70, 220, 90),
    "water": (255, 220, 0),
    "blocked": (50, 70, 245),
}


@dataclass(frozen=True)
class Plot:
    x: int
    y: int
    w: int
    h: int
    kind: str
    partial: bool = False
    water_pixels: int = 0
    rock_pixels: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _get_roi(frame: np.ndarray, roi: tuple[int, int, int, int] | None):
    height, width = frame.shape[:2]
    if roi is None:
        return frame, 0, 0
    x, y, w, h = (int(v) for v in roi)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        return frame[:0, :0], x0, y0
    return frame[y0:y1, x0:x1], x0, y0


def detect_plots(bgr: np.ndarray, roi: tuple[int, int, int, int] | None = None) -> list[Plot]:
    """Find plots from BGR image. Coordinates are relative to the input image.

    HSV soil segmentation -> repeated plot-sized components -> classify the
    middle of each plot by cyan droplet and pale rock colors. Rotated game
    graphics or changed textures may need recalibration.
    """
    if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3 or not bgr.size:
        return []
    crop, ox, oy = _get_roi(bgr, roi)
    if crop.size == 0:
        return []
    height, width = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    # Brown/orange plot ground: excludes green grass, blue water, gray rocks.
    soil = cv2.inRange(hsv, (5, 53, 55), (24, 255, 255))
    if min(width, height) >= 230:
        soil = cv2.morphologyEx(soil, cv2.MORPH_CLOSE, np.ones((3, 3), dtype=np.uint8))

    _, _, stats, _ = cv2.connectedComponentsWithStats(soil, connectivity=8)
    candidates: list[tuple[int, int, int, int, int]] = []
    for x, y, w, h, area in stats[1:]:
        x, y, w, h, area = map(int, (x, y, w, h, area))
        if w < 16 or h < 10 or area < 95:
            continue
        # Prevent large UI/background components from changing cluster scale.
        if w > width * .8 or h > height * .8:
            continue
        candidates.append((x, y, w, h, area))

    # Get the most common complete plot dimensions, rather than assuming a
    # fixed display resolution or emulator scale.
    regular = [(w, h) for _, _, w, h, a in candidates
               if 1.55 <= w / h <= 2.3 and a / (w * h) >= .42]
    if len(regular) < 2:
        return []  # Avoid pretending one isolated brown box is a farm.
    best_w, best_h = max(
        regular,
        key=lambda s: (
            sum(abs(w - s[0]) <= .13 * s[0] and abs(h - s[1]) <= .22 * s[1]
                for w, h in regular),
            s[0] * s[1],
        ),
    )
    cluster = [(w, h) for w, h in regular if abs(w - best_w) <= .13 * best_w
               and abs(h - best_h) <= .22 * best_h]
    ref_w = float(np.median([w for w, _ in cluster]))
    ref_h = float(np.median([h for _, h in cluster]))
    if len(cluster) < 3:
        return []

    plots: list[Plot] = []
    for x, y, w, h, area in candidates:
        edge = x <= 1 or y <= 1 or (x + w >= width - 1) or (y + h >= height - 1)
        full = .77 * ref_w <= w <= 1.22 * ref_w and .77 * ref_h <= h <= 1.25 * ref_h
        clipped = (edge and .35 * ref_w <= w <= 1.22 * ref_w
                   and .52 * ref_h <= h <= 1.25 * ref_h
                   and area >= .25 * ref_w * ref_h)
        if not (full or clipped):
            continue
        if area / (w * h) < .30:
            continue
        # Test the middle region; corners often contain grass or adjacent plots.
        x0, x1 = x + round(w * .15), x + round(w * .84)
        y0, y1 = y + round(h * .20), y + round(h * .82)
        inner = hsv[y0:y1, x0:x1]
        if inner.size == 0:
            continue
        hi, si, vi = cv2.split(inner)
        water_pixels = int(np.count_nonzero((hi > 80) & (hi < 115) & (si > 100) & (vi > 170)))
        rock_pixels = int(np.count_nonzero((si < 80) & (vi > 95) & (vi < 240)))
        inner_area = inner.shape[0] * inner.shape[1]
        if water_pixels >= max(7, inner_area * .035):
            kind = "water"
        elif rock_pixels >= max(7, inner_area * .023):
            kind = "blocked"
        else:
            kind = "empty"
        plots.append(Plot(x + ox, y + oy, w, h, kind, edge, water_pixels, rock_pixels))
    plots.sort(key=lambda p: (p.y, p.x))
    return plots


def summarize(plots: Iterable[Plot]) -> dict[str, int]:
    amounts = Counter(p.kind for p in plots)
    return {"empty": amounts["empty"], "water": amounts["water"],
            "blocked": amounts["blocked"], "total": sum(amounts.values())}


def draw_boxes(bgr: np.ndarray, plots: Iterable[Plot], *, summary: bool = True,
               roi: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """Draw detection on a copy. Partial border plots use a dashed label *."""
    output = bgr.copy()
    items = list(plots)
    height, width = output.shape[:2]
    line_width = max(1, round(width / 850))
    font_scale = max(.38, min(1, width / 1250))
    for plot in items:
        color = COLORS[plot.kind]
        x, y, w, h = plot.x, plot.y, plot.w, plot.h
        cv2.rectangle(output, (x, y), (x + w, y + h), color, line_width)
        tag = {"empty": "E", "water": "W", "blocked": "R"}[plot.kind]
        if plot.partial:
            tag += "*"
        y_text = max(12, y - 3)
        if y_text <= 12:
            y_text = min(height - 2, y + 15)
        cv2.putText(output, tag, (x + 2, y_text), cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale, (0, 0, 0), line_width + 2, cv2.LINE_AA)
        cv2.putText(output, tag, (x + 2, y_text), cv2.FONT_HERSHEY_SIMPLEX,
                    font_scale, color, line_width, cv2.LINE_AA)
    if roi is not None:
        x, y, w, h = roi
        cv2.rectangle(output, (x, y), (x + w, y + h), (240, 240, 240), 1)
    if summary:
        counts = summarize(items)
        bar_height = 50
        # Add a separate header: do not hide any top-edge farm tiles.
        output = cv2.copyMakeBorder(output, bar_height, 0, 0, 0,
                                    cv2.BORDER_CONSTANT, value=(17, 25, 34))
        fs = max(.37, min(.75, width / 1100))
        lines = [f"Empty: {counts['empty']} | Water: {counts['water']} | Rocks: {counts['blocked']}",
                 f"Total visible: {counts['total']}  (* = partial tile)"]
        for idx, line in enumerate(lines):
            cv2.putText(output, line, (7, 18 + idx * 23),
                        cv2.FONT_HERSHEY_SIMPLEX, fs, (239, 248, 249), 1, cv2.LINE_AA)
    return output