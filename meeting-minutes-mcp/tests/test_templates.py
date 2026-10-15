from pathlib import Path

import pytest

from meeting_minutes_mcp.templates import TemplateError, get_template, load_templates, parse_template

BUILTIN = {"general", "standup", "decision", "interview"}


def test_builtin_templates_load():
    templates = load_templates()
    assert BUILTIN <= set(templates)
    for t in templates.values():
        assert t.builtin
        assert t.title and t.description
        assert "待確認" in t.body


def test_parse_front_matter():
    text = "---\nname: weekly\ntitle: 週會\ndescription: 每週一次\n---\n# 週會\n\n內容\n"
    t = parse_template(text, Path("weekly.md"))
    assert (t.name, t.title, t.description) == ("weekly", "週會", "每週一次")
    assert t.body == "# 週會\n\n內容\n"


def test_name_defaults_to_filename():
    t = parse_template("---\ntitle: x\n---\nbody\n", Path("my-template.md"))
    assert t.name == "my-template"


@pytest.mark.parametrize(
    "text",
    [
        "no front matter\n",
        "---\nname: x\nbody without closing\n",
        "---\nname: bad name!\n---\nbody\n",
        "---\nname: empty\n---\n\n",
        "---\nnot a key value\n---\nbody\n",
    ],
)
def test_invalid_templates_raise(text):
    with pytest.raises(TemplateError):
        parse_template(text, Path("bad.md"))


def test_user_dir_overrides_builtin(tmp_path):
    (tmp_path / "general.md").write_text("---\nname: general\ntitle: 我的版本\n---\n自訂內容\n", encoding="utf-8")
    (tmp_path / "weekly.md").write_text("---\ntitle: 週會\n---\n週會內容\n", encoding="utf-8")
    templates = load_templates([tmp_path])
    assert templates["general"].title == "我的版本"
    assert not templates["general"].builtin
    assert templates["weekly"].body == "週會內容\n"
    assert templates["standup"].builtin


def test_get_template_unknown_lists_available():
    with pytest.raises(KeyError, match="general"):
        get_template("nope")
    assert get_template("GENERAL").name == "general"
