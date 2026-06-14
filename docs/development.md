# 開發指南

本文件提供人類開發者維護 `MeetingRecorder` 所需的背景、環境與流程。
agent 規則與文件同步要求請看根目錄 [AGENTS.md](../AGENTS.md)。

## 文件分工

- `README.md`：產品與部署入口，給第一次使用專案的人看
- `docs/development.md`：開發、除錯、測試與架構說明
- `AGENTS.md`：AI agent 必須遵守的工作規則，不要再新增平行的 agent 文件
- `Plan.md`：架構改善方向與優先級
- `Task.md`：可執行改善任務清單，完成後必須標記 `[x]`
- `Lesson.md`：踩坑、誤判與避免方式

## 執行模型概觀

系統由 FastAPI 應用程式啟動，並在啟動時完成以下工作：

1. 初始化資料庫
2. 清理前一次中斷後遺留在執行中狀態的 jobs
3. 建立 app-owned `RecordingWorker`、`JobRunner`、`SchedulerService` 並放入 `app.state`
4. 啟動 APScheduler 並載入已啟用的 schedules
5. 若有設定 `TELEGRAM_BOT_TOKEN`，則同步啟動 Telegram Bot

FastAPI 使用 lifespan 管理 runtime ownership。Route import 不應初始化 DB、啟動 scheduler 或建立 recorder runtime；這些副作用只能在 lifespan startup 發生。

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

跨平台開發最穩定的方式是 Docker。dev/test runtime 必須和正式 recorder 隔離，避免替換已部署的 container。

```bash
cp .env.example .env
python -m scripts.dev_compose up --build -d
docker compose -p meeting-recorder-dev-<workspace-hash> logs -f app
```

`scripts.dev_compose` 會自動設定：

- `COMPOSE_PROJECT_NAME=meeting-recorder-dev-<workspace-hash>`
- `APP_PORT=8001`
- `VNC_PORT=5901`
- `MEETING_RECORDER_IMAGE=meeting-recorder:dev-<workspace-hash>`

它也會檢查 Docker labels、working directory、host ports 與 image tag；若會碰到正式 recorder，就會拒絕執行。

相關 Compose 檔案：

| 檔案 | 角色 |
| --- | --- |
| `docker-compose.yml` | 共用設定、volume、port、env |
| `docker-compose.override.yml` | 本地原始碼建置，建議透過 `scripts.dev_compose` 使用 |
| `docker-compose.deploy.yml` | 從原始碼正式部署，使用 production runtime identity |
| `docker-compose.prod.yml` | 使用 GHCR 已發布映像 |

正式部署從原始碼建置時使用：

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up --build -d
```

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
- 認證與 API 邊界：`AUTH_PASSWORD`、`AUTH_SESSION_SECRET`、`CORS_ALLOWED_ORIGINS`
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

錄製 runtime 的有效設定由 `services/runtime_config.py` 解析，優先順序是：明確 job/schedule/API override > DB `app_settings` > `.env` / `config.settings` > code default。路徑、secret、auth、Telegram、YouTube 與 DB URL 仍屬於 `.env` / `config.settings` 管理，不放入 DB overlay。

手動錄製省略 `lobby_wait_sec` 時會使用 DB/global 預設。新建 schedule 省略 `lobby_wait_sec`、`resolution_w`、`resolution_h` 時，會在建立當下解析成 concrete value 寫入 schedule；既有 schedule 的錄製設定視為該 schedule 的明確覆蓋值。

### 3. JSON 類設定

同樣存放在 `app_settings`，但內容為 JSON：

- `detection_config`：會議結束偵測器設定
- `notification_config`：SMTP / webhook 通知設定

### Secret Handling

- 本專案目前採 Rename + Redact，不提供真正 at-rest encryption。
- `meetings.password_encrypted` 與 `recording_jobs.password_hash` 是 legacy DB column name；Python 端應使用 `meeting_password_plaintext`，不要再新增 `password_encrypted` / `password_hash` 屬性依賴。
- Meeting API 只回傳 `has_password`，Web UI edit form 不回填既有密碼；空白代表保留，清除需明確勾選。
- Notification config API 必須遮罩 `smtp_password` 與 `webhook_secret`；提交 `********` 代表保留既有 secret，空字串代表清除。
- YouTube token 仍是 plaintext JSON，寫入後只做 best-effort owner-only file permission。部署者必須保護 `.env`、SQLite DB、`data/` volume 與備份。

## 重要架構事實

### Service Layer

- `services/meeting_service.py` 集中 meeting create/update/delete，包含 request `password` 到 ORM `meeting_password_plaintext` 的映射。
- `services/schedule_service.py` 集中 schedule create/update/delete/toggle/trigger，負責 meeting validation、cron validation、RuntimeConfigService 解析，以及 APScheduler 同步。
- `services/job_service.py` 集中 immediate recording start，負責 `JobRunner` busy 判斷、`run_immediate()` 呼叫與 DB job 回讀。
- API routes、Web UI 與 Telegram 的 write/trigger path 應呼叫 service；read-only list/detail query 可暫時留在入口層。
- FastAPI routes 應透過 `api/runtime.py` 從 `request.app.state` 建立 app-state-backed service；非 FastAPI 入口可使用 service 的 compatibility fallback。

### Module Boundaries

- `api/routes/ui.py` 是 Web UI route 聚合點；route implementation 不應再塞回此檔。Shared templates/context/settings 已移到 `api/routes/ui_common.py`，job failure log 解析與 excerpt 載入已移到 `api/routes/ui_job_diagnostics.py`。auth、dashboard、meeting、schedule、settings、jobs、recordings route 分別由 `ui_auth.py`、`ui_dashboard.py`、`ui_meetings.py`、`ui_schedules.py`、`ui_settings.py`、`ui_jobs.py`、`ui_recordings.py` 負責。
- `api.routes.ui.router` 是對外聚合入口；`api/main.py` 與 router-only tests 仍只需要 include 這個 router。拆新的 UI 子 router 時，要由 `ui.py` include 回聚合 router。
- UI 子 router 不得 import `api.routes.ui`；需要 template rendering 或 UI settings 時依賴 `ui_common`，需要 job log helper 時依賴 `ui_job_diagnostics`。
- `telegram_bot/conversations.py` 是相容 re-export 聚合器；create schedule、edit schedule、create meeting conversation 實作分別位於 `conversation_create_schedule.py`、`conversation_edit_schedule.py`、`conversation_create_meeting.py`，共用 cancel/time/duration helper 位於 `conversation_common.py`。
- `providers/base.py` 是 provider bounded wait helper 的 owner；Jitsi/Webex/Zoom join/prejoin flow 不應新增裸 `asyncio.sleep()`，必要 debounce 要透過共用 helper 或註解說明。
- `scheduling/job_runner.py` 應專注在 queue orchestration、schedule lifecycle 與 upload 委派。Schedule queue、pending、duplicate 與 queue position 狀態已移到 `scheduling/schedule_queue.py`；recording retry、attempt DB 更新、status callback 與 stage notification 已移到 `scheduling/recording_executor.py`；YouTube upload 前的 remux/transcode、upload progress 與 YouTube metadata 已移到 `scheduling/upload_runner.py`。
- `recording/monitor.py` 是錄製監控 loop owner，集中處理 duration、finish/cancel request、FFmpeg stall 與 auto-detect end 判斷；`RecordingWorker` 只負責 orchestration 並透過 wrapper 委派。
- `services/recording_manager.py` 的 list、cleanup 與 disk usage 應共用單次 filesystem scan 產生的 entry/stat metadata；新增錄影檔功能時不要在同一 request 內重複 `rglob()` 或對同一影片重複 `stat()`。
- 後續拆大型檔案時，優先選擇能用現有 tests 保護的邊界，並保留必要的相容 import 或同步更新測試 fixture。

### Runtime Lifecycle

- FastAPI lifespan 是 worker、job runner 與 scheduler 的主要 owner。
- `app.state.worker`、`app.state.job_runner`、`app.state.scheduler` 是 API/Web UI 入口的 runtime 來源。
- `recording.worker.get_worker()`、`scheduling.job_runner.get_job_runner()`、`scheduling.scheduler.get_scheduler()` 保留為相容 accessor，主要供 Telegram、測試與非 FastAPI 入口 fallback 使用。
- `api/routes/*.py` 不應在 import 階段呼叫 `init_db()` 或啟動任何 runtime。
- FastAPI shutdown 需要停止 scheduler/Telegram 並關閉已建立的 YouTube uploader HTTP client；不要為了 close 而建立新的 uploader singleton。

### Database Layer

- `database/base.py` 只定義 SQLAlchemy declarative `Base`。
- `database/models.py` 只放 ORM model、enum 與 model helper method；短期仍 re-export `get_engine()`、`get_session_local()`、`get_db()`、`init_db()` 與 `_run_schema_migrations`，供舊 import 相容。
- `database/session.py` 是 engine/session/FastAPI DB dependency 與 `init_db()` 的 owner，也保留 `JobRepository` 與 recording job result mapping helper。
- `database/migrations.py` 集中 SQLite idempotent ad hoc migration helper，包括 `run_schema_migrations()` 與 `ensure_column()`。

目前尚未正式導入 Alembic；`init_db()` 仍會先 `Base.metadata.create_all()`，再執行 SQLite idempotent migrations。後續導入 Alembic 時，metadata source 應以 `database/base.py` 的 `Base` 加上 `database/models.py` 的 ORM model 為準，並逐步移除 `database.models` 內的 DB lifecycle compatibility re-export。

### 單工錄製

- FastAPI app 內由 lifespan 建立 app-owned `RecordingWorker`
- `scheduling.job_runner.JobRunner` 使用單一 `asyncio.Lock`
- 現況一次只支援一個錄製工作
- 排程衝突時，新的工作會進 queue 等待

### 排程行為

- `ScheduleType` 支援 `once` 與 `cron`
- CRON 使用標準五欄位格式，scheduler 內部會把 weekday 轉成 APScheduler 格式
- scheduler 會在啟動時從 DB 載入已啟用排程
- scheduler 也會同步 `next_run_at`，並在特定情境做 catch-up 判斷；`_sync_all_next_run_times()` 應用單一 DB session 批次同步，且跳過 unchanged `next_run_at`。
- 手動 trigger schedule 時，fixed duration 從觸發當下起算；APScheduler 自動觸發才使用 schedule 原始時間窗
- schedule lifecycle 欄位語意如下：
  - `last_triggered_at`：APScheduler、manual trigger 或 catch-up 觸發時間
  - `last_started_at`：`JobRunner` 實際取得 lock 並開始執行該 schedule 的時間
  - `last_completed_at`：該 schedule 對應 job 結束時間，成功、失敗或取消都會更新
  - `last_run_at`：短期相容欄位，現在視為 `last_started_at` 的 legacy alias，不再於 trigger 當下更新
- catch-up 判斷不再把 trigger 當成已執行；若同一 schedule 正在執行或已在 queue，會跳過 catch-up。若最近一筆對應 job 已成功或以 auto-detected 結束，也會跳過。
- manual trigger 會透過 `JobRunner.queue_schedule()` 回傳 `triggered`、`queued` 或 duplicate。系統 busy 但可排隊時不回 409；同一 schedule 已在執行或 queue 中時才回 duplicate。

### 自動偵測結束

偵測流程在 `recording/` 中實作，主要包含：

- 文字指示
- 視訊元素狀態
- WebRTC 連線
- URL 變更
- 螢幕凍結

偵測結果會寫入 `detection_logs`，並可經由 `/api/detection/*` 查詢、匯出、標記準確度與清空。

### Provider 現況

- provider registry metadata 是 provider 名稱、UI 選項、表單 hint、Telegram keyboard 與 API provider validation 的單一來源。
- 目前正式支援：Jitsi、Webex、Zoom。
- Zoom 建議使用完整邀請連結做 `meeting_code`，包含 URL 內的 `pwd` 參數時可由 provider 保留並帶入加入流程
- provider join flow 應優先使用 Playwright selector/state/function bounded wait；需要固定 sleep 時要是短 fallback 或明確 debounce，避免在熱路徑累積無條件等待。
- 若未來新增或移除 provider，應先更新 `providers/__init__.py` 的 provider class registration 與 metadata，再同步 README、測試與 agent 文件。

## API 與功能面概觀

### 核心 HTTP 入口

- `/health`：健康檢查
- `/api`：服務與環境概覽
- `/api/environment`：目前錄製能力與執行環境狀態

以上三個 HTTP 入口在啟用 `AUTH_PASSWORD` 時仍維持公開。其他 `/api/*` 路由必須透過 session cookie 或 `X-API-Key` 認證。CORS 預設不啟用；若需要跨來源 browser client，必須以 `CORS_ALLOWED_ORIGINS` 明確列出允許的 origins，不支援 wildcard `*`。

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

`POST /api/v1/schedules/{id}/trigger` 的 response 語意：

- immediate accepted：HTTP 200，`status: "triggered"`，`queue_position: 0`
- recorder busy but queued：HTTP 202，`status: "queued"`，`queue_position` 為佇列位置
- duplicate running/queued schedule：HTTP 409，`detail: "Schedule is already running or queued"`

`ScheduleResponse` 會回傳 `last_triggered_at`、`last_started_at`、`last_completed_at` 與 legacy `last_run_at`。新程式應優先使用 lifecycle 欄位，不要再用 `last_run_at` 推論 trigger 是否發生。

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
| `database/` | SQLAlchemy base、models、session、SQLite migration 與 repository 支援 |
| `providers/` | Jitsi / Webex / Zoom provider |
| `recording/` | 錄製 worker、虛擬環境、FFmpeg、偵測器 |
| `scheduling/` | APScheduler 與 job runner |
| `services/` | service layer、app settings、通知、錄影管理 |
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
- 改善方向、待辦任務與經驗教訓放根目錄 `Plan.md`、`Task.md`、`Lesson.md`
- 不要再把 roadmap、TODO、歷史變更摘要混進本文件正文
