"""透過真正的 MCP stdio client 連上 server，驗證 tools / prompt 的對外行為。

Whisper 模型用假的取代（環境變數 MINUTES_FAKE_MODEL=1 由 conftest 之外的 sitecustomize 不好做，
所以這裡改用 monkeypatch 在子行程裡載入 fake：用 -c 啟動並 patch Transcriber.load_model）。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

FAKE_BOOT = """
import sys
from types import SimpleNamespace
from meeting_minutes_mcp import transcriber as tr
class FakeModel:
    def transcribe(self, path, **kw):
        segs = iter([SimpleNamespace(start=0.0, end=3.0, text="大家好，"), SimpleNamespace(start=3.0, end=9.0, text="今天討論預算。")])
        return segs, SimpleNamespace(language="zh", language_probability=0.95, duration=9.0)
tr.Transcriber.load_model = lambda self: FakeModel()
from meeting_minutes_mcp.server import main
main()
"""


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


@pytest.fixture
def session_params(tmp_path):
    custom = tmp_path / "templates"
    custom.mkdir()
    (custom / "weekly.md").write_text("---\ntitle: 週會\ndescription: 自訂\n---\n# 週會\n{內容}\n", encoding="utf-8")
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", FAKE_BOOT],
        env={
            "MINUTES_DATA_DIR": str(tmp_path / "data"),
            "MINUTES_TEMPLATES_DIR": str(custom),
            "MINUTES_MODEL": "tiny",
            "PATH": "",
        },
    )


@pytest.mark.anyio
async def test_end_to_end(session_params, tmp_path):
    async with stdio_client(session_params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()

            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == {"upload_audio", "transcribe_audio", "list_templates", "get_template", "save_minutes"}
            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert prompts == {"meeting_minutes"}

            listing = _text(await s.call_tool("list_templates", {}))
            assert "general" in listing and "weekly" in listing and "自訂" in listing

            tpl = _text(await s.call_tool("get_template", {"name": "weekly"}))
            assert "# 週會" in tpl

            bad = await s.call_tool("get_template", {"name": "nope"})
            assert bad.is_error and "找不到範本" in _text(bad)

            up = await s.call_tool(
                "upload_audio",
                {"filename": "../../會議 錄音.mp3", "data_base64": base64.b64encode(b"audio").decode()},
            )
            assert not up.is_error
            saved = Path(_text(up).split("：", 1)[1].split("（")[0])
            assert saved.is_file() and saved.parent == tmp_path / "data" / "uploads"
            assert saved.name.endswith("-會議_錄音.mp3")

            bad_ext = await s.call_tool("upload_audio", {"filename": "x.exe", "data_base64": "AAAA"})
            assert bad_ext.is_error

            tr = _text(await s.call_tool("transcribe_audio", {"audio_path": str(saved), "language": "zh"}))
            assert "[00:00:03] 今天討論預算。" in tr and "語言：zh" in tr

            tr2 = _text(await s.call_tool("transcribe_audio", {"audio_path": str(saved), "timestamps": False}))
            assert "大家好，今天討論預算。" in tr2

            prompt = await s.get_prompt("meeting_minutes", {"audio_path": str(saved), "template": "weekly", "extra_instructions": "重點放預算"})
            body = prompt.messages[0].content.text
            assert "# 週會" in body and "[00:00:00] 大家好，" in body and "重點放預算" in body
            assert "使用快取" in body  # 第二次用同一檔案

            bad_prompt = await s.get_prompt("meeting_minutes", {"audio_path": str(saved), "template": "nope"})
            assert "找不到範本 'nope'" in bad_prompt.messages[0].content.text

            out = await s.call_tool("save_minutes", {"content": "# 記錄\n", "filename": "0101"})
            assert not out.is_error
            path = Path(_text(out).split("：", 1)[1])
            assert path == tmp_path / "data" / "minutes" / "0101.md" and path.read_text(encoding="utf-8") == "# 記錄\n"

            dup = await s.call_tool("save_minutes", {"content": "x", "filename": "0101"})
            assert dup.is_error and "已存在" in _text(dup)
            ok = await s.call_tool("save_minutes", {"content": "y", "filename": str(path), "overwrite": True})
            assert not ok.is_error and path.read_text(encoding="utf-8") == "y"
