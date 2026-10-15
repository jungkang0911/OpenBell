"""MCP server：上傳音檔 → 轉逐字稿 → 挑範本 → 由呼叫端的 LLM 產出會議記錄。

工具（tools）
  upload_audio       接收 base64 音檔，存到本機後回傳路徑
  transcribe_audio   把音檔轉成帶時間戳的逐字稿（本機 faster-whisper，有快取）
  list_templates     列出可用的會議記錄範本
  get_template       取得某個範本的內容
  save_minutes       把產出的會議記錄存成 .md 檔

提示（prompts）
  meeting_minutes    一步到位：轉文字 + 套範本，回傳給 LLM 的完整指令
"""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import inspect
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import Settings, load_settings
from .templates import Template, TemplateError, load_templates, get_template as _get_template
from .transcriber import AUDIO_EXTENSIONS, Transcriber, Transcript, format_time

INSTRUCTIONS = """\
這個 server 把會議錄音轉成逐字稿，並提供會議記錄範本。
建議流程：
1. 若使用者給的是檔案路徑，直接呼叫 transcribe_audio；若給的是檔案內容，先 upload_audio 取得路徑。
2. 用 list_templates 讓使用者挑範本（預設 general）。
3. 依範本與逐字稿撰寫會議記錄；只寫逐字稿有的內容，不確定的放「待確認」。
4. 需要存檔時呼叫 save_minutes。
也可以直接用 meeting_minutes 這個 prompt，一次完成步驟 1 到 3。
"""

settings: Settings = load_settings()
transcriber = Transcriber(settings)
mcp = MCPServer(
    "meeting-minutes",
    title="會議記錄（本機 Whisper）",
    instructions=INSTRUCTIONS,
    version=__version__,
)


# ---------------------------------------------------------------- helpers

# 這些是「使用者看得懂、LLM 可以據此修正參數」的錯誤。mcp 2.x 會把其他例外藏成
# 通用的「Error executing tool」，所以要轉成 ToolError 才能把訊息帶回給呼叫端。
USER_ERRORS = (ValueError, KeyError, FileNotFoundError, FileExistsError, TemplateError)


def _error_message(e: BaseException) -> str:
    if isinstance(e, KeyError) and e.args:
        return str(e.args[0])
    return str(e)


def user_facing_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except USER_ERRORS as e:
                raise ToolError(_error_message(e)) from e

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except USER_ERRORS as e:
            raise ToolError(_error_message(e)) from e

    return wrapper



def _transcript_header(t: Transcript) -> str:
    cache_note = "（使用快取）" if t.cached else ""
    return (
        f"來源：{t.source}\n"
        f"模型：{t.model}{cache_note}\n"
        f"語言：{t.language}（信心 {t.language_probability:.0%}）\n"
        f"長度：{format_time(t.duration)}\n"
        f"段落數：{len(t.segments)}"
    )


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    name = re.sub(r"[^\w.\-一-鿿]+", "_", name)
    if not name or name.startswith("."):
        raise ValueError(f"檔名不合法：{filename!r}")
    if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError(f"不支援的副檔名：{filename!r}，支援：{', '.join(sorted(AUDIO_EXTENSIONS))}")
    return name


def _describe_template(t: Template) -> str:
    origin = "內建" if t.builtin else f"自訂：{t.path}"
    return f"- **{t.name}**：{t.title}。{t.description}（{origin}）"


def build_minutes_prompt(transcript: Transcript, template: Template, extra_instructions: str = "") -> str:
    extra = f"\n額外要求：{extra_instructions.strip()}\n" if extra_instructions.strip() else ""
    return f"""你是專業的會議記錄員。下面是一段會議錄音的逐字稿，由 Whisper 自動辨識，可能有錯字、同音字錯誤或斷句不自然。請依照指定範本整理成會議記錄。

規則：
1. 只根據逐字稿撰寫，不要編造逐字稿裡沒有的資訊。範本中填不出來的欄位寫「未提及」。
2. 保留範本的章節結構與順序，輸出 Markdown；大括號 {{…}} 是填寫說明，輸出時要換成實際內容，不要留下大括號。
3. 使用繁體中文撰寫；逐字稿若是其他語言，仍以繁體中文整理，專有名詞可保留原文。
4. 明顯的辨識錯誤依上下文修正；沒把握的放進「待確認」並附時間戳。
5. 日期、人名、數字等逐字稿沒有明確提到的，不要猜。
{extra}
=== 範本：{template.title}（{template.name}）===
{template.body}
=== 逐字稿 ===
{_transcript_header(transcript)}

{transcript.render(timestamps=True)}
=== 逐字稿結束 ===

請直接輸出會議記錄的 Markdown，不要加其他說明。"""


# ---------------------------------------------------------------- tools


@mcp.tool()
@user_facing_errors
async def upload_audio(filename: str, data_base64: str) -> str:
    """上傳音檔（base64 編碼）並存到本機，回傳可用於 transcribe_audio 的路徑。

    若使用者給的是本機檔案路徑，不需要上傳，直接用 transcribe_audio。
    支援的副檔名：mp3、wav、m4a、flac、ogg、opus、webm、mp4 等。
    """
    name = _safe_filename(filename)
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"data_base64 不是合法的 base64：{e}") from e
    if not raw:
        raise ValueError("上傳的內容是空的")

    upload_dir = settings.upload_dir
    upload_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = upload_dir / f"{stamp}-{name}"
    await anyio.to_thread.run_sync(target.write_bytes, raw)
    return f"已儲存：{target}（{len(raw):,} bytes）"


@mcp.tool()
@user_facing_errors
async def transcribe_audio(audio_path: str, language: str | None = None, timestamps: bool = True) -> str:
    """把音檔轉成逐字稿（本機 faster-whisper，免費、不上傳雲端）。

    參數：
    - audio_path：音檔的本機路徑（或 upload_audio 回傳的路徑）
    - language：語言代碼，如 zh、en、ja；留空自動偵測。台灣國語建議填 zh。
    - timestamps：每段前面是否加 [hh:mm:ss]（整理會議記錄時建議保留）

    第一次使用某個模型時會自動下載（medium 約 1.5 GB），之後都用本機快取。
    同一個檔案再次轉文字會直接回傳快取結果。
    """
    transcript = await anyio.to_thread.run_sync(transcriber.transcribe, Path(audio_path), language)
    return f"{_transcript_header(transcript)}\n\n{transcript.render(timestamps=timestamps)}"


@mcp.tool()
@user_facing_errors
def list_templates() -> str:
    """列出可用的會議記錄範本（內建 + 自訂）。"""
    templates = load_templates(settings.templates_dirs)
    if not templates:
        return "沒有任何範本。"
    lines = ["可用的會議記錄範本："] + [_describe_template(t) for t in templates.values()]
    if settings.templates_dirs:
        lines.append("")
        lines.append("自訂範本資料夾：" + "、".join(str(d) for d in settings.templates_dirs))
    return "\n".join(lines)


@mcp.tool()
@user_facing_errors
def get_template(name: str) -> str:
    """取得指定範本的完整內容（Markdown 骨架與填寫說明）。"""
    t = _get_template(name, settings.templates_dirs)
    return f"# 範本：{t.title}（{t.name}）\n{t.description}\n\n---\n{t.body}"


@mcp.tool()
@user_facing_errors
async def save_minutes(content: str, filename: str, overwrite: bool = False) -> str:
    """把會議記錄存成 Markdown 檔。

    - filename：檔名或完整路徑。只給檔名時存到預設輸出資料夾（MINUTES_OUTPUT_DIR）。
    - overwrite：檔案已存在時是否覆蓋，預設不覆蓋。
    """
    if not content.strip():
        raise ValueError("content 是空的")
    path = Path(filename).expanduser()
    if not path.is_absolute() and path.parent == Path("."):
        path = settings.minutes_dir / path
    if path.suffix.lower() != ".md":
        path = path.with_suffix(path.suffix + ".md") if path.suffix else path.with_suffix(".md")
    if path.exists() and not overwrite:
        raise FileExistsError(f"檔案已存在：{path}（要覆蓋請設 overwrite=true）")
    path.parent.mkdir(parents=True, exist_ok=True)
    await anyio.to_thread.run_sync(path.write_text, content, "utf-8")
    return f"已儲存：{path}"


# ---------------------------------------------------------------- prompts


@mcp.prompt()
async def meeting_minutes(
    audio_path: str,
    template: str = "general",
    language: str | None = None,
    extra_instructions: str = "",
) -> str:
    """把一個音檔整理成會議記錄：自動轉逐字稿、套用指定範本。

    - audio_path：音檔路徑
    - template：範本名稱（用 list_templates 查看），預設 general
    - language：語言代碼，留空自動偵測
    - extra_instructions：額外要求，例如「重點放在預算討論」
    """
    # prompt 的例外會被 SDK 蓋成通用訊息，所以錯誤改用文字回傳，LLM 才知道該怎麼修正
    try:
        tpl = _get_template(template, settings.templates_dirs)
        transcript = await anyio.to_thread.run_sync(transcriber.transcribe, Path(audio_path), language)
    except USER_ERRORS as e:
        return (
            f"無法產生會議記錄：{_error_message(e)}\n"
            "請告訴使用者這個問題；可用 list_templates 查看範本，或確認音檔路徑是否正確。"
        )
    return build_minutes_prompt(transcript, tpl, extra_instructions)


# ---------------------------------------------------------------- entry


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="meeting-minutes-mcp", description=__doc__)
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio",
        help="stdio 給 Claude Desktop / Claude Code 用；streamable-http 給遠端 client 用",
    )
    parser.add_argument("--warmup", action="store_true", help="先下載並載入 Whisper 模型後離開，不啟動 server")
    parser.add_argument("--list-templates", action="store_true", help="列出範本後離開")
    args = parser.parse_args(argv)

    if args.list_templates:
        print(list_templates())
        return
    if args.warmup:
        print(f"載入模型 {settings.model}（device={settings.device}, compute_type={settings.compute_type}）…", file=sys.stderr)
        transcriber.load_model()
        print("完成。", file=sys.stderr)
        return
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
