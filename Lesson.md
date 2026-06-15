# 經驗教訓

本檔記錄開發與盤點過程中的錯誤判斷、耗時痛點與避免方式。新增 Lesson 時，請寫成可被未來 agent 或 contributor 直接套用的判斷規則。

- 不要在未確認 middleware public path prefix 行為前假設 API 已受保護。
- 不要只看資料表欄位或 UI 表單就認定設定已實際套用到 worker/session。
- 不要把 process-local singleton 誤描述成跨 process/跨 container 的並發保護。
- 不要用誤導性欄位名稱如 `password_encrypted` 表示未加密資料。
- 跑測試時避免和 `uv sync` 並行，否則可能出現暫時性 import error。
- 發現 route、Web UI、Telegram 對同一流程有不同做法時，優先抽 service 層收斂行為，而不是在三個入口各修一次。
- Windows 的 git ignore 比對可能讓 `plan.md` 擋住 `Plan.md`；官方追蹤文件若大小寫相近，要在 `.gitignore` 用 `!Plan.md` 這類規則明確 unignore。
- 測試設定解析邏輯時，不要用未規格化的 `Mock` 當完整 settings；`Mock` 會為未定義屬性產生假值，應使用 `SimpleNamespace` 或只讀 `__dict__`/Pydantic field 內實際存在的欄位。
- 不要把 trigger accepted 視為 recording started；排程生命週期要分開記錄 trigger、實際開始與完成，否則 queue 或 lock 等待期間會污染 catch-up 判斷。
- 去重 manual trigger 時不要只看 queue 容器；立即觸發的 task 在取得 lock 前也需要 pending 狀態，否則同一 schedule 可能在短時間內重複入列。
- 抽 service 層時要順手檢查三個入口的欄位名稱是否真的存在；Telegram meeting creation 曾使用不存在的 `meeting_url`，應統一走 `MeetingService` 的 model 欄位映射。
- 將 runtime 移到 FastAPI lifespan 時要保留 router-only 測試 fallback；否則單獨 include router 的 TestClient 會因缺少 `app.state` runtime 而失敗。
- 拆 DB 模組時，`init_db()` 必須在 `Base.metadata.create_all()` 前 import `database.models` 讓 metadata 註冊完整；只 import `database.base.Base` 會讓 create_all 看不到任何 ORM table。
- password input 不可用既有 secret value 回填；即使 API 不回傳 secret，HTML form value 仍會把 secret 暴露給瀏覽器與測試輸出。
- Provider 新增或移除時不要分頭修改 API `Literal`、Web UI 選項與 Telegram keyboard；先讓 provider registry metadata 成為唯一來源，再由各入口讀取。
- 拆大型 route 或 runner 時要先搜尋測試是否直接 import private helper 或 monkeypatch module-level dependency；抽出模組後要保留相容 re-export，或把測試 fixture 的 patch target 同步移到新 owner。
- 拆 Web UI 子 router 時要保留 `api.routes.ui.router` 作為聚合入口；router-only tests 和 `api/main.py` 只 include 聚合 router，若子 router 沒有 include 回去會造成整組 HTML route 消失。
- 移動像 `QueueScheduleResult` 這類已被 API、Telegram 或 tests import 的 internal-public 型別時，要先在舊模組保留 re-export，再逐步改呼叫端，避免一次性破壞相容 import。
- 子 router 不應反向 import 聚合 router 來取 render/settings/helper；這會形成雙向依賴，應抽 `ui_common` 這類共用層讓依賴方向維持單向。
- 錄影檔 list/cleanup/disk usage 不要各自重新掃描 filesystem 或重複 `stat()`；先建立帶 metadata 的 entry，再由排序、分頁與 response 組裝共用。
- FastAPI shutdown 關閉 singleton 資源時，不要為了 close 而呼叫會 lazy-create 的 getter；應提供只關閉既有 instance 的 helper。
- provider 固定 sleep 不能一次全部機械替換；先用 bounded wait 搭短 fallback，保留必要 UI debounce，否則容易把等待不足變成間歇性 join failure。
- 用 PowerShell 測試會 redirect 的 Web UI POST 時，不要用 `Invoke-WebRequest -MaximumRedirection 0` 後直接假設請求失敗；它可能已經送出 POST 但在處理 303 redirect 時丟例外，重試前要先查 job/schedule 狀態避免重複觸發。
- `scripts.dev_compose` 若在 Docker create 階段回 EOF，不要立刻重跑完整 build；先檢查是否已有 Created container、殘留 compose process 或 network，必要時啟動既有 container 或清掉殘留資源再重試。
- 拆 route handler 到子 router 後，測試 monkeypatch 要改到真正 owner module；FastAPI `dependency_overrides` 則應 target 共用 dependency callable，而不是依賴聚合模組的相容 re-export。
- 拆大型 conversation module 時先保留舊 import path 作 re-export 聚合器；測試要改 patch 真正 owner module，避免相容層遮蔽 helper 依賴。
- 用 `asyncio.as_completed()` 做 bounded wait 時，成功返回前也要 cancel 並 gather 其他 task，否則已完成的失敗 task 可能在測試結束時印出 `Task exception was never retrieved`。
- 測試通過但留下 deprecation warning 時不要視為完全乾淨；框架升級相關 warning 應盡早改成新 API，避免之後版本升級變成硬錯誤。
- Zoom 這類第三方 join page 不要假設固定頁面順序；應先判斷目前頁面狀態，再依狀態推進 cookie、browser join、name/password、join、lobby 或 in-meeting，並避免在 provider state evidence 中保存 invite token。
- Zoom 進入會議後仍可能出現遮擋共享畫面的 transient toast；錄影開始前要讓 provider best-effort 清理這類 UI，不要用改 Chromium GPU 參數取代 DOM 層 dismissal。
- 在 Xvfb 裡不要用 Chromium kiosk/fullscreen launch flags 作為乾淨錄影保證。Jitsi smoke 實測 `crop=0` 搭配 kiosk/fullscreen 仍錄到 Chrome tab/address bar；`manual crop=80` 只移除主要工具列但仍留下 7px 白/灰視窗邊界；`manual crop=88` 可成功但固定值依 Chrome/桌面環境可能不穩。若需要 normal browser fallback，才用 `recording_crop_mode=auto` 依 `outerHeight - innerHeight + padding` 解析 offset，並用 runtime browser dimensions 與抽幀驗證實際 capture frame。
- Jitsi live smoke 若用隨機空房間當對照，可能卡在 `waiting_lobby` 而無法進入錄製階段；這是 provider/房間狀態問題，不應拿來判斷 capture crop 是否失效。需要乾淨對照時，使用可直接加入的既有測試房間，或先用人工主持人建立房間。
- Chromium `--app` 只有作用在實際錄製用的 app window；`--app=about:blank` 後再 `browser.new_context()` / `context.new_page()` 會產生普通 Chrome page，仍可能錄到 address bar。可行模式是 `launch_persistent_context(..., --app=<join_url>)` 後使用 `context.pages[0]`，再把同一個 page 交給 provider join flow。
- Chromium app window hardening 時不要只依賴 `context.pages[0]` 立即存在；Playwright persistent context 啟動後應 bounded wait initial page，且 app mode 不需要再 request DOM fullscreen。`runtime.json` / `metadata.json` 也不能保存完整 join URL query/hash，否則 Zoom/Webex 的 `pwd` 或 token 會被 diagnostics 留下。
- app window 已以 Jitsi live smoke 驗證；Webex/Zoom 因本輪沒有可加入的測試會議連結，只能先用單元測試保護啟動/diagnostics/fallback 行為。拿到有效 Webex/Zoom 測試連結後，要補抽幀確認 redirect、browser join 或 popup 不會脫離 app window。
