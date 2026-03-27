# MeetingRecorder

自動化線上會議錄製系統。服務會用 Playwright 以來賓身分加入會議，並在 Linux 錄製環境中透過 Xvfb、PipeWire、FFmpeg 產生錄影檔，提供 Web UI、排程、Telegram 通知與 YouTube 上傳能力。

> 錄製核心依賴 Linux。Windows 與 macOS 使用者請透過 Docker 部署；若直接在原始碼環境執行，僅 Linux 支援實際錄製。

## 支援能力

- 會議平台：Jitsi Meet、Cisco Webex
- 排程模式：單次排程、CRON 週期排程、手動立即觸發
- 錄製控制：大廳等待、提前加入、自動偵測會議結束、手動停止/提前完成
- 整合能力：Telegram Bot 通知與管理、YouTube Device Flow 授權與上傳
- 除錯能力：診斷截圖、頁面 HTML、console log、FFmpeg/remux/transcode log

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
- `DATABASE_URL`: 預設為 `sqlite:///./data/app.db`
- `TELEGRAM_BOT_TOKEN`: 要使用 Telegram Bot 時設定
- `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET`: 要使用 YouTube 上傳時設定

其餘錄製與通知相關設定可先使用預設值。

### 4. 啟動服務

使用已發布映像：

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

若你要從目前原始碼自行建置：

```bash
docker compose up --build -d
```

啟動後開啟 [http://localhost:8000](http://localhost:8000)。

## 首次設定建議

### Telegram Bot

1. 透過 [@BotFather](https://t.me/BotFather) 建立 Bot。
2. 將 Token 寫入 `.env` 的 `TELEGRAM_BOT_TOKEN`。
3. 重新啟動服務。
4. 使用者先對 Bot 發送 `/start`。
5. 進入 Web UI 的 `/settings` 或 Telegram 管理流程核准使用者。

### YouTube 上傳

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 建立專案並啟用 YouTube Data API v3。
2. 建立 OAuth 2.0 Desktop App 憑證。
3. 將 `YOUTUBE_CLIENT_ID`、`YOUTUBE_CLIENT_SECRET` 寫入 `.env`。
4. 重啟服務後，到 Web UI 的 `/settings` 完成 Device Flow 授權。

## 資料保存位置

| 路徑 | 內容 |
| --- | --- |
| `data/` | SQLite 資料庫、YouTube 授權資料 |
| `recordings/` | 錄影輸出檔案 |
| `diagnostics/` | 錄製失敗與除錯資料 |
| `logs/` | 應用程式日誌 |

這些目錄都會掛載到容器外部，重新部署後仍會保留。

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

- RAM：建議 4 GB 以上
- CPU：建議 2 vCPU 以上
- 磁碟：視錄製長度而定，1080p 錄影大約數百 MB 到數 GB

### 錄製失敗時先看哪裡？

1. `docker compose logs -f app`
2. `diagnostics/<job_id>/`
3. Web UI 的工作詳情與偵測日誌頁面

### 錄製檔案會存成什麼格式？

錄製原始輸出為 `.mkv`。在下載或 YouTube 上傳流程中，系統會視情況 remux 或轉成 `.mp4`。

## 文件導覽

- [docs/development.md](docs/development.md)：給人看的開發與維護指南
- [AGENTS.md](AGENTS.md)：給 agent 與 contributor 的唯一規格來源

## License

MIT
