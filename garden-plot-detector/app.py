"""Garden Plot Detector: local screenshot / live detection GUI.

CLI: python app.py --image examples/sample.png --output annotated.png
GUI: python app.py  (Windows, supports multi-monitor and optional overlay)
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
from pathlib import Path
import sys
import threading
import time
import traceback

import cv2
import numpy as np

from detector import COLORS, LABELS, Plot, detect_plots, draw_boxes, summarize

HERE = Path(__file__).resolve().parent


def set_dpi_awareness() -> None:
    """Ensure MSS screenshot coordinates agree with overlay window geometry."""
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class OverlayWindow:
    """Transparent, topmost, mouse-click-through Tk window for a monitor."""
    def __init__(self, root, mon: dict):
        import tkinter as tk
        self.tk = tk
        self.mon = dict(mon)
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg="#f900fa")
        self.window.attributes("-topmost", True)
        try:
            self.window.wm_attributes("-transparentcolor", "#f900fa")
        except tk.TclError:
            self.window.destroy()
            raise RuntimeError("This overlay requires Windows transparency support")
        x, y = int(mon["left"]), int(mon["top"])
        w, h = int(mon["width"]), int(mon["height"])
        self.window.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self.canvas = tk.Canvas(self.window, bg="#f900fa", bd=0, highlightthickness=0,
                                width=w, height=h, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self.window.deiconify()
        self.window.update_idletasks()
        self._enable_clickthrough()

    def _enable_clickthrough(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Desktop overlay currently supports Windows only")
        user = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        user.GetParent.restype = ctypes.c_void_p
        user.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = int(self.window.winfo_id())
        wrapper = int(user.GetParent(ctypes.c_void_p(hwnd)) or 0)
        if hasattr(user, "GetWindowLongPtrW"):
            getter, setter = user.GetWindowLongPtrW, user.SetWindowLongPtrW
            getter.restype = ctypes.c_ssize_t
            setter.restype = ctypes.c_ssize_t
            getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
            setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        else:
            getter, setter = user.GetWindowLongW, user.SetWindowLongW
            getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
            setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        for handle in {hwnd, wrapper} - {0}:
            h = ctypes.c_void_p(handle)
            style = getter(h, GWL_EXSTYLE)
            setter(h, GWL_EXSTYLE,
                   style | WS_EX_LAYERED | WS_EX_TRANSPARENT
                   | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

    def draw(self, plots: list[Plot], capture_monitor: dict, counts: dict[str, int]) -> None:
        self.canvas.delete("all")
        offset_x = capture_monitor["left"] - self.mon["left"]
        offset_y = capture_monitor["top"] - self.mon["top"]
        colors = {"empty": "#31d581", "water": "#36d9f4", "blocked": "#fc617b"}
        total = 0
        for p in plots:
            x, y = p.x + offset_x, p.y + offset_y
            if x + p.w < 0 or y + p.h < 0 or x >= self.mon["width"] or y >= self.mon["height"]:
                continue
            total += 1
            color = colors[p.kind]
            self.canvas.create_rectangle(x, y, x + p.w, y + p.h,
                                         outline=color, width=2, tags="box")
            label = {"empty": "EMPTY", "water": "WATER", "blocked": "ROCK"}[p.kind]
            if p.partial:
                label += "*"
            # Draw a text label with dark outline for contrast.
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                self.canvas.create_text(x + 4 + dx, max(15, y - 7) + dy,
                                        text=label, anchor="w", fill="#101725",
                                        font=("Segoe UI", 10, "bold"))
            self.canvas.create_text(x + 4, max(15, y - 7), text=label,
                                    anchor="w", fill=color, font=("Segoe UI", 10, "bold"))
        # A small always-visible count bar; the main GUI has full counts.
        if total:
            summary = (f"Empty {counts['empty']}  |  Water {counts['water']}  |  "
                       f"Rocks {counts['blocked']}")
            self.canvas.create_rectangle(9, 6, 300, 36, fill="#141c28", outline="")
            self.canvas.create_text(17, 20, text=summary, anchor="w", fill="#f6f8fb",
                                    font=("Segoe UI", 10, "bold"))

    def destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass


class LiveApp:
    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox, ttk
        from PIL import Image, ImageTk
        import mss

        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.Image, self.ImageTk = Image, ImageTk
        self.monitors = []
        with mss.mss() as cap:
            self.monitors = [dict(m) for m in cap.monitors]
        if len(self.monitors) < 2:
            raise RuntimeError("No screen was detected. Please run on a desktop session.")

        self.root = tk.Tk()
        self.root.title("Garden Plot Detector | 即時田地辨識")
        self.root.geometry("1180x820")
        self.root.minsize(740, 560)
        self.root.configure(bg="#0d1523")
        self.running = False
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.last_drawn_seq = 0
        self.seq = 0
        self.error = ""
        self.roi: tuple[int, int, int, int] | None = None
        self.view_origin = (0, 0)
        self.view_size = (0, 0)
        self.view_scale = 1.0
        self.photo_ref = None
        self.drag_start = None
        self.drag_rect_id = None
        self.overlays: dict[int, OverlayWindow] = {}
        self.monitor_var = tk.StringVar(value="螢幕 1")
        self.overlay_var = tk.BooleanVar(value=False)
        self.fps_var = tk.StringVar(value="5")
        self.target_fps = 5
        self.status_var = tk.StringVar(value="尚未開始。按「開始辨識」即可持續擷取畫面。")
        self.count_vars = {key: tk.StringVar(value="0") for key in ("empty", "water", "blocked", "total")}
        self.region_var = tk.StringVar(value="辨識範圍：整個所選螢幕（可在預覽圖上拖曳框選）")
        self.mode_labels = ["全部螢幕"] + [f"螢幕 {i}" for i in range(1, len(self.monitors))]

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(160, self._poll)

    def _button(self, parent, text, command, color="#22364e"):
        return self.tk.Button(parent, text=text, command=command, bg=color,
                              fg="#ecf4fb", activebackground="#315276",
                              activeforeground="#ffffff", font=("Microsoft JhengHei UI", 10),
                              bd=0, padx=14, pady=9, cursor="hand2")

    def _build_ui(self):
        tk = self.tk
        head = tk.Frame(self.root, bg="#121f30", pady=16, padx=20)
        head.pack(fill="x")
        tk.Label(head, text="🌱  Garden Plot Detector", font=("Segoe UI", 20, "bold"),
                 fg="#e9f4ff", bg="#121f30").pack(anchor="w")
        tk.Label(head, text="擷取螢幕 → 辨識空地 / 水珠 / 灰色石塊 → 畫框、計數（不會自動點擊）",
                 font=("Microsoft JhengHei UI", 11), fg="#a6bbce", bg="#121f30").pack(anchor="w", pady=(5, 0))

        toolbar = tk.Frame(self.root, bg="#0d1523", pady=12, padx=18)
        toolbar.pack(fill="x")
        tk.Label(toolbar, text="擷取", fg="#c5d7ea", bg="#0d1523").pack(side="left", padx=(0, 8))
        combo = self.ttk.Combobox(toolbar, textvariable=self.monitor_var, values=self.mode_labels,
                                  state="readonly", width=13)
        combo.pack(side="left", padx=(0, 12))
        combo.bind("<<ComboboxSelected>>", self._change_monitor)
        self._button(toolbar, "▶ 開始辨識", self.start, "#146e55").pack(side="left", padx=3)
        self._button(toolbar, "■ 停止", self.stop, "#743348").pack(side="left", padx=3)
        self._button(toolbar, "儲存辨識截圖", self.save, "#253c5a").pack(side="left", padx=3)
        self._button(toolbar, "清除框選範圍", self.clear_roi, "#253c5a").pack(side="left", padx=3)
        tk.Label(toolbar, text="FPS", fg="#a4bed5", bg="#0d1523").pack(side="left", padx=(15, 4))
        fps_box = self.ttk.Combobox(toolbar, textvariable=self.fps_var, values=["2", "3", "5", "8", "10"],
                                    state="readonly", width=4)
        fps_box.pack(side="left")
        fps_box.bind("<<ComboboxSelected>>", lambda e: setattr(self, "target_fps", int(self.fps_var.get())))

        extras = tk.Frame(self.root, bg="#0d1523", padx=20)
        extras.pack(fill="x")
        checkbox = tk.Checkbutton(extras, text="在遊戲畫面上顯示透明即時框（Windows，滑鼠可以穿透）",
                                  variable=self.overlay_var, command=self._toggle_overlay,
                                  fg="#ccedfb", bg="#0d1523", activebackground="#0d1523",
                                  activeforeground="#fff", selectcolor="#1a3147",
                                  font=("Microsoft JhengHei UI", 10))
        checkbox.pack(side="left")
        if sys.platform != "win32":
            checkbox.config(state="disabled")
        tk.Label(extras, text="建議使用視窗化／無邊框視窗化遊戲，第一次先觀察預覽。",
                 fg="#7c96b4", bg="#0d1523").pack(side="right")

        tiles = tk.Frame(self.root, bg="#0d1523", padx=20, pady=12)
        tiles.pack(fill="x")
        for kind, label, color in [("empty", "空地", "#39da8e"),
                                   ("water", "有水珠", "#53d8ed"),
                                   ("blocked", "石塊／不能種", "#ff637f"),
                                   ("total", "畫面田塊總數", "#f1f6fb")]:
            card = tk.Frame(tiles, bg="#192a3d", padx=14, pady=9)
            card.pack(side="left", fill="x", expand=True, padx=5)
            tk.Label(card, text=label, bg="#192a3d", fg="#a8bfd1",
                     font=("Microsoft JhengHei UI", 10)).pack(anchor="w")
            tk.Label(card, textvariable=self.count_vars[kind], bg="#192a3d", fg=color,
                     font=("Segoe UI", 23, "bold")).pack(anchor="w")

        tk.Label(self.root, textvariable=self.region_var, bg="#0d1523", fg="#d2dbe6",
                 font=("Microsoft JhengHei UI", 10), anchor="w", padx=25).pack(fill="x", pady=(1, 5))
        frame = tk.Frame(self.root, bg="#17293c", padx=2, pady=2)
        frame.pack(fill="both", expand=True, padx=22, pady=(0, 8))
        self.preview = tk.Canvas(frame, bg="#080e19", bd=0, highlightthickness=0,
                                  cursor="crosshair")
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", lambda e: self._redraw_preview())
        self.preview.bind("<ButtonPress-1>", self._roi_down)
        self.preview.bind("<B1-Motion>", self._roi_move)
        self.preview.bind("<ButtonRelease-1>", self._roi_up)
        tk.Label(self.root, textvariable=self.status_var, font=("Microsoft JhengHei UI", 10),
                 bg="#0d1523", fg="#a8c3d8", anchor="w", padx=22, pady=8).pack(fill="x")
        tk.Label(self.root, text="說明：藍框＝水珠，綠框＝空地，紅框＝灰石；* 表示畫面邊緣不完整，統計僅供參考。",
                 font=("Microsoft JhengHei UI", 9), bg="#0d1523", fg="#889db3",
                 anchor="w", padx=22, pady=8).pack(fill="x")

    def _selected_monitor(self) -> dict:
        key = self.monitor_var.get()
        idx = 0 if key == "全部螢幕" else int(key.split(" ")[1])
        idx = min(max(idx, 0), len(self.monitors) - 1)
        return dict(self.monitors[idx])

    def _change_monitor(self, _event=None):
        was_running = self.running
        self.stop()
        self.clear_roi()
        self._destroy_overlays()
        if was_running:
            self.start()

    def start(self):
        if self.running:
            return
        self.error = ""
        self.stop_event = threading.Event()
        spec = self._selected_monitor()
        self.running = True
        self.worker = threading.Thread(target=self._capture, args=(spec, self.stop_event),
                                       daemon=True)
        self.worker.start()
        self.status_var.set("辨識中。可在預覽畫面拖曳矩形，只辨識指定區域。")

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.stop_event.set()
        self._destroy_overlays()
        self.status_var.set("已停止擷取。")

    def _capture(self, mon: dict, stop_event: threading.Event):
        import mss
        try:
            with mss.mss() as cap:
                while not stop_event.is_set():
                    begin = time.monotonic()
                    shot = cap.grab(mon)
                    frame = np.asarray(shot)[:, :, :3].copy()
                    with self.lock:
                        roi = self.roi
                    plots = detect_plots(frame, roi=roi)
                    elapsed = time.monotonic() - begin
                    with self.lock:
                        self.seq += 1
                        self.latest = (self.seq, frame, plots, mon, elapsed)
                    # Do not access Tk variables from a worker thread.
                    fps = max(1, min(12, self.target_fps))
                    stop_event.wait(max(.01, 1 / fps - (time.monotonic() - begin)))
        except Exception as exc:
            with self.lock:
                self.error = f"截圖失敗：{type(exc).__name__}: {exc}"
            self.stop_event.set()

    def _poll(self):
        if not self.root.winfo_exists():
            return
        with self.lock:
            latest = self.latest
            error = self.error
        if error:
            self.stop()
            self.status_var.set(error)
            self.error = ""
        if latest is not None and latest[0] != self.last_drawn_seq:
            self.last_drawn_seq = latest[0]
            _, frame, plots, monitor, elapsed = latest
            self.last_frame = frame
            self.last_plots = plots
            self.last_capture_monitor = monitor
            counts = summarize(plots)
            for key, var in self.count_vars.items():
                var.set(str(counts[key]))
            edge_count = sum(p.partial for p in plots)
            self.status_var.set(f"已辨識 {counts['total']} 塊（邊緣不完整 {edge_count} 塊）；"
                                f"偵測耗時 {elapsed * 1000:.0f} ms；"
                                f"擷取座標起點 ({monitor['left']}, {monitor['top']})")
            self._redraw_preview()
            if self.overlay_var.get():
                self._update_overlays(plots, monitor, counts)
        try:
            self.root.after(160, self._poll)
        except self.tk.TclError:
            pass

    def _redraw_preview(self):
        if not hasattr(self, "last_frame"):
            return
        frame = self.last_frame
        drawn = draw_boxes(frame, self.last_plots, summary=False, roi=self.roi)
        max_w = max(100, self.preview.winfo_width() - 14)
        max_h = max(100, self.preview.winfo_height() - 14)
        h, w = drawn.shape[:2]
        scale = min(max_w / w, max_h / h)
        out_w, out_h = max(1, round(w * scale)), max(1, round(h * scale))
        if abs(scale - 1.0) > .001:
            drawn = cv2.resize(drawn, (out_w, out_h),
                               interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(drawn, cv2.COLOR_BGR2RGB)
        self.photo_ref = self.ImageTk.PhotoImage(image=self.Image.fromarray(rgb))
        left = (self.preview.winfo_width() - out_w) // 2
        top = (self.preview.winfo_height() - out_h) // 2
        self.preview.delete("all")
        self.preview.create_image(left, top, anchor="nw", image=self.photo_ref)
        self.view_origin, self.view_size, self.view_scale = (left, top), (out_w, out_h), scale
        if self.drag_start is not None:
            self.drag_rect_id = self.preview.create_rectangle(self.drag_start[0],self.drag_start[1],
                                                               self.drag_start[0],self.drag_start[1],
                                                               dash=(5, 3), outline="#f5f8fa", width=2)

    def _to_frame_xy(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self.view_origin
        if not hasattr(self, "last_frame"):
            return 0, 0
        h, w = self.last_frame.shape[:2]
        return (max(0, min(w, round((x - ox) / self.view_scale))),
                max(0, min(h, round((y - oy) / self.view_scale))))

    def _roi_down(self, event):
        if not hasattr(self, "last_frame"):
            return
        self.drag_start = (event.x, event.y)
        if self.drag_rect_id:
            self.preview.delete(self.drag_rect_id)
        self.drag_rect_id = self.preview.create_rectangle(event.x, event.y, event.x, event.y,
                                                          outline="#f9f9f9", dash=(5, 3), width=2)

    def _roi_move(self, event):
        if self.drag_start and self.drag_rect_id:
            self.preview.coords(self.drag_rect_id, *self.drag_start, event.x, event.y)

    def _roi_up(self, event):
        if self.drag_start is None:
            return
        x1, y1 = self._to_frame_xy(*self.drag_start)
        x2, y2 = self._to_frame_xy(event.x, event.y)
        left, top = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        self.drag_start = None
        if w > 25 and h > 25:
            with self.lock:
                self.roi = (left, top, w, h)
            self.region_var.set(f"辨識範圍：({left}, {top})  {w} × {h}；按清除框選可恢復全畫面")
        self._redraw_preview()

    def clear_roi(self):
        with self.lock:
            self.roi = None
        self.region_var.set("辨識範圍：整個所選螢幕（可在預覽圖上拖曳框選）")
        self._redraw_preview()

    def save(self):
        if not hasattr(self, "last_frame"):
            self.messagebox.showinfo("尚無畫面", "請先按開始辨識。")
            return
        output_dir = HERE / "captures"
        output_dir.mkdir(exist_ok=True)
        path = output_dir / f"garden_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        image = draw_boxes(self.last_frame, self.last_plots, summary=True, roi=self.roi)
        cv2.imwrite(str(path), image)
        self.status_var.set(f"圖片已儲存至 {path}")

    def _destroy_overlays(self):
        for ov in self.overlays.values():
            ov.destroy()
        self.overlays.clear()

    def _toggle_overlay(self):
        if not self.overlay_var.get():
            self._destroy_overlays()
        elif sys.platform != "win32":
            self.overlay_var.set(False)
            self.messagebox.showwarning("不支援", "透明置頂框線目前只支援 Windows。")

    def _update_overlays(self, plots, capture_monitor, counts):
        indices = (range(1, len(self.monitors)) if capture_monitor == self.monitors[0]
                   else [next((i for i, m in enumerate(self.monitors) if m == capture_monitor), 1)])
        try:
            for idx in indices:
                if idx not in self.overlays:
                    self.overlays[idx] = OverlayWindow(self.root, self.monitors[idx])
                self.overlays[idx].draw(plots, capture_monitor, counts)
        except Exception as exc:
            self._destroy_overlays()
            self.overlay_var.set(False)
            self.status_var.set(f"透明框不可用（預覽模式仍可用）：{exc}")

    def close(self):
        self.stop_event.set()
        self.running = False
        self._destroy_overlays()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local screenshot garden plot detector")
    parser.add_argument("--image", help="Analyze one PNG/JPG file instead of capturing the screen")
    parser.add_argument("--output", help="Output annotated PNG path for --image mode")
    parser.add_argument("--roi", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                        help="Optional crop for --image mode")
    parser.add_argument("--monitors", action="store_true", help="List monitor positions")
    args = parser.parse_args(argv)
    if args.image:
        # cv2.imread cannot reliably handle Unicode Windows paths; imdecode can.
        path = Path(args.image)
        if not path.is_file():
            parser.error(f"File not found: {path}")
        bgr = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            parser.error("Could not decode image")
        result = detect_plots(bgr, tuple(args.roi) if args.roi else None)
        counts = summarize(result)
        print(f"Empty / 空地: {counts['empty']}")
        print(f"Water / 水珠: {counts['water']}")
        print(f"Rocks / 石塊: {counts['blocked']}")
        print(f"Total / 總數: {counts['total']} (partial at edge: {sum(p.partial for p in result)})")
        destination = Path(args.output) if args.output else path.with_name(path.stem + "_annotated.png")
        destination.parent.mkdir(parents=True, exist_ok=True)
        annotated = draw_boxes(bgr, result, roi=tuple(args.roi) if args.roi else None)
        ok, encoded = cv2.imencode(".png", annotated)
        if not ok:
            raise RuntimeError("PNG encoding failed")
        encoded.tofile(str(destination))
        print("Saved:", destination.resolve())
        return 0

    set_dpi_awareness()
    if args.monitors:
        import mss
        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors):
                print(f"{i}: {dict(mon)}")
        return 0
    try:
        app = LiveApp()
        app.run()
        return 0
    except Exception as exc:
        print("Failed to start live GUI:", exc, file=sys.stderr)
        traceback.print_exc()
        print("Install packages with: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())