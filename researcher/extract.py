"""Pull facts out of fetched pages, verbatim. Two kinds:
- profile: label/value pairs from 会社概要-style tables (資本金, 従業員数 ...)
- evidence: whole lines that contain a topic's keywords (輸入, 輸出, 採用実績 ...), quoted as-is
Nothing is summarised or interpreted. Every item carries its URL and the pass that found it.
"""
from __future__ import annotations

import re

# 2026-11-05 METHOD.md 4-5：メニュー・見出しの文字を事実にしない（JETRO の「米国関税措置への対応」「政府公共調達データベース」が
# 人型ロボットの事実として brief に入った失敗から）。20字以下・数字なし・読点なしの行はサイトのメニューとみなす。
MENU_RE = re.compile(r"^[^0-9０-９、，,。]{1,20}$")


# 2026-11-05：brief に「BRAND VOICE・Paid Program」（広告枠の見出し）や投資信託の手数料の免責文が業界の事実として入った失敗から。
# 広告・タイアップ・免責・問い合わせの定型文は事実にしない（METHOD.md 4-21）
BOILER_RE = re.compile(r"(Paid Program|BRAND VOICE|Sponsored|タイアップ|広告掲載|手数料がかか|お問い合わせ(下|くだ)さい|免責|投資勧誘|将来の運用成果|元本(割れ|保証)|Copyright|All rights reserved|利用規約|プライバシーポリシー|Cookie)", re.I)


def _is_menu(ln: str) -> bool:
    return bool(MENU_RE.match(ln)) or bool(BOILER_RE.search(ln))


HISTORY_RE = re.compile(r"^(?:19|20)\d{2}\s*年|^(?:昭和|平成|令和)\s*(?:元|\d{1,2})\s*年")


def _lines(text: str) -> list[str]:
    out = []
    for ln in re.split(r"[\n。]", text or ""):
        ln = re.sub(r"\s+", " ", ln).strip()
        if 6 <= len(ln) <= 400:
            out.append(ln)
    return out


# 2026-11-05 METHOD.md 4-8：欄ごとの値の型を確かめる（mfg_recruit_cos で株主欄に「10,018名」、従業員欄に「541,700 1.61」が入った失敗から）
YEAR_IN = re.compile(r"(?:18|19|20)\d{2}|(?:明治|大正|昭和|平成|令和)\s*(?:元|\d{1,2})")
COUNT_ONLY = re.compile(r"^[\s約]*[\d,，]+\s*(?:名|人|社)?\s*(?:[（(].*)?$")
BAD_LABEL = re.compile(r"株主数|株主総会|株主優待|株主様|株主の皆様|株主還元|発行済|従業員の声|社員の声|売上ランキング")


def valid_value(field: str, label: str, value: str) -> bool:
    if BAD_LABEL.search(label):
        return False
    v = value.strip()
    if "株主" in field or "出資" in field or "親会社" in field:
        return not COUNT_ONLY.match(v) and not v.startswith(("http", "IR", "株主"))  # 人数・株数だけは株主構成ではない
    if any(k in field for k in ("創業", "設立", "創立")):
        return bool(YEAR_IN.search(v))
    if any(k in field for k in ("従業員", "社員", "人数")):
        return bool(re.search(r"\d[\d,]*\s*(?:名|人)", v))
    if any(k in field for k in ("資本金", "売上", "年商")):
        return bool(re.search(r"\d", v)) and bool(re.search(r"円|ドル|USD|\$|€|EUR", v))
    return True


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
                if not valid_value(field, label_n, value):
                    break
                val = value.strip()[:500]
                # 創業と設立は別の年。どちらの言葉で書かれていたかを値に残す（取り違え防止）
                if any(k in field for k in ("創業", "設立", "創立")) and not re.match(r"^(設立|創業|創立)", val):
                    val = f"{label.strip()}：{val}"
                found.setdefault(field, []).append({"label": label.strip(), "value": val,
                                                    "url": page["final_url"], "pass": pass_no})
                break
    return found


def evidence(page: dict, cats: dict[str, list[str]], pass_no: int, per_cat: int = 25) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    lines = _lines(page.get("text", ""))
    for cat, kws in cats.items():
        hits = []
        for ln in lines:
            if cat != "沿革" and _is_menu(ln):
                continue
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
