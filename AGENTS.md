# AGENTS.md

本檔是此 repo 的 agent 行為規格來源。  
未來協助開發的 agent 應優先讀這一份；改善追蹤請看根目錄 `Plan.md`、`Task.md`、`Lesson.md`。

## 專案定位

`MeetingRecorder` 是一個自動化線上會議錄製系統：

- 以 FastAPI 提供 Web UI 與 API
- 以 Playwright 自動加入會議
- 在 Linux 錄製環境中以 Xvfb、PipeWire、FFmpeg 進行錄影
- 支援排程、Telegram 通知、YouTube 上傳與診斷資料收集

README 面向使用者與部署者；`docs/development.md` 面向人類開發者；本檔只放 agent 必須遵守的規則、同步責任與容易誤判的專案事實。

## 文件規則

- `README.md`：人類使用者的產品/部署入口
- `docs/development.md`：人類開發者的技術與維護指南
- `AGENTS.md`：AI agent 行為規則來源
- `Plan.md`：架構改善方向與優先級
- `Task.md`：可執行改善任務清單，完成後必須標記 `[x]`
- `Lesson.md`：開發過程的踩坑、誤判與避免方式

規則：

1. 不要新增新的 agent 指南檔案；`Plan.md`、`Task.md`、`Lesson.md` 是官方改善追蹤文件，不是 agent 指南。
2. 修改功能時，必須判斷是否同步更新 README 與 `docs/development.md`。
3. 若改動影響使用者可見能力、部署方式、支援平台或支援 provider，README 必須更新。
4. 若改動影響架構、流程、設定來源、測試方式或 API 範圍，`docs/development.md` 必須更新。
5. 若改動完成或改變改善方向，必須同步更新 `Plan.md`、`Task.md`、`Lesson.md` 中對應內容。
6. 不要新增其他平行追蹤文件；若需要記錄計畫、任務或教訓，更新既有三份文件。

## 專案地圖

| 路徑 | 角色 |
| --- | --- |
| `api/` | FastAPI app、REST routes、HTML routes |
| `config/` | `.env` 設定與 logging |
| `database/` | SQLAlchemy models、DB session |
| `providers/` | 會議 provider 實作 |
| `recording/` | 錄製 worker、虛擬環境、FFmpeg、偵測器 |
| `scheduling/` | APScheduler 與受控並行 job runner |
| `services/` | app settings、通知、錄影管理 |
| `telegram_bot/` | Telegram bot 流程與 conversation handlers |
| `uploading/` | YouTube 上傳與進度 |
| `web/` | Jinja templates / static assets |
| `tests/` | pytest 測試 |

## 操作資訊來源

- 使用者部署、首次設定、日常操作與資料保存位置：看 `README.md`。
- 人類開發流程、API 範圍、設定來源、診斷與測試命令：看 `docs/development.md`。
- 改善目標、待辦任務與經驗教訓：看 `Plan.md`、`Task.md`、`Lesson.md`。
- 本檔只保留 agent 在修改時必須遵守的判斷規則與專案事實。

## 重要架構事實

### 受控並行錄製

- `recording.worker.get_worker()` 是 singleton
- `recording.job_types.py` 是 `RecordingJob` / `RecordingResult` DTO owner；`recording.worker` 不 re-export DTO，新內部模組不要為了 DTO import worker implementation
- `scheduling.job_runner.JobRunner` 以 `MAX_CONCURRENT_RECORDINGS` 控制同時錄製數；不要恢復成單一長時間 global lock
- `scheduling.schedule_queue.ScheduleRunQueue` 負責 unified FIFO run queue，統一管理 schedule/immediate queued item、pending、active schedule set、duplicate、queued cancellation 與 queue position 狀態；不要把第二條 direct queue 或 queue 容器狀態重新塞回 `JobRunner`
- `scheduling.recording_executor.RecordingExecutor` 負責單次 recording attempt DB 更新、status callback 與 stage notification；retry wait 必須由 `JobRunner` delayed requeue 管理，不可在 active recording task 內 sleep 佔用 slot。stage notification 是 best-effort async task，送出前必須重讀 DB status 並 skip stale status，避免舊 `recording` / `finalizing` 訊息覆蓋已 `succeeded` / `failed` / `canceled` 的 job
- retry attempt 必須攜帶 process-local hard deadline，baseline duration 不可把 `dynamic_extension_max_sec` 重複加算；若 fixed baseline 已過但仍在 bounded extension window，retry 應直接進 extension/hard-deadline 模式
- delayed retry waiting 不屬於 FIFO queue position，`queue_length` 不應包含它；但它必須透過 `retry_waiting_items[]` / Web UI / Telegram 可觀測，並可用 job stop 語意取消
- 錄製 capacity slot 只涵蓋實際 capture runtime；`RecordingWorker` 在 FFmpeg `finalize_capture()` 後要先 cleanup browser、Xvfb 與 per-job audio lease，smart trim 與本機 MP4 canonicalization 由 tracked post-processing task 處理，不可放回 active recording task 內佔用 `MAX_CONCURRENT_RECORDINGS`
- `recording.post_processing.RecordingPostProcessor` 負責成功 raw capture 後的 completed-file smart trim、本機 MP4 canonicalization、trim metadata/runtime summary 更新、完成通知與 upload request 建立；raw recording 成功優先於 smart trim 成敗，後處理例外應保留 raw recording 並讓 job 收斂為 `succeeded`
- post-processing `process` task 失敗或取消時最多只能排一次 raw-success `settle` task；`settle` task 失敗只記 log，不可遞迴重排，也不可改成無追蹤的 fire-and-forget
- DetectionLog 寫入是 diagnostics best-effort；寫入失敗只能 rollback + warning，不得阻斷 raw recording terminal success
- `scheduling.upload_runner.YouTubeUploadRunner` 負責錄影完成後的 remux/transcode progress 與 YouTube upload；不要把 upload 細節重新塞回 `JobRunner`
- `scheduling.job_runner.JobRunner` 負責 delayed retry、active recording、tracked post-processing 與 tracked upload task shutdown；新增後處理或 upload path 時不要回到 fire-and-forget 且無 interrupted cleanup 的 task
- `recording.capacity_guard.RecordingCapacityGuard` 負責 process-local disk reservation，估算必須包含已啟用的 bounded `dynamic_extension_max_sec`；若 `dynamic_extension_max_sec=0`，用 `MAX_RECORDING_SEC` 作為無上限延長的保守估算上限；不要只用單 job free-space check 判斷多路長錄製容量
- `recording.runtime_resources.RuntimeResourceAllocator` 負責每個 active job 的 Xvfb display 與 PipeWire/Pulse sink lease；不要在 provider 或 runner 內硬編 `:99` 或共用 `virtual_speaker` 作為並行錄製資源
- `recording.pactl.short_names()` 是 `pactl list ... short` device name parsing owner；runtime checks、virtual audio setup 與 FFmpeg audio source checks 不要各自複製 parser
- `RecordingWorker` 維護 active job registry，對外只用 `active_jobs` / `active_count` 表示 runtime active state；不要恢復舊 `is_busy` / `current_status` 全域狀態。cancel/finish 是 per-job 狀態，API/Web UI 應以 job_id 指定操作對象。`_current_job` 只保留作為 worker 內部/相容欄位，route、template、Telegram handler 不得用它推論 active recording 或容量
- `services.job_actions.JobActionService` 是 job stop/finish/delete/cancel queued 的單一狀態決策層；queued cancel 必須使用 `JobRunner.cancel_queued_job_for_action()` 的 structured result 判斷 FIFO / retry waiting 來源，不要恢復 boolean `cancel_queued_job()` 或 route/service 自行推論 retry state。`ACTIVE_RECORDING_STATUSES` 與 `TERMINAL_JOB_STATUSES` 也由同一模組提供，REST route、Web UI route 與 template 不要各自維護不同的 job status 操作表
- `services.job_runtime_state.JobRuntimeStateService` 是 API / Web UI / Telegram active、FIFO queued、retry waiting view 的單一組裝層；runner capacity/count fallback 與 invalid value normalization 也必須集中在這裡處理，不要在 route、template 或 Telegram handler 重新拼 active job ids、queue maps、capacity fallback 或 retry countdown maps
- `services/__init__.py` 不 re-export 具體 service；新程式應直接 import service owner module，避免 package import eager-load unrelated service modules
- legacy schedule `duration_mode` 只保留為 DB string column 與 migration target；不要恢復 `DurationMode` enum 或 provider-level auto-detect-end API
- provider registry metadata 是 provider 名稱與選項的單一來源；不要在 ORM model 恢復 `ProviderType` enum 或其他第二份 provider 清單
- queued immediate job 可由 job stop endpoint 取消；queued schedule run 要用 schedule cancel-queued 語意，不要把 queued schedule 當成已有 job row 的 recording job
- delete job 只允許 `succeeded`、`failed`、`canceled` 終態；`uploading` 代表錄影已成功但正在處理/上傳，不可 stop/finish/delete，upload issue 應回到 `succeeded` 並記錄在既有 job 欄位
- app restart 時 stale `finalizing` 若已有 `raw_output_path` 或 `output_path` 指向存在檔案，應恢復為 `succeeded` 並記錄 post-processing interrupted；只有沒有可用錄影檔的 stale finalizing/running job 才標 failed
- Telegram `/list` 與無參數 `/stop` 必須以 worker active registry 與 DB active status 交集為準，避免 stale DB active row 誤導；Telegram `/stop` 必須走 `JobActionService`，多路錄製下無參數只停止最新 active recording，指定 `/stop <job_id>` 時只作用於該 job
- Telegram notification API 呼叫不可無界等待；send/edit/fallback-send 必須走 bounded timeout helper，fanout 必須 bounded concurrent，timeout 只能記 log，不得阻斷 recording/post-processing/upload 主流程
- Telegram 建立排程 wizard 的「現在會排隊」提示必須看 `JobRuntimeStateService` snapshot 的 `available_slots`，不可恢復 busy flag 判斷，因為多路錄製下有 active job 不代表容量已滿
- 現況預設支援 2 個同時錄製工作，可由 `.env` 的 `MAX_CONCURRENT_RECORDINGS` 調整
- `MAX_CONCURRENT_RECORDINGS` 必須小於等於 `RECORDING_DISPLAY_POOL_SIZE`，設定錯誤會在 settings validation 階段 fail fast
- 若新 schedule 或 immediate job 進來時錄製 slot 已滿，會進 queue 等待

### Web UI 模組邊界

- `api/routes/ui.py` 仍是 Web UI route 聚合點，但不應繼續承擔新 helper 或新 route 群組邏輯
- template/context/settings 共用能力已由 `api/routes/ui_common.py` 負責
- job failure log 解析與 excerpt 載入已由 `api/routes/ui_job_diagnostics.py` 負責
- auth/dashboard/meeting/schedule/settings routes 已由 `api/routes/ui_auth.py`、`api/routes/ui_dashboard.py`、`api/routes/ui_meetings.py`、`api/routes/ui_schedules.py`、`api/routes/ui_settings.py` 負責
- jobs routes 已由 `api/routes/ui_jobs.py` 負責；recordings routes 已由 `api/routes/ui_recordings.py` 負責
- 對外仍由 `api.routes.ui.router` 聚合，`api/main.py` 不需要逐一 include UI 子 router
- UI 子 router 不得 import `api.routes.ui`，避免子模組反向依賴聚合模組
- `api.routes.ui` 不再 re-export helper；需要 template/context/settings 時 import `ui_common`，需要 job log helper 時 import `ui_job_diagnostics`
- jobs / recordings UI 需要標記 trimmed upload artifact 是否已移除、或 recordings UI 需要判斷本機下載是否可用 / preferred existing output 時，使用 `api.routes.ui_recording_artifacts`，不要在子 router 內複製 artifact display/download helper

### Telegram 模組邊界

- create schedule、edit schedule、create meeting conversation 分別由 `telegram_bot/conversation_create_schedule.py`、`telegram_bot/conversation_edit_schedule.py`、`telegram_bot/conversation_create_meeting.py` 負責；不要重新新增 `telegram_bot/conversations.py` 這類 re-export 聚合器
- 共用 cancel handler、時間與時長解析 helper 由 `telegram_bot/conversation_common.py` 負責
- Telegram handler/conversation 需要直接 DB session 時，使用 `telegram_bot/session.py` 的 `get_db_session()`；`telegram_bot/__init__.py` 不應 re-export DB helper 或 eager import database layer
- Conversation domain module 不應互相 import；若需要共用能力，先放到 `conversation_common.py`

### 儲存與保留策略

- 本機長期錄影格式是 `.mp4`；錄製仍先輸出 `.mkv`，成功 fast remux 並驗證 `.mp4` 後刪除 `.mkv` 並更新 `recording_jobs.output_path/file_size/runtime_summary_json`。
- MKV/MP4 sibling artifact 判斷使用 `recording.remux.recording_file_variants()`；trimmed/upload artifact best-effort 刪除使用 `recording.remux.delete_recording_artifacts()`。不要在 UI route、maintenance、YouTube route 或 upload runner 內各自複製這些檔案規則
- 本機 MP4 canonicalization 不等於 YouTube upload compression；本機 canonical 固定用 fast remux，不讀 `FFMPEG_TRANSCODE_ON_UPLOAD`。只有 YouTube upload helper 可依該設定產生 temporary transcode upload file，且不可覆寫本機 canonical MP4。
- MP4 canonicalization 失敗不可讓成功錄影改成 failed；保留 `.mkv` 並讓每日 maintenance 或後續上傳流程重試。remux/transcode 必須先寫 temporary MP4，驗證成功後才 replace 正式 MP4；不得因 partial/corrupt MP4 刪除 MKV。
- `services.storage_maintenance.StorageMaintenanceService` 是本機錄影、diagnostics、rotated app logs、detection logs 與 SQLite `VACUUM` 清理邏輯 owner；不要把清理細節分散塞回 API route、Web UI 或 scheduler。
- Web UI `/settings` Storage Management、`POST /api/recordings/maintenance` 與舊相容 `POST /api/recordings/cleanup` 都必須走 `StorageMaintenanceService`；舊 cleanup endpoint 不得恢復成 `RecordingManager` 依檔案修改時間或數量直接刪錄影，避免 DB/local recording state 不一致。
- `GET /api/recordings/check-disk?auto_cleanup=true` 若觸發低空間清理，也必須走 `StorageMaintenanceService`，不可直接逐檔刪 `recordings/`。
- Scheduler 會用 internal job id `storage_maintenance_daily` 每日 03:30 local time 執行 maintenance；它不是使用者 schedule，不應參與 schedule lifecycle 或 `next_run_at` 同步。
- 已上傳 YouTube 的本機錄影檔保存 14 天後可刪除；DB job、YouTube video id 與歷史狀態仍保留，並以 `local_recording_deleted_at` / `local_recording_cleanup_reason` 標記。
- `diagnostics/` 不分 provider 統一保留 14 天；刪除 diagnostics 後要同步清掉 DB diagnostic path/flags，但保留 `runtime_summary_json`。
- `logs/app.log` 由 Python rotating handler 控制，maintenance 只清 rotated log，不刪目前的 `app.log` 或 `.gitkeep` 佔位檔；Docker container log rotation 由 Compose `json-file` `20m x 5` 控制。

### 設定來源不是單一層

不要把所有設定都描述成「只在 `.env`」或「都已移到 DB」。

目前真實狀態是：

- `.env` / `config.settings`：資料庫、認證、Telegram、YouTube、並行錄製上限、display pool、最低磁碟空間、FFmpeg 進階參數、路徑
- `services.app_settings` + `app_settings` table：部分 UI 可調整設定，包括錄製解析度、lobby 等待、`recording_browser_mode`、`recording_crop_mode`、`recording_crop_top_px`、smart trim 與 dynamic extension/activity thresholds
- `notification_config`：JSON 形式存於 `app_settings`

Settings API / Web UI 應以 `get_all_settings()` 讀取完整 overlay，並以 `update_settings()` batch upsert 已知 key；不要恢復未接線的單 key getter/setter。
只在 owner module 內使用的 helper 應維持私有，例如 upload MP4 path derivation、secret mask detection、settings defaults builder、notification channel implementation；不要為方便測試或單一呼叫端擴大 public surface。

### 錄製畫面與裁切

- `RecordingSession` 預設以 Chromium app window (`--app=<join_url>`) 準備錄製畫面；乾淨畫面主要依賴 app window，而不是 kiosk/fullscreen launch flags 或 auto top-crop。
- app mode 必須使用 `launch_persistent_context(..., --app=<join_url>)` 產生的 persistent context 初始 page；要 bounded wait page 產生，不要恢復 `--app=about:blank` 後再 `new_page()` 的流程。
- app mode 不應主動 request DOM fullscreen；fullscreen best-effort 只保留給 normal/fallback mode，避免 app window 主方案引入額外 provider UI side effect。
- `recording_browser_mode` 是全域 UI/API 設定，合法值為 `app`、`normal`；它不是 schedule 欄位，既有 schedule 下次執行也會使用當下解析出的全域值。
- `recording_crop_mode` 是全域 UI/API 設定，合法值為 `auto`、`manual`、`off`，預設 `off`；它不是 schedule 欄位，既有 schedule 下次執行也會使用當下解析出的全域值。
- `recording_crop_top_px` 是 manual offset 與 auto fallback，合法範圍為 `0 <= value < resolution_h`；它不是 schedule 欄位。
- 若 app mode 在 FFmpeg capture 前失敗，worker 會對同一 job fallback 一次到 normal mode；若原 crop mode 是 `off`，fallback 的有效 crop mode 會改成 `auto`。
- `auto` 模式會使用額外 Xvfb 高度並在 capture 前以 browser `outerHeight - innerHeight` 解析實際 offset；`manual` 模式直接使用 `recording_crop_top_px`；`off` 模式從 X11 `y=0` 擷取。
- FFmpeg 輸出仍是 `resolution_w x resolution_h`，top crop 只改變 X11 capture offset。
- `runtime.json` 與 failure `metadata.json` 的 URL/meeting code 必須 redacted query/fragment；Zoom/Webex invite token、`pwd` 或 meeting secret 不應寫入 diagnostics。
- v1 不做 provider-aware dynamic content crop；不要把 Jitsi/Webex/Zoom DOM selector 塞進 recording runtime 來計算裁切區域。
- provider 自身 overlay 或控制列仍由 provider layout/overlay hook 處理，不要把它和瀏覽器 chrome 裁切混成同一層責任。

### 智慧錄影邊界

- `recording/activity.py` 負責媒體活動分析、live extension probe 與 post-recording trim helper；不要把 provider DOM selector 放進這一層。
- `smart_trim_enabled` 與 `dynamic_extension_enabled` 有全域預設，也可由 schedule nullable 欄位覆寫；`None` 代表繼承全域設定。schedule create/update 必須驗證有效組合，特別是 `dynamic_extension_max_sec == 0 or dynamic_extension_max_sec >= dynamic_extension_idle_sec`。
- `RecordingMonitor` 到達 `duration_sec` 後才進入 dynamic extension phase；音訊或影像任一 active 就繼續錄，兩者都 inactive 達 `dynamic_extension_idle_sec` 或達 `dynamic_extension_max_sec` 才停止。
- 原始錄影檔必須保留；`raw_output_path` 指向原始檔，`output_path` 是 Web UI/API preferred local output，`trimmed_output_path` 是裁剪檔 metadata。
- 完成檔案的 smart trim analysis 應使用 batch media probes；不要回到每個 sample 各自啟動 FFmpeg 子程序的做法。
- 完成檔案的 smart trim analysis 與 trim subprocess 受 `recording.post_processing.ActivityAnalysisLimiter` / `MAX_PARALLEL_ACTIVITY_ANALYSES` 節流；live dynamic extension probe 不應被這個後處理 semaphore 阻塞，`recording.activity` 也不應直接讀 app settings。等待 limiter 時不得持有 browser/Xvfb/audio runtime，也不得佔用 recording capacity slot。
- smart trim 實際裁剪維持 stream-copy；錄影 GOP 應保持約 1 秒 keyframe interval，並在 trim diagnostics 記錄 expected/actual output duration。
- `trim_recording()` 不應共用會完整 `communicate()` stdout/stderr 的 generic probe runner；trim stderr 應串流寫入 log 或 bounded excerpt，避免長錄影後處理放大記憶體。
- remux、MP4 validation、duration probe、thumbnail 等一般 FFmpeg/ffprobe helper 應使用 `recording.subprocess_utils.run_bounded_subprocess()` 或等價 bounded runner；不要新增裸 `communicate()` 且無 timeout 的 media subprocess path。
- Detection Logs 是 activity/extension diagnostics；查詢、summary、CSV export 應套用同一組 filter，SQLite 應保留 `triggered_at`、`job_id + triggered_at`、`detector_type + detected + triggered_at` indexes。
- 自動 YouTube 上傳使用 preferred output；若裁剪檔上傳成功，會刪除本地裁剪檔與其 MP4 artifact，並將 DB `output_path` 回退到 raw output。
- Legacy provider-level end detection 已移除；不要重新加入 WebRTC/Text/Video/URL/ScreenFreeze/AudioSilence detector 作為錄影停止條件，也不要把 screen top crop、provider overlay、provider end state 與 smart trim 混成同一責任。

### Provider 支援現況

- 對外文件與 provider registry 正式支援：Jitsi、Webex、Zoom
- `providers/__init__.py` 的 provider registration + metadata 是 UI 選項、API provider validation 與 Telegram provider keyboard 的單一來源
- Zoom 建議使用完整邀請連結作為 meeting code；URL 內的 `pwd` 參數會被保留，並由 Zoom provider 加上 `zc=0` 走瀏覽器加入流程
- Provider join/prejoin 等待應使用 `providers/base.py` 的 bounded wait helper 或集中短 debounce，不要在 provider domain 檔新增裸 `asyncio.sleep()`
- 若新增或移除 provider，必須先更新 provider registry metadata，再同步處理 `README.md`、`docs/development.md`、`AGENTS.md`、測試與對外 API 描述

### 錄製環境限制

- 錄製核心依賴 Linux
- Windows / macOS 應透過 Docker 使用
- `/health` 的 recording runtime 檢查只要求音訊 server 可用；per-job sink 會在錄製啟動時建立，不要再把共用 `virtual_speaker` 當成並行錄製 ready 的必要條件
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

### 改動以下內容時，要更新 `Plan.md`、`Task.md` 或 `Lesson.md`

- 改善目標、優先級或範圍改變時，更新 `Plan.md`
- 任務完成、拆分、取消或新增時，更新 `Task.md`
- 過程中踩坑、誤判或發現可避免的耗時模式時，更新 `Lesson.md`

## 常用檢查命令

優先使用這些命令建立事實基礎再動手：

```bash
git status --short
Get-ChildItem -Recurse -File
Select-String -Path api/routes/*.py -Pattern '@router'
uv run --all-extras pytest tests/ -v
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
2. 正式規格文件不要混入臨時 TODO、roadmap 或歷史變更摘要；改善追蹤應放在 `Plan.md`、`Task.md`、`Lesson.md`。
3. 不要再建立平行 agent 文件或其他平行改善追蹤文件。
