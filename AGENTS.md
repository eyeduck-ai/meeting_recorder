# AGENTS.md

本檔是此 repo 唯一的 agent 規格來源。  
未來協助開發的 agent 應優先讀這一份，不要再新增 `CLAUDE.md`、`plan.md`、`TODO.md` 等平行文件。

## 專案定位

`MeetingRecorder` 是一個自動化線上會議錄製系統：

- 以 FastAPI 提供 Web UI 與 API
- 以 Playwright 自動加入會議
- 在 Linux 錄製環境中以 Xvfb、PipeWire、FFmpeg 進行錄影
- 支援排程、Telegram 通知、YouTube 上傳與診斷資料收集

README 面向使用者與部署者；本檔面向 agent 與 contributor。

## 文件規則

- `README.md`：人類使用者的產品/部署入口
- `docs/development.md`：人類開發者的技術與維護指南
- `AGENTS.md`：唯一 agent 規格來源

規則：

1. 不要新增新的 agent 指南檔案。
2. 修改功能時，必須判斷是否同步更新 README 與 `docs/development.md`。
3. 若改動影響使用者可見能力、部署方式、支援平台或支援 provider，README 必須更新。
4. 若改動影響架構、流程、設定來源、測試方式或 API 範圍，`docs/development.md` 必須更新。

## 專案地圖

| 路徑 | 角色 |
| --- | --- |
| `api/` | FastAPI app、REST routes、HTML routes |
| `config/` | `.env` 設定與 logging |
| `database/` | SQLAlchemy models、DB session |
| `providers/` | 會議 provider 實作 |
| `recording/` | 錄製 worker、虛擬環境、FFmpeg、偵測器 |
| `scheduling/` | APScheduler 與單工 job runner |
| `services/` | app settings、通知、錄影管理 |
| `telegram_bot/` | Telegram bot 流程與 conversation handlers |
| `uploading/` | YouTube 上傳與進度 |
| `web/` | Jinja templates / static assets |
| `tests/` | pytest 測試 |

## 真實執行方式

### Docker

本專案跨平台最可靠的運行方式是 Docker。

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f app
```

使用已發布映像：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### 本地開發

只把 Linux 視為可直接跑錄製流程的原始碼開發平台。

```bash
uv sync --extra dev
uv run playwright install chromium
uv run uvicorn api.main:app --reload
```

### 測試與檢查

```bash
uv run pytest tests/ -v
uv run pytest tests/ --cov=api --cov=providers --cov=database --cov=recording --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
```

## 重要架構事實

### 單工錄製

- `recording.worker.get_worker()` 是 singleton
- `scheduling.job_runner.JobRunner` 以單一 lock 控制執行
- 現況一次只支援一個錄製工作
- 若新 schedule 進來時已有工作進行中，會進 queue 等待

### 設定來源不是單一層

不要把所有設定都描述成「只在 `.env`」或「都已移到 DB」。

目前真實狀態是：

- `.env` / `config.settings`：資料庫、認證、Telegram、YouTube、FFmpeg 進階參數、路徑
- `services.app_settings` + `app_settings` table：部分 UI 可調整設定
- `detection_config`、`notification_config`：JSON 形式存於 `app_settings`

### Provider 支援現況

- 對外文件與 UI 主力支援：Jitsi、Webex
- `providers/zoom.py`、測試、部分 API 型別已存在 Zoom 痕跡
- 但 Zoom 尚未被完整整合到 UI 與 README 的對外承諾中

若要正式宣告 Zoom 支援，必須同步處理：

- `README.md`
- `docs/development.md`
- `web/templates/meetings/form.html`
- 測試與對外 API 描述

### 錄製環境限制

- 錄製核心依賴 Linux
- Windows / macOS 應透過 Docker 使用
- 不要在文件裡把非 Linux 原始碼執行描述成正式支援路徑

## 修改時的同步責任

### 改動以下內容時，要更新 `README.md`

- 支援的 provider
- 部署方式
- 使用者可見功能
- 資料目錄與持久化行為
- 初次設定流程（Telegram、YouTube、認證）

### 改動以下內容時，要更新 `docs/development.md`

- API 路由
- 錄製/排程/上傳流程
- 設定來源與優先順序
- 測試命令、CI、開發依賴
- 除錯與診斷輸出

### 改動以下內容時，要更新 `AGENTS.md`

- 文件分工規則
- agent 工作規則
- 架構真實狀態與已知落差
- 需要 agent 特別避免誤判的事實

## 常用檢查命令

優先使用這些命令建立事實基礎再動手：

```bash
git status --short
Get-ChildItem -Recurse -File
Select-String -Path api/routes/*.py -Pattern '@router'
uv run pytest tests/ -v
```

若要找字串，優先用 `rg`；若環境不可用，再退回 PowerShell 的 `Select-String`。

## 文件與實作的一致性要求

修改文件前後，請自行核對至少下列來源：

- `config/settings.py`
- `services/app_settings.py`
- `api/routes/*.py`
- `providers/*.py`
- `docker-compose*.yml`
- `docker/Dockerfile`
- `.github/workflows/ci.yml`

最低標準：

1. 文件中的命令、檔名、路徑、env 名稱必須在 repo 中有對應來源。
2. 不要保留 TODO、roadmap、歷史變更摘要做為正式規格。
3. 不要再建立平行 agent 文件。
