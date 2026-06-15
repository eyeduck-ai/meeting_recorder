# 改善任務

本檔是可執行任務清單。完成任務後，請把 `[ ]` 改成 `[x]`；若過程中有踩坑、誤判或可避免的耗時模式，請同步更新 `Lesson.md`。

- [x] P0：修 API auth bypass，補 middleware integration tests。
- [x] P0：收斂 CORS 設定與 API public path 清單。
- [x] P1：建立 `RuntimeConfigService`，定義 env / DB / schedule override precedence。
- [x] P1：讓 worker/session 使用解析後的 runtime config，套用 schedule resolution 與 lobby wait。
- [x] P1：修 recording manager 使用 `settings.recordings_dir`，並能處理 job 子目錄。
- [x] P1：重整 schedule/job 狀態欄位與 catch-up 判斷。
- [x] P1：建立 `MeetingService`、`ScheduleService`、`JobService`，讓 API/Web UI/Telegram 行為一致。
- [x] P2：改用 FastAPI lifespan 管理 worker/scheduler/job runner。
- [x] P2：移除 route import 階段的 `init_db()` 副作用。
- [x] P2：拆 DB engine/session/migration，規劃 Alembic 導入。
- [x] P2：調整手動 trigger 的 queue/409 語意。
- [x] P2：改善 secret/token at rest 記錄、命名與保護方式。
- [x] P3：統一 provider 單一來源。
- [x] P3：拆出 Web UI job diagnostics helper 與 scheduling upload runner，降低大型檔案耦合。
- [x] P3：拆出 Web UI jobs/recordings routes 到獨立子 router，保留 `api.routes.ui.router` 聚合入口。
- [x] P3：拆出 `ui_common`，移除 UI 子 router 對 `api.routes.ui` 聚合模組的反向依賴。
- [x] P3：拆出 `ScheduleRunQueue`，集中 schedule queue、pending、duplicate 與 queue position 狀態。
- [x] P3：拆出 `RecordingExecutor`，集中 recording retry、attempt DB 更新、status callback 與 stage notification。
- [x] P3：最佳化 `RecordingManager`，讓 list、cleanup、disk usage 共用單次 scan/stat entry。
- [x] P3：最佳化 scheduler `next_run_at` 同步，改成單一 DB session batch update 並跳過 unchanged 值。
- [x] P3：拆出 `RecordingMonitor`，集中 duration、finish/cancel、FFmpeg stall 與 auto-detect 判斷。
- [x] P3：在 FastAPI shutdown 關閉既有 YouTube uploader HTTP client，並將 upload chunk read 改成 non-blocking helper。
- [x] P3：新增 provider bounded wait helper，先替換 Webex/Zoom 粗粒度固定 sleep。
- [x] P3：持續拆分 `api/routes/ui.py` 的 auth/dashboard/meeting/schedule/settings route 群組。
- [x] P3：拆分 `telegram_bot/conversations.py` 的 create schedule、edit schedule、create meeting conversation 群組。
- [x] P3：持續將 Jitsi/Webex/Zoom 剩餘固定 sleep 改成 selector/state/function bounded wait，僅保留必要 debounce/fallback。
- [x] P3：將 Zoom join/prejoin 改成狀態推進流程，覆蓋 cookie banner、Join from browser、name/password form、lobby 與 in-meeting 分支。
- [x] P3：新增 provider transient overlay dismissal，讓 Zoom 錄製前清掉 hardware acceleration 等遮擋提示。
