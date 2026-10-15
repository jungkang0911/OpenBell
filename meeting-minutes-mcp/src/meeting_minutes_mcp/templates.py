"""會議記錄範本：一個範本就是一個 .md 檔，開頭用 front matter 描述名稱與用途。

格式：
    ---
    name: general
    title: 一般會議記錄
    description: 適用大多數內部會議
    ---
    （Markdown 骨架，給 LLM 填寫）

新增範本只要把檔案丟進範本資料夾，不用改程式。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "templates"
FRONT_MATTER_DELIM = "---"


@dataclass(frozen=True)
class Template:
    name: str
    title: str
    description: str
    body: str
    path: Path
    builtin: bool


class TemplateError(ValueError):
    pass


def parse_template(text: str, path: Path, builtin: bool = False) -> Template:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        raise TemplateError(f"{path}: 範本必須以 '---' front matter 開頭")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == FRONT_MATTER_DELIM)
    except StopIteration:
        raise TemplateError(f"{path}: front matter 沒有結尾的 '---'") from None

    meta: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            raise TemplateError(f"{path}: front matter 格式錯誤：{raw!r}")
        key, _, value = raw.partition(":")
        meta[key.strip().lower()] = value.strip().strip("\"'")

    name = meta.get("name") or path.stem
    if not name.replace("-", "").replace("_", "").isalnum():
        raise TemplateError(f"{path}: name 只能用英數字、- 和 _：{name!r}")
    body = "\n".join(lines[end + 1 :]).strip("\n") + "\n"
    if not body.strip():
        raise TemplateError(f"{path}: 範本內容是空的")
    return Template(
        name=name,
        title=meta.get("title", name),
        description=meta.get("description", ""),
        body=body,
        path=path,
        builtin=builtin,
    )


def _load_dir(directory: Path, builtin: bool) -> dict[str, Template]:
    found: dict[str, Template] = {}
    if not directory.is_dir():
        return found
    for file in sorted(directory.glob("*.md")):
        tpl = parse_template(file.read_text(encoding="utf-8"), file, builtin=builtin)
        found[tpl.name] = tpl
    return found


def load_templates(extra_dirs: tuple[Path, ...] | list[Path] = ()) -> dict[str, Template]:
    """內建範本先載入，使用者資料夾後載入，同名以後者為準。"""
    templates = _load_dir(BUILTIN_DIR, builtin=True)
    for d in extra_dirs:
        templates.update(_load_dir(d, builtin=False))
    return templates


def get_template(name: str, extra_dirs: tuple[Path, ...] | list[Path] = ()) -> Template:
    templates = load_templates(extra_dirs)
    key = name.strip().lower()
    if key not in templates:
        available = ", ".join(sorted(templates)) or "（無）"
        raise KeyError(f"找不到範本 {name!r}，可用的範本：{available}")
    return templates[key]
