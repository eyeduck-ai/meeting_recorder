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
| `scheduling/` | APScheduler 與單工 job runner |
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

### 單工錄製

- `recording.worker.get_worker()` 是 singleton
- `scheduling.job_runner.JobRunner` 以單一 lock 控制執行
- `scheduling.schedule_queue.ScheduleRunQueue` 負責 schedule queue、pending、duplicate 與 queue position 狀態；不要把這些容器狀態重新塞回 `JobRunner`
- `scheduling.recording_executor.RecordingExecutor` 負責 recording retry、attempt DB 更新、status callback 與 stage notification；不要把 retry/status flow 重新塞回 `JobRunner`
- `scheduling.upload_runner.YouTubeUploadRunner` 負責錄影完成後的 remux/transcode progress 與 YouTube upload；不要把 upload 細節重新塞回 `JobRunner`
- 現況一次只支援一個錄製工作
- 若新 schedule 進來時已有工作進行中，會進 queue 等待

### Web UI 模組邊界

- `api/routes/ui.py` 仍是 Web UI route 聚合點，但不應繼續承擔新 helper 或新 route 群組邏輯
- template/context/settings 共用能力已由 `api/routes/ui_common.py` 負責
- job failure log 解析與 excerpt 載入已由 `api/routes/ui_job_diagnostics.py` 負責
- auth/dashboard/meeting/schedule/settings routes 已由 `api/routes/ui_auth.py`、`api/routes/ui_dashboard.py`、`api/routes/ui_meetings.py`、`api/routes/ui_schedules.py`、`api/routes/ui_settings.py` 負責
- jobs routes 已由 `api/routes/ui_jobs.py` 負責；recordings routes 已由 `api/routes/ui_recordings.py` 負責
- 對外仍由 `api.routes.ui.router` 聚合，`api/main.py` 不需要逐一 include UI 子 router
- UI 子 router 不得 import `api.routes.ui`，避免子模組反向依賴聚合模組
- 若因相容性需要從 `api.routes.ui` 匯出 helper，實作仍應留在獨立 helper 模組

### Telegram 模組邊界

- `telegram_bot/conversations.py` 是相容 re-export 聚合器，不應再放新的 conversation handler 實作
- create schedule、edit schedule、create meeting conversation 分別由 `telegram_bot/conversation_create_schedule.py`、`telegram_bot/conversation_edit_schedule.py`、`telegram_bot/conversation_create_meeting.py` 負責
- 共用 cancel handler、時間與時長解析 helper 由 `telegram_bot/conversation_common.py` 負責
- Conversation domain module 不應互相 import；若需要共用能力，先放到 `conversation_common.py`

### 設定來源不是單一層

不要把所有設定都描述成「只在 `.env`」或「都已移到 DB」。

目前真實狀態是：

- `.env` / `config.settings`：資料庫、認證、Telegram、YouTube、FFmpeg 進階參數、路徑
- `services.app_settings` + `app_settings` table：部分 UI 可調整設定，包括錄製解析度、lobby 等待、`recording_browser_mode`、`recording_crop_mode`、`recording_crop_top_px`、smart trim 與 dynamic extension/activity thresholds
- `detection_config`、`notification_config`：JSON 形式存於 `app_settings`

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
- 自動 YouTube 上傳使用 preferred output；若裁剪檔上傳成功，會刪除本地裁剪檔與其 MP4 artifact，並將 DB `output_path` 回退到 raw output。
- 媒體活動偵測與 provider-level end detection 是不同層；不要把 screen top crop、provider overlay、provider end state 與 smart trim 混成同一責任。

### Provider 支援現況

- 對外文件與 provider registry 正式支援：Jitsi、Webex、Zoom
- `providers/__init__.py` 的 provider registration + metadata 是 UI 選項、API provider validation 與 Telegram provider keyboard 的單一來源
- Zoom 建議使用完整邀請連結作為 meeting code；URL 內的 `pwd` 參數會被保留，並由 Zoom provider 加上 `zc=0` 走瀏覽器加入流程
- Provider join/prejoin 等待應使用 `providers/base.py` 的 bounded wait helper 或集中短 debounce，不要在 provider domain 檔新增裸 `asyncio.sleep()`
- 若新增或移除 provider，必須先更新 provider registry metadata，再同步處理 `README.md`、`docs/development.md`、`AGENTS.md`、測試與對外 API 描述

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
2. 正式規格文件不要混入臨時 TODO、roadmap 或歷史變更摘要；改善追蹤應放在 `Plan.md`、`Task.md`、`Lesson.md`。
3. 不要再建立平行 agent 文件或其他平行改善追蹤文件。
