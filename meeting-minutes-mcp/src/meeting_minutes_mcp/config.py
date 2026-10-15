"""所有設定都從環境變數讀取（前綴 MINUTES_），方便寫在 MCP client 的 env 欄位。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_PREFIX = "MINUTES_"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _default_data_dir() -> Path:
    return Path(_env("DATA_DIR", str(Path.home() / ".meeting-minutes-mcp")))


@dataclass(frozen=True)
class Settings:
    # Whisper 模型：tiny / base / small / medium / large-v3 / distil-large-v3 …
    # 中文建議 medium 以上；純 CPU 想快一點可用 small。
    model: str = "medium"
    # cpu / cuda / auto
    device: str = "auto"
    # int8 / float16 / auto（auto 會依裝置挑最快的）
    compute_type: str = "auto"
    # 預設語言，留空自動偵測；台灣會議建議設 zh
    language: str | None = None
    # 語言為中文時給 Whisper 的提示詞，可引導輸出繁體
    initial_prompt_zh: str = "以下是一場會議的逐字稿，使用繁體中文。"
    # 自訂範本資料夾（可多個，用 os.pathsep 分隔）；同名會覆蓋內建範本
    templates_dirs: tuple[Path, ...] = field(default_factory=tuple)
    data_dir: Path = field(default_factory=_default_data_dir)

    @property
    def cache_dir(self) -> Path:
        return Path(_env("CACHE_DIR", str(self.data_dir / "cache")))

    @property
    def upload_dir(self) -> Path:
        return Path(_env("UPLOAD_DIR", str(self.data_dir / "uploads")))

    @property
    def minutes_dir(self) -> Path:
        return Path(_env("OUTPUT_DIR", str(self.data_dir / "minutes")))


def load_settings() -> Settings:
    dirs_raw = _env("TEMPLATES_DIR", "")
    dirs = tuple(Path(p).expanduser() for p in dirs_raw.split(os.pathsep) if p.strip()) if dirs_raw else ()
    return Settings(
        model=_env("MODEL", "medium") or "medium",
        device=_env("DEVICE", "auto") or "auto",
        compute_type=_env("COMPUTE_TYPE", "auto") or "auto",
        language=_env("LANGUAGE", None),
        initial_prompt_zh=_env("INITIAL_PROMPT_ZH", Settings.initial_prompt_zh) or "",
        templates_dirs=dirs,
        data_dir=_default_data_dir(),
    )
