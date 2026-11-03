"""Company-name variants, so a company listed under one spelling is still found
under another (全角/半角, 株式会社の有無・位置, 英字/カタカナの別名, Japan/日本/K.K.).
Deterministic; no AI.
"""
from __future__ import annotations

import re
import unicodedata

_CORP = ["株式会社", "（株）", "(株)", "㈱", "有限会社", "合同会社", "K.K.", "KK", "Co., Ltd.", "Co.,Ltd.", "Inc.", "Ltd.", "Corporation", "Corp."]
_PAREN = re.compile(r"[（(]([^）)]*)[）)]")


def to_full(s: str) -> str:
    out = []
    for ch in s:
        o = ord(ch)
        if ch == " ":
            out.append("　")
        elif 0x21 <= o <= 0x7E:
            out.append(chr(o + 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def to_half(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def strip_corp(s: str) -> str:
    t = s
    for c in _CORP:
        t = t.replace(c, " ")
    return re.sub(r"\s+", " ", t).strip(" ・,")


def base_names(name: str, aliases: list[str] | None = None) -> list[str]:
    """The distinct names a company goes by: the listed name, anything in brackets,
    parts split by '/', and given aliases."""
    raw = [name] + list(aliases or [])
    names: list[str] = []
    for r in raw:
        r = to_half(r or "").strip()
        if not r:
            continue
        parts = [r]
        parts += [p for p in _PAREN.findall(r)]
        parts.append(_PAREN.sub(" ", r))
        for p in list(parts):
            parts += re.split(r"[/／;；]| or ", p)
        for p in parts:
            p = strip_corp(p)
            p = re.sub(r"[\s　]+", " ", p).strip()
            if len(p) >= 2 and p not in names and not re.fullmatch(r"[\W\d]+", p):
                names.append(p)
    return names


def search_variants(name: str, aliases: list[str] | None = None, limit: int = 8) -> list[str]:
    out: list[str] = []
    for b in base_names(name, aliases):
        cands = [b, f"株式会社{b}", f"{b} 株式会社", to_full(b), b.replace(" ", ""), b.replace("・", "")]
        if re.search(r"[A-Za-z]", b):
            cands += [f"{b} Japan", f"{b} 日本", f"{b} K.K."]
        for c in cands:
            c = c.strip()
            if c and c not in out:
                out.append(c)
    return out[:limit]


def name_hits(text: str, name: str, aliases: list[str] | None = None) -> bool:
    """Does the text mention the company under any of its names (width-insensitive)?"""
    t = to_half(text or "").lower().replace(" ", "")
    for b in base_names(name, aliases):
        k = b.lower().replace(" ", "")
        if len(k) >= 2 and k in t:
            return True
    return False


def phrase_hits(text: str, name: str, aliases: list[str] | None = None) -> bool:
    """Market items: True if any name/alias appears, or if every word of a multi-word
    alias (split on spaces) appears somewhere in the text. Aliases like
    '金属製品製造業 事業所数 規模別' are search phrases, not contiguous strings."""
    if name_hits(text, name, aliases):
        return True
    t = to_half(text or "").lower()
    for a in [name] + list(aliases or []):
        words = [w for w in re.split(r"[\s　]+", to_half(a or "").lower()) if len(w) >= 2]
        if len(words) >= 2 and all(w in t for w in words):
            return True
    return False
