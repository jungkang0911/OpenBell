"""用 faster-whisper 在本機把音檔轉成逐字稿，結果以檔案內容雜湊快取。

同一個音檔換不同範本、或重開 client，都不用重新辨識。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import Settings

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".webm",
    ".mp4", ".mov", ".mkv", ".m4v", ".amr", ".aiff", ".aif",
}
CACHE_FORMAT = 1


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    source: str
    model: str
    language: str
    language_probability: float
    duration: float
    segments: list[Segment]
    cached: bool = False

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments).strip()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("cached", None)
        data["format"] = CACHE_FORMAT
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], cached: bool = True) -> "Transcript":
        return cls(
            source=data["source"],
            model=data["model"],
            language=data["language"],
            language_probability=float(data["language_probability"]),
            duration=float(data["duration"]),
            segments=[Segment(**s) for s in data["segments"]],
            cached=cached,
        )

    def render(self, timestamps: bool = True) -> str:
        """給 LLM 讀的純文字逐字稿。有時間戳才能在會議記錄裡標「待確認」位置。"""
        if not self.segments:
            return "（沒有辨識到任何語音）"
        if not timestamps:
            return self.text
        return "\n".join(f"[{format_time(s.start)}] {s.text.strip()}" for s in self.segments)


def format_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Transcriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Any = None
        self._lock = threading.Lock()

    # -- 模型 -------------------------------------------------------------

    def load_model(self) -> Any:
        """第一次呼叫才載入模型（第一次會從 Hugging Face 下載，之後都用本機快取）。"""
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    self.settings.model,
                    device=self.settings.device,
                    compute_type=self.settings.compute_type,
                )
            return self._model

    # -- 快取 -------------------------------------------------------------

    def _cache_path(self, digest: str, language: str | None) -> Path:
        key = f"{digest}-{self.settings.model}-{language or 'auto'}.json"
        return self.settings.cache_dir / key

    def _read_cache(self, path: Path) -> Transcript | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format") != CACHE_FORMAT:
                return None
            return Transcript.from_dict(data, cached=True)
        except (ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, path: Path, transcript: Transcript) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # -- 轉文字 -----------------------------------------------------------

    def transcribe(self, audio_path: Path, language: str | None = None, use_cache: bool = True) -> Transcript:
        audio_path = Path(audio_path).expanduser()
        if not audio_path.is_file():
            raise FileNotFoundError(f"找不到音檔：{audio_path}")
        if audio_path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError(
                f"不支援的副檔名 {audio_path.suffix!r}，支援：{', '.join(sorted(AUDIO_EXTENSIONS))}"
            )

        language = (language or self.settings.language or None) or None
        if language:
            language = language.strip().lower() or None

        cache_path = self._cache_path(file_digest(audio_path), language)
        if use_cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                return cached

        transcript = self._run_model(audio_path, language)
        self._write_cache(cache_path, transcript)
        return transcript

    def _run_model(self, audio_path: Path, language: str | None) -> Transcript:
        model = self.load_model()
        kwargs: dict[str, Any] = {
            "language": language,
            "beam_size": 5,
            # 跳過長時間靜音，會議錄音常見，可大幅減少亂猜的幻覺句
            "vad_filter": True,
            "vad_parameters": {"min_silence_duration_ms": 700},
            "condition_on_previous_text": False,
        }
        if language and language.startswith("zh") and self.settings.initial_prompt_zh:
            kwargs["initial_prompt"] = self.settings.initial_prompt_zh

        segments_iter, info = model.transcribe(str(audio_path), **kwargs)
        segments = [
            Segment(start=float(s.start), end=float(s.end), text=s.text)
            for s in segments_iter
            if s.text and s.text.strip()
        ]
        return Transcript(
            source=str(audio_path),
            model=self.settings.model,
            language=info.language,
            language_probability=float(info.language_probability),
            duration=float(info.duration),
            segments=segments,
            cached=False,
        )
