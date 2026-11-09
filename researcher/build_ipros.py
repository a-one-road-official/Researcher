"""イプロス掲載企業から、採用・広告の支援を売る営業先の名簿を作る（2026-11-10 本人指示）。

本人の条件（2026-11-10 訂正）：①製造業 ②売上1億円以上 ③有料の求人を出している、の3つのうち2つ以上で名簿に入れる。
自動車は除外しない（手がかりは記録だけ）。求人媒体に載せていなくても採用に困っている会社はあるので、1つ欠けても落とさない。
Claude は1社ずつ読まない。判定と門番（verify_list）はすべてここ（GitHub）で回し、Claude は verify.md の集計だけを見る。

段階
  A. 種：mono.ipros.com の企業検索を業種（business_class）ごとに全ページめくり、企業ID・社名・業種を取る（業種で落とさない。並び順だけ製造らしい業種を先に）。
  B. 1社ずつ：イプロスの企業ページで公式サイトを取り、公式サイトの会社概要を読む（build_award_chain.check_one を流用：
     資本金・所在地・事業内容・取引先・売上・問い合わせ先・メール・製造業の判定〈3つの手がかりのうち2つ〉・自動車の手がかり）。
     有料の求人：検索で、有料の求人媒体（doda・マイナビ・リクナビ・エン転職・type・ビズリーチ・Green など）に社名つきの求人が出ているか。
  C. 門番：4条件（製造業・売上1億円以上・自動車でない・有料求人あり）を1社ずつ引用で確かめ、1つでも欠ける会社は名簿に入れない。
  出力：out/ipros_hr/seeds.csv・checked.csv（調べた全社）・list.csv（通過だけ）・verify.md・stats.md、inputs/ipros_hr.csv
時間で区切り、次の回に続きから回る。
"""
from __future__ import annotations

import csv
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

from researcher.targets import ROOT

OUT = ROOT / "out" / "ipros_hr"
SEEDS = OUT / "seeds.csv"
CHECKED = OUT / "checked.csv"
INPUT = ROOT / "inputs" / "ipros_hr.csv"
BASE = "https://mono.ipros.com"
SEED_FIELDS = ["ipros_id", "name", "business_class", "class_name"]
PAID_HOSTS = re.compile(r"doda\.jp|mynavi\.jp|rikunabi\.com|en-japan\.com|type\.jp|bizreach\.jp|green-japan\.com|"
                        r"r-agent\.com|careerindex\.jp|hatalike\.jp|baitoru\.com|townwork\.net|jobchange\.jp|kikan\.jp|wantedly\.com")
MFG_FIRST = re.compile(r"製造|メーカー|機械|部品|金属|加工|電子|電機|化学|樹脂|素材|材料|装置|設備|工業|鋳|鍛|めっき|プレス|金型|半導体")
FIELDS = ["ipros_id", "name", "class_name", "status", "official_url", "capital_man", "capital_quote", "pref", "address_quote",
          "business_quote", "revenue_man", "revenue_quote", "mfg_industry", "mfg_signals", "mfg_judge", "auto_signals", "auto_judge",
          "paid_job", "paid_job_quote", "contact_url", "emails", "met", "judge"]


def _read(p) -> list[dict]:
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(p, rows, fields) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def collect_seeds(deadline: float) -> bool:
    from researcher.fetch import fetch
    have = {r["ipros_id"]: r for r in _read(SEEDS)}
    done_cls = {r["business_class"] for r in _read(OUT / "classes_done.csv")}
    top = fetch(f"{BASE}/search/company/")
    classes = {}
    for label, href in top.get("links") or []:
        m = re.search(r"business_class=(\d+)", href or "")
        if m:
            classes.setdefault(m.group(1), re.sub(r"\s*\(?\d[\d,]*\)?\s*$", "", (label or "").strip()))
    order = sorted(classes, key=lambda c: (0 if MFG_FIRST.search(classes[c]) else 1, int(c)))
    for c in order:
        if c in done_cls:
            continue
        page = 1
        while True:
            if time.monotonic() > deadline:
                _write(SEEDS, list(have.values()), SEED_FIELDS)
                return False
            p = fetch(f"{BASE}/search/company/?business_class={c}&page={page}")
            new = 0
            for label, href in p.get("links") or []:
                m = re.search(r"/company/detail/(\d+)/?$", href or "")
                nm = (label or "").strip()
                if m and nm and len(nm) >= 2 and m.group(1) not in have:
                    have[m.group(1)] = {"ipros_id": m.group(1), "name": nm, "business_class": c, "class_name": classes[c]}
                    new += 1
            if new == 0 or page >= 500:
                break
            page += 1
        done_cls.add(c)
        _write(SEEDS, list(have.values()), SEED_FIELDS)
        _write(OUT / "classes_done.csv", [{"business_class": x} for x in done_cls], ["business_class"])
    return bool(classes)


def official_from_ipros(ipros_id: str) -> str:
    from researcher.fetch import fetch
    p = fetch(f"{BASE}/company/detail/{ipros_id}/")
    for label, href in p.get("links") or []:
        host = urlparse(href or "").netloc
        if host and "ipros" not in host and re.search(r"公式|ホームページ|Webサイト|ウェブサイト|URL|http", (label or "") + " " + href):
            if not re.search(r"facebook|twitter|x\.com|youtube|instagram|linkedin|google", host):
                return f"{urlparse(href).scheme}://{host}/"
    return ""


def paid_job(name: str) -> tuple[str, str]:
    from researcher.build_award_chain import cmp_key
    from researcher.search import search
    for q in (f"{name} 求人 中途採用", f"{name} 新卒採用 求人"):
        for h in search(q, 10):
            if PAID_HOSTS.search(h.get("url", "")) and cmp_key(name) in cmp_key(h.get("title", "") + h.get("snippet", "")):
                return "あり", f"{h.get('title', '')[:80]}（{h['url'][:100]}）"
    return "", ""


CONDS = {
    "製造業": (lambda r: r.get("mfg_judge") == "製造業", lambda r: r.get("mfg_signals", "")),
    "売上1億円以上": (lambda r: float(r.get("revenue_man") or 0) >= 10000 and bool(r.get("revenue_quote")), lambda r: r.get("revenue_quote", "")),
    "有料求人あり": (lambda r: r.get("paid_job") == "あり" and bool(r.get("paid_job_quote")), lambda r: r.get("paid_job_quote", "")),
}


def judge(r: dict) -> str:
    if r.get("status") != "読了":
        return "未判定（" + (r.get("status") or "未読") + "）"
    met = [c for c, (ok, _) in CONDS.items() if ok(r)]
    r["met"] = "・".join(met)
    return ("対象（" if len(met) >= 2 else "外す（") + f"{len(met)}/3：" + ("・".join(met) or "なし") + "）"


def run_checks(deadline: float) -> bool:
    from researcher.build_award_chain import check_one
    seeds = _read(SEEDS)
    done = {r["ipros_id"]: r for r in _read(CHECKED)}
    seeds.sort(key=lambda s: 0 if MFG_FIRST.search(s.get("class_name", "")) else 1)
    for s in seeds:
        if s["ipros_id"] in done:
            continue
        if time.monotonic() > deadline:
            _write(CHECKED, list(done.values()), FIELDS)
            return False
        try:
            home = official_from_ipros(s["ipros_id"])
            r = check_one(s["name"], 0, "イプロス", home=home) if home else {"status": "公式サイト見つからず"}
            if r.get("status") == "読了":
                r["paid_job"], r["paid_job_quote"] = paid_job(s["name"])
        except Exception as e:
            r = {"status": f"読み取りエラー:{type(e).__name__}"}
        r.update({"ipros_id": s["ipros_id"], "name": s["name"], "class_name": s.get("class_name", "")})
        r["judge"] = judge(r)
        done[s["ipros_id"]] = r
        print(f"ipros {s['name']}: {r['judge']}", flush=True)
        if len(done) % 10 == 0:
            _write(CHECKED, list(done.values()), FIELDS)
    _write(CHECKED, list(done.values()), FIELDS)
    return len(done) >= len(seeds) > 0


def write_outputs(finished: bool) -> None:
    from researcher.verify_list import verify
    rows = _read(CHECKED)
    for r in rows:
        if r.get("status") == "読了":
            r["judge"] = judge(r)  # 条件を変えたら、調べ直さずに判定だけやり直す
    _write(CHECKED, rows, FIELDS)
    cand = [r for r in rows if r.get("judge", "").startswith("対象")]
    ok = verify(cand, {"3条件のうち2つ以上（引用つき）": (
        lambda r: sum(1 for ok_, _ in CONDS.values() if ok_(r)) >= 2,
        lambda r: " ／ ".join(f"{c}：{q(r)[:60]}" for c, (ok_, q) in CONDS.items() if ok_(r)))}, OUT)
    _write(OUT / "list.csv", ok, FIELDS)
    INPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in ok:
            w.writerow([r["name"], r.get("official_url", ""), "", f"イプロス・{r.get('class_name', '')}・売上{r.get('revenue_man', '')}万円"])
    jc = Counter(re.sub(r"：.*", "", r.get("judge", "")) + ("" if not r.get("judge", "")[:2] in ("対象", "外す") else "") for r in rows)
    mc = Counter(c for r in rows if r.get("status") == "読了" for c, (ok_, _) in CONDS.items() if ok_(r))
    md = [f"# イプロス 製造業・売上1億円以上・有料求人の3つのうち2つ以上{'（完了）' if finished else '（途中）'}", "",
          f"- 種：{len(_read(SEEDS))}社　調べた会社：{len(rows)}社　門番を通った会社：{len(ok)}社", "", "## 判定の内訳（外した理由は重複して数える）"]
    md += [f"- {k}：{v}" for k, v in jc.most_common()]
    md += ["", "## 条件ごとに満たした社数（読了した会社のうち）"] + [f"- {c}：{mc.get(c, 0)}" for c in CONDS]
    (OUT / "stats.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(ok)}\n", encoding="utf-8")


def main(deadline_s: float = 90 * 60) -> int:
    deadline = time.monotonic() + deadline_s
    OUT.mkdir(parents=True, exist_ok=True)
    if not collect_seeds(deadline):
        write_outputs(False)
        return 0
    write_outputs(run_checks(deadline))
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
