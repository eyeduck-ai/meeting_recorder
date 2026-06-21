# 改善計畫

本檔記錄後續架構改善方向與優先級。任務細項請同步維護在 `Task.md`；過程中的踩坑與誤判請記錄到 `Lesson.md`。

## P0：安全邊界

- 修正 `/api` public path 造成 API 認證繞過的問題。
- 收斂 wildcard CORS 與 API 認證邊界，避免受保護 API 被跨來源濫用。

## P1：設定、狀態與儲存一致性

- 建立 `RuntimeConfigService`，統一 `.env`、`config.settings`、`app_settings` table、schedule override 的 precedence。
- 讓 schedule/job 狀態語意更精準，拆出 `triggered_at`、`started_at`、`completed_at` 等欄位，讓 missed/catch-up 判斷更可靠。
- 修正 schedule 解析度、lobby wait 等可設定但未完整套用到 worker/session 的問題。
- 修正 recording manager 掃描錄影檔目錄與 `recordings_dir` 設定不一致的問題。
- 已新增 storage maintenance，統一本機 MP4 canonicalization、YouTube 後錄影保留、diagnostics/log/detection log retention 與 Docker log rotation；本機 canonicalization 已 harden 為 temporary MP4 + ffprobe validation + fast remux-only。
- 已收斂手動 storage cleanup 入口：Web UI Storage Management、`/api/recordings/maintenance`、舊 `/api/recordings/cleanup` 相容別名與低空間 auto cleanup 都走 `StorageMaintenanceService`，避免直接刪檔造成 DB/local state drift。
- 已新增 smart trim 與 dynamic extension 設定解析，支援全域預設與 schedule nullable 覆寫。
- smart/dynamic schedule 覆寫已在 create/update 當下驗證有效組合，避免 invalid config 延後到 job runner 才失敗。
- 已修正 storage retention canonical MP4 與 smart trim preferred/raw/trimmed metadata 的合併後 path propagation，避免 YouTube 上傳或 DB 狀態指向已刪除 MKV。

## P1：服務層與入口一致性

- 新增 service 層，例如 `MeetingService`、`ScheduleService`、`JobService`，集中建立、更新、觸發與同步 scheduler/worker 的流程。
- 減少 API、Web UI、Telegram 各自直接 `db.commit()`、呼叫 `get_scheduler()`、`get_worker()` 造成的行為分歧。

## P2：生命週期與資料庫演進

- 將 global singleton 改由 FastAPI lifespan 與 `app.state` 管理，讓測試、重啟與 app lifecycle 更乾淨；runtime state 清理由 lifespan shutdown 直接負責，不保留單次使用的 private wrapper。
- 將 ad hoc DB migration 從 `database/models.py` 拆離；中期導入 Alembic，避免 schema 演進失控。
- 調整手動 trigger 的 queue/409 語意，讓 API 與 Web UI 行為一致。
- 改善 secret/token at rest 的命名、文件與保護方式；已先完成 Rename + Redact，若未來需要真正加密再另行規劃。

## P3：可維護性

- 統一 provider registry、API 型別與 UI/Telegram 選項來源，並移除只剩 model default 用途的 `ProviderType` enum，減少新增 provider 時的同步漏改。
- 已先將 Web UI job diagnostics helper 抽到 `api/routes/ui_job_diagnostics.py`，並將 YouTube upload/remux 流程抽到 `scheduling/upload_runner.py`。
- 已將 Web UI jobs/recordings routes 拆到 `api/routes/ui_jobs.py` 與 `api/routes/ui_recordings.py`，`api.routes.ui.router` 保留為聚合入口。
- 已將 Web UI 共用 template/context/settings 抽到 `api/routes/ui_common.py`，避免 UI 子 router 反向依賴聚合 router。
- 已將 schedule queue、pending、duplicate 與 queue position 狀態拆到 `scheduling/schedule_queue.py`。
- 已將 recording retry、attempt DB 更新、status callback 與 stage notification 拆到 `scheduling/recording_executor.py`。
- 已將 recording monitor loop 抽到 `recording/monitor.py`，集中 duration、finish/cancel、FFmpeg stall 與 auto-detect 判斷。
- 已新增 `recording/activity.py`，集中媒體活動 probe、完成檔 batch activity analysis 與 smart trim helper，避免 provider DOM selector 進入媒體邊界判斷。
- 已移除 legacy provider-level `duration_mode=auto` / auto-detect-end 的 UI/API 設定與 recording stop path，統一改用 smart trim + dynamic extension 作為動態起訖機制。
- 已清除 legacy provider end detector 死碼與測試，移除只剩 default 用途的 `DurationMode` enum，並補上 fixed baseline duration update 驗證，避免舊 auto-detect 語意殘留。
- 已重整 Detection Logs 為 activity/extension diagnostics，並以 server-side filters 與約 1 秒 GOP 改善 smart trim 輸出邊界精度。
- 已將 `api/routes/ui.py` 的 auth/dashboard/meeting/schedule/settings route 群組拆到獨立子 router；`api.routes.ui.router` 保留為聚合入口。
- 已將 jobs / recordings UI 共用的 recording artifact display/download helper 收斂到 `api.routes.ui_recording_artifacts`，避免子 router 複製 trimmed removed、local download availability 與 preferred existing output 判斷。
- 已將 Telegram create schedule、edit schedule、create meeting conversation 拆到獨立模組，並移除舊 `telegram_bot.conversations` re-export 聚合器。
- 已移除未引用的 app settings、Telegram notification/bot/keyboard 與 MP4 remux compatibility helper，並將只在 owner module 內使用的 secret sentinel、settings defaults、notification channel helper 私有化；`pactl list ... short` device name parsing 已收斂到 `recording.pactl.short_names()`，MKV/MP4 sibling variant 與 trimmed/upload artifact best-effort deletion 已收斂到 `recording.remux`，delete helper 會自行展開 sibling variants，fresh MP4 validation 留在 canonicalization flow 內，upload transcode temporary path 留在 upload preparation flow 內，不在 retention/UI/remux service 保留二次規則或薄 wrapper，避免舊 public-looking helper 或重複 parser 誤導後續開發。
- 已移除 `services/__init__.py` service re-export 聚合層，service package import 不再 eager-load 多個具體 service owner module。
- 已移除 `recording.worker` 的 DTO re-export，讓 `RecordingJob` / `RecordingResult` 只由 `recording.job_types` 提供。
- 已將 Zoom join/prejoin 改為 provider 專用狀態推進，處理 cookie banner、Join from browser、name/password form、lobby 與 in-meeting 分支，避免假設固定頁面順序。
- 已新增 provider transient overlay dismissal，讓 Zoom 進入會議後能在錄製前清除 hardware acceleration 等遮擋提示。
- 已新增 Chromium app window 主錄製路徑、`recording_browser_mode` 與 `recording_crop_mode` fallback；app mode 失敗於 capture 前會以同一 logical job/result 自動 fallback 一次到 normal browser + crop，runtime diagnostics 會 redacted URL query/fragment。

## P3：運作效率

- 已將錄製執行模型改成受控並行：`JobRunner` 依 `MAX_CONCURRENT_RECORDINGS` 啟動多個 recording task，超過容量的 schedule/immediate job 進 queue，並以 per-job Xvfb display 與 PipeWire/Pulse sink lease 隔離錄影與音訊。
- 已將 schedule/immediate queue 收斂為 `ScheduleRunQueue` 的 unified FIFO，補 queued immediate/schedule cancellation、queue position API/UI 與 runtime cleanup/audio exact-match hardening。
- 已將 job lifecycle 操作收斂到 `JobActionService`，讓 REST/Web UI 共用 queued cancel、active stop/finish、terminal-only delete 與 queued schedule cancellation 的狀態機，queued cancel 只走 structured `cancel_queued_job_for_action()` result，不再保留 boolean cancel wrapper 或 retry-state 推論 fallback；並由該模組提供 active/terminal status 常數。YouTube upload issue 會回到 `succeeded`，但成功 upload 後的 Telegram 通知失敗只記 warning，不改寫 YouTube metadata 或 job 結果。
- 已補強多路錄製 robustness：retry wait 改為 delayed requeue 不佔錄製 slot，active jobs 以 process-local disk reservation 防止容量 overcommit，stale `uploading` 會於 restart/shutdown 回到 `succeeded`，Telegram `/stop <job_id>` 可精準取消指定 active job。
- 已補強 retry/queue robustness：schedule retry cancel 會釋放 duplicate state，delayed retry waiting 透過 API/UI/Telegram 可見且可取消但不計入 FIFO queue position，Telegram active job 選擇改為 worker registry 與 DB status 交集，shutdown 會 best-effort 收斂 active recording task。
- 已將 active / FIFO queued / retry waiting runtime view 收斂到 `JobRuntimeStateService`，讓 `/jobs/active`、Web UI dashboard/jobs 與 Telegram `/list` / 無參數 `/stop` 共用同一份 snapshot，並由 `services.job_actions.job_status_value()` 統一 enum/string status normalization，降低狀態 view drift。
- 已補強多路錄製與 dynamic boundary merge 後 robustness：disk reservation 納入最長 dynamic extension，retry attempt 以 hard deadline 避免 double extension，smart trim 後處理受 `MAX_PARALLEL_ACTIVITY_ANALYSES` 節流。
- 已收斂 smart trim 後處理資源邊界：FFmpeg finalize 後先釋放 browser/Xvfb/audio runtime，再進入受 `ActivityAnalysisLimiter` 節流的 completed-file analysis；unbounded dynamic extension 以 `MAX_RECORDING_SEC` 作為 admission guard 的保守估算上限。
- 已收斂錄製 slot 與後處理責任邊界：`MAX_CONCURRENT_RECORDINGS` 只計算實際 capture task，smart trim / 本機 MP4 canonicalization 改由 tracked post-processing task 執行，後處理等待不再阻塞 FIFO queue drain。
- 已補強 post-processing robustness：task state 區分 process/settle，settle failure 不再遞迴重排；DetectionLog 寫入改為 best-effort；stale `finalizing` 若已有 raw/output 檔，restart cleanup 會恢復 `succeeded`。
- 已收斂多路錄製狀態邊界：`/jobs/current` 不再讀 worker private `_current_job` fallback，Telegram 建立排程提示改看 snapshot capacity，worker cancel/finish 移除舊全域 flag，只保留 per-job state。
- 已補強 runtime state 與 notification robustness：Telegram stage notification 送出前重讀 DB status 並 skip stale update，`RecordingWorker` 移除舊 `is_busy` / `current_status` 全域相容 surface，`JobRuntimeStateService` 集中 partial runner capacity/count fallback。
- 已補強 runtime notification 與 snapshot fallback：Telegram send/edit/fallback-send 加 10 秒 timeout，成功 raw capture 會明確送出 `finalizing` stage notification且仍受 stale guard 保護，snapshot 對 invalid runner capacity/count 值會回到安全 fallback。
- 已補強 notification fanout 與 media subprocess robustness：Telegram notification 改為 bounded concurrent fanout，timeout helper 收斂為 callable wrapper，remux/MP4 validation/duration probe/thumbnail 改用 bounded subprocess runner 避免無界 `communicate()`。
- 已讓 `RecordingManager` 的 list、disk usage 共用單次 filesystem scan 與 stat metadata，並移除舊逐檔 destructive cleanup 責任，避免同一 request 反覆 `rglob()` / `stat()` 或繞過 storage maintenance。
- 已將 scheduler `next_run_at` 同步改成單一 DB session 批次更新，並跳過 unchanged `next_run_at`。
- 已在 FastAPI shutdown 關閉既有 YouTube uploader HTTP client，並將 YouTube upload chunk read 包到 thread，避免大檔讀取阻塞 event loop。
- 已讓自動 YouTube 上傳使用 preferred trimmed output，成功後刪除本地裁剪 artifact 並回退 DB `output_path` 到 raw recording。
- 已新增 provider bounded wait helper，並將 Jitsi/Webex/Zoom join/prejoin flow 剩餘固定 sleep 收斂為 selector/state/function bounded wait 或集中短 debounce。
- 已最佳化 smart trim / dynamic extension 媒體活動辨識效能，包含 live audio 長駐 meter、streaming completed-file probes、並行 audio/video probe 與 boundary refinement diagnostics。
- 已改善 smart trim 實際輸出精度，錄影 GOP 約 1 秒、trim 使用 duration-based stream copy，並記錄 expected/actual trimmed output duration。
- 已改善 Detection Logs 與 trim runner 的效能穩定性，包含 filtered summary/indexes、filter-aware CSV export、bounded trim stderr handling，以及 immediate/schedule retry window 共用 bounded dynamic extension 規則。
