import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meeting_minutes_mcp.config import Settings
from meeting_minutes_mcp.transcriber import Segment, Transcriber, Transcript, format_time


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        segments = iter([
            SimpleNamespace(start=0.0, end=2.5, text=" 大家好，"),
            SimpleNamespace(start=2.5, end=61.0, text="今天討論預算。"),
            SimpleNamespace(start=61.0, end=62.0, text="   "),
        ])
        info = SimpleNamespace(language="zh", language_probability=0.97, duration=62.0)
        return segments, info


@pytest.fixture
def audio(tmp_path):
    f = tmp_path / "meeting.mp3"
    f.write_bytes(b"fake audio bytes")
    return f


@pytest.fixture
def transcriber(tmp_path, monkeypatch):
    monkeypatch.setenv("MINUTES_DATA_DIR", str(tmp_path / "data"))
    t = Transcriber(Settings(model="tiny", language=None))
    t._model = FakeModel()
    return t


def test_format_time():
    assert format_time(0) == "00:00:00"
    assert format_time(61.9) == "00:01:01"
    assert format_time(3725) == "01:02:05"


def test_transcribe_and_render(transcriber, audio):
    t = transcriber.transcribe(audio, language="zh")
    assert not t.cached
    assert t.language == "zh"
    assert [s.text for s in t.segments] == [" 大家好，", "今天討論預算。"]  # 空白段落被丟掉
    assert t.text == "大家好，今天討論預算。"
    assert t.render() == "[00:00:00] 大家好，\n[00:00:02] 今天討論預算。"
    assert t.render(timestamps=False) == "大家好，今天討論預算。"

    _, kwargs = transcriber._model.calls[0]
    assert kwargs["language"] == "zh"
    assert kwargs["vad_filter"] is True
    assert "繁體中文" in kwargs["initial_prompt"]


def test_no_initial_prompt_for_other_languages(transcriber, audio):
    transcriber.transcribe(audio, language="en")
    _, kwargs = transcriber._model.calls[0]
    assert "initial_prompt" not in kwargs
    assert kwargs["language"] == "en"


def test_cache_hit_skips_model(transcriber, audio):
    first = transcriber.transcribe(audio)
    second = transcriber.transcribe(audio)
    assert len(transcriber._model.calls) == 1
    assert second.cached and second.segments == first.segments
    cache_files = list(transcriber.settings.cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    assert json.loads(cache_files[0].read_text(encoding="utf-8"))["language"] == "zh"


def test_cache_keyed_by_content_and_language(transcriber, audio):
    transcriber.transcribe(audio, language="zh")
    transcriber.transcribe(audio, language="en")
    audio.write_bytes(b"different bytes")
    transcriber.transcribe(audio, language="zh")
    assert len(transcriber._model.calls) == 3


def test_missing_file_and_bad_extension(transcriber, tmp_path):
    with pytest.raises(FileNotFoundError):
        transcriber.transcribe(tmp_path / "nope.mp3")
    bad = tmp_path / "notes.txt"
    bad.write_text("x")
    with pytest.raises(ValueError, match="副檔名"):
        transcriber.transcribe(bad)


def test_roundtrip_dict():
    t = Transcript("a.mp3", "tiny", "zh", 0.9, 5.0, [Segment(0, 1, "嗨")])
    assert Transcript.from_dict(t.to_dict()).segments == t.segments
