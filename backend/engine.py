"""Safe-by-default, local desktop workflow runtime."""
from __future__ import annotations

import copy
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .vision import capture_screen, decode_template, match_image, normalize_monitor

NODE_TYPES = {"start", "condition", "click", "wait", "hotkey", "end"}
OUTPUTS = {
    "start": {"next"},
    "condition": {"found", "missing"},
    "click": {"found", "missing"},
    "wait": {"next"},
    "hotkey": {"next"},
    "end": set(),
}


def verify_flow(flow: dict[str, Any]) -> None:
    nodes = flow.get("nodes")
    edges = flow.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("流程圖必須包含 nodes 與 edges 陣列")
    if not nodes or len(nodes) > 150 or len(edges) > 350:
        raise ValueError("節點數必須介於 1 至 150，連線至多 350 條")
    by_id: dict[str, dict] = {}
    starts = []
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") not in NODE_TYPES:
            raise ValueError("包含不支援的節點類型")
        nid = node.get("id")
        if not isinstance(nid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}", nid) or nid in by_id:
            raise ValueError("節點 ID 不合法或重複")
        by_id[nid] = node
        if node["type"] == "start":
            starts.append(node)
        if node["type"] in {"condition", "click"} and not node.get("image"):
            raise ValueError(f"「{node.get('title', nid)}」還沒有圖片，請先貼上")
        if node["type"] in {"condition", "click"}:
            decode_template(node["image"])
            normalize_monitor(node.get("monitor", "all"))
    if len(starts) != 1:
        raise ValueError("每個流程必須剛好有一個『開始』節點")
    seen_ports = set()
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("連線格式錯誤")
        source, target, port = edge.get("source"), edge.get("target"), edge.get("sourceHandle")
        if source not in by_id or target not in by_id:
            raise ValueError("連線指向不存在的節點")
        if port not in OUTPUTS[by_id[source]["type"]]:
            raise ValueError("節點的輸出連接點不正確")
        if (source, port) in seen_ports:
            raise ValueError("同一個輸出只能接到一個節點")
        seen_ports.add((source, port))
    if len(str(flow)) > 22_000_000:
        raise ValueError("工作流檔案太大")


class Desktop:
    """Separate adapter so tests can avoid real mouse and screen interactions."""
    def capture(self, monitor: str = 'all'):
        return capture_screen(monitor)

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        # pynput works with native Windows virtual-desktop coordinates, including
        # negative coordinates on screens left/above the primary monitor.
        from pynput.mouse import Controller, Button
        mouse = Controller()
        if 0 <= mouse.position[0] <= 2 and 0 <= mouse.position[1] <= 2:
            raise RuntimeError('滑鼠位於主螢幕左上角，已觸發緊急停止')
        mouse.position = (x, y)
        mouse.click({'left': Button.left, 'right': Button.right, 'middle': Button.middle}[button], clicks)

    def hotkey(self, keys: list[str]) -> None:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.hotkey(*keys)


class WorkflowEngine:
    def __init__(self, desktop: Desktop | None = None):
        self.desktop = desktop or Desktop()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._listener = None
        self._state: dict[str, Any] = {}
        self._reset()

    def _reset(self) -> None:
        self._state = {
            "status": "idle", "current": None, "lastEdge": None,
            "lastMatch": None, "history": [], "logs": [], "dryRun": True,
            "startedAt": None, "finishedAt": None, "error": None,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def _patch(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _log(self, message: str, node_id: str | None = None) -> None:
        with self._lock:
            self._state["logs"].append({
                "time": datetime.now().strftime("%H:%M:%S"), "node": node_id, "message": message,
            })
            self._state["logs"] = self._state["logs"][-160:]

    def _start_listener(self) -> None:
        try:
            from pynput import keyboard
            def on_press(key):
                if key == keyboard.Key.f8:
                    self.stop()
            self._listener = keyboard.Listener(on_press=on_press)
            self._listener.daemon = True
            self._listener.start()
        except Exception:
            self._log("F8 監聽不可用，仍可按網頁停止或把滑鼠移至左上角")

    def start(self, flow: dict, dry_run: bool = True) -> None:
        verify_flow(flow)
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise RuntimeError("已有流程正在執行")
            self._stop.clear()
            self._reset()
            self._state.update({"status": "running", "dryRun": bool(dry_run), "startedAt": datetime.now().isoformat(timespec="seconds")})
            snapshot = copy.deepcopy(flow)
            self._worker = threading.Thread(target=self._run, args=(snapshot, bool(dry_run)), daemon=True)
            self._start_listener()
            self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._log("使用者要求停止（F8／停止按鈕）")

    def _find(self, node: dict, timeout: float) -> dict:
        threshold = max(0.1, min(1.0, float(node.get("threshold", 0.84))))
        template = decode_template(node["image"])
        deadline = time.monotonic() + max(0, min(float(timeout), 120))
        while not self._stop.is_set():
            monitor = normalize_monitor(node.get('monitor', 'all'))
            frame = self.desktop.capture(monitor) if monitor != 'all' else self.desktop.capture()
            match = match_image(frame, template, threshold)
            self._patch(lastMatch=match)
            if match["found"] or time.monotonic() >= deadline:
                return match
            if self._stop.wait(0.3):
                break
        return {"found": False, "score": 0, "rect": None}

    def _run(self, flow: dict, dry_run: bool) -> None:
        by_id = {n["id"]: n for n in flow["nodes"]}
        edges = {(e["source"], e["sourceHandle"]): e for e in flow["edges"]}
        current = next(n["id"] for n in flow["nodes"] if n["type"] == "start")
        step_limit = min(2000, max(30, int(flow.get("maxSteps", 500))))
        steps = 0
        try:
            self._log("開始執行：" + ("模擬模式（不會點擊）" if dry_run else "實際操作模式"))
            while not self._stop.is_set():
                steps += 1
                if steps > step_limit:
                    raise RuntimeError(f"已執行 {step_limit} 個節點，為避免無限迴圈已停止")
                node = by_id[current]
                nid, typ = node["id"], node["type"]
                with self._lock:
                    self._state["current"] = nid
                    self._state["history"].append(nid)
                    self._state["history"] = self._state["history"][-80:]
                self._log(f'執行「{node.get("title", typ)}」', nid)
                output = "next"
                if typ == "end":
                    self._patch(status="finished", current=nid)
                    self._log("已抵達結束節點", nid)
                    break
                if typ == "condition":
                    result = self._find(node, node.get("timeout", 0))
                    output = "found" if result["found"] else "missing"
                    self._log(f'辨識 {"成功" if result["found"] else "失敗"}（信心值 {result["score"]:.3f}）', nid)
                    if result["found"] and node.get("clickOnFound", False):
                        self._click_match(node, result, dry_run)
                elif typ == "click":
                    result = self._find(node, node.get("timeout", 3))
                    output = "found" if result["found"] else "missing"
                    if result["found"]:
                        self._click_match(node, result, dry_run)
                    else:
                        self._log(f'找不到要點擊的圖片（信心值 {result["score"]:.3f}）', nid)
                elif typ == "wait":
                    sec = max(0, min(120, float(node.get("seconds", 1))))
                    self._log(f"等待 {sec:g} 秒", nid)
                    self._stop.wait(sec)
                elif typ == "hotkey":
                    keys = [x.strip().lower() for x in str(node.get("keys", "enter")).split("+") if x.strip()]
                    allowed = {"ctrl", "alt", "shift", "win", "enter", "esc", "tab", "space", "backspace", "delete", "up", "down", "left", "right", "home", "end", "pageup", "pagedown"} | {f"f{i}" for i in range(1, 13)} | set("abcdefghijklmnopqrstuvwxyz0123456789")
                    if not keys or len(keys) > 5 or any(k not in allowed for k in keys):
                        raise ValueError("按鍵格式錯誤，例如 ctrl+s、enter、alt+tab")
                    if not dry_run:
                        self.desktop.hotkey(keys)
                    self._log(f'{"模擬" if dry_run else "送出"}按鍵：{ "+".join(keys)}', nid)
                if self._stop.is_set():
                    break
                edge = edges.get((nid, output))
                if not edge:
                    self._log(f"「{node.get('title', typ)}」的 {output} 沒有接線，流程自然結束", nid)
                    self._patch(status="finished")
                    break
                self._patch(lastEdge={"source": nid, "sourceHandle": output, "target": edge["target"]})
                current = edge["target"]
            if self._stop.is_set():
                self._patch(status="stopped", current=None)
                self._log("流程已停止")
        except Exception as exc:
            self._patch(status="error", error=str(exc))
            self._log(f"執行錯誤：{exc}", current)
        finally:
            self._patch(finishedAt=datetime.now().isoformat(timespec="seconds"))
            if self._listener is not None:
                try:
                    self._listener.stop()
                except Exception:
                    pass
                self._listener = None

    def _click_match(self, node: dict, result: dict, dry_run: bool) -> None:
        r = result["rect"]
        px = max(0, min(1, float(node.get("clickX", 0.5))))
        py = max(0, min(1, float(node.get("clickY", 0.5))))
        x = r["x"] + min(r["w"] - 1, round(px * r["w"]))
        y = r["y"] + min(r["h"] - 1, round(py * r["h"]))
        button = node.get("button", "left")
        if button not in ("left", "right", "middle"):
            raise ValueError("不支援的滑鼠按鍵")
        clicks = 2 if node.get("doubleClick") else 1
        if not dry_run:
            self.desktop.click(x, y, button, clicks)
        self._log(f'{"模擬點擊" if dry_run else "點擊"}座標 ({x}, {y})，比對範圍 {r["w"]}×{r["h"]}', node["id"])
