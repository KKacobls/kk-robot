"""Multi-display screen capture and template matching in physical desktop coordinates."""
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
    """Prevent per-monitor scaling from offsetting native mouse/screenshot coordinates."""
    if os.name != 'nt':
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def decode_template(value: str) -> np.ndarray:
    if not isinstance(value, str) or not value.startswith('data:image/'):
        raise ValueError('請先貼上 PNG、JPEG 或 WebP 圖片')
    header, sep, data = value.partition(',')
    if not sep or not any(header.startswith(f'data:image/{kind};base64') for kind in ('png', 'jpeg', 'webp')):
        raise ValueError('只支援 PNG、JPEG、WebP 圖片')
    if len(data) > MAX_IMAGE_BYTES * 4 // 3 + 8:
        raise ValueError('圖片超過 3MB，請縮小截圖範圍')
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('圖片編碼錯誤') from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('圖片超過 3MB，請縮小截圖範圍')
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError('無法解碼圖片')
    if image.shape[0] * image.shape[1] > MAX_PIXELS:
        raise ValueError('圖片解析度太大，請縮小截圖範圍')
    if len(image.shape) == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    elif image.shape[2] == 4:
        alpha = image[:, :, 3:4].astype(np.float32) / 255
        image = (image[:, :, :3] * alpha + 255 * (1 - alpha)).astype(np.uint8)
    else:
        image = image[:, :, :3]
    return np.ascontiguousarray(image)


def normalize_monitor(value: str | int | None) -> str:
    if value is None or value == '' or value == 'all' or value == 0 or value == '0':
        return 'all'
    if isinstance(value, bool):
        raise ValueError('螢幕編號不合法')
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise ValueError('螢幕編號不合法') from None
    if number < 1 or number > 32 or str(value).strip() != str(number):
        raise ValueError('螢幕編號不合法')
    return str(number)


def _screen_info(monitor: dict, index: int) -> dict:
    return {
        'id': str(index), 'name': '全部螢幕' if index == 0 else f'螢幕 {index}',
        'left': int(monitor['left']), 'top': int(monitor['top']),
        'width': int(monitor['width']), 'height': int(monitor['height']),
    }


def list_monitors() -> list[dict]:
    """MSS index 0 is the virtual desktop; 1..N enumerate physical monitors."""
    try:
        import mss
        with mss.mss() as sct:
            return [_screen_info(m, i) for i, m in enumerate(sct.monitors)]
    except Exception as exc:
        raise RuntimeError(f'無法列出螢幕。請在有登入桌面的 Windows 執行：{exc}') from exc


@dataclass
class Screen:
    bgr: np.ndarray
    left: int
    top: int
    monitors: list[dict] | None = None
    monitor_id: str = 'all'


def capture_screen(monitor: str | int = 'all') -> Screen:
    """Capture one physical monitor, or full virtual desktop incl. negative coordinates."""
    selection = normalize_monitor(monitor)
    try:
        import mss
        with mss.mss() as sct:
            monitors = [_screen_info(m, i) for i, m in enumerate(sct.monitors)]
            index = 0 if selection == 'all' else int(selection)
            if index >= len(monitors):
                raise ValueError(f'螢幕 {selection} 不存在；請重新選擇')
            info = monitors[index]
            shot = sct.grab(sct.monitors[index])
            bgr = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
            return Screen(bgr=bgr, left=info['left'], top=info['top'],
                          monitors=monitors if index == 0 else [info], monitor_id=selection)
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f'無法擷取桌面畫面。請在有登入桌面的 Windows 執行：{exc}') from exc


def _match_one(frame: np.ndarray, template: np.ndarray) -> tuple[float, tuple[int, int]]:
    frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    target_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    if float(np.std(target_gray)) < 2.0:
        results = cv2.matchTemplate(frame_gray, target_gray, cv2.TM_SQDIFF_NORMED)
        min_val, _, min_loc, _ = cv2.minMaxLoc(results)
        return max(0.0, 1.0 - float(min_val)), min_loc
    results = cv2.matchTemplate(frame_gray, target_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(results)
    return max(0.0, float(max_val)), max_loc


def match_image(screen: Screen, template: np.ndarray, threshold: float = 0.84) -> dict[str, Any]:
    """Scan each physical monitor independently; never match over a virtual-screen gap."""
    frame = screen.bgr
    h, w = template.shape[:2]
    if w < 4 or h < 4:
        raise ValueError('圖片至少需要 4×4 像素')
    candidates: list[dict] = []
    regions = screen.monitors
    if regions:
        regions = [m for m in regions if m['id'] != '0']
    else:
        regions = [{'id': screen.monitor_id, 'left': screen.left, 'top': screen.top,
                    'width': frame.shape[1], 'height': frame.shape[0]}]
    for monitor in regions:
        x = monitor['left'] - screen.left
        y = monitor['top'] - screen.top
        mw, mh = monitor['width'], monitor['height']
        if mw < w or mh < h:
            continue
        subframe = frame[y:y+mh, x:x+mw]
        if subframe.shape[0] < h or subframe.shape[1] < w:
            continue
        score, (px, py) = _match_one(subframe, template)
        candidates.append({
            'monitor': monitor['id'], 'score': round(score, 4),
            'rect': {'x': monitor['left'] + px, 'y': monitor['top'] + py, 'w': w, 'h': h},
        })
    if not candidates:
        raise ValueError('範本比所有已選螢幕的可辨識區域還大，請重新截圖或選擇其他螢幕')
    best = max(candidates, key=lambda item: item['score'])
    return {'found': best['score'] >= max(.1, min(1.0, float(threshold))),
            'score': best['score'], 'rect': best['rect'], 'monitor': best['monitor'],
            'candidates': candidates}


def detect(image_data: str, threshold: float = 0.84, screen: Screen | None = None,
           monitor: str | int = 'all') -> dict[str, Any]:
    template = decode_template(image_data)
    selected = screen if screen is not None else capture_screen(monitor)
    return match_image(selected, template, threshold)


def annotated_preview(image_data: str | None, threshold: float = .84,
                      click_x: float = .5, click_y: float = .5,
                      monitor: str | int = 'all') -> tuple[bytes, dict[str, Any]]:
    screen = capture_screen(monitor)
    preview = screen.bgr.copy()
    result: dict[str, Any] = {'found': False, 'score': None, 'rect': None, 'monitor': None}
    if screen.monitor_id == 'all' and screen.monitors:
        for info in screen.monitors[1:]:
            x = info['left'] - screen.left
            y = info['top'] - screen.top
            cv2.rectangle(preview, (x+1, y+1), (x+info['width']-2, y+info['height']-2), (154, 134, 107), 2)
            cv2.putText(preview, f"Monitor {info['id']}", (x+13,y+28),
                        cv2.FONT_HERSHEY_SIMPLEX, .75, (235, 206, 142), 2)
    if image_data:
        result = detect(image_data, threshold, screen)
        r = result['rect']
        x1, y1 = r['x'] - screen.left, r['y'] - screen.top
        x2, y2 = x1 + r['w'], y1 + r['h']
        color = (75, 215, 102) if result['found'] else (64, 149, 255)
        cv2.rectangle(preview, (x1, y1), (x2, y2), color, 3)
        title = f"MON {result['monitor']} {'MATCH' if result['found'] else 'LOW'} {result['score']:.3f}"
        cv2.putText(preview, title, (max(8, x1), max(28, y1 - 10)), cv2.FONT_HERSHEY_SIMPLEX, .69, color, 2)
        px = x1 + round(r['w'] * max(0, min(1, click_x)))
        py = y1 + round(r['h'] * max(0, min(1, click_y)))
        cv2.drawMarker(preview, (px, py), (255, 230, 70), cv2.MARKER_CROSS, 25, 2)
    h, w = preview.shape[:2]
    if w > 1600:
        preview = cv2.resize(preview, (1600, round(h * 1600 / w)), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError('無法建立預覽畫面')
    return data.tobytes(), result


def capture_png_data_url(monitor: str | int = 'all') -> str:
    screen = capture_screen(monitor)
    ok, data = cv2.imencode('.png', screen.bgr)
    if not ok:
        raise RuntimeError('無法建立螢幕截圖')
    return 'data:image/png;base64,' + base64.b64encode(data.tobytes()).decode('ascii')
