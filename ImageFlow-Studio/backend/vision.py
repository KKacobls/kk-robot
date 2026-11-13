"""Screen capture and OpenCV image matching; screen positions are physical pixels."""
from __future__ import annotations

import base64
import binascii
import ctypes
import os
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 5_000_000


def windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def decode_template(value: str) -> np.ndarray:
    if not isinstance(value, str) or not value.startswith("data:image/"):
        raise ValueError("請先貼上 PNG、JPEG 或 WebP 圖片")
    header, sep, data = value.partition(",")
    if not sep or not any(header.startswith(f"data:image/{kind};base64") for kind in ("png", "jpeg", "webp")):
        raise ValueError("只支援 PNG、JPEG、WebP 圖片")
    if len(data) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
        raise ValueError("圖片超過 3MB，請縮小截圖範圍")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("圖片編碼錯誤") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("圖片超過 3MB，請縮小截圖範圍")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("無法解碼圖片")
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise ValueError("圖片解析度太大，請縮小截圖範圍")
    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.shape[2] == 4:
        # Paste transparency on white, so transparent borders have predictable values.
        alpha = image[:, :, 3:4].astype(np.float32) / 255
        image = (image[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    else:
        image = image[:, :, :3]
    return np.ascontiguousarray(image)


@dataclass
class Screen:
    bgr: np.ndarray
    left: int
    top: int


def capture_screen() -> Screen:
    # Lazy import: backend API and unit tests also work without an attached display.
    try:
        import mss
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # full virtual desktop, including multiple screens
            shot = sct.grab(monitor)
            bgr = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
            return Screen(bgr=bgr, left=int(monitor["left"]), top=int(monitor["top"]))
    except Exception as exc:
        raise RuntimeError(f"無法擷取桌面畫面。請在有登入桌面的 Windows 執行：{exc}") from exc


def match_image(screen: Screen, template: np.ndarray, threshold: float = 0.84) -> dict[str, Any]:
    frame = screen.bgr
    h, w = template.shape[:2]
    if w > frame.shape[1] or h > frame.shape[0]:
        raise ValueError("截圖比目前螢幕還大，請重新擷取範本")
    if w < 4 or h < 4:
        raise ValueError("圖片至少需要 4×4 像素")

    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    target_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    # Uniform templates make TM_CCOEFF_NORMED report pathological 1.0 scores.
    if float(np.std(target_gray)) < 2.0:
        results = cv2.matchTemplate(frame_gray, target_gray, cv2.TM_SQDIFF_NORMED)
        min_val, _, min_loc, _ = cv2.minMaxLoc(results)
        score = float(1.0 - min_val)
        x, y = min_loc
    else:
        results = cv2.matchTemplate(frame_gray, target_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(results)
        score = max(0.0, float(max_val))
        x, y = max_loc
    return {
        "found": score >= max(0.1, min(1.0, float(threshold))),
        "score": round(score, 4),
        "rect": {"x": screen.left + int(x), "y": screen.top + int(y), "w": int(w), "h": int(h)},
    }


def detect(image_data: str, threshold: float = 0.84, screen: Screen | None = None) -> dict[str, Any]:
    template = decode_template(image_data)
    return match_image(screen or capture_screen(), template, threshold)


def annotated_preview(image_data: str | None, threshold: float = 0.84, click_x: float = 0.5, click_y: float = 0.5) -> tuple[bytes, dict[str, Any]]:
    screen = capture_screen()
    preview = screen.bgr.copy()
    result: dict[str, Any] = {"found": False, "score": None, "rect": None}
    if image_data:
        result = detect(image_data, threshold, screen)
        r = result["rect"]
        x1, y1 = r["x"] - screen.left, r["y"] - screen.top
        x2, y2 = x1 + r["w"], y1 + r["h"]
        color = (75, 215, 102) if result["found"] else (64, 149, 255)
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, 3)
        title = f'{"MATCH" if result["found"] else "LOW"} {result["score"]:.3f}'
        cv2.putText(preview, title, (max(8, x1), max(25, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.74, color, 2)
        px = x1 + round(r["w"] * max(0, min(1, click_x)))
        py = y1 + round(r["h"] * max(0, min(1, click_y)))
        cv2.drawMarker(preview, (px, py), (255, 230, 70), cv2.MARKER_CROSS, 25, 2)
    # Downscale only the preview, not the coordinates reported by the matcher.
    h, w = preview.shape[:2]
    if w > 1600:
        size = (1600, round(h * 1600 / w))
        preview = cv2.resize(preview, size, interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError("無法建立預覽畫面")
    return data.tobytes(), result


def capture_png_data_url() -> str:
    screen = capture_screen()
    ok, data = cv2.imencode(".png", screen.bgr)
    if not ok:
        raise RuntimeError("無法建立螢幕截圖")
    return "data:image/png;base64," + base64.b64encode(data.tobytes()).decode("ascii")
