# 開發者指南

本指南涵蓋環境變數設定、API 使用、開發環境設置、專案架構、除錯工具與貢獻指南。

---

## Docker 開發與部署（推薦）

> Windows/macOS 必須使用 Docker；Linux 也可使用 Docker 以保持環境一致。

### Docker Compose 架構

本專案採用 Docker Compose Override 模式：

| 檔案 | 用途 | 說明 |
|------|------|------|
| `docker-compose.yml` | 基礎設定 | 定義 Volume, Network, Environment 等共用配置 |
| `docker-compose.override.yml` | 開發設定 | **預設自動載入**。定義 `build` context，用於本地建構 |
| `docker-compose.prod.yml` | 生產設定 | 定義 GHCR image 來源。需透過 `-f` 參數顯式指定 |

### 從原始碼啟動（Docker）

```bash
# 1. Clone 專案
git clone https://github.com/eyeduck-ai/meeting_recorder.git
cd meeting_recorder

# 2. 設定環境變數
cp .env.example .env
nano .env

# 3. 建構並啟動（自動讀取 docker-compose.override.yml）
docker compose up --build -d
```

---

## 本地開發環境（僅限 Linux）

> ⚠️ 本地開發需要 Linux 環境，Windows/macOS 請使用 Docker 進行開發。

### 系統需求

| 需求 | 說明 |
|------|------|
| Python 3.12+ | 主程式語言 |
| FFmpeg | 影音編碼 |
| Xvfb | 虛擬 X11 顯示器 |
| PipeWire | 虛擬音訊系統（取代 PulseAudio，更低延遲） |
| Chromium | 瀏覽器自動化 |

### 安裝步驟

```bash
# 安裝 uv（Python 套件管理器）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安裝依賴
uv sync

# 安裝 Playwright 瀏覽器
uv run playwright install chromium
uv run playwright install-deps chromium

# 啟動虛擬音訊（PipeWire）
# 確保 pipewire 和 wireplumber 服務已啟動
systemctl --user start pipewire pipewire-pulse wireplumber

# 啟動開發伺服器
uv run uvicorn api.main:app --reload
```

---

## 環境變數

| 變數 | 說明 | 預設值 |
|------|------|--------|
| `TZ` | 時區 | `UTC` |
| `DATABASE_URL` | 資料庫連接字串 | `sqlite:///./data/app.db` |
| `AUTH_PASSWORD` | 登入密碼（不設定則無需登入） | - |
| `AUTH_SESSION_SECRET` | Session 加密金鑰 | `change-me` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | - |
| `YOUTUBE_CLIENT_ID` | YouTube OAuth Client ID | - |
| `YOUTUBE_CLIENT_SECRET` | YouTube OAuth Client Secret | - |
| `RESOLUTION_W` | 錄製解析度寬度 | `1920` |
| `RESOLUTION_H` | 錄製解析度高度 | `1080` |
| `LOBBY_WAIT_SEC` | 等候室最長等待時間 | `900` |
| `FFMPEG_PRESET` | FFmpeg 編碼預設 | `ultrafast` |
| `FFMPEG_THREAD_QUEUE_SIZE` | FFmpeg 來源佇列大小 | `1024` |
| `FFMPEG_AUDIO_FILTER` | 音訊時間戳修正濾鏡 | `aresample=async=1000:first_pts=0` |
| `FFMPEG_DEBUG_TS` | 啟用 FFmpeg 時間戳除錯 | `false` |
| `FFMPEG_STOP_GRACE_SEC` | 停止錄影時等待 FFmpeg 正常結束秒數 | `5` |
| `FFMPEG_SIGINT_TIMEOUT_SEC` | 發送 SIGINT 後等待秒數 | `8` |
| `FFMPEG_SIGTERM_TIMEOUT_SEC` | 發送 SIGTERM 後等待秒數 | `5` |
| `FFMPEG_STALL_TIMEOUT_SEC` | 輸出檔案無成長視為卡住的秒數 | `120` |
| `FFMPEG_STALL_GRACE_SEC` | 錄影開始後的監看緩衝秒數 | `30` |
| `FFMPEG_TRANSCODE_ON_UPLOAD` | 上傳前轉檔壓縮成 MP4 | `false` |
| `FFMPEG_TRANSCODE_PRESET` | 轉檔 preset | `slow` |
| `FFMPEG_TRANSCODE_CRF` | 轉檔 CRF | `30` |
| `FFMPEG_TRANSCODE_AUDIO_BITRATE` | 轉檔音訊位元率 | `96k` |
| `FFMPEG_TRANSCODE_VIDEO_BITRATE` | 轉檔視訊位元率上限 | `1500k` |
| `DEBUG_VNC` | 啟用 VNC 遠端桌面 | `0` |
| `SMTP_ENABLED` | 啟用 Email 通知 | `false` |
| `SMTP_HOST` | SMTP 伺服器 | - |
| `SMTP_PORT` | SMTP 端口 | `587` |
| `SMTP_USER` | SMTP 用戶名 | - |
| `SMTP_PASSWORD` | SMTP 密碼 | - |
| `SMTP_FROM` | 發件人地址 | - |
| `SMTP_TO` | 收件人 (逗號分隔) | - |
| `WEBHOOK_ENABLED` | 啟用 Webhook 通知 | `false` |
| `WEBHOOK_URL` | Webhook URL | - |
| `WEBHOOK_SECRET` | Webhook 簽名密鑰 | - |
| `EARLY_JOIN_SEC` | 提前加入時間 | `30` |
| `MIN_DURATION_SEC` | 最少錄製時間（可在排程設定） | - |
| `STILLNESS_TIMEOUT_SEC` | 靜止偵測超時 | `180` |

完整設定請參考 `.env.example`。

---

## 使用方式

### Web UI

| 頁面 | 說明 |
|------|------|
| `/` | Dashboard 總覽（含即時錄製狀態） |
| `/meetings` | 會議設定管理 |
| `/schedules` | 排程管理（支援自動偵測會議結束） |
| `/jobs` | 錄製工作記錄 |
| `/recordings` | 錄製檔案下載 |
| `/detection-logs` | 會議結束偵測日誌 |
| `/settings` | 系統設定（偵測器、通知、錄製管理） |

### Telegram Bot 指令

| 指令 | 說明 |
|------|------|
| `/start` | 註冊帳號 |
| `/help` | 顯示說明 |
| `/list` | 查看排程（含錄製狀態） |
| `/record` | 新增排程 / 立即錄製 |
| `/meetings` | 會議列表 |
| `/trigger <ID>` | 立即觸發排程 |
| `/stop` | 停止錄製 |

### API 端點

```
GET  /health                    # 健康檢查
GET  /api                       # API 資訊

# Jobs
POST /api/v1/jobs/record        # 觸發錄製
GET  /api/v1/jobs/{job_id}      # 查詢 Job
POST /api/v1/jobs/{job_id}/stop # 停止錄製

# Meetings
GET  /api/v1/meetings           # 會議列表
POST /api/v1/meetings           # 建立會議

# Schedules
GET  /api/v1/schedules          # 排程列表
POST /api/v1/schedules          # 建立排程
POST /api/v1/schedules/{id}/trigger  # 手動觸發

# Detection (會議結束偵測)
GET  /api/detection/config      # 偵測設定
POST /api/detection/config      # 儲存偵測設定
GET  /api/detection/logs        # 偵測日誌
GET  /api/detection/logs/export # 匯出日誌 (JSON/CSV)

# Recording Management (錄製管理)
GET  /api/recordings/list       # 錄製列表
GET  /api/recordings/disk-usage # 磁碟使用量
POST /api/recordings/cleanup    # 清理舊錄製

# YouTube
GET  /api/v1/youtube/status     # 授權狀態
POST /api/v1/youtube/auth/start # 開始授權
```

API 認證方式：
- **Session Cookie**：透過 `/login` 頁面登入
- **X-API-Key Header**：`curl -H "X-API-Key: your-password" ...`

---

## 新增會議錄製（API 範例）

### Jitsi Meeting

```bash
curl -X POST "http://localhost:8000/api/v1/meetings" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-password" \
  -d '{
    "name": "每週團隊會議",
    "provider": "jitsi",
    "meeting_code": "my-team-meeting",
    "default_display_name": "Recorder Bot"
  }'
```

### Webex Meeting (Guest Join)

```bash
curl -X POST "http://localhost:8000/api/v1/meetings" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-password" \
  -d '{
    "name": "Webex 會議",
    "provider": "webex",
    "meeting_code": "https://company.webex.com/meet/username",
    "default_display_name": "Recorder Bot"
  }'
```

> **Webex 注意事項**：`meeting_code` 欄位請填入**完整會議連結**

### 建立 Schedule

```bash
curl -X POST "http://localhost:8000/api/v1/schedules" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-password" \
  -d '{
    "meeting_id": 1,
    "schedule_type": "cron",
    "cron_expression": "0 14 * * 1",
    "duration_sec": 3600,
    "start_time": "2024-01-01T14:00:00"
  }'
```

---

## 後端開發重點（近期更新）

### 錄製穩定性改進

- **PipeWire 取代 PulseAudio**：更低延遲（~3-10ms vs ~20-50ms）、更好的緩衝管理、減少音訊斷續。
- **Xvfb 每次錄製使用新實例**：避免長時間運行導致的 x11grab 阻塞問題。
- **FFmpeg 音訊配置優化**：
  - 添加 `-fflags +genpts` 生成時間戳
  - 添加 `-rtbufsize 100M` 增加緩衝區
  - 移除音訊輸入的 `use_wallclock_as_timestamps` 避免同步問題

### Docker Image 優化

- 移除非必要套件（x11vnc、wget、procps、build-essential）
- 移除未使用的 Python 依賴（aiosqlite、alembic、python-multipart）
- Image 大小減少約 125MB

### 錄影與上傳

- 錄影輸出改為 `.mkv`，YouTube 上傳前會 remux 成 `.mp4`（不重編碼），log 會寫到 `diagnostics/{job_id}/remux.log`。
- YouTube 上傳改為背景任務，不再佔用錄影 lock；上傳本身用獨立鎖避免多個上傳互搶。
- 目前上傳併發數為 1（使用獨立 lock 控制），可依需求改成 semaphore 提升併發上傳數。
- 下載錄影時優先提供 `.mp4`，刪除會同步清除同名的 `.mkv`/`.mp4`。

### FFmpeg 設定

- 時間戳相關設定：`FFMPEG_THREAD_QUEUE_SIZE`、`FFMPEG_AUDIO_FILTER`、`FFMPEG_DEBUG_TS`（開啟時 `diagnostics/{job_id}/ffmpeg.log` 會包含更完整時間戳資訊）。
- 錄影期間會監看檔案大小，若超過 `FFMPEG_STALL_TIMEOUT_SEC` 無成長，視為 FFmpeg 卡住並標記失敗（不會觸發上傳）。

---

## 專案結構

```
.
├── api/                # FastAPI 應用與路由
├── config/             # 設定模組
├── database/           # SQLAlchemy 模型
├── docker/             # Docker 相關檔案
├── providers/          # 會議平台 Provider (Jitsi, Webex)
├── recording/          # FFmpeg 錄製管線 + 偵測框架
├── scheduling/         # APScheduler 排程
├── services/           # 通知 + 錄製管理服務
├── telegram_bot/       # Telegram Bot
├── uploading/          # YouTube 上傳
├── web/                # Web UI 模板
├── tests/              # 單元測試
├── data/               # SQLite 資料庫
├── recordings/         # 錄製檔案
└── diagnostics/        # 診斷資料
```

---

## 技術架構

- **Backend**: FastAPI + SQLAlchemy + APScheduler
- **Browser Automation**: Playwright (Chromium)
- **Recording**: FFmpeg + Xvfb + PipeWire（低延遲音訊）
- **Frontend**: Jinja2 + HTMX + Tailwind CSS (DaisyUI)
- **Notifications**: python-telegram-bot
- **Deployment**: Docker + docker-compose
- **CI/CD**: GitHub Actions

---

## 除錯工具

### VNC 遠端桌面

> **注意**：為減少 image 大小，x11vnc 預設不安裝。如需 VNC 功能，請在 Dockerfile 中加入 `x11vnc` 套件。

```bash
# 開發模式（自動讀取 override，啟用 build）
DEBUG_VNC=1 docker compose up --build

# VNC 連線：localhost:5900（無需密碼）
```

若 x11vnc 未安裝，設定 `DEBUG_VNC=1` 會顯示警告但不影響錄製功能。

推薦 VNC 客戶端：
- Windows: [TightVNC Viewer](https://www.tightvnc.com/)
- macOS: 內建 Screen Sharing 或 [RealVNC](https://www.realvnc.com/)
- Linux: `vncviewer` 或 Remmina

### 診斷資料

錄製失敗時會自動收集：
- `diagnostics/{job_id}/screenshot.png` - 截圖
- `diagnostics/{job_id}/page.html` - 頁面 HTML
- `diagnostics/{job_id}/console.log` - 瀏覽器 console
- `diagnostics/{job_id}/metadata.json` - 錯誤資訊

### Provider 測試腳本

```bash
# Jitsi 測試
python -m scripts.test_provider --url "https://meet.jit.si/test-room"

# Webex 測試
python -m scripts.test_provider --url "https://company.webex.com/meet/username" --provider webex

# 互動模式（每步暫停）
python -m scripts.test_provider --url "https://meet.jit.si/test-room" --interactive
```

完整參數：`--provider`, `--name`, `--password`, `--interactive`, `--slowmo`, `--timeout`, `--output-dir`

---

## 開發流程

### 安裝開發依賴

```bash
uv sync --extra dev
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

### 執行測試

```bash
uv run pytest tests/ -v
uv run pytest tests/ --cov=api --cov=providers --cov=database --cov=recording
uv run ruff check .
uv run ruff format .
```

### Pre-commit Hooks

| 時機 | 自動執行 |
|------|----------|
| `git commit` | ruff check + ruff format |
| `git push` | pytest 測試 |

### CI/CD Pipeline

| Job | 觸發條件 | 功能 |
|-----|---------|------|
| `test` | push / PR | 執行 pytest 測試 |
| `lint` | push / PR | 執行 ruff 檢查 |
| `docker` | push main | 建置並推送 Docker image 至 GHCR |

---

## 資料安全

使用 GHCR image 部署時，以下敏感資料**不會**包含在 image 中：

| 資料 | 儲存位置 | 說明 |
|------|----------|------|
| `.env` | 本地檔案 | 使用者自行建立 |
| `youtube_token.json` | `data/` volume | Runtime 產生 |
| `app.db` | `data/` volume | SQLite 資料庫 |
| 錄製檔案 | `recordings/` volume | 存在本地 |

---

## 未來改進方向

### 錄製架構

- **puppeteer-stream 方案**：使用瀏覽器內建 MediaRecorder API 直接錄製，音視頻天然同步，不依賴 Xvfb 和 PipeWire。適合追求最高穩定性的場景。
- **ALSA loopback**：作為 PipeWire 的備選方案，更底層但配置較複雜。

### 瀏覽器媒體控制

- 探索更底層的 Chromium 參數（如 `--disable-media-stream`）
- 研究 Playwright 的 `browserContext.grantPermissions` 進階用法

### Provider 狀態偵測

- 增加更多平台專屬的偵測選擇器
- 考慮增加 Google Meet 等其他平台支援

### 效能優化

- 考慮使用 GPU 加速編碼（NVENC/VAAPI）
- 支援多任務併發錄製（需要獨立的 display 和音訊 sink）
