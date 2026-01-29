# MeetingRecorder

自動線上會議錄製系統，使用 Python + Playwright 自動加入會議，透過 Xvfb + PipeWire + FFmpeg 在無頭環境中錄製影音。

> **⚠️ 錄製功能僅支援 Linux**：Windows/macOS 使用者請透過 Docker 部署。

## 功能特色

- **多平台支援**：Jitsi Meet、Cisco Webex (Guest Join)
- **自動化錄製**：Playwright 自動加入會議、處理等候室
- **智慧會議結束偵測**：WebRTC、文字指示、影片元素、URL 變更、螢幕凍結、音訊靜音
- **錄影可靠性增強**：MKV 抗損毀格式、網路錯誤自動重試、PipeWire 低延遲音訊
- **排程管理**：支援單次與週期性 (cron) 排程
- **通知系統**：Telegram Bot、YouTube 自動上傳

## 先決條件

請先安裝 Docker 環境：

| 平台 | 安裝方式 |
|------|----------|
| Windows / macOS | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| Linux | [Docker Engine](https://docs.docker.com/engine/install/) 或 `curl -fsSL https://get.docker.com \| sh` |

安裝完成後執行 `docker --version` 確認安裝成功。

## 快速開始

```bash
# 1. 建立部署目錄
mkdir meeting-recorder && cd meeting-recorder
mkdir -p data recordings diagnostics

# 2. 下載設定檔
curl -O https://raw.githubusercontent.com/eyeduck-ai/meeting_recorder/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/eyeduck-ai/meeting_recorder/main/docker-compose.prod.yml
curl -O https://raw.githubusercontent.com/eyeduck-ai/meeting_recorder/main/.env.example
cp .env.example .env

# 3. 設定密碼（編輯 .env）
# AUTH_PASSWORD=your-secure-password

# 4. 啟動服務
docker pull ghcr.io/eyeduck-ai/meeting_recorder:latest
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

開啟 **http://localhost:8000** 即可使用。

## YouTube 授權設定

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立專案並啟用 **YouTube Data API v3**
3. 建立 OAuth 2.0 憑證（桌面應用程式類型）
4. 設定環境變數 `YOUTUBE_CLIENT_ID` 和 `YOUTUBE_CLIENT_SECRET`
5. 在 Web UI `/settings` 頁面完成 Device Code 授權流程

## Telegram Bot 設定

1. 向 [@BotFather](https://t.me/BotFather) 建立 Bot 並取得 Token
2. 設定環境變數 `TELEGRAM_BOT_TOKEN`
3. 啟動服務後，向 Bot 發送 `/start` 註冊
4. 在 Web UI `/settings` 頁面核准用戶

## 常見問題（FAQ）

### Q: Docker 容器需要多少資源？

建議配置：
- **RAM**：4GB 以上（2GB 為最低需求）
- **CPU**：2 核心以上
- **磁碟**：視錄製時長而定，1080p 影片約 500MB/小時

### Q: 如何查看錄製過程中的畫面？

VNC 功能需要額外安裝 x11vnc。如需使用，請自行修改 Dockerfile 加入 `x11vnc` 套件，然後：

```bash
DEBUG_VNC=1 docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

使用 VNC 客戶端連線到 `localhost:5900`（無需密碼）。

> **注意**：為了減少 image 大小和提高錄製穩定性，預設不安裝 x11vnc。

### Q: 錄製失敗如何排查？

1. 查看容器日誌：`docker compose logs -f`
2. 檢查診斷資料：`diagnostics/{job_id}/` 目錄（含截圖、HTML、錯誤詳情）
3. 啟用 VNC 觀察實際執行狀況

### Q: 如何更新到最新版本？

```bash
docker pull ghcr.io/eyeduck-ai/meeting_recorder:latest
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Q: 資料會保存在哪裡？

| 目錄 | 內容 |
|------|------|
| `./data/` | SQLite 資料庫、YouTube token |
| `./recordings/` | 錄製的影片檔案 |
| `./diagnostics/` | 失敗診斷資料 |

即使刪除容器，這些資料也會保留。

## 文件

- 🛠️ [開發者指南](docs/development.md) - 環境變數、API、架構、調試、測試

## License

MIT
