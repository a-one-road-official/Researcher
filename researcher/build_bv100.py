"""ベストベンチャー100（イシン株式会社主催、https://best100.v-tsushin.jp/）の選出企業を 2016〜2026年度で洗い出す。

本人の指示（2026-11-05）：
- 2016〜2026年の10年間で、どの領域のベンチャーが何社いるかを、事実・数字で出す。
- 領域は経済センサス（日本標準産業分類）の構造でMECEに。ブランク（大きいのにベンチャーが少ない領域）を見る。
- 業種はキーワード一致で付けない。各社の事業内容を一次情報（公式サイト・会社概要）から引用で残す。

段階
  A. 名簿集め：公式サイトを内部リンクでたどり（年度ページ・企業ページ）、ページの題名と本文から社名と年度を抜く。
     加えて「ベストベンチャー100 {年} 選出」の検索結果（選出企業のプレスリリース）からも社名と年度を抜く。
     出典の種類（公式サイト／プレスリリース）とURLを残す。公式サイトで年度が確かめられたものを「確定」とする。
  出力：out/bv100/names.csv（社名・年度・出典・URL）、pages.csv（たどったページ）、stats.md（年度別の社数）、
        inputs/bv100.csv（会社ごとの事業内容・資本金・設立・株主の調査は topics/bv100.yaml）。
時間切れでも途中から再開する（キャッシュCSV）。
"""
from __future__ import annotations

import csv
import re
import time
from collections import defaultdict
from urllib.parse import urljoin, urlparse as _raw

from researcher.targets import ROOT

OUT = ROOT / "out" / "bv100"
NAMES = OUT / "names.csv"
PAGES = OUT / "pages.csv"
DONE_Q = OUT / "queries_done.txt"
INPUT = ROOT / "inputs" / "bv100.csv"
SITE = "best100.v-tsushin.jp"
START = [f"https://{SITE}/", f"https://{SITE}/company/", f"https://{SITE}/companies/", f"https://{SITE}/list/"]
YEARS = list(range(2016, 2027))
MAX_PAGES = 1500

CORP = r"(?:株式会社|有限会社|合同会社|㈱|\(株\)|（株）)"
NAME_CHARS = r"[一-龥ぁ-んァ-ヶーA-Za-zＡ-Ｚａ-ｚ0-9０-９・&＆\.\-]"
PRE_RX = re.compile(rf"{CORP}\s?({NAME_CHARS}{{2,25}})")
POST_RX = re.compile(rf"({NAME_CHARS}{{2,25}}){CORP}")
YEAR_RX = re.compile(r"(20(?:1[6-9]|2[0-6]))\s*(?:年度?|年版)?")
NOT_NAME = re.compile(r"イシン|ベンチャー通信|ベストベンチャー|選出|一覧|ランキング|受賞|お知らせ|プレスリリース|PR TIMES|ニュース")
NAME_FIELDS = ["name", "years", "official_years", "sources", "urls", "mentions"]


def urlparse(u):
    try:
        return _raw(u or "")
    except ValueError:
        return _raw("")


def _norm(n: str) -> str:
    n = re.sub(CORP, "", n)
    n = n.translate(str.maketrans("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９＆",
                                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789&"))
    return n.strip(" ・-.")


def names_in(text: str) -> list[str]:
    out = []
    found = [m.group(1) for m in PRE_RX.finditer(text or "")] + [m.group(1) for m in POST_RX.finditer(text or "")]
    for raw in found:
        core = _norm(raw)
        if len(core) >= 2 and not re.fullmatch(r"[0-9\.\-]+", core) and not NOT_NAME.search(core) and not re.search(r"(の|で|を|に|は|が|と)$", core):
            out.append(core)
    return out


def _years(*texts: str) -> set[str]:
    ys = set()
    for t in texts:
        ys |= set(YEAR_RX.findall(t or ""))
    return ys


def _load() -> dict:
    rows = {}
    if NAMES.exists():
        with open(NAMES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["name"]] = r
    return rows


def _save(rows: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(NAMES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NAME_FIELDS)
        w.writeheader()
        for r in sorted(rows.values(), key=lambda r: (-int(r["mentions"]), r["name"])):
            w.writerow(r)


def _add(rows: dict, name: str, years: set, official: bool, source: str, url: str) -> None:
    r = rows.setdefault(name, {"name": name, "years": "", "official_years": "", "sources": "", "urls": "", "mentions": "0"})
    r["mentions"] = str(int(r["mentions"]) + 1)
    r["years"] = ";".join(sorted(set(filter(None, r["years"].split(";"))) | years))
    if official:
        r["official_years"] = ";".join(sorted(set(filter(None, r["official_years"].split(";"))) | years))
    r["sources"] = ";".join(sorted(set(filter(None, r["sources"].split(";"))) | {source}))
    us = [u for u in r["urls"].split(" ") if u]
    if url not in us and len(us) < 6:
        us.append(url)
    r["urls"] = " ".join(us)


def crawl_site(rows: dict, deadline: float) -> bool:
    """公式サイトを内部リンクでたどる。企業ページの題名（「株式会社○○」）と、年度ページの本文から社名を抜く。"""
    from researcher.fetch import fetch

    seen, log = set(), []
    if PAGES.exists():
        with open(PAGES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                seen.add(r["url"])
    queue = [u for u in START if u not in seen]
    # 年度ページの推測URL（あれば拾う、なければ404で終わる）
    for y in YEARS:
        for pat in ("{y}/", "year/{y}/", "company/{y}/", "list/{y}/", "?year={y}"):
            u = f"https://{SITE}/" + pat.format(y=y)
            if u not in seen:
                queue.append(u)
    finished = True
    try:
        while queue:
            if time.monotonic() > deadline or len(seen) >= MAX_PAGES:
                finished = len(seen) >= MAX_PAGES
                break
            u = queue.pop(0).split("#")[0]
            if u in seen:
                continue
            seen.add(u)
            p = fetch(u)
            n_names = 0
            if p["status"] == "ok":
                title, text = p.get("title", ""), p.get("text", "")
                ys = _years(u, title)
                page_ys = ys or _years(text[:3000])
                # 企業ページ：題名の社名を1社。一覧ページ：本文の社名を全部（年度は URL・題名・本文冒頭から）
                tnames = names_in(title)
                if tnames:
                    for n in tnames[:1]:
                        _add(rows, n, page_ys, True, "公式サイト（企業ページ）", u)
                        n_names += 1
                else:
                    for n in set(names_in(text)):
                        _add(rows, n, page_ys, True, "公式サイト（一覧ページ）", u)
                        n_names += 1
                for link in p.get("links", []):
                    href = link[1] if isinstance(link, (tuple, list)) else link
                    if not href:
                        continue
                    a = urljoin(u, href).split("#")[0]
                    if urlparse(a).netloc == SITE and a not in seen and not re.search(r"\.(jpg|png|gif|pdf|zip)$", a.lower()):
                        queue.append(a)
            log.append({"url": u, "status": p["status"], "title": p.get("title", "")[:120], "names": n_names})
    finally:
        new = not PAGES.exists()
        OUT.mkdir(parents=True, exist_ok=True)
        with open(PAGES, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["url", "status", "title", "names"])
            if new:
                w.writeheader()
            w.writerows(log)
        _save(rows)
    return finished and not queue


def search_press(rows: dict, deadline: float) -> bool:
    """選出企業のプレスリリース（「ベストベンチャー100に選出」）から社名と年度を抜く。"""
    from researcher.search import search

    done = set(DONE_Q.read_text(encoding="utf-8").splitlines()) if DONE_Q.exists() else set()
    plan = []
    for y in YEARS:
        plan += [f"ベストベンチャー100 {y} 選出", f"\"ベストベンチャー100\" {y}年度 選出 株式会社",
                 f"ベストベンチャー100 {y} prtimes", f"ベストベンチャー100 {y} 選ばれました"]
    try:
        for q in plan:
            if q in done:
                continue
            if time.monotonic() > deadline:
                return False
            qy = set(re.findall(r"20\d\d", q))
            for res in search(q, 10):
                blob = res["title"] + " " + res["snippet"]
                if "ベストベンチャー" not in blob:
                    continue
                ys = _years(blob) & {str(y) for y in YEARS} or qy
                src = "プレスリリース" if "prtimes" in res["url"] else "検索結果"
                for n in set(names_in(blob)):
                    _add(rows, n, ys, urlparse(res["url"]).netloc == SITE, src, res["url"])
            done.add(q)
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        DONE_Q.write_text("\n".join(sorted(done)), encoding="utf-8")
        _save(rows)
    return True


def write_outputs(rows: dict, finished: bool) -> None:
    by_year_off, by_year_any = defaultdict(int), defaultdict(int)
    for r in rows.values():
        for y in filter(None, r["official_years"].split(";")):
            by_year_off[y] += 1
        for y in filter(None, r["years"].split(";")):
            by_year_any[y] += 1
    lines = ["# ベストベンチャー100 選出企業の名簿（段階A）", "",
             f"- 社名の候補：{len(rows)}社（重複は社名で1つ）",
             f"- 公式サイトで年度まで確かめられた社：{sum(1 for r in rows.values() if r['official_years'])}社", "",
             "| 年度 | 公式サイトで確認 | 候補（検索込み） |", "|---|---|---|"]
    for y in YEARS:
        lines.append(f"| {y} | {by_year_off.get(str(y), 0)} | {by_year_any.get(str(y), 0)} |")
    lines += ["", "1年あたり100社が満数。足りない年度は公式サイトの構造が取れていない。pages.csv を見て直す。"]
    (OUT / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    keep = [r for r in rows.values() if r["official_years"] or int(r["mentions"]) >= 2]
    INPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in sorted(keep, key=lambda r: r["name"]):
            ys = r["official_years"] or r["years"]
            w.writerow([f"株式会社{r['name']}", "", r["name"], f"ベストベンチャー100 選出年度 {ys}"])
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(keep)}\n", encoding="utf-8")


def main(deadline_s: float = 4 * 3600) -> int:
    deadline = time.monotonic() + deadline_s
    rows = _load()
    a = crawl_site(rows, deadline)
    b = search_press(rows, deadline)
    write_outputs(rows, a and b)
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()
