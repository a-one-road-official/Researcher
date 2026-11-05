"""Pull facts out of fetched pages, verbatim. Two kinds:
- profile: label/value pairs from 会社概要-style tables (資本金, 従業員数 ...)
- evidence: whole lines that contain a topic's keywords (輸入, 輸出, 採用実績 ...), quoted as-is
Nothing is summarised or interpreted. Every item carries its URL and the pass that found it.
"""
from __future__ import annotations

import re

HISTORY_RE = re.compile(r"^(?:19|20)\d{2}\s*年|^(?:昭和|平成|令和)\s*(?:元|\d{1,2})\s*年")


def _lines(text: str) -> list[str]:
    out = []
    for ln in re.split(r"[\n。]", text or ""):
        ln = re.sub(r"\s+", " ", ln).strip()
        if 6 <= len(ln) <= 400:
            out.append(ln)
    return out


def profile(page: dict, keys: dict[str, list[str]], pass_no: int) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    pairs = list(page.get("pairs") or [])
    for ln in _lines(page.get("text", "")):
        m = re.match(r"^([^\s：:]{2,12})\s*[：:]\s*(.+)$", ln)
        if m:
            pairs.append((m.group(1), m.group(2)))
    for label, value in pairs:
        label_n = re.sub(r"\s", "", label)
        if not value or len(label_n) > 20:
            continue
        for field, syns in keys.items():
            if any(s in label_n for s in syns):
                found.setdefault(field, []).append({"label": label.strip(), "value": value.strip()[:500],
                                                    "url": page["final_url"], "pass": pass_no})
                break
    return found


def evidence(page: dict, cats: dict[str, list[str]], pass_no: int, per_cat: int = 25) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    lines = _lines(page.get("text", ""))
    for cat, kws in cats.items():
        hits = []
        for ln in lines:
            if cat == "沿革" and HISTORY_RE.search(ln):
                hits.append(ln)
            elif any(str(k).lower() in ln.lower() for k in kws):
                hits.append(ln)
        seen = set()
        for h in hits:
            if h in seen:
                continue
            seen.add(h)
            found.setdefault(cat, []).append({"quote": h, "url": page["final_url"], "pass": pass_no})
            if len(found[cat]) >= per_cat:
                break
    return found


def merge_into(dst: dict[str, list[dict]], src: dict[str, list[dict]], cap: int = 60) -> None:
    for k, items in src.items():
        bucket = dst.setdefault(k, [])
        have = {(i.get("value") or i.get("quote"), i["url"]) for i in bucket}
        for it in items:
            key = (it.get("value") or it.get("quote"), it["url"])
            if key not in have and len(bucket) < cap:
                bucket.append(it)
                have.add(key)


def figures(evidence: dict[str, list[dict]], patterns: dict[str, str]) -> dict[str, list[dict]]:
    """Pull the bare figures (e.g. every 'xx.x%' in a CAGR line) out of the quoted lines,
    each still tied to its quote's URL, so the numbers can be lined up across sources.
    patterns: {figure name: "category|regex"}; the regex's first group is the figure."""
    out: dict[str, list[dict]] = {}
    for fname, spec in (patterns or {}).items():
        cat, _, rx = spec.partition("|")
        r = re.compile(rx, re.I)
        seen = set()
        for it in evidence.get(cat, []):
            for m in r.finditer(it["quote"]):
                key = (m.group(1), it["url"])
                if key in seen:
                    continue
                seen.add(key)
                out.setdefault(fname, []).append({"value": m.group(1), "url": it["url"], "quote": it["quote"][:300]})
    return out
