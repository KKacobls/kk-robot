# KK Robot — Discord + Ollama + Admin Dashboard

**狀態：功能實作版本，已完成靜態檢查及單元測試；尚未在你的 Discord／Ollama／Docker 實機做整合測試，不能視為已部署。**

這份程式可以放在你現有的 `~/kk-robot` 目錄，作為獨立 `kk_robot.py` 使用；**不覆蓋原版的 `llmcord.py`、`config.yaml`、`requirements.txt`。** 設計參考 [jakobdylanc/llmcord](https://github.com/jakobdylanc/llmcord) 的 Discord 對話方式與 MIT 授權，保留來源授權檔；主程式由我們重新實作，以加入本專案的安全與管理功能。

## 已實作的功能

- Discord 原生 **Reply**：新訊息先回覆 `🤔 思考中…`，模型回傳後 `edit` 同一則訊息；忽略其他 Bot、Webhooks、DM。
- 管理介面列出 Bot 在所選 Discord 伺服器**可查看的文字頻道**；無發言權限者停用勾選；新增頻道重新整理可見。
- **預設全部頻道未勾選，沒有勾選就完全不會自動回覆**；管理員手動勾選並儲存，立即生效。三種模式：只在 @ 時回覆、指定頻道全訊息、混合模式。
- 各伺服器設定以 SQLite 分離；按需讀取同一 Discord 頻道／討論串最近 50 則訊息，並依 18k 字元上限截斷，不會回覆舊訊息。
- Ollama `127.0.0.1:11555`，`qwen3.5:9b`、`qwen3.5:35b-a3b` 可選，`think=false`、`num_ctx=8192`，單一模型並行、最多 16 件待處理。
- 按管理員開關使用 **DuckDuckGo HTML 免費搜尋**，支援 AI 工具呼叫和 `/search`，會回傳來源連結。搜尋 HTML 格式／速率限制可能改變，因此搜尋失敗會回報不可用，不假裝搜尋成功。
- 按管理員開關使用 **Docker 隔離 Python**：AI 可用 `run_python` 工具，或用 `/python` 指令。預設關閉；只有已列白名單的使用者或 Discord 伺服器管理員能使用。
- 網頁監控 Linux CPU、RAM、NVIDIA GPU 使用率與 VRAM（讀取 `/proc`、`nvidia-smi`），統計每伺服器請求數、輸入／輸出 Token、整體產生速度與錯誤次數。
- 管理頁使用密碼驗證、綁定 `127.0.0.1`，API 資料不緩存、阻擋異來源 POST，Token 僅存在 `.env` 且加入 `.gitignore`。

## 重要限制

1. 網路搜尋依賴第三方搜尋 HTML，可能被限速，不能保證每次都成功；其結果會提供給 AI，不保證內容正確。
2. Python **絕不可直接在校園共用主機執行使用者的任意程式碼**。本程式的 Python 只走 Docker 容器：`--network none`、唯讀根目錄、無主機掛載、丟棄 capabilities、限制 CPU／RAM／程序／輸出／時間。但 Docker daemon 的存取權限本身可能相當於主機管理員權限；**未取得系統管理員核准之前，請讓 Python 功能保持關閉。** 強敵意程式碼應使用更強的虛擬化隔離與審計。
3. 管理後台的 Basic Auth 只適合透過 SSH 隧道存取；**不可以直接將 8765 埠公開到 Internet**。
4. Bot 必須具有 `View Channel`、`Send Messages`、`Read Message History`（討論串需 `Send Messages in Threads`），以及 Discord Developer Portal 的 **Message Content Intent**。
5. Bot 在 selected channels 會處理其他使用者的訊息；應先告知成員並遵守伺服器規範。
6. 本機管理 API 與模型尚未整合實測，請先在測試伺服器確認所有權限和功能。

## 安裝（Ubuntu／WSL2，延用已安裝的 uv）

你的 `~/kk-robot/.venv` 已經存在，原版 llmcord 依賴也已安裝。先在你原本的 `~/kk-robot` 專案根目錄**備份自己的修改並從 KKacobls/kk-robot 拉取新增檔案**；再安裝必要套件：

```bash
cd ~/kk-robot
~/.local/bin/uv pip install --python .venv/bin/python -r requirements-kk.txt
cp .env.example .env
chmod 600 .env
nano .env
```

在 `.env` 填入 Discord Bot Token 和至少 16 字元的隨機後台密碼，**不要傳給任何人**。

在 Discord Developer Portal 啟用 Bot 的 Message Content Intent，邀請 Bot 使用 `bot` 與 `applications.commands` scopes；先選一個只有管理員知道的測試伺服器。

測試啟動：

```bash
cd ~/kk-robot
.venv/bin/python kk_robot.py
```

Ollama 仍需在另一個程序常駐聆聽 `127.0.0.1:11555`。首次啟動**不會在任何頻道自動發言**，除非你在後台手動勾選頻道並儲存； `/chat` 及被 @ 的行為則依模式運作。

Windows PowerShell 使用 SSH tunnel 存取後台（依自己的 SSH 主機與金鑰設定更改）：

```powershell
ssh -p 26 -i "$env:USERPROFILE\.ssh\id_ed25519" -N -L 8765:127.0.0.1:8765 your-ssh-user@your-server-host
```

瀏覽器開 `http://127.0.0.1:8765`。不要暴露給公共網路。

### Python 工具預先準備（需管理員核准）

```bash
# 預先拉取映像檔，避免首次執行時臨時下載
# 執行前請先徵詢共用校園伺服器管理員意見
# docker pull python:3.12-slim
```

管理介面勾選 Python 執行、加入有權限者的 Discord 使用者 ID 才能使用。未勾選時不會為 AI 提供 Python 工具。

## 結構

- `kk_robot.py`：Discord Bot、Ollama、搜尋、Docker sandbox、管理 API。
- `core.py`：純函式與搜尋 HTML 解析器。
- `admin.html`：管理後台，含頻道勾選與硬體監控。
- `requirements-kk.txt`：新增服務所需的依賴。
- `.env.example`：環境變數樣板；實際 `.env` 必須保密。
- `tests/test_core.py`：離線單元測試。

測試：

```bash
cd ~/kk-robot
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m py_compile kk_robot.py core.py
```

## 授權與來源

- 原始 Discord/Ollama 參考實作：[llmcord](https://github.com/jakobdylanc/llmcord)，MIT License；參見 `LICENSE-UPSTREAM-llmcord.md`。
- 沒有修改你的 GitHub 上其他既有專案；此專案已上傳到公開 GitHub 儲存庫，可直接在伺服器以 HTTPS 拉取；請勿上傳實際 `.env`、密碼、Bot Token。
