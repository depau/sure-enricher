import re
from pathlib import Path

from spe.i18n import IT, Tr
from spe.web import JS_STRINGS

SRC = Path(__file__).parent.parent / "src" / "spe"
# _("…") / _('…') / _.n("…", "…") in templates; tr("…") / tr.n("…", "…") in Python
CALLS = re.compile(r"""(?:\b_|\btr)(?:\.n)?\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')(?:\s*,\s*"((?:[^"\\]|\\.)*)")?""")


def used_strings() -> set[str]:
    found = set(JS_STRINGS)
    for f in [*SRC.glob("templates/*.html"), *(p for p in SRC.glob("*.py") if p.name != "i18n.py")]:
        for m in CALLS.finditer(f.read_text()):
            found.update(x.replace('\\"', '"') for x in m.groups() if x)
    return found


def test_every_ui_string_has_an_italian_translation():
    missing = sorted(s for s in used_strings() if s not in IT)
    assert missing == []


def test_translations_keep_their_placeholders():
    def names(s: str) -> list[str]:
        return sorted(re.findall(r"%\((\w+)\)s", s))
    assert [k for k, v in IT.items() if names(k) != names(v)] == []


def test_language_follows_the_sure_locale_and_english_is_the_source():
    assert Tr("it")("Review %(n)s changes", n=3) == "Rivedi 3 modifiche"
    assert Tr("en").n("1 line", "%(n)s lines", 2) == "2 lines"
