import os
import sys
from pathlib import Path

import pytest

# 測試時不碰使用者家目錄
os.environ["MINUTES_DATA_DIR"] = str(Path(__file__).parent / ".data")
os.environ.pop("MINUTES_TEMPLATES_DIR", None)
os.environ.pop("MINUTES_LANGUAGE", None)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINUTES_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"
