# 🎙️ meeting-minutes-mcp：音檔 → 逐字稿 → 會議記錄

免費、全部在本機跑的 MCP server。丟一個會議錄音給 Claude，它會用 Whisper 轉成逐字稿，再依你選的範本整理成會議記錄。沒有任何 API 費用，音檔也不會離開你的電腦。

- **轉文字**：[faster-whisper](https://github.com/SYSTRAN/faster-whisper)（Whisper 的高速版），支援中文、英文、日文等 90 多種語言，自動偵測
- **會議記錄**：由呼叫這個 server 的 Claude（或任何支援 MCP 的 LLM）依範本撰寫，不需要另外接 Ollama 或付費 API
- **範本可擴充**：一個範本就是一個 `.md` 檔，丟進資料夾就能用，不用改程式
- **快取**：同一個音檔換範本不用重新辨識

內建範本：`general`（一般會議）、`standup`（每日站會）、`decision`（決策會議）、`interview`（訪談／客戶會議）。

---

## 安裝

需要 Python 3.10 以上。

```bash
cd meeting-minutes-mcp
python3 -m venv .venv && source .venv/bin/activate   # Windows：.venv\Scripts\activate
pip install -e .
```

第一次用會自動下載 Whisper 模型（`medium` 約 1.5 GB，之後都用本機快取）。想先下載好：

```bash
MINUTES_MODEL=medium meeting-minutes-mcp --warmup
```

---

## 接到 Claude

### Claude Code

```bash
claude mcp add meeting-minutes \
  -e MINUTES_MODEL=medium -e MINUTES_LANGUAGE=zh \
  -- /完整路徑/meeting-minutes-mcp/.venv/bin/meeting-minutes-mcp
```

### Claude Desktop

在 `claude_desktop_config.json` 加入：

```json
{
  "mcpServers": {
    "meeting-minutes": {
      "command": "/完整路徑/meeting-minutes-mcp/.venv/bin/meeting-minutes-mcp",
      "env": {
        "MINUTES_MODEL": "medium",
        "MINUTES_LANGUAGE": "zh"
      }
    }
  }
}
```

Windows 的 command 是 `C:\\...\\meeting-minutes-mcp\\.venv\\Scripts\\meeting-minutes-mcp.exe`。

---

## 怎麼用

接好之後直接跟 Claude 說：

> 把 ~/Downloads/0315週會.m4a 整理成會議記錄，用 decision 範本

或是用 prompt（Claude Desktop 的 `+` 選單、Claude Code 的 `/meeting-minutes:meeting_minutes`），填音檔路徑和範本名稱即可。

Claude 會：轉逐字稿 → 讀範本 → 寫出會議記錄 → 問你要不要存檔（`save_minutes`）。

### 工具一覽

| 工具 | 用途 |
|------|------|
| `transcribe_audio` | 音檔 → 帶時間戳的逐字稿 |
| `upload_audio` | 以 base64 上傳音檔到本機（給遠端 client 用；本機有檔案路徑就不需要） |
| `list_templates` | 列出可用範本 |
| `get_template` | 看某個範本的內容 |
| `save_minutes` | 把會議記錄存成 `.md` |

Prompt `meeting_minutes(audio_path, template, language, extra_instructions)` 一次做完轉文字＋套範本。

---

## 自訂範本

建一個資料夾，裡面放 `.md` 檔，開頭用 front matter 描述：

```markdown
---
name: weekly
title: 部門週會
description: 每週一的例行會議：各組進度、跨組協調、下週重點
---
# 部門週會 {日期}

## 各組進度
### {組名}
- {重點}

## 需要跨組協調的事
- {…}

## 下週重點
- {…}

## 待確認
{聽不清楚或可能辨識錯誤的地方，附時間戳；沒有則寫「無」}
```

大括號 `{…}` 是給 LLM 的填寫說明。然後設定環境變數：

```
MINUTES_TEMPLATES_DIR=/path/to/my-templates
```

跟內建範本同名（例如也叫 `general`）會蓋掉內建的。多個資料夾用 `:` 分隔（Windows 用 `;`）。

**建議每個範本都留一節「待確認」**：Whisper 對專有名詞、人名、中英夾雜的辨識不一定準，讓 LLM 把沒把握的地方連同時間戳列出來，比默默寫錯好。

---

## 設定

全部用環境變數，前綴 `MINUTES_`：

| 變數 | 預設 | 說明 |
|------|------|------|
| `MINUTES_MODEL` | `medium` | Whisper 模型：`tiny` / `base` / `small` / `medium` / `large-v3` / `distil-large-v3`。中文建議 `medium` 以上 |
| `MINUTES_DEVICE` | `auto` | `cpu` / `cuda` / `auto` |
| `MINUTES_COMPUTE_TYPE` | `auto` | `int8`（CPU 省記憶體）/ `float16`（GPU）/ `auto` |
| `MINUTES_LANGUAGE` | 自動偵測 | 預設語言代碼，台灣國語建議 `zh` |
| `MINUTES_INITIAL_PROMPT_ZH` | 繁體提示句 | 語言是中文時給 Whisper 的提示，用來引導輸出繁體字 |
| `MINUTES_TEMPLATES_DIR` | 無 | 自訂範本資料夾 |
| `MINUTES_DATA_DIR` | `~/.meeting-minutes-mcp` | 上傳檔、快取、輸出的根目錄 |
| `MINUTES_CACHE_DIR` | `<DATA_DIR>/cache` | 逐字稿快取 |
| `MINUTES_UPLOAD_DIR` | `<DATA_DIR>/uploads` | `upload_audio` 存檔位置 |
| `MINUTES_OUTPUT_DIR` | `<DATA_DIR>/minutes` | `save_minutes` 只給檔名時的存檔位置 |

### 速度參考

一小時的會議錄音大約需要：

| 硬體 | `small` | `medium` | `large-v3` |
|------|---------|----------|------------|
| 純 CPU（8 核） | 5 分鐘 | 15 分鐘 | 40 分鐘 |
| NVIDIA GPU | 30 秒 | 1 分鐘 | 2 分鐘 |
| Apple Silicon（CPU） | 3 分鐘 | 10 分鐘 | 25 分鐘 |

數字會因機器差很多，僅供抓感覺。CPU 使用者建議 `MINUTES_COMPUTE_TYPE=int8`。

---

## 遠端 / 手機上傳

預設用 stdio（給 Claude Desktop、Claude Code）。要讓別的機器連進來：

```bash
meeting-minutes-mcp --transport streamable-http
```

遠端 client 沒有本機檔案路徑，就用 `upload_audio` 把音檔以 base64 送進來。注意這個模式**沒有任何驗證**，只適合內網。

---

## 開發

```bash
pip install -e ".[dev]"
pytest
```

測試用假的 Whisper 模型，不需要下載模型也不需要 GPU；其中 `test_server.py` 會真的啟動 server 並透過 MCP stdio client 呼叫每個工具。

### 未來可以加的

- 說話者分離（pyannote.audio，需要 HuggingFace token）：在逐字稿標出「誰說的」
- 以 Claude 之外的本機 LLM（Ollama）在 server 內直接產出會議記錄
- 把產出的會議記錄直接送到 Notion / HackMD / Google Docs

## 授權

MIT
