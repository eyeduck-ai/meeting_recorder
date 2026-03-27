# 開發指南

本文件提供人類開發者維護 `MeetingRecorder` 所需的背景、環境與流程。
agent 規則與文件同步要求請看根目錄 [AGENTS.md](../AGENTS.md)。

## 文件分工

- `README.md`：產品與部署入口，給第一次使用專案的人看
- `docs/development.md`：開發、除錯、測試與架構說明
- `AGENTS.md`：唯一 agent 規格來源，不要再新增平行的 agent 文件

## 執行模型概觀

系統由 FastAPI 應用程式啟動，並在啟動時完成以下工作：

1. 初始化資料庫
2. 清理前一次中斷後遺留在執行中狀態的 jobs
3. 啟動 APScheduler 並載入已啟用的 schedules
4. 若有設定 `TELEGRAM_BOT_TOKEN`，則同步啟動 Telegram Bot

錄製主流程如下：

1. API 或排程建立 `RecordingJob`
2. `JobRunner` 以單一 lock 控制錄製併發
3. `RecordingWorker` 建立虛擬錄製環境與 Playwright 瀏覽器
4. Provider 負責加入會議、等待大廳、調整版面
5. `FFmpegPipeline` 進行錄製
6. 依固定時長或自動偵測條件結束
7. 寫回 job 狀態、診斷資料、通知與可選的 YouTube 上傳

## 開發環境

### Docker 開發模式

跨平台開發最穩定的方式是 Docker。`docker-compose.override.yml` 會在 `docker compose up` 時自動載入，使用本地 `docker/Dockerfile` 建置映像。

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs -f app
```

相關 Compose 檔案：

| 檔案 | 角色 |
| --- | --- |
| `docker-compose.yml` | 共用設定、volume、port、env |
| `docker-compose.override.yml` | 本地原始碼建置 |
| `docker-compose.prod.yml` | 使用 GHCR 已發布映像 |

### 本地原始碼開發

僅 Linux 適合直接執行錄製流程。必要元件可從 `docker/Dockerfile` 對照，目前核心依賴包括：

- Python 3.12
- FFmpeg
- Xvfb
- PipeWire / pipewire-pulse / wireplumber
- pulseaudio-utils
- D-Bus
- Playwright Chromium

Python 開發環境：

```bash
uv sync --extra dev
uv run playwright install chromium
```

啟動 API：

```bash
uv run uvicorn api.main:app --reload
```

## 設定來源與優先順序

### 1. 環境變數與 `.env`

`config/settings.py` 會從 `.env` 載入基礎設定。這一層主要包含：

- 系統與路徑：`TZ`、`DATABASE_URL`
- 認證：`AUTH_PASSWORD`、`AUTH_SESSION_SECRET`
- Telegram：`TELEGRAM_BOT_TOKEN`
- YouTube：`YOUTUBE_CLIENT_ID`、`YOUTUBE_CLIENT_SECRET`、`YOUTUBE_DEFAULT_PRIVACY`
- FFmpeg 進階參數：`FFMPEG_*`

目前程式碼中的預設時區是 `Asia/Taipei`，不是 `UTC`。

### 2. `app_settings` 資料表

`services/app_settings.py` 目前管理一組可透過 UI/API 調整的設定鍵：

- `resolution_w`
- `resolution_h`
- `lobby_wait_sec`
- `ffmpeg_preset`
- `ffmpeg_crf`
- `ffmpeg_audio_bitrate`
- `jitsi_base_url`
- `pre_join_seconds`
- `tz`

注意：這層目前是「部分設定 DB 化」，不是所有錄製參數都完全改由資料庫驅動。文件與實作都應維持這個描述精度。

### 3. JSON 類設定

同樣存放在 `app_settings`，但內容為 JSON：

- `detection_config`：會議結束偵測器設定
- `notification_config`：SMTP / webhook 通知設定

## 重要架構事實

### 單工錄製

- `recording.worker.get_worker()` 是 singleton
- `scheduling.job_runner.JobRunner` 使用單一 `asyncio.Lock`
- 現況一次只支援一個錄製工作
- 排程衝突時，新的工作會進 queue 等待

### 排程行為

- `ScheduleType` 支援 `once` 與 `cron`
- CRON 使用標準五欄位格式，scheduler 內部會把 weekday 轉成 APScheduler 格式
- scheduler 會在啟動時從 DB 載入已啟用排程
- scheduler 也會同步 `next_run_at`，並在特定情境做 catch-up 判斷

### 自動偵測結束

偵測流程在 `recording/` 中實作，主要包含：

- 文字指示
- 視訊元素狀態
- WebRTC 連線
- URL 變更
- 螢幕凍結

偵測結果會寫入 `detection_logs`，並可經由 `/api/detection/*` 查詢、匯出、標記準確度與清空。

### Provider 現況

- UI 與對外文件主力支援：Jitsi、Webex
- 程式碼與測試中已有 Zoom provider 與 API 型別痕跡
- 但 UI 表單與 README 尚未把 Zoom 視為完整對外支援能力

若未來要正式宣告 Zoom 支援，需同步更新 README、UI、測試與 agent 文件。

## API 與功能面概觀

### 核心 HTTP 入口

- `/health`：健康檢查
- `/api`：服務與環境概覽
- `/api/environment`：目前錄製能力與執行環境狀態

### Jobs

- `/api/v1/jobs/record`
- `/api/v1/jobs/current`
- `/api/v1/jobs/progress/active`
- `/api/v1/jobs/{job_id}`
- `/api/v1/jobs/{job_id}/progress`
- `/api/v1/jobs/{job_id}/stop`
- `/api/v1/jobs/{job_id}/finish`
- `/api/v1/jobs/{job_id}/diagnostics`

### Meetings / Schedules

- `/api/v1/meetings/*`
- `/api/v1/schedules/*`

### Settings / Detection / Recording Management

- `/api/v1/settings`
- `/api/detection/*`
- `/api/recordings/*`

### Telegram / YouTube

- `/api/v1/telegram/*`
- `/api/v1/youtube/*`

這些路由才是目前文件應描述的真實 API 範圍；若路由有新增、刪除或改名，請同步更新本文件。

## 專案結構

| 目錄 | 說明 |
| --- | --- |
| `api/` | FastAPI app、API routes、Web UI routes |
| `config/` | 環境設定與 logging |
| `database/` | SQLAlchemy models、session、repository 支援 |
| `providers/` | Jitsi / Webex / Zoom provider |
| `recording/` | 錄製 worker、虛擬環境、FFmpeg、偵測器 |
| `scheduling/` | APScheduler 與 job runner |
| `services/` | app settings、通知、錄影管理 |
| `telegram_bot/` | Telegram bot handlers 與 conversations |
| `uploading/` | YouTube 上傳與進度追蹤 |
| `web/` | Jinja templates 與靜態資源 |
| `tests/` | pytest 測試 |
| `docker/` | Dockerfile 與 entrypoint |

## 除錯與診斷

### 容器日誌

```bash
docker compose logs -f app
```

### 失敗診斷目錄

錄製失敗時常見輸出：

- `diagnostics/<job_id>/metadata.json`
- `diagnostics/<job_id>/screenshot.png`
- `diagnostics/<job_id>/page.html`
- `diagnostics/<job_id>/console.log`
- `diagnostics/<job_id>/ffmpeg.log`
- `diagnostics/<job_id>/remux.log`
- `diagnostics/<job_id>/transcode.log`

### Provider 測試腳本

```bash
python -m scripts.test_provider --url "https://meet.jit.si/test-room"
python -m scripts.test_provider --url "https://company.webex.com/meet/user" --provider webex
python -m scripts.test_provider --url "https://zoom.us/j/123456789" --provider zoom
```

## 測試與品質檢查

安裝開發工具：

```bash
uv sync --extra dev
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

常用檢查：

```bash
uv run pytest tests/ -v
uv run pytest tests/ --cov=api --cov=providers --cov=database --cov=recording --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
```

CI 目前由 `.github/workflows/ci.yml` 定義，主要包含：

- `test`
- `lint`
- `docker`（僅 push 時建置與推送映像）

## 文件維護原則

- 對使用者可見的穩定事實放 `README.md`
- 對開發者有用的實作背景放 `docs/development.md`
- 對 agent 的操作規格、同步責任與已知落差放 `AGENTS.md`
- 不要再把 roadmap、TODO、歷史變更摘要混進這份文件
