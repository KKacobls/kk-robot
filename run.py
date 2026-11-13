"""Launch ImageFlow Studio locally on Windows."""
import argparse
import threading
import webbrowser

import uvicorn
from backend.vision import windows_dpi_awareness


def main():
    parser = argparse.ArgumentParser(description="ImageFlow Studio")
    parser.add_argument("--port", type=int, default=4317)
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args()
    windows_dpi_awareness()
    url = f"http://127.0.0.1:{options.port}"
    print("=" * 55)
    print(" ImageFlow Studio｜圖片辨識自動化工作流")
    print(f" 本機網址：{url}")
    print(" 請按 Ctrl+C 關閉伺服器。停止執行中的流程：F8")
    print("=" * 55)
    if not options.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run("backend.app:app", host="127.0.0.1", port=options.port, log_level="warning")


if __name__ == "__main__":
    main()
