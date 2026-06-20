# MeetingRecorder

自動化線上會議錄製系統。服務會用 Playwright 以來賓身分加入會議，並在 Linux 錄製環境中透過 Xvfb、PipeWire、FFmpeg 產生錄影檔，提供 Web UI、排程、Telegram 通知與 YouTube 上傳能力。

> 錄製核心依賴 Linux。Windows 與 macOS 使用者請透過 Docker 部署；若直接在原始碼環境執行，僅 Linux 支援實際錄製。

## 支援能力

- 會議平台：Jitsi Meet、Cisco Webex、Zoom
- 排程模式：單次排程、CRON 週期排程、手動立即觸發
- 錄製控制：受上限保護的並行錄製、FIFO 排隊、大廳等待、提前加入、依音訊/影像活動動態延長結束時間、手動停止/提前完成
- 錄製畫面：預設以 Chromium app window 開啟會議，避免錄到瀏覽器工具列；保留裁切 fallback
- 智慧輸出：可依音訊/影像活動裁掉會議開始前與結束後的靜止片段，原始錄影仍會保留
- 整合能力：Telegram Bot 通知與管理、YouTube Device Flow 授權與上傳
- 除錯能力：診斷截圖、頁面 HTML、console log、FFmpeg/remux/transcode log，並自動清理過期資料

## 快速開始

### 1. 安裝 Docker

| 平台 | 建議方式 |
| --- | --- |
| Windows / macOS | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| Linux | [Docker Engine](https://docs.docker.com/engine/install/) |

安裝完成後，先確認 `docker --version` 與 `docker compose version` 可正常執行。

### 2. 取得部署檔案

```bash
git clone https://github.com/eyeduck-ai/meeting_recorder.git
cd meeting_recorder
cp .env.example .env
mkdir -p data recordings diagnostics logs
```

### 3. 編輯 `.env`

至少確認下列項目：

- `AUTH_PASSWORD`: Web UI / API 密碼；留空代表不啟用密碼保護
- `CORS_ALLOWED_ORIGINS`: 需要跨來源 browser client 呼叫 API 時才設定，使用逗號分隔明確 origins，不支援 `*`
- `DATABASE_URL`: 預設為 `sqlite:///./data/app.db`
- `MAX_CONCURRENT_RECORDINGS`: 同時錄製上限，預設 `2`
- `RECORDING_DISPLAY_START` / `RECORDING_DISPLAY_POOL_SIZE`: 每路錄製使用的 Xvfb display pool，預設從 `:100` 起共 `16` 個
- `MIN_FREE_DISK_GB_BEFORE_RECORDING`: 啟動錄製前最低剩餘磁碟空間，預設 `10`
- `MAX_PARALLEL_TRANSCODES`: YouTube 上傳前 remux/transcode 並行上限，預設 `1`
- `TELEGRAM_BOT_TOKEN`: 要使用 Telegram Bot 時設定
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`: 要使用 YouTube 上傳時設定

其餘錄製與通知相關設定可先使用預設值。Web UI 的設定頁可調整錄製解析度、lobby 等待時間、瀏覽器啟動模式、上方裁切 fallback，以及 Detection & Activity 中的智慧裁剪與動態延長預設值。解析度與 lobby 等待時間會套用到手動錄製與之後新建立的排程，既有排程會保留自己的錄製設定；瀏覽器模式與上方裁切是全域錄製設定，會套用到之後執行的錄製工作。智慧裁剪與動態延長可用全域預設，也可在單一排程的 Advanced Options 覆寫。

`MAX_CONCURRENT_RECORDINGS` 必須大於等於 `1`，且不可大於 `RECORDING_DISPLAY_POOL_SIZE`；設定錯誤時服務會在啟動時 fail fast。錄製 slot 滿時，立即錄製與手動觸發排程會依進入順序排隊；Web UI 的 Dashboard / Jobs 會顯示 queue position，queued immediate job 與 queued schedule run 都可取消，queued job 不支援 Finish。錄製中的 job 才能 Stop/Finish；等待 retry 的 job 會顯示 retry 倒數並可取消，但不佔錄製 slot，也不分配 FIFO queue position。YouTube remux/upload 中的 job 只顯示 Processing，不支援 Stop、Finish 或 Delete。Jobs 頁的 Delete completed 只刪除 `succeeded`、`failed`、`canceled` 等終態 job，會保留 queued、active 與 uploading job。

多路錄製會對每個 active job 做保守磁碟預留，避免多場長錄製同時通過單點 free-space 檢查後把磁碟打滿；若預估後不足以保留 `MIN_FREE_DISK_GB_BEFORE_RECORDING`，job 會以 `DISK_FULL` 失敗。遇到可重試的 join/network failure 時，retry 等待不會佔用錄製 slot，會延遲後以同一 job id 重新排隊。若服務在 YouTube upload 中重啟，job 會恢復為 `succeeded` 並記錄 upload interrupted，不會永久停在 `uploading`。

### 4. 啟動服務

使用已發布映像：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

若你要從目前原始碼自行建置：

```bash
docker compose -f docker-compose.yml -f docker-compose.deploy.yml up --build -d
```

啟動後開啟 [http://localhost:8000](http://localhost:8000)。

### 本地測試與 live validation

不要用裸 `docker compose up --build -d` 在其他 worktree 做測試；那可能替換已部署的 recorder。
本地測試請使用隔離 wrapper，預設會使用獨立 project、`APP_PORT=8001`、`VNC_PORT=5901` 與 workspace 專屬 image tag：

```bash
python -m scripts.dev_compose up --build -d
```

測試服務啟動後開啟 [http://localhost:8001](http://localhost:8001)。

## 首次設定建議

### Telegram Bot

1. 透過 [@BotFather](https://t.me/BotFather) 建立 Bot。
2. 將 Token 寫入 `.env` 的 `TELEGRAM_BOT_TOKEN`。
3. 重新啟動服務。
4. 使用者先對 Bot 發送 `/start`。
5. 進入 Web UI 的 `/settings` 或 Telegram 管理流程核准使用者。

常用指令包含 `/list`、`/record`、`/edit`、`/meetings` 與 `/stop`。多場錄製同時進行時，`/list` 會顯示 active job id、FIFO queue count 與 retry waiting count；`/stop` 會停止最新 active recording，`/stop <job_id>` 可停止指定 active job，或取消指定 queued / retry waiting job。

### YouTube 上傳

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 建立專案並啟用 YouTube Data API v3。
2. 建立 OAuth 2.0 Desktop App 憑證。
3. 將 `YOUTUBE_CLIENT_ID`、`YOUTUBE_CLIENT_SECRET` 寫入 `.env`。
4. 重啟服務後，到 Web UI 的 `/settings` 完成 Device Flow 授權。

## 資料保存位置

| 路徑 | 內容 |
| --- | --- |
| `data/` | SQLite 資料庫、YouTube 授權資料 |
| `recordings/` | 原始錄影檔與本地裁剪輸出檔 |
| `diagnostics/` | 錄製失敗與除錯資料 |
| `logs/` | 應用程式日誌 |

這些目錄都會掛載到容器外部，重新部署後仍會保留。

### 資料保留與清理

- 本機錄影長期保存為 `.mp4`；系統錄製時會先產生 `.mkv`，成功 fast remux 成 validated `.mp4` 後刪除 `.mkv`。YouTube 上傳壓縮才會依設定使用臨時轉檔檔案，不改變本機 canonical MP4。
- 每日 03:30 會自動執行 storage maintenance，也可在 Web UI `/settings` 的 Storage Management 手動預覽或執行。
- 已成功上傳 YouTube 的本機錄影檔，若已保存 14 天以上會刪除本機檔案，但保留工作紀錄與 YouTube 連結。
- `diagnostics/`、rotated app logs 與 detection logs 預設保留 14 天。
- Docker container log 已設定 `json-file` rotation：每個 container 最多 `20m x 5`。既有 container 需要 recreate 後才會套用新的 log rotation 設定。

### Secret 保存提醒

會議密碼、通知 webhook secret 與 YouTube token 目前由本機 `.env`、SQLite DB 或 `data/youtube_token.json` 保存；Web UI/API 會遮罩顯示，但不是加密儲存。部署時請保護 `.env`、`data/` volume 與備份檔案的讀取權限。

## 日常操作

查看容器狀態與日誌：

```bash
docker compose ps
docker compose logs -f app
```

更新到最新發布映像：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## 常見問題

### Windows 或 macOS 為什麼不能直接執行錄製？

錄製流程依賴 Linux 上的 Xvfb、PipeWire 與相關瀏覽器/影音元件，因此非 Linux 主機需要透過 Docker 容器提供相容環境。

### 需要多少資源？

- 單路 1080p：建議 2-4 vCPU、4 GB RAM 以上
- 2 路 1080p：建議 6-8 vCPU、8-12 GB RAM、100 GB 以上可用磁碟
- 4 路 1080p：建議 12-16 vCPU、16-24 GB RAM、250 GB 以上可用磁碟
- 磁碟：每路 1080p 約 0.5-6 GB/小時；上傳前 remux/transcode 可能短時間需要接近 2 倍影片大小的暫存空間

預設 SQLite 適合低並行部署；若長期提高到 4 路以上或同時有大量 API/UI 操作，建議改用 Postgres 類外部資料庫。

### 錄製失敗時先看哪裡？

1. `docker compose logs -f app`
2. `diagnostics/<job_id>/`
3. Web UI 的工作詳情與偵測日誌頁面

### 錄製檔案會存成什麼格式？

錄製流程會先產生 `.mkv`，完成後成功 fast remux 並通過 MP4 驗證時，`.mp4` 會成為本機保存與下載格式，原 `.mkv` 會被刪除。若智慧裁剪判斷需要剪掉開頭或結尾靜止片段，Web UI 會優先提供裁剪後的本地輸出；下載或 YouTube 上傳流程中，系統會視情況 remux 或轉成 `.mp4`。自動 YouTube 上傳成功後，若本次上傳使用本地裁剪檔，系統會刪除該裁剪檔並讓 Web UI 回退到原始錄影檔。YouTube 上傳壓縮是 upload-only temporary file 行為，不影響本機 canonical MP4。

### 動態延長如何決定何時停止？

到達排程指定的錄製長度後，若啟用 Dynamic End Extension，系統會用音訊能量與畫面差異判斷是否仍有活動。只要音訊或影像其中之一仍有變化，就會繼續錄製；當音訊靜音且畫面固定連續達到設定的 Idle Stop 時間，或達到 Max Extension 上限時才停止。預設 Idle Stop 為 300 秒，Max Extension 為 3600 秒。

### 為什麼錄影畫面還看到瀏覽器工具列？

系統預設使用 Chromium `--app=<join_url>` 開啟實際會議頁，這種 app window 通常不顯示 tab、網址列與工具列。若特定 provider 或環境不適合 app window，可在 Web UI `/settings` 將 Browser Mode 改為 Normal，並用 Top Crop Mode/Top Crop Fallback 作為保底；輸出解析度會維持不變。Normal browser 搭配 crop off 只適合除錯，可能錄到 Chrome UI。

## 文件導覽

- [docs/development.md](docs/development.md)：給人看的開發與維護指南
- [AGENTS.md](AGENTS.md)：給 AI agent 遵守的工作規則
- [Plan.md](Plan.md)、[Task.md](Task.md)、[Lesson.md](Lesson.md)：維護者使用的改善追蹤文件

## License

MIT
