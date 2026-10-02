# 每週自動寄信

設定一次後，由 GitHub Actions 執行，自己的電腦不用保持開機。

## 預設行為

- 每週一 **MSU 當地時間 02:00** 檢查執行當天至 7 天後的菜單，包含頭尾共 8 個日期；排程可能稍有延遲。
- 完整抓取午餐菜單後篩選；郵件只列午餐的起司蛋糕候選，以及另外標示的相關甜點（例如冰淇淋）。
- 郵件表格只列今天起的候選；沒有候選時仍寄出表格，並在表格內標示沒有結果。
- 每次執行 **send** 都會寄出未來 8 個日期的午餐摘要，即使結果與上次相同。每週只自動檢查一次，週一之後更新的菜單可手動執行 **send** 查詢。
- 幾千筆寬鬆候選保存在執行結果，沒有全部塞入信件。
- 抓取或通知失敗時嘗試寄故障通知；若寄信服務本身失效，改看 Actions 的失敗紀錄及 GitHub 通知。

## 一次性設定（Gmail）

1. 寄件 Gmail 帳號需要開啟兩步驟驗證。在 [Google 應用程式密碼](https://myaccount.google.com/apppasswords) 建立供這個工具使用的密碼。由帳號本人操作，不要把密碼貼在程式、聊天、README 或 GitHub issue。
2. 開啟 [GitHub Actions Secrets](https://github.com/jhtong33/msu-cheesecake/settings/secrets/actions)，按 **New repository secret**。Name 填 `SMTP_PASSWORD`，Secret 填剛產生的應用程式密碼，再儲存。
3. 在 [Actions Variables](https://github.com/jhtong33/msu-cheesecake/settings/variables/actions) 設定下表。
4. 到 [MSU menu email](https://github.com/jhtong33/msu-cheesecake/actions/workflows/menu-email.yml)，按 **Run workflow**，mode 選 **test-email**，確認收得到測試信。
5. 將 `EMAIL_ENABLED` 改為 `true`，啟用每週自動執行。也可手動選 **send** 立即抓取並寄出候選。

如果目前帳號的寄件／收件地址已由維護者設定好，第 3 步不用重填。

| Variable | 值 |
|---|---|
| `SMTP_USERNAME` | 寄件 Gmail 地址 |
| `EMAIL_TO` | 收件地址；多個地址用逗號分隔 |
| `EMAIL_ENABLED` | 測試前為 `false`，測試成功後改成 `true` |
| `SMTP_HOST` | 可省略，預設 `smtp.gmail.com` |
| `SMTP_PORT` | 可省略，預設 `587` |
| `SMTP_SECURITY` | 可省略，預設 `starttls`；使用 465 時改成 `ssl` |

寄件和收件可使用同一個信箱。地址也可放在同名 Secrets，Secrets 會優先於 Variables。郵件 From 預設就是 `SMTP_USERNAME`。

Google 帳號若看不到應用程式密碼選項，先閱讀 [Google 官方說明](https://support.google.com/accounts/answer/185833?hl=zh-Hant)；部分帳號設定不提供此選項。不要改用普通登入密碼。

## 手動模式

| mode | 用途 |
|---|---|
| `preview` | 抓執行當天至 7 天後的菜單並產生信件預覽，不寄信、不更新寄送紀錄；不需要 Gmail 密碼 |
| `test-email` | 只寄設定測試信，不抓菜單、不改菜單通知紀錄 |
| `send` | 抓最新菜單，寄出執行當天至 7 天後的午餐候選表格；沒有候選也寄信 |
| `retry-pending` | 確認上一封未收到後，解除不確定狀態並重試；若其實已收到，可能重寄 |
| `acknowledge-pending` | 確認上一封已收到後，標記該批成功並解除暫停；下次執行 **send** 仍會寄出新摘要 |

每次執行頁面底部的 **Artifacts** 可下載 `msu-menu-results-...`，含完整菜單、摘要和 `email-preview.html`。預設保留 7 天。

## 寄送狀態

寄送狀態保存於獨立的 `codex/notification-state` 分支，不依賴會過期的快取。Workflow 使用 `contents: write` 保存這個狀態，不把密碼或信箱地址寫進 Git。收件人組合以雜湊識別。

寄信前先保存 pending 狀態，SMTP 接受後才標記完成。如果 SMTP 回覆逾時或狀態保存失敗，無法百分之百確定是否已寄達，所以後續菜單信件會暫停。先檢查信箱（包含垃圾郵件），再使用 `acknowledge-pending` 或 `retry-pending`。這避免自動重試造成重複信件，但不是「永遠只寄一次」的保證。

同一時間只允許一個通知 workflow 執行。請勿刪除通知狀態分支，否則待確認的寄送紀錄可能遺失。

## 本機寄信

本機可設定相同名稱的環境變數後執行 `python notify_menus.py --mode send`。SMTP 密碼請用安全的環境設定方式輸入，避免寫入 shell 歷史。必須使用最近 6 小時內以 `--refresh` 取得、且涵蓋今天的完整報表；寄送狀態預設保存在 `probe-output/notification-state.json`。

雲端和本機預設使用不同寄送紀錄；若兩邊都執行 **send**，會各寄一封。日常使用建議只開啟 GitHub Actions。
