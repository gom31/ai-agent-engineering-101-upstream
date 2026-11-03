"""The system card: one document every agent reads, whatever its model (HARNESS.md 1).

card/system_card.md is cut into sections on its "## " headings. assemble() puts together what one
reader gets, in a fixed order so the unchanging part comes first and prompt caching can hit (C1):

    core sections -> the reader's role -> intent (framed for that reader) -> the active priorities

The intent is the same text for everyone; only its frame differs (HARNESS.md 1): executors follow it,
the strategy agent develops it, the audit agent verifies it.
"""
import re
from pathlib import Path

from core import paths

FRAMES = {
    "executor": "The user's intent below is the direction to follow.",
    "strategist": "The user's intent below is your starting point; develop it from the records.",
    "auditor": "The user's intent and the current hypothesis below are claims to verify against the scoring "
               "formula and the data, not directions to follow.",
}


def card_path() -> Path:
    return paths.root() / "card" / "system_card.md"


def parse(text):
    """(card_version, {section name: text}) from the card's front matter and '## ' headings."""
    m = re.search(r"^card_version:\s*(\d+)", text, re.M)
    version = int(m.group(1)) if m else None
    body = re.sub(r"<!--.*?-->", "", text.split("\n---", 2)[-1] if text.startswith("---") else text, flags=re.S)
    sections = {}
    for chunk in re.split(r"^## ", body, flags=re.M)[1:]:
        title, _, content = chunk.partition("\n")
        sections[title.strip()] = content.strip()
    return version, sections


def load():
    return parse(card_path().read_text(encoding="utf-8"))


def section(name):
    """One section by name, for memory.card_section."""
    _, s = load()
    if name not in s:
        raise KeyError(f"no section {name!r}; sections: {sorted(s)}")
    return s[name]


def assemble(role: str, reader: str = "executor", priorities=None):
    """(card_version, text) for one reader. role is a 'role: ...' section name without the prefix."""
    version, s = load()
    if f"role: {role}" not in s:
        raise KeyError(f"no role {role!r}; roles: {sorted(k[6:] for k in s if k.startswith('role: '))}")
    if reader not in FRAMES:
        raise KeyError(f"reader must be one of {sorted(FRAMES)}")
    parts = [f"# {k}\n{v}" for k, v in s.items() if k.startswith("core: ")]
    parts.append(f"# your role: {role}\n{s['role: ' + role]}")
    parts.append(f"# intent\n{FRAMES[reader]}\n\n{s['intent']}")
    if priorities:
        parts.append("# current priorities (strategy in force)\n" + "\n".join(f"- {p}" for p in priorities))
    return version, "\n\n".join(parts)
