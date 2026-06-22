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
2. `JobRunner` 以 `MAX_CONCURRENT_RECORDINGS` 控制同時錄製數，超過上限的工作留在 queue
3. `RecordingWorker` 為每個 active job 建立獨立虛擬錄製環境與 Playwright 瀏覽器
4. Provider 負責加入會議、等待大廳、調整版面
5. `RecordingSession` 準備固定尺寸 browser capture surface，必要時套用上方裁切 offset
6. `FFmpegPipeline` 進行錄製
7. 依固定時長或自動偵測條件結束
8. 成功錄影會 best-effort fast remux 成本機 canonical `.mp4`，再寫回 job 狀態、診斷資料、通知與可選的 YouTube 上傳

retryable join/network failure 不在 active recording task 內 sleep；`RecordingExecutor` 只回傳 delayed retry request，`JobRunner` 釋放錄製 slot 後先把同一 logical job 暴露為 retry waiting，延遲到期才重新送回 FIFO queue。Retry waiting 可取消、可由 API/UI/Telegram 觀測，但不計入 `queue_length`，也沒有 FIFO queue position。

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
- 錄製預設：`RECORDING_BROWSER_MODE`、`RECORDING_CROP_MODE`、`RECORDING_CROP_TOP_PX`
- 並行與保護：`MAX_CONCURRENT_RECORDINGS`、`RECORDING_DISPLAY_START`、`RECORDING_DISPLAY_POOL_SIZE`、`MIN_FREE_DISK_GB_BEFORE_RECORDING`、`MAX_RECORDING_SEC`、`MAX_PARALLEL_TRANSCODES`、`MAX_PARALLEL_ACTIVITY_ANALYSES`
- FFmpeg 進階參數：`FFMPEG_*`

目前程式碼中的預設時區是 `Asia/Taipei`，不是 `UTC`。

### 2. `app_settings` 資料表

`services/app_settings.py` 目前管理一組可透過 UI/API 調整的設定鍵：

- `resolution_w`
- `resolution_h`
- `recording_browser_mode`
- `recording_crop_mode`
- `recording_crop_top_px`
- `smart_trim_enabled`
- `dynamic_extension_enabled`
- `dynamic_extension_idle_sec`
- `dynamic_extension_max_sec`
- `activity_audio_threshold_db`
- `activity_video_diff_threshold`
- `activity_sample_interval_sec`
- `activity_sample_window_sec`
- `smart_trim_pre_roll_sec`
- `smart_trim_end_post_roll_sec`
- `lobby_wait_sec`
- `ffmpeg_preset`
- `ffmpeg_crf`
- `ffmpeg_audio_bitrate`
- `jitsi_base_url`
- `pre_join_seconds`
- `tz`

Settings API 與 Web UI 應使用 `get_all_settings()` 讀取完整 overlay，並使用 `update_settings()` 一次 batch upsert 已知 key；不要新增或恢復未接線的單 key getter/setter。只在 owner module 內使用的 settings defaults builder 應維持私有。

錄製 runtime 的有效設定由 `services/runtime_config.py` 解析，優先順序是：明確 job/schedule/API override > DB `app_settings` > `.env` / `config.settings` > code default。路徑、secret、auth、Telegram、YouTube 與 DB URL 仍屬於 `.env` / `config.settings` 管理，不放入 DB overlay。

手動錄製省略 `lobby_wait_sec` 時會使用 DB/global 預設。新建 schedule 省略 `lobby_wait_sec`、`resolution_w`、`resolution_h` 時，會在建立當下解析成 concrete value 寫入 schedule；既有 schedule 的錄製設定視為該 schedule 的明確覆蓋值。`recording_browser_mode`、`recording_crop_mode` 與 `recording_crop_top_px` 是全域 capture 設定，會在每次錄製執行時重新解析，不寫入 schedule。`smart_trim_enabled`、`dynamic_extension_enabled`、`dynamic_extension_idle_sec` 與 `dynamic_extension_max_sec` 有全域預設，也可由 schedule nullable 欄位覆寫；`None` 代表繼續繼承 global default。schedule create/update 會用覆寫後的有效組合驗證 `dynamic_extension_max_sec == 0 or dynamic_extension_max_sec >= dynamic_extension_idle_sec`，避免 invalid schedule 延後到執行時才失敗。

### 3. JSON 類設定

同樣存放在 `app_settings`，但內容為 JSON：

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
- `services/job_service.py` 集中 immediate recording start，負責 `JobRunner.run_immediate()` 呼叫與 DB job 回讀；容量已滿時由 `JobRunner` 排隊，不在 service 層回 busy conflict。
- `services/storage_maintenance.py` 集中本機 MP4 canonicalization、uploaded recording retention、diagnostics/log cleanup、detection log cleanup 與 SQLite `VACUUM`。本機 canonicalization 固定使用 fast remux；YouTube upload path 才依 `FFMPEG_TRANSCODE_ON_UPLOAD` 決定是否產生臨時壓縮上傳檔。
- `services.secrets` 只對外提供 mask / preserve secret contract；mask sentinel detection 是內部細節，不應暴露成第二個 public helper。`services.notification` 對外保留 `NotificationService` / `get_notification_service()` / `reset_notification_service()`，email/webhook channel implementation class 與 cached service global 維持 owner module 私有。
- `services/__init__.py` 不 re-export 具體 service；呼叫端應直接從 owner module import，例如 `services.schedule_service` 或 `services.recording_manager`，避免 package import 時 eager-load 無關 service。
- API routes、Web UI 與 Telegram 的 write/trigger path 應呼叫 service；read-only list/detail query 可暫時留在入口層。
- FastAPI routes 應透過 `api/runtime.py` 從 `request.app.state` 建立 app-state-backed service；非 FastAPI 入口可使用 service 的 compatibility fallback。

### Module Boundaries

- `api/routes/ui.py` 是 Web UI route 聚合點；route implementation 不應再塞回此檔。Shared templates/context/settings 已移到 `api/routes/ui_common.py`，job failure log 解析與 excerpt 載入已移到 `api/routes/ui_job_diagnostics.py`，recording artifact display/download state 已移到 `api/routes/ui_recording_artifacts.py`。auth、dashboard、meeting、schedule、settings、jobs、recordings route 分別由 `ui_auth.py`、`ui_dashboard.py`、`ui_meetings.py`、`ui_schedules.py`、`ui_settings.py`、`ui_jobs.py`、`ui_recordings.py` 負責。
- `api.routes.ui.router` 是對外聚合入口；`api/main.py` 與 router-only tests 仍只需要 include 這個 router。拆新的 UI 子 router 時，要由 `ui.py` include 回聚合 router。
- UI 子 router 與測試不得透過 `api.routes.ui` 取得 helper re-export；需要 template rendering 或 UI settings 時依賴 `ui_common`，需要 job log helper 時依賴 `ui_job_diagnostics`，需要 recording artifact display/download state 時依賴 `ui_recording_artifacts`。
- Telegram conversation 不再透過 re-export 聚合器；create schedule、edit schedule、create meeting conversation 實作分別位於 `conversation_create_schedule.py`、`conversation_edit_schedule.py`、`conversation_create_meeting.py`，共用 cancel/time/duration helper 位於 `conversation_common.py`；handler/conversation 需要直接 DB session 時使用 `telegram_bot/session.py`，不要從 `telegram_bot/__init__.py` re-export。
- `providers/base.py` 是 provider bounded wait helper 的 owner；Jitsi/Webex/Zoom join/prejoin flow 不應新增裸 `asyncio.sleep()`，必要 debounce 要透過共用 helper 或註解說明。
- `providers/zoom.py` 使用 Zoom 專用 page-stage/action loop 推進 launch page、cookie banner、Join from browser、name/password form、waiting room 與 in-meeting 狀態；不要再把 Zoom join 寫成固定頁面順序。
- Provider 可實作 `dismiss_transient_overlays()` 清理進入會議後遮擋錄影的暫時 UI；`RecordingSession` 只呼叫 provider hook，不應知道各 provider DOM selector。
- `RecordingSession` 預設以 Chromium app window 啟動實際 join URL，並使用 persistent context 的第一個 page 作為錄製頁；normal browser mode 只作為 fallback/debug 路徑。
- app mode 會 bounded wait initial page，且不主動 request DOM fullscreen；normal/fallback mode 才保留 fullscreen best-effort。
- `RecordingSession.prepare_capture_surface()` 負責進入錄製前的瀏覽器 capture surface 準備、crop 解析與 browser dimension diagnostics；provider 不應承擔 Chromium launch flags 或 FFmpeg crop offset。
- `scheduling/job_runner.py` 應專注在 queue orchestration、schedule lifecycle、delayed retry requeue、post-processing task ownership 與 upload task ownership。Schedule queue、pending、duplicate 與 queue position 狀態已移到 `scheduling/schedule_queue.py`；recording attempt DB 更新、status callback 與 stage notification 已移到 `scheduling/recording_executor.py`，stage notification task 必須在送出前重讀 DB status 並 skip stale status；成功 raw capture 進入 post-processing 前會明確送出 `finalizing` stage notification，worker status callback 只更新 `finalizing` DB 狀態、不另送同階段通知，避免同內容 Telegram 訊息重複；成功 raw capture 的 smart trim / 本機 MP4 canonicalization 已移到 `recording/post_processing.py`；YouTube upload 前的 remux/transcode、upload progress 與 YouTube metadata 已移到 `scheduling/upload_runner.py`。
- `recording/job_types.py` 是 `RecordingJob` / `RecordingResult` DTO owner；`recording.worker` 不 re-export DTO。新內部模組應依賴 DTO module，避免 post-processing、runner 或 DB helper import worker implementation。
- `recording/runtime_resources.py` 負責 process-local display/audio lease 分配；不要在 `JobRunner` 或 provider 內硬編 `:99` 或共用 `virtual_speaker` 作為並行錄製資源。
- `recording/pactl.py` 是 `pactl list ... short` device name parsing owner；runtime checks、virtual audio setup 與 FFmpeg audio source checks 應共用它，不要在各模組複製 parser。
- `recording/monitor.py` 是錄製監控 loop owner，集中處理 duration、dynamic extension、finish/cancel request 與 FFmpeg stall；`RecordingWorker` 只負責 orchestration 並透過 wrapper 委派。
- `recording/activity.py` 是媒體活動判斷 owner，包含 live PulseAudio/FFmpeg 音訊 probe、browser screenshot 差異 probe、完成檔案的 streaming batch activity sampling、boundary refinement 與 trim helper。不要把 provider DOM selector 放進這一層；provider UI 狀態與媒體活動是兩種不同訊號。
- `recording/remux.py` 對外保留 canonical / upload MP4 preparation helpers、MKV/MP4 sibling variant helper 與 best-effort artifact deletion helper；只服務 upload transcode 的 path derivation helper 維持私有，不作為 public utility。
- `services/recording_manager.py` 的 list、cleanup 與 disk usage 應共用單次 filesystem scan 產生的 entry/stat metadata；新增錄影檔功能時不要在同一 request 內重複 `rglob()` 或對同一影片重複 `stat()`。
- 後續拆大型檔案時，優先選擇能用現有 tests 保護的邊界，並保留必要的相容 import 或同步更新測試 fixture。

### Runtime Lifecycle

- FastAPI lifespan 是 worker、job runner 與 scheduler 的主要 owner。
- `app.state.worker`、`app.state.job_runner`、`app.state.scheduler` 是 API/Web UI 入口的 runtime 來源。
- `recording.worker.get_worker()`、`scheduling.job_runner.get_job_runner()`、`scheduling.scheduler.get_scheduler()` 保留為相容 accessor，主要供 Telegram、測試與非 FastAPI 入口 fallback 使用。
- `api/routes/*.py` 不應在 import 階段呼叫 `init_db()` 或啟動任何 runtime。
- FastAPI shutdown 需要停止 scheduler/Telegram、呼叫 `JobRunner.shutdown()` 收斂 delayed retry、active recording、tracked post-processing 與 tracked upload tasks，並關閉已建立的 YouTube uploader HTTP client；不要為了 close 而建立新的 uploader singleton。
- APScheduler 會新增 internal job `storage_maintenance_daily`，每日 03:30 local time 執行 storage maintenance；它不是使用者 recording schedule，不應寫入 schedule lifecycle 欄位。

### Database Layer

- `database/base.py` 只定義 SQLAlchemy declarative `Base`。
- `database/models.py` 只放 ORM model、仍被呼叫端使用的 enum 與 model helper method；provider 名稱由 provider registry metadata 作為單一來源，不再保留 `ProviderType` enum；legacy `duration_mode` 只保留為 persisted string column / migration target，不再保留 `DurationMode` enum。不要從這裡 re-export DB lifecycle helper，也不要為 API response 保留通用 ORM `to_dict()` serializer。
- `database/session.py` 是 engine/session factory、FastAPI `get_db()` dependency 與 `init_db()` 的 owner，也保留 `JobRepository` 與 recording job result mapping helper；非 FastAPI 入口應直接使用 `get_session_local()` 建立明確生命週期的 session，不要新增第二套 context-manager wrapper。
- `database/migrations.py` 集中 SQLite idempotent ad hoc migration helper，包括 `run_schema_migrations()` 與 `ensure_column()`；legacy schedule 欄位如 `duration_mode` 與 `dry_run` 也由這裡補齊，不再保留一次性手動 SQLite migration script。

目前尚未正式導入 Alembic；`init_db()` 仍會先 `Base.metadata.create_all()`，再執行 SQLite idempotent migrations。後續導入 Alembic 時，metadata source 應以 `database/base.py` 的 `Base` 加上 `database/models.py` 的 ORM model 為準。

### 受控並行錄製

- FastAPI app 內由 lifespan 建立 app-owned `RecordingWorker`
- `scheduling.job_runner.JobRunner` 使用容量控制，預設同時執行 `MAX_CONCURRENT_RECORDINGS=2` 個錄製工作
- `scheduling.schedule_queue.ScheduleRunQueue` 是 unified FIFO queue owner，統一保存 schedule 與 immediate queued item、queue position、pending schedule、active schedule set 與 duplicate 狀態
- `JobRunner` 不再持有第二條 direct queue；它只保存 immediate job 的 execution payload 與 delayed retry payload，並從 `ScheduleRunQueue` 依 enqueue 順序 drain 到可用 worker slot；retry waiting 不算 active recording slot，也不屬於 FIFO queue position，但必須透過 `retry_waiting_items[]` 可見且可取消
- recording capacity slot 只涵蓋 capture runtime：`RecordingWorker` 在 FFmpeg `finalize_capture()` 後 cleanup browser/Xvfb/audio lease 並回傳 raw result；smart trim 與本機 MP4 canonicalization 由 `RecordingPostProcessor` 以 tracked post-processing task 執行，不計入 `MAX_CONCURRENT_RECORDINGS`
- `JobRunner` 以 post-processing task state 區分 `process` 與 raw-success `settle`；`process` 失敗或取消最多只排一次 `settle`，`settle` 失敗只記 log，不可遞迴重排或改回 fire-and-forget。shutdown 期間若 post-processing 已產生 upload request 但 upload 尚未啟動，job 會維持 `succeeded` 並記錄 upload interrupted。
- `RecordingWorker` 維護 active job registry；對外 active state 只看 `active_jobs` / `active_count`，舊 `is_busy` / `current_status` 全域狀態已移除。cancel/finish flag 是 per-job 狀態。`_current_job` 只保留給 worker 內部相容，不是 API/Web UI/Telegram 的 active 或容量 source of truth
- `services.job_actions.JobActionService` 是 REST API 與 Web UI 的 job lifecycle 決策層，統一處理 queued cancel、active stop/finish、terminal-only delete 與 queued schedule cancellation；queued cancel 只使用 `JobRunner.cancel_queued_job_for_action()` 的 structured result 判斷 FIFO / retry waiting 來源，不再保留 boolean `cancel_queued_job()` fallback 或 retry state 推論 wrapper；`ACTIVE_RECORDING_STATUSES` 與 `TERMINAL_JOB_STATUSES` 也由該模組提供，route/template 不應各自複製 status list
- `services.job_runtime_state.JobRuntimeStateService` 是 API、Web UI 與 Telegram runtime state view 的組裝層，統一把 DB、worker active registry、FIFO queue 與 delayed retry waiting 合成 `JobRuntimeSnapshot`；runner 缺少 partial capacity/count 欄位或回傳 `None`、負數、非數字時的 fallback 也由這裡集中推導，route、template 與 Telegram handler 不應各自重建 active/queued/retry map 或容量 fallback
- 每個 active recording attempt 會取得一個 `RuntimeResourceLease`，包含獨立 Xvfb display 與 Pulse/PipeWire sink，例如 `:100`、`mr_sink_<job_id>`
- 每個 active recording attempt 也會透過 `recording.capacity_guard.RecordingCapacityGuard` 做 process-local disk reservation；1080p 預估 2.5GB/小時、720p 預估 1.2GB/小時，其他解析度依像素比例縮放且最低預留 1GB；若 dynamic extension enabled，預留時間會包含 bounded `dynamic_extension_max_sec`，而 `dynamic_extension_max_sec=0` 會用 `MAX_RECORDING_SEC` 作為無上限延長的保守估算上限
- `VirtualEnvironment` 只清理該 job 擁有的 Xvfb process、audio keepalive process 與自己建立的 sink module
- Pulse/PipeWire sink/source readiness 以 `pactl list ... short` 的 device name 欄位 exact match 判斷；health check 不再要求預先存在共用 `virtual_speaker`，per-job sink 會在錄製啟動時建立
- `YouTubeUploadRunner` 仍以 `_upload_lock` 序列化 YouTube upload；remux/transcode 受 `MAX_PARALLEL_TRANSCODES` 控制，預設一次一個；YouTube 未設定、未授權或 upload 失敗時，錄影成功語意仍保留為 `succeeded`，並用 `error_message` 記錄 upload issue，避免 job 卡在 `uploading`；若 upload 已成功且 YouTube metadata 已寫入 DB，後續 Telegram 通知失敗只記 warning，不改寫 job 結果
- 啟動錄製前會檢查 `MIN_FREE_DISK_GB_BEFORE_RECORDING` 與已保留的 active job 預估容量；不足時 job 以 `DISK_FULL` 失敗，不進入 browser/FFmpeg runtime
- app startup 會把 stale `uploading` job 恢復為 `succeeded` 並記錄 `"YouTube upload interrupted by server restart"`，避免 upload 中斷後永久卡在非終態；stale `finalizing` 若已有 `raw_output_path` 或 `output_path` 指向存在檔案，會恢復為 `succeeded` 並記錄 `"Recording post-processing interrupted by server restart"`，沒有可用錄影檔時才標 failed
- 啟動時會驗證 `MAX_CONCURRENT_RECORDINGS >= 1`、`RECORDING_DISPLAY_POOL_SIZE >= 1`、`MAX_CONCURRENT_RECORDINGS <= RECORDING_DISPLAY_POOL_SIZE` 且 `MAX_PARALLEL_ACTIVITY_ANALYSES >= 1`
- 預設 SQLite 適合低並行；4 路以上或高 API/UI 寫入量部署建議改用外部 DB，例如 Postgres

### 錄製畫面範圍

- `recording_browser_mode` 是 UI/API 可調的全域設定，支援 `app`、`normal`，預設為 `app`。
- app mode 使用 `launch_persistent_context(..., --app=<join_url>)` 開啟實際會議 URL，並 bounded wait persistent context 的第一個 page；不要再用 `--app=about:blank` 後另開普通 page。
- normal mode 保留 `launch()` + `new_context()` + `new_page()` + `goto(join_url)`，主要供 app mode fallback 或除錯。
- `recording_crop_mode` 是 UI/API 可調的全域設定，支援 `auto`、`manual`、`off`，預設為 `off`。
- `recording_crop_top_px` 是 manual 模式的 offset，也是 auto 偵測失敗時的 fallback，必須是 `0 <= value < resolution_h`。
- `auto` 模式會保留額外虛擬桌面高度，錄影前由 `outerHeight - innerHeight + padding` 解析實際 `capture_y`；`manual` 模式直接使用 `recording_crop_top_px`；`off` 模式強制 `capture_y=0`。
- 若 app mode 在 FFmpeg 開始前失敗，worker 會用同一 logical result 進行一次 normal mode attempt；若原 crop mode 是 `off`，fallback 會改用 `auto` 以避免錄到 browser chrome。
- FFmpeg 仍輸出 `resolution_w x resolution_h`，只改變 X11 display 的擷取 offset，不做縮放。
- `runtime.json` 與 failure `metadata.json` 的 URL/meeting code 欄位會移除 query/fragment，避免 Zoom/Webex invite token 或密碼參數落入診斷輸出。
- v1 不做 provider-aware dynamic DOM cropping；provider 自身控制列或 transient overlay 仍由 provider layout/overlay hook 處理。

### 智慧錄影邊界

- `smart_trim_enabled` 啟用後，錄製完成會先保留 raw MKV，再用 `recording/activity.py` 分析原始檔的音訊能量與畫面差異，必要時產生 `*.trimmed.mkv` 作為 preferred local output。
- `output_path` 代表 Web UI / API 優先使用的本地輸出；`raw_output_path` 永遠指向原始錄影；`trimmed_output_path` 保留本次裁剪輸出的路徑，即使自動 YouTube 上傳成功後該檔案已被刪除。
- 起點裁剪以第一個「音訊或影像有活動」的 sample 為基準，保留 `smart_trim_pre_roll_sec`。完成檔案分析使用 streaming batch FFmpeg probes，避免長錄影每個 sample 各自啟動子程序，也避免一次把整段 PCM/raw frames 留在記憶體；若純影像差異出現在兩個 sample 之間，會回推到前一個 sample 作為活動起點。
- Smart trim 會先用 `activity_sample_interval_sec` 做全檔 coarse scan，再只針對第一個與最後一個 active sample 附近用 1 秒 sample 做 boundary refinement；FFmpeg finalize 後會先 cleanup browser、Xvfb 與 per-job audio sink，再由 `JobRunner` 啟動 tracked post-processing task 進入 completed-file activity analysis。後處理 slot 由 `recording.post_processing.ActivityAnalysisLimiter` 控制，受 `MAX_PARALLEL_ACTIVITY_ANALYSES` 限制，不佔 `MAX_CONCURRENT_RECORDINGS` 錄製容量，避免多場同時 finalizing 時 FFmpeg 後處理暴衝；`runtime.json` 的 `trim.diagnostics` 會記錄 probe elapsed time、sample count、refinement status 與 unavailable reason。
- 後處理的 DetectionLog 寫入與完成通知都是 best-effort；它們失敗時只能 rollback/log warning，不得讓已成功產出的 raw recording 回退為 failed 或卡在 `finalizing`。
- 錄影 FFmpeg GOP 以約 1 秒 keyframe interval 輸出，讓 `trim_recording()` 可以維持 stream-copy 裁剪又降低 keyframe 對齊造成的邊界誤差；trim command 使用 duration-based `-ss` / `-t`，stderr 會串流寫入 log 或 bounded excerpt，不用 `communicate()` 保留完整輸出；trim diagnostics 會記錄 expected 與 ffprobe actual output duration。
- 結尾裁剪以最後一個活動 sample 加上 `smart_trim_end_post_roll_sec` 為基準。
- `dynamic_extension_enabled` 啟用後，`RecordingMonitor` 到達 `duration_sec` 後進入 extension phase；只要音訊或影像任一仍 active 就繼續錄，當兩者都 inactive 持續 `dynamic_extension_idle_sec` 或達到 `dynamic_extension_max_sec` 時停止。
- live extension probe 會在接近指定結束時間前預熱音訊 meter 與 video baseline；進入 extension phase 後音訊使用單一長駐 FFmpeg PulseAudio meter，monitor check 只讀取最近峰值快照，不再每次啟動短 FFmpeg probe。音訊 meter 不可用時仍可用 video 判斷；音訊與影像都不可用時，monitor 會在一個 baseline interval 後回退停止，並在 job/detection log 記錄 `activity_probe_unavailable`。
- 自動 YouTube 上傳使用 preferred output；若 preferred output 是裁剪檔，上傳成功後 `scheduling/upload_runner.py` 會刪除本地裁剪檔與其 remux/transcode artifact，並把 DB `output_path` 回退到 raw output。

### 排程行為

- `ScheduleType` 支援 `once` 與 `cron`
- CRON 使用標準五欄位格式，scheduler 內部會把 weekday 轉成 APScheduler 格式
- scheduler 會在啟動時從 DB 載入已啟用排程
- scheduler 也會同步 `next_run_at`，並在特定情境做 catch-up 判斷；`_sync_all_next_run_times()` 應用單一 DB session 批次同步，且跳過 unchanged `next_run_at`。
- 手動 trigger schedule 時，fixed duration 從觸發當下起算；APScheduler 自動觸發才使用 schedule 原始時間窗
- retry window 以 fixed baseline end 加上 bounded `dynamic_extension_max_sec` 計算；`dynamic_extension_max_sec=0` 只代表錄影 monitor 可無上限延長，不會讓 retry window 無限延長。retry attempt 會攜帶 process-local hard deadline，baseline duration 不會把 dynamic extension max 重複加算；若 fixed baseline 已過但仍在 bounded extension window 內，retry 會直接進入 extension/hard-deadline 模式。
- schedule lifecycle 欄位語意如下：
  - `last_triggered_at`：APScheduler、manual trigger 或 catch-up 觸發時間
  - `last_started_at`：`JobRunner` 實際取得錄製 slot 並開始執行該 schedule 的時間
  - `last_completed_at`：該 schedule 對應 job 結束時間，成功、失敗或取消都會更新
  - `last_run_at`：短期相容欄位，現在視為 `last_started_at` 的 legacy alias，不再於 trigger 當下更新
- catch-up 判斷不再把 trigger 當成已執行；若同一 schedule 正在執行或已在 queue，會跳過 catch-up。若最近一筆對應 job 已成功，也會跳過。
- manual trigger 會透過 `JobRunner.queue_schedule()` 回傳 `triggered`、`queued` 或 duplicate。系統 busy 但可排隊時不回 409；同一 schedule 已在執行或 queue 中時才回 duplicate。
- SQLite migration 會一次性把 legacy `duration_mode=auto` schedule 改成 fixed；若有正數 `min_duration_sec` 會用它回填 `duration_sec`，`min_duration_sec=0` 或 `NULL` 會保留原 `duration_sec`，避免 immediate auto schedule 變成 0 秒固定錄影。migration 也會將既有 smart/dynamic per-schedule overrides 清成 `NULL` 以繼承全域預設，並以 `app_settings` marker 保護，不會在之後每次啟動重複覆蓋使用者新設定。

### Detection Logs

Legacy provider-level `duration_mode=auto` / auto-detect-end 已移除；schedule 現在一律以固定基準時長錄製，結束延長由 `dynamic_extension_enabled` 的媒體活動偵測決定。舊的 `recording/detection.py`、`recording/detectors.py` 與 provider detector tests 已刪除，不再是錄影停止條件或 UI/API 可調功能。

Smart trim 與 dynamic extension 的媒體活動事件會寫入 `detection_logs`，並可經由 `/api/detection/logs` 查詢、匯出、標記準確度與清空。`/api/detection/logs` 與 export 支援 `job_id`、`detector_type`、`detected` server-side filters；logs response 會回傳 filtered total 與 summary counts，UI stats 與 CSV export 都以目前 filter 為準。SQLite migration 與 ORM metadata 會建立 `triggered_at`、`job_id + triggered_at`、`detector_type + detected + triggered_at` indexes，避免診斷資料累積後 logs 查詢退化。目前 UI 只顯示 `media_activity` 與 `dynamic_extension` 篩選，歷史未知 detector type 仍會以 legacy/raw label 顯示。
Storage maintenance 會刪除超過 14 天的 detection logs；SQLite 部署刪除後會 best-effort `VACUUM` 回收 DB 檔案空間。

### Storage maintenance

- 本機長期錄影格式是 `.mp4`。錄製仍先輸出 `.mkv` 以提高錄製穩定性；raw capture 成功後，`RecordingPostProcessor` 會 best-effort fast remux preferred output 成 validated `.mp4`，成功後刪除對應 `.mkv` 並把 `recording_jobs.output_path/file_size/runtime_summary_json` 改指 MP4。
- Remux/transcode 都必須先寫入 same-directory temporary MP4，ffprobe 驗證可讀且有合理 video duration 後才 atomic replace 正式 MP4；任一失敗路徑都要刪 temporary file，且不得刪原 MKV。
- Remux、MP4 validation、duration probe 與 thumbnail generation 等一般 media subprocess 應使用 `recording.subprocess_utils.run_bounded_subprocess()`，統一 timeout、terminate/kill cleanup、stdout/stderr excerpt 與 optional stderr log；smart trim 的 streaming trim runner 與 transcode progress runner 保留專用 streaming 實作。
- 若 MP4 canonicalization 失敗，錄影 job 不會因此失敗；DB 保留原 `.mkv`，每日 maintenance 下次會重試 legacy MKV canonicalization。
- YouTube 自動與手動上傳成功都必須寫入 `youtube_video_id` 與 `youtube_uploaded_at`。Legacy MKV 在上傳前會先建立本機 canonical MP4；只有 upload path 會依 `FFMPEG_TRANSCODE_ON_UPLOAD` 產生 temporary upload MP4，完成或失敗後都不應長期留下第二份影片。
- 已上傳 YouTube 且本機錄影已存在 14 天以上時，maintenance 會刪除本機影片與 thumbnail，保留 DB job 與 YouTube link，並寫入 `local_recording_deleted_at` / `local_recording_cleanup_reason`。
- `diagnostics/` 不分 provider 統一保留 14 天；刪除後會清掉 job 的 `diagnostic_dir` 與 diagnostic flags。`runtime_summary_json` 仍保留在 DB。
- Rotated app logs 保留 14 天，當前 `logs/app.log` 與 `.gitkeep` 佔位檔永遠不由 maintenance 刪除。Docker container logs 由 Compose `json-file` rotation 控制，預設 `20m x 5`。
- Web UI `/settings` 的 Storage Management 與 API `POST /api/recordings/maintenance` 是同一個手動 maintenance 入口。舊 `POST /api/recordings/cleanup` 只保留為相容別名，會忽略 `max_age_days` / `max_count` 並改跑 `StorageMaintenanceService`；不要重新啟用 `RecordingManager` 的檔案年齡式刪除，避免 DB 與檔案狀態不同步。
- API `GET /api/recordings/check-disk?auto_cleanup=true` 低空間自動清理也必須呼叫 `StorageMaintenanceService`，不可再走逐檔刪除。
- Web UI `/recordings` 的 `Delete all` 會刪除 recording job 與本機錄影檔，是使用者明確破壞性刪除，不是 retention maintenance。

### Provider 現況

- provider registry metadata 是 provider 名稱、UI 選項、表單 hint、Telegram keyboard 與 API provider validation 的單一來源；provider package 保留 `list_provider_metadata()` / `provider_form_config_map()` 等共享入口，不保留只有單一 UI 呼叫端使用的 metadata map helper。
- 目前正式支援：Jitsi、Webex、Zoom。
- Zoom 建議使用完整邀請連結做 `meeting_code`，包含 URL 內的 `pwd` 參數時可由 provider 保留並帶入加入流程
- provider join flow 應優先使用 Playwright selector/state/function bounded wait；需要固定 sleep 時要是短 fallback 或明確 debounce，避免在熱路徑累積無條件等待。
- Zoom provider state evidence 只能記錄去除 query/fragment 的 URL 與 `url_kind`；不要把 `tk`、`pwd`、`uuid` 等 invite token 寫入 provider state log。
- Zoom 進入會議後可能出現 hardware acceleration 等提示遮住共享畫面；錄製開始前應由 provider hook best-effort 關閉這類 transient overlay，而不是改 Docker Chromium GPU 策略。
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
- `/api/v1/jobs/active`
- `/api/v1/jobs/progress/active`
- `/api/v1/jobs/{job_id}`
- `/api/v1/jobs/{job_id}/progress`
- `/api/v1/jobs/{job_id}/stop`
- `/api/v1/jobs/{job_id}/finish`
- `/api/v1/jobs/{job_id}/diagnostics`

### Meetings / Schedules

- `/api/v1/meetings/*`
- `/api/v1/schedules/*`
- `/api/v1/schedules/{schedule_id}/cancel-queued`

`POST /api/v1/schedules/{id}/trigger` 的 response 語意：

- immediate accepted：HTTP 200，`status: "triggered"`，`queue_position: 0`
- recorder busy but queued：HTTP 202，`status: "queued"`，`queue_position` 為佇列位置
- duplicate running/queued schedule：HTTP 409，`detail: "Schedule is already running or queued"`

`POST /api/v1/jobs/record` 會建立 immediate job；若錄製 slot 已滿，job 保持 `queued` 並由 `JobRunner` 後續啟動，不再因 busy 回 409。`GET /api/v1/jobs/current` 保留舊相容 shape，並在多 active job 時附 `active_count`；它和 `GET /api/v1/jobs/active` 都只使用 `JobRuntimeStateService` snapshot，不讀 worker private `_current_job` fallback。新 UI 應優先用 `GET /api/v1/jobs/active` 取得所有 active jobs、`queued_items[]`、`retry_waiting_items[]`、queue 長度與容量資訊。`GET /api/v1/jobs/active` 的 payload 由 `JobRuntimeStateService` 產生並有 Pydantic response model 固定 shape；`available_slots`、`queue_length` 與 `retry_waiting_count` 的 runner fallback 也集中在 snapshot service；`queue_length` 只代表 FIFO queue，不包含 delayed retry waiting。

`POST /api/v1/jobs/{job_id}/stop` 對 active job 送 per-job cancel；若 job 是仍在 process-local queue 的 immediate job 或 retry waiting job，會從 queue/retry state 移除並把 DB job 更新為 `canceled` / `CANCELED`。`POST /api/v1/jobs/{job_id}/finish` 只接受 active recording，queued/uploading/orphaned non-terminal job 會回 400。job delete 僅允許 `succeeded`、`failed`、`canceled` 終態；Web UI 的 Delete completed 只刪終態 job，保留 queued、active 與 uploading。`POST /api/v1/schedules/{schedule_id}/cancel-queued` 只取消尚未取得錄製 slot 的 queued schedule run，不停用 schedule 本身。

`ScheduleResponse` 會回傳 `last_triggered_at`、`last_started_at`、`last_completed_at` 與 legacy `last_run_at`。新程式應優先使用 lifecycle 欄位，不要再用 `last_run_at` 推論 trigger 是否發生。

### Settings / Detection / Recording Management

- `/api/v1/settings`
- `/api/detection/*`
- `/api/recordings/*`
- `/api/recordings/maintenance`

### Telegram / YouTube

- `/api/v1/telegram/*`
- `/api/v1/youtube/*`

Telegram `/list` 會透過 `JobRuntimeStateService` 顯示 active recording job、FIFO queued count 與 retry waiting count。`/stop` 無參數時使用同一份 snapshot 選出最新 active recording job；多路錄製時應優先使用 `/list` 取得 job id，再用 `/stop <job_id>` 精準停止或取消 queued/retry waiting job。Telegram stop 也必須走 `JobActionService`，不要直接呼叫 worker cancel flag。Telegram create schedule wizard 的 queue warning 應看 snapshot `available_slots`，不要恢復 busy flag 判斷容量是否已滿。

Telegram recording stage、completion、failure、retry 與 upload notifications 都是 best-effort；send/edit/fallback-send 會以最多 3 chat 的 bounded concurrent fanout 執行，且每個 Telegram API 呼叫都有 10 秒 timeout。Timeout 或 API exception 只記錄 log，不能阻斷錄製、後處理或上傳主流程；Telegram 回報 `Message is not modified` 時視為 edit no-op success，不可 fallback-send 造成重複訊息。現行 DB 仍只保存單一 `telegram_message_id`，多 chat per-message tracking 留到未來 schema 版本。

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
- `diagnostics/<job_id>/runtime.json`
- `diagnostics/<job_id>/screenshot.png`
- `diagnostics/<job_id>/page.html`
- `diagnostics/<job_id>/console.log`
- `diagnostics/<job_id>/ffmpeg.log`
- `diagnostics/<job_id>/remux.log`
- `diagnostics/<job_id>/transcode.log`

`runtime.json` 會保留 browser mode、crop/capture frame 與 browser surface dimensions；`runtime.json` 與 `metadata.json` 的 URL 欄位只保留不含 query/fragment 的 redacted form。
Diagnostics 目錄會由每日 storage maintenance 保留 14 天；需要長期保存時應在期限內匯出或備份。

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
uv run --all-extras pytest tests/ -v
uv run --all-extras pytest tests/ --cov=api --cov=providers --cov=database --cov=recording --cov-report=term-missing
uv run --all-extras ruff check .
uv run --all-extras ruff format --check .
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
