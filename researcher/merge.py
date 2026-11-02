"""Build the readable outputs of a topic from the per-company JSON files:
researcher/out/<topic>/facts.md (company by company, quotes + URLs) and summary.csv.
Facts only; no ranking, no interpretation.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from researcher.targets import ROOT, load_topic


def first(rec: dict, field: str) -> str:
    v = rec.get("profile", {}).get(field) or []
    return v[0]["value"] if v else ""


def main(argv=None) -> int:
    topic_name = (argv or sys.argv[1:])[0]
    topic = load_topic(topic_name)
    base = ROOT / "out" / topic_name
    recs = []
    for p in sorted((base / "companies").glob("*.json")):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    if topic["kind"] == "market":
        return merge_market(topic_name, topic, base, recs)
    keys, cats = list(topic["profile_keys"]), list(topic["evidence"])
    with open(base / "summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "特定方法"] + keys + [f"{c}_件数" for c in cats] + ["記載なし項目", "エラー"])
        for r in recs:
            ev = r.get("evidence", {})
            w.writerow([r.get("name"), r.get("official_url", ""), r.get("identified_by", "")] + [first(r, k) for k in keys]
                       + [len(ev.get(c, [])) for c in cats] + ["／".join(r.get("missing", {})), r.get("error", "")])
    lines = [f"# {topic.get('title', topic_name)}（事実のみ・{len(recs)}社）", ""]
    for r in recs:
        lines += [f"## {r.get('name')}", f"- 公式URL：{r.get('official_url') or '特定できず'}（{r.get('identified_by', '')}）"]
        if r.get("error"):
            lines.append(f"- エラー：{r['error']}")
        for k in keys:
            for it in r.get("profile", {}).get(k, [])[:3]:
                lines.append(f"- {k}：{it['value']}（{it['url']}・{it['pass']}回目）")
        for c in cats + ["検索結果の抜粋"]:
            items = r.get("evidence", {}).get(c, [])
            if items:
                lines.append(f"- {c}：")
                lines += [f"  - 「{it['quote']}」（{it['url']}・{it['pass']}回目）" for it in items[:12]]
        for k, v in r.get("missing", {}).items():
            lines.append(f"- {k}：{v}")
        lines.append("")
    (base / "facts.md").write_text("\n".join(lines), encoding="utf-8")
    if topic.get("sheet"):
        write_sheet(topic, base, recs)
    print(f"merged {len(recs)}")
    return 0


def write_sheet(topic: dict, base: Path, recs: list[dict]) -> None:
    """Draft sheet for the person to finish by hand. One row per target in input order,
    including ones not researched yet, so rows never move: his own columns placed to
    the right of an IMPORTDATA of this file stay aligned as results come in."""
    from researcher.targets import load_targets

    by_name = {r.get("name"): r for r in recs}
    slots = topic["sheet"]
    head = ["社名", "公式URL", "文脈"] + [f"{label}（下書き・引用）" for label, _ in slots] + ["根拠URL", "状態"]
    with open(base / "sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        for t in load_targets(topic):
            r = by_name.get(t["name"])
            if not r:
                w.writerow([t["name"], t["url"], t["context"]] + [""] * len(slots) + ["", "未調査"])
                continue
            ev, urls = r.get("evidence", {}), []
            row = [t["name"], r.get("official_url", ""), t["context"]]
            for _, cat in slots:
                items = list({i["quote"]: i for i in ev.get(cat, [])}.values())[:4]
                urls += [i["url"] for i in items]
                row.append("\n".join(f"「{i['quote'][:160]}」" for i in items))
            miss = "／".join(r.get("missing", {}))
            row += ["\n".join(dict.fromkeys(urls)), ("エラー" if r.get("error") else "調査済み") + (f"（記載なし：{miss}）" if miss else "")]
            w.writerow(row)


def merge_market(topic_name: str, topic: dict, base: Path, recs: list[dict]) -> int:
    """One row per item: every figure found, each with its URL count; facts.md quotes all lines."""
    order = {r["name"]: i for i, r in enumerate(__import__("researcher.targets", fromlist=["x"]).load_targets(topic))}
    recs.sort(key=lambda r: order.get(r.get("name"), 9999))
    cats = list(topic["evidence"])
    figs = list(topic.get("figure_patterns", {}))
    with open(base / "summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["品目", "英語名", "文脈"] + [f"{g}（値・出典数）" for g in figs] + [f"{c}_引用数" for c in cats]
                   + ["読んだ頁数", "記載なし項目", "エラー"])
        for r in recs:
            row = [r.get("name"), r.get("en", ""), r.get("context", "")]
            for g in figs:
                vals: dict[str, set] = {}
                for it in r.get("figures", {}).get(g, []):
                    vals.setdefault(it["value"], set()).add(it["url"])
                row.append(" / ".join(f"{v}({len(u)})" for v, u in sorted(vals.items(), key=lambda x: -len(x[1]))[:8]))
            ev = r.get("evidence", {})
            row += [len(ev.get(c, [])) for c in cats]
            row += [len(r.get("checked", {}).get("pass3", [])), "／".join(r.get("missing", {})), r.get("error", "")]
            w.writerow(row)
    lines = [f"# {topic.get('title', topic_name)}（事実のみ・{len(recs)}品目）", "",
             "数字は引用元の文そのまま。調査会社ごとに範囲・期間が違う。順位づけ・解釈はしていない。", ""]
    for r in recs:
        lines += [f"## {r.get('name')}" + (f"（{r['en']}）" if r.get("en") else ""), f"- 文脈：{r.get('context', '')}"]
        if r.get("error"):
            lines.append(f"- エラー：{r['error']}")
        for c in cats + ["検索結果の抜粋"]:
            items = r.get("evidence", {}).get(c, [])
            if items:
                lines.append(f"- {c}：")
                lines += [f"  - 「{it['quote']}」（{it['url']}）" for it in items[:15]]
        for k, v in r.get("missing", {}).items():
            lines.append(f"- {k}：{v}")
        lines.append("")
    (base / "facts.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"merged {len(recs)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
