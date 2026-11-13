"""FastAPI application. Bind exclusively to loopback; changes require an anti-CSRF token."""
from __future__ import annotations

import copy
import json
import re
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .engine import WorkflowEngine, verify_flow
from .vision import annotated_preview, capture_png_data_url, detect, list_monitors

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "workflows"
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = secrets.token_urlsafe(36)
ENGINE = WorkflowEngine()
FILE_LOCK = threading.Lock()
app = FastAPI(title="ImageFlow Studio", docs_url=None, redoc_url=None)


def sample_flow() -> dict:
    """An editable runnable-after-pasting sample; no embedded proprietary screenshots."""
    return {
        "id": "starter", "name": "範例：找到 A → 點擊 → 判斷 B", "maxSteps": 500,
        "nodes": [
            {"id": "start", "type": "start", "title": "開始", "x": 30, "y": 275},
            {"id": "image_a", "type": "condition", "title": "圖片 A 存在嗎？", "x": 300, "y": 175,
             "image": "", "threshold": 0.85, "timeout": 0, "clickOnFound": True,
             "clickX": 0.5, "clickY": 0.5, "button": "left"},
            {"id": "image_b", "type": "condition", "title": "圖片 B 存在嗎？", "x": 565, "y": 80,
             "image": "", "threshold": 0.85, "timeout": 1.5, "clickOnFound": False,
             "clickX": 0.5, "clickY": 0.5},
            {"id": "click_c", "type": "click", "title": "沒找到 B → 點擊 C", "x": 565, "y": 380,
             "image": "", "threshold": 0.85, "timeout": 2, "clickX": 0.5, "clickY": 0.5},
            {"id": "wait", "type": "wait", "title": "等候後再試 A", "x": 300, "y": 510, "seconds": 1},
            {"id": "success", "type": "end", "title": "結束：成功", "x": 835, "y": 70},
            {"id": "miss", "type": "end", "title": "結束：C 也沒找到", "x": 835, "y": 530},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "next", "target": "image_a"},
            {"source": "image_a", "sourceHandle": "found", "target": "image_b"},
            {"source": "image_a", "sourceHandle": "missing", "target": "wait"},
            {"source": "wait", "sourceHandle": "next", "target": "image_a"},
            {"source": "image_b", "sourceHandle": "found", "target": "success"},
            {"source": "image_b", "sourceHandle": "missing", "target": "click_c"},
            {"source": "click_c", "sourceHandle": "found", "target": "success"},
            {"source": "click_c", "sourceHandle": "missing", "target": "miss"},
        ],
    }


def flow_path(flow_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", flow_id):
        raise HTTPException(400, "工作流 ID 不合法")
    return DATA / f"{flow_id}.json"


def initialize() -> None:
    if not any(DATA.glob("*.json")):
        (DATA / "starter.json").write_text(json.dumps(sample_flow(), ensure_ascii=False, indent=2), encoding="utf-8")


initialize()


@app.middleware("http")
async def protect_local_api(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()
    if host not in {"127.0.0.1", "localhost", "[::1]"}:
        return JSONResponse({"detail": "僅允許從本機開啟"}, status_code=403)
    if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
            return JSONResponse({"detail": "來源網站不被允許"}, status_code=403)
        if request.headers.get("x-imageflow-token") != TOKEN:
            return JSONResponse({"detail": "安全驗證失敗，請重新整理頁面"}, status_code=403)
        try:
            if int(request.headers.get("content-length", "0")) > 25_000_000:
                return JSONResponse({"detail": "請縮小圖片或減少工作流大小（上限 25MB）"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Content-Length 不合法"}, status_code=400)
    return await call_next(request)


@app.get("/api/bootstrap")
def bootstrap():
    return {"token": TOKEN, "version": "1.1.0"}


@app.get("/api/workflows")
def list_flows():
    result = []
    for path in sorted(DATA.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": path.stem, "name": data.get("name") or path.stem})
        except (OSError, json.JSONDecodeError):
            continue
    return result


@app.get("/api/workflows/{flow_id}")
def load_flow(flow_id: str):
    path = flow_path(flow_id)
    if not path.is_file():
        raise HTTPException(404, "找不到此工作流")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(400, "工作流檔案損壞") from None


@app.post("/api/workflows")
def save_flow(flow: dict):
    flow_id = flow.get("id")
    if not isinstance(flow_id, str):
        raise HTTPException(400, "缺少工作流 ID")
    path = flow_path(flow_id)
    if not isinstance(flow.get("name"), str) or not flow["name"].strip():
        raise HTTPException(400, "請輸入工作流名稱")
    if not isinstance(flow.get("nodes"), list) or not isinstance(flow.get("edges"), list):
        raise HTTPException(400, "工作流格式錯誤")
    if len(flow["nodes"]) > 150 or len(flow["edges"]) > 350:
        raise HTTPException(400, "節點或連線過多")
    content = json.dumps(flow, ensure_ascii=False, indent=2)
    if len(content.encode("utf-8")) > 22_000_000:
        raise HTTPException(413, "工作流超過 22MB")
    with FILE_LOCK:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return {"ok": True, "id": flow_id}


@app.delete("/api/workflows/{flow_id}")
def delete_flow(flow_id: str):
    if ENGINE.state()["status"] == "running":
        raise HTTPException(409, "請先停止正在執行的流程")
    path = flow_path(flow_id)
    if not path.exists():
        raise HTTPException(404, "工作流不存在")
    path.unlink()
    return {"ok": True}


@app.get("/api/state")
def get_state():
    return ENGINE.state()


@app.post("/api/run")
def run_workflow(body: dict):
    flow = body.get("flow", {})
    try:
        ENGINE.start(flow, dry_run=bool(body.get("dryRun", True)))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@app.post("/api/stop")
def stop_workflow():
    ENGINE.stop()
    return {"ok": True}


@app.get("/api/monitors")
def monitors():
    try:
        return list_monitors()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/test")
def test_match(body: dict):
    try:
        return detect(body.get("image", ""), float(body.get("threshold", 0.84)), monitor=body.get("monitor", "all"))
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/preview")
def preview(body: dict):
    try:
        encoded, result = annotated_preview(
            body.get("image"), float(body.get("threshold", 0.84)),
            float(body.get("clickX", 0.5)), float(body.get("clickY", 0.5)),
            monitor=body.get("monitor", "all"),
        )
        return Response(encoded, media_type="image/jpeg", headers={
            "X-Match-Found": "true" if result["found"] else "false",
            "X-Match-Score": str(result["score"]) if result["score"] is not None else "",
            "X-Match-Monitor": str(result.get("monitor") or ""),
            "Cache-Control": "no-store",
        })
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/capture")
def capture(body: dict):
    try:
        return {"image": capture_png_data_url(body.get("monitor", "all"))}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
