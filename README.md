# MeetingRecorder

自動線上會議錄製系統，使用 Python + Playwright 自動加入會議，透過 Xvfb + PulseAudio + FFmpeg 在無頭環境中錄製影音。

> **⚠️ 重要提醒：本系統僅支援 Linux 環境**
>
> 錄製功能依賴 Linux 專用元件（Xvfb 虛擬顯示器、PulseAudio 虛擬音訊），**無法在 Windows 或 macOS 上直接執行**。
>
> - **Windows/macOS 使用者**：請使用 Docker 部署（Docker 內部運行 Linux 容器）
> - **Linux 使用者**：可直接本地執行或使用 Docker

## 功能特色

- **多平台支援**：Jitsi Meet、Cisco Webex (Guest Join)
- **自動化錄製**：Playwright 自動加入會議、處理等候室
- **排程管理**：支援單次與週期性 (cron) 排程
- **Web UI**：Dashboard 管理介面
- **Telegram Bot**：遠端控制與通知
- **YouTube 上傳**：錄製完成自動上傳
- **簡易認證**：密碼保護 API 與 Web UI

## 系統需求

### Docker 部署（推薦所有平台）

| 需求 | 說明 |
|------|------|
| Docker Desktop | Windows/macOS 需安裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| Docker Engine | Linux 可安裝 [Docker Engine](https://docs.docker.com/engine/install/) |
| Docker Compose | 通常隨 Docker 一起安裝 |
| 硬體資源 | 建議 4GB+ RAM，2+ CPU cores |

### 本地開發（僅限 Linux）

| 需求 | 說明 |
|------|------|
| Python 3.12+ | 主程式語言 |
| FFmpeg | 影音編碼 |
| Xvfb | 虛擬 X11 顯示器 |
| PulseAudio | 虛擬音訊系統 |
| Chromium | 瀏覽器自動化 |

## 快速開始（Docker 部署教學）

> **適用對象**：Windows、macOS、Linux 使用者
>
> 以下步驟將引導您從零開始部署 MeetingRecorder。

---

### 步驟 1：安裝 Docker

#### Windows

1. 下載並安裝 [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
2. 安裝完成後重新啟動電腦
3. 開啟 Docker Desktop，確認右下角圖示顯示 "Docker Desktop is running"
4. 開啟 PowerShell 或 CMD，輸入 `docker --version` 確認安裝成功

#### macOS

1. 下載並安裝 [Docker Desktop for Mac](https://www.docker.com/products/docker-desktop/)
2. 開啟 Docker Desktop
3. 開啟 Terminal，輸入 `docker --version` 確認安裝成功

#### Linux (Ubuntu/Debian)

```bash
# 安裝 Docker
curl -fsSL https://get.docker.com | sh

# 將當前使用者加入 docker 群組（免 sudo）
sudo usermod -aG docker $USER

# 重新登入後驗證
docker --version
```

---

### 步驟 2：建立部署目錄

```bash
# 建立專案目錄
mkdir meeting-recorder
cd meeting-recorder

# 建立資料目錄（用於持久化儲存）
mkdir -p data recordings diagnostics
```

---

### 步驟 3：下載設定檔

**方式 A：使用 curl 下載（Linux/macOS/Windows Git Bash）**

```bash
# 下載 docker-compose 設定檔
curl -O https://raw.githubusercontent.com/eyeduck-ai/meeting_recorder/main/docker-compose.hub.yml

# 下載環境變數範本
curl -O https://raw.githubusercontent.com/eyeduck-ai/meeting_recorder/main/.env.example
```

**方式 B：手動建立檔案**

建立 `docker-compose.hub.yml`：

```yaml
services:
  meeting-recorder:
    image: ghcr.io/eyeduckai/meeting_recorder:latest
    container_name: meeting-recorder
    ports:
      - "8000:8000"
      - "${VNC_PORT:-5900}:5900"
    volumes:
      - ./recordings:/app/recordings
      - ./diagnostics:/app/diagnostics
      - ./data:/app/data
    env_file:
      - .env
    environment:
      - TZ=${TZ:-Asia/Taipei}
      - DATABASE_URL=${DATABASE_URL:-sqlite:///./data/app.db}
    privileged: true
    shm_size: '2gb'
    restart: unless-stopped
```

---

### 步驟 4：設定環境變數

```bash
# 複製範本
cp .env.example .env

# 編輯設定檔
nano .env   # Linux/macOS
notepad .env  # Windows
```

**最小必要設定：**

```env
# 時區設定
TZ=Asia/Taipei

# 登入密碼（建議設定）
AUTH_PASSWORD=your-secure-password

# Session 加密金鑰（請更改為隨機字串）
AUTH_SESSION_SECRET=change-this-to-random-string
```

**可選設定：**

```env
# Telegram Bot（用於遠端通知與控制）
TELEGRAM_BOT_TOKEN=your-bot-token

# YouTube 上傳（需先在 Google Cloud Console 建立 OAuth 憑證）
YOUTUBE_CLIENT_ID=your-client-id
YOUTUBE_CLIENT_SECRET=your-client-secret

# 錄製設定
RESOLUTION_W=1920
RESOLUTION_H=1080
```

---

### 步驟 5：啟動服務

```bash
# 拉取最新映像檔
docker pull ghcr.io/eyeduckai/meeting_recorder:latest

# 啟動服務（背景執行）
docker-compose -f docker-compose.hub.yml up -d

# 查看執行狀態
docker-compose -f docker-compose.hub.yml ps

# 查看即時日誌
docker-compose -f docker-compose.hub.yml logs -f
```

---

### 步驟 6：開始使用

1. 開啟瀏覽器，前往 **http://localhost:8000**
2. 如有設定密碼，輸入 `AUTH_PASSWORD` 登入
3. 在 Dashboard 中建立會議和排程

---

### 常用操作指令

```bash
# 停止服務
docker-compose -f docker-compose.hub.yml down

# 重新啟動服務
docker-compose -f docker-compose.hub.yml restart

# 更新到最新版本
docker pull ghcr.io/eyeduckai/meeting_recorder:latest
docker-compose -f docker-compose.hub.yml up -d

# 查看容器日誌
docker-compose -f docker-compose.hub.yml logs -f

# 進入容器除錯
docker exec -it meeting-recorder bash
```

---

## 進階部署選項

### 從原始碼部署

適合需要自訂修改的開發者：

```bash
# 1. Clone 專案
git clone https://github.com/eyeduck-ai/meeting_recorder.git
cd meeting_recorder

# 2. 設定環境變數
cp .env.example .env
nano .env

# 3. 建構並啟動
cd docker
docker-compose up -d --build
```

### 本地開發（僅限 Linux）

> ⚠️ 本地開發需要 Linux 環境，Windows/macOS 請使用 Docker。

```bash
# 安裝 uv（Python 套件管理器）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安裝依賴
uv sync

# 安裝 Playwright 瀏覽器
uv run playwright install chromium
uv run playwright install-deps chromium

# 啟動虛擬音訊（需要 PulseAudio）
pulseaudio --start

# 啟動開發伺服器
uv run uvicorn api.main:app --reload
```

### 開發模式（含 VNC 遠端桌面）

可透過 VNC 查看容器內的瀏覽器畫面：

```bash
cd docker
docker-compose --profile dev up

# VNC 連線資訊
# 地址: localhost:5900
# 密碼: secret
```

推薦 VNC 客戶端：
- Windows: [TightVNC Viewer](https://www.tightvnc.com/)
- macOS: 內建 Screen Sharing 或 [RealVNC](https://www.realvnc.com/)
- Linux: `vncviewer` 或 Remmina

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
| `DEBUG_VNC` | 啟用 VNC 遠端桌面 | `0` |

完整設定請參考 `.env.example`。

## 使用方式

### Web UI

| 頁面 | 說明 |
|------|------|
| `/` | Dashboard 總覽 |
| `/meetings` | 會議設定管理 |
| `/schedules` | 排程管理 |
| `/jobs` | 錄製工作記錄 |
| `/recordings` | 錄製檔案下載 |
| `/settings` | YouTube 授權、Telegram 設定 |

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

**選單按鈕**：
- 📋 查看排程 - 顯示最近排程（含錄製狀態）
- ➕ 新增排程 - 建立新排程或立即錄製

> 部分指令需管理員核准後才能使用。

### API 端點

主要 API 端點：

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

# YouTube
GET  /api/v1/youtube/status     # 授權狀態
POST /api/v1/youtube/auth/start # 開始授權
```

API 認證方式：
- **Session Cookie**：透過 `/login` 頁面登入
- **X-API-Key Header**：`curl -H "X-API-Key: your-password" ...`

## 新增會議錄製

### 1. 建立 Meeting

**Jitsi Meeting:**
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

**Webex Meeting (Guest Join):**
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

> **Webex 注意事項**：
> - 支援 Personal Room URL 或完整會議連結
> - 自動處理等候室（Lobby）等待
> - 會議主持人需允許訪客加入

### 2. 建立 Schedule

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

### 3. 手動觸發測試

```bash
curl -X POST "http://localhost:8000/api/v1/schedules/1/trigger" \
  -H "X-API-Key: your-password"
```

## YouTube 授權設定

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立專案並啟用 **YouTube Data API v3**
3. 建立 OAuth 2.0 憑證（桌面應用程式類型）
4. 設定環境變數 `YOUTUBE_CLIENT_ID` 和 `YOUTUBE_CLIENT_SECRET`
5. 在 Web UI `/settings` 頁面完成 Device Code 授權流程

## Telegram Bot 設定

1. 向 [@BotFather](https://t.me/BotFather) 建立 Bot 並取得 Token
2. 設定環境變數 `TELEGRAM_BOT_TOKEN`
3. 啟動服務後，向 Bot 發送 `/start` 註冊
4. 在 Web UI `/settings` 頁面核准用戶

## 除錯工具

### VNC 遠端桌面

查看 Docker 容器內的瀏覽器畫面：

```bash
# 啟用 VNC
DEBUG_VNC=1 docker-compose up

# 連線到 localhost:5900（密碼：secret）
```

### 診斷資料

錄製失敗時會自動收集診斷資料：
- `diagnostics/{job_id}/screenshot.png` - 截圖
- `diagnostics/{job_id}/page.html` - 頁面 HTML
- `diagnostics/{job_id}/console.log` - 瀏覽器 console
- `diagnostics/{job_id}/metadata.json` - 錯誤資訊

## 專案結構

```
.
├── api/                # FastAPI 應用與路由
├── config/             # 設定模組
├── database/           # SQLAlchemy 模型
├── docker/             # Docker 相關檔案
├── providers/          # 會議平台 Provider (Jitsi, Webex)
├── recording/          # FFmpeg 錄製管線
├── scheduling/         # APScheduler 排程
├── telegram_bot/       # Telegram Bot
├── uploading/          # YouTube 上傳
├── web/                # Web UI 模板
├── data/               # SQLite 資料庫
├── recordings/         # 錄製檔案
└── diagnostics/        # 診斷資料
```

## 技術架構

- **Backend**: FastAPI + SQLAlchemy + APScheduler
- **Browser Automation**: Playwright (Chromium)
- **Recording**: FFmpeg + Xvfb + PulseAudio
- **Frontend**: Jinja2 + HTMX + Tailwind CSS (DaisyUI)
- **Notifications**: python-telegram-bot
- **Deployment**: Docker + docker-compose
- **CI/CD**: GitHub Actions

## 開發

### 執行測試

```bash
# 安裝開發依賴
uv pip install pytest pytest-asyncio pytest-cov ruff

# 執行所有測試
uv run pytest tests/ -v

# 執行測試並顯示覆蓋率
uv run pytest tests/ --cov=api --cov=providers --cov=database --cov=recording

# 執行 linter
uv run ruff check .
```

### CI/CD Pipeline

專案使用 GitHub Actions 自動化測試和部署：

| Job | 觸發條件 | 功能 |
|-----|---------|------|
| `test` | push / PR | 執行 pytest 測試 |
| `lint` | push / PR | 執行 ruff 檢查 |
| `docker` | push main | 建置並推送 Docker image 至 GHCR |

Docker image 會自動推送至 GitHub Container Registry：
- `ghcr.io/eyeduckai/meeting_recorder:latest` - 最新版本
- `ghcr.io/eyeduckai/meeting_recorder:sha-xxxxxx` - 特定 commit

## 資料安全

使用 GHCR image 部署時，以下敏感資料**不會**包含在 image 中：

| 資料 | 儲存位置 | 說明 |
|------|----------|------|
| `.env` | 本地檔案 | 使用者自行建立 |
| `youtube_token.json` | `data/` volume | Runtime 產生，存在本地 |
| `app.db` | `data/` volume | SQLite 資料庫，存在本地 |
| 錄製檔案 | `recordings/` volume | 存在本地 |

所有敏感資料都透過 volume mount 存放在使用者的本地機器，不會上傳到 GitHub Container Registry。

## Docker Image

```bash
# 拉取最新版本
docker pull ghcr.io/eyeduckai/meeting_recorder:latest
```

## 常見問題（FAQ）

### Q: 為什麼 Windows/macOS 無法直接執行？

本系統的錄製功能依賴以下 Linux 專用元件：
- **Xvfb**：虛擬 X11 顯示器，用於在無頭環境運行瀏覽器
- **PulseAudio**：虛擬音訊系統，用於擷取會議音訊

這些元件沒有 Windows/macOS 版本，因此必須透過 Docker 容器（內部運行 Linux）來執行。

### Q: Docker 容器需要多少資源？

建議配置：
- **RAM**：4GB 以上（2GB 為最低需求）
- **CPU**：2 核心以上
- **磁碟**：視錄製時長而定，1080p 影片約 500MB/小時

### Q: 如何查看錄製過程中的畫面？

啟用 VNC 遠端桌面功能：

```bash
# 方式 1：使用開發模式
cd docker
docker-compose --profile dev up

# 方式 2：設定環境變數
DEBUG_VNC=1 docker-compose -f docker-compose.hub.yml up -d
```

然後使用 VNC 客戶端連線到 `localhost:5900`，密碼為 `secret`。

### Q: 錄製失敗如何排查？

1. 查看容器日誌：
   ```bash
   docker-compose -f docker-compose.hub.yml logs -f
   ```

2. 檢查診斷資料（錄製失敗時自動產生）：
   ```
   diagnostics/{job_id}/
   ├── screenshot.png    # 失敗時的截圖
   ├── page.html         # 頁面 HTML
   ├── console.log       # 瀏覽器 console
   └── metadata.json     # 錯誤詳情
   ```

3. 啟用 VNC 觀察實際執行狀況

### Q: 如何更新到最新版本？

```bash
# 拉取最新映像檔
docker pull ghcr.io/eyeduckai/meeting_recorder:latest

# 重新啟動容器
docker-compose -f docker-compose.hub.yml up -d
```

### Q: 端口被占用怎麼辦？

修改 `docker-compose.hub.yml` 中的端口映射：

```yaml
ports:
  - "9000:8000"  # 將 8000 改為其他可用端口
```

然後透過 `http://localhost:9000` 訪問。

### Q: 資料會保存在哪裡？

所有資料都透過 Docker volume 保存在本地：

| 目錄 | 內容 |
|------|------|
| `./data/` | SQLite 資料庫、YouTube token |
| `./recordings/` | 錄製的影片檔案 |
| `./diagnostics/` | 失敗診斷資料 |

即使刪除容器，這些資料也會保留。

## License

MIT
