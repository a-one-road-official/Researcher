"""ベストベンチャー100（イシン株式会社主催、https://best100.v-tsushin.jp/）の選出企業を 2016〜2026年度で洗い出す。

本人の指示（2026-11-05）：
- 2016〜2026年の10年間で、どの領域のベンチャーが何社いるかを、事実・数字で出す。
- 領域は経済センサス（日本標準産業分類）の構造でMECEに。ブランク（大きいのにベンチャーが少ない領域）を見る。
- 領域は「ベンチャーが何屋か」（サイトの業界タグ：IT・Web／広告／人材…）ではなく、「どの産業を相手に稼いでいるか」
  （製造・物流・農業・建設・医療…＝日本標準産業分類の大分類）で見る。サイトのタグや検索だけでは並べ替えられない。
  そのため1社ずつ、導入事例・主要取引先・客の業種の記述を一次情報から引用で残し、それを読んで客の産業を決める。
- 業種はキーワード一致で付けない。各社の事業内容を一次情報（公式サイト・会社概要）から引用で残す。

段階
  A. 名簿集め：公式サイトを内部リンクでたどり（年度ページ・企業ページ）、ページの題名と本文から社名と年度を抜く。
     加えて「ベストベンチャー100 {年} 選出」の検索結果（選出企業のプレスリリース）からも社名と年度を抜く。
     出典の種類（公式サイト／プレスリリース）とURLを残す。公式サイトで年度が確かめられたものを「確定」とする。
  A2. 過去の年度：Internet Archive（web.archive.org）に残る各年のスナップショットを年ごとに読み、その年に載っていた社を取る
     （サイトは「2年目以降も審査を経て掲載継続」なので、年ごとの掲載社＝その年のスナップショット）。
  C. 産業の判定（表紙で判断しない）：サイトの「業界」ラベル（IT・Web／人材／広告…）は「何をするか（機能）」であって、
     「どの産業のお金で食っているか」ではない。例：PLEXは人材・SaaSだが、客は運送会社、扱う人はドライバー＝運輸業。
     1社ずつ公式サイトの本文を読み、次の独立した5つの手がかりを別々に数える（単語が出ただけでは数えない）。
       S1 取引先・導入企業の社名 → その会社の業種（上場企業は東証33業種 → 日本標準産業分類の大分類に写す）
       S2 客の呼び方の型（「○○向け」「○○業界の」「○○会社様」「○○事業者」「○○の皆様」）の○○が、どの産業の事業者か
       S3 扱う人の職種（求人・紹介・派遣・教育の文脈の「ドライバー」「看護師」「施工管理」…）→ その職種が働く産業
       S4 自社が持つ許認可・登録（一般貨物自動車運送事業・宅建業免許・建設業許可・医療法人…）→ 自分で営む産業
       S5 サイトのラベルと自分の言い方（人材／広告／SaaS／コンサル）→ 機能の軸（産業とは別の列に置く）
     2つ以上の手がかりが同じ大分類を指せば「確定」、1つだけなら「候補」、産業を選ばない（全業種向け）なら「横断」、
     手がかりなしは「要読込」として引用を残す（人が読んで決める）。
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
WAYBACK_PER_YEAR = 400
CLASSIFIED = OUT / "classified.csv"
SITE_LABELS = ["IT・Web", "人材サービス", "コンサルティング", "広告", "情報通信サービス", "不動産", "マーケティング",
               "ECソリューション", "リユース", "その他", "金融", "医療", "教育", "建設", "飲食", "製造", "物流"]

CORP = r"(?:株式会社|有限会社|合同会社|㈱|\(株\)|（株）)"
NAME_CHARS = r"[一-龥ぁ-んァ-ヶーA-Za-zＡ-Ｚａ-ｚ0-9０-９・&＆\.\-]"
PRE_RX = re.compile(rf"{CORP}\s?({NAME_CHARS}{{2,25}})")
POST_RX = re.compile(rf"({NAME_CHARS}{{2,25}}){CORP}")
YEAR_RX = re.compile(r"(20(?:1[6-9]|2[0-6]))\s*(?:年度?|年版)?")
NOT_NAME = re.compile(r"イシン|ベンチャー通信|ベストベンチャー|選出|一覧|ランキング|受賞|お知らせ|プレスリリース|PR TIMES|ニュース")
NAME_FIELDS = ["name", "years", "official_years", "sources", "urls", "mentions", "site_label", "home_url"]


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
                r.setdefault("site_label", "")
                r.setdefault("home_url", "")
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
    r = rows.setdefault(name, {"name": name, "years": "", "official_years": "", "sources": "", "urls": "", "mentions": "0",
                               "site_label": "", "home_url": ""})
    r["mentions"] = str(int(r["mentions"]) + 1)
    r["years"] = ";".join(sorted(set(filter(None, r["years"].split(";"))) | years))
    if official:
        r["official_years"] = ";".join(sorted(set(filter(None, r["official_years"].split(";"))) | years))
    r["sources"] = ";".join(sorted(set(filter(None, r["sources"].split(";"))) | {source}))
    us = [u for u in r["urls"].split(" ") if u]
    if url not in us and len(us) < 6:
        us.append(url)
    r["urls"] = " ".join(us)


SNS = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "linkedin.com", "line.me", "note.com",
       "v-tsushin.jp", "jo-katsu.com", "archive.org", "google.", "prtimes.jp", "wantedly.com")


def _harvest(rows: dict, u: str, p: dict, ys: set, src: str) -> int:
    """1ページから社名を抜く。題名に社名＝企業ページ（外部リンクの最初を公式サイトとする）、なければ一覧ページ。
    題名・見出しに「業界」ラベルがあれば、そのページの社にラベルを付ける（機能の軸。産業の判定には使わない）。"""
    title, text = p.get("title", ""), p.get("text", "")
    label = next((l for l in SITE_LABELS if l in title or l in text[:200]), "")
    tnames = names_in(title)
    found = tnames[:1] if tnames else sorted(set(names_in(text)))
    for n in found:
        _add(rows, n, ys, True, src + ("（企業ページ）" if tnames else "（一覧ページ）"), u)
        r = rows[n]
        if label and label not in r["site_label"]:
            r["site_label"] = ";".join(filter(None, [r["site_label"], label]))
        if tnames and not r["home_url"]:
            for _, href in p.get("links", []):
                h = urlparse(href).netloc.lower()
                if href.startswith("http") and h and not any(x in h for x in SNS):
                    r["home_url"] = href
                    break
    return len(found)


def crawl_wayback(rows: dict, deadline: float) -> bool:
    """各年のスナップショット（web.archive.org）を読み、その年に載っていた社を取る。"""
    import requests
    from researcher.fetch import fetch

    done_f = OUT / "wayback_done.txt"
    done = set(done_f.read_text(encoding="utf-8").splitlines()) if done_f.exists() else set()
    try:
        for y in YEARS:
            if str(y) in done:
                continue
            try:
                r = requests.get("https://web.archive.org/cdx/search/cdx", timeout=60, params={
                    "url": f"{SITE}/*", "from": str(y), "to": str(y), "output": "json", "filter": ["statuscode:200", "mimetype:text/html"],
                    "collapse": "urlkey", "limit": "5000"})
                cdx = r.json()[1:] if r.status_code == 200 and r.text.strip() else []
            except Exception:
                continue  # 次の回でやり直す
            # 企業・一覧・業界のページを先に読む
            cdx.sort(key=lambda c: 0 if re.search(r"company|detail|list|category|industry|gyokai|\d{3,}", c[2]) else 1)
            for c in cdx[:WAYBACK_PER_YEAR]:
                if time.monotonic() > deadline:
                    return False
                ts, orig = c[1], c[2]
                p = fetch(f"https://web.archive.org/web/{ts}id_/{orig}")
                if p["status"] == "ok":
                    _harvest(rows, orig, p, {str(y)}, f"アーカイブ{y}年")
            done.add(str(y))
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        done_f.write_text("\n".join(sorted(done)), encoding="utf-8")
        _save(rows)
    return True


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
                page_ys = ys or {"2026"}  # 今のサイトに載っている社＝2026年の掲載（本文の年は創業年などが混ざるので使わない）
                # 企業ページ：題名の社名を1社。一覧ページ：本文の社名を全部（年度は URL・題名・本文冒頭から）
                n_names = _harvest(rows, u, p, page_ys, "公式サイト")
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


def crawl_archive(rows: dict, deadline: float) -> bool:
    """過去年度（2016〜2025）は今の公式サイトに残っていない（今は今年度の約100社だけ）。
    Internet Archive の保存ページ（CDX）から年度ごとの企業ページを引き、題名の社名を「その年に載っていた」として残す。"""
    import json

    from researcher.fetch import fetch

    seen = set()
    if PAGES.exists():
        with open(PAGES, encoding="utf-8") as f:
            seen = {r["url"] for r in csv.DictReader(f)}
    log, finished = [], True
    try:
        for y in YEARS:
            cdx = (f"https://web.archive.org/cdx/search/cdx?url={SITE}/*&from={y}&to={y}&output=json"
                   f"&filter=statuscode:200&filter=mimetype:text/html&collapse=urlkey&fl=timestamp,original&limit=3000")
            if cdx in seen:
                continue
            if time.monotonic() > deadline:
                return False
            p = fetch(cdx)
            try:
                items = json.loads(p.get("text") or "[]")[1:]
            except Exception:
                items = []
            log.append({"url": cdx, "status": p["status"], "title": f"CDX {y}", "names": len(items)})
            done_all = True
            for ts, orig in items[:400]:
                u = f"https://web.archive.org/web/{ts}/{orig}"
                if u in seen:
                    continue
                if time.monotonic() > deadline:
                    done_all = False
                    break
                seen.add(u)
                q = fetch(u)
                n = 0
                if q["status"] == "ok":
                    tn = names_in(q.get("title", ""))
                    for name in (tn[:1] if tn else set(names_in(q.get("text", "")))):
                        _add(rows, name, {ts[:4]}, True, "公式サイト（保存ページ）", u)
                        n += 1
                log.append({"url": u, "status": q["status"], "title": q.get("title", "")[:120], "names": n})
            if not done_all:
                finished = False
                break
            seen.add(cdx)
    finally:
        new = not PAGES.exists()
        with open(PAGES, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["url", "status", "title", "names"])
            if new:
                w.writeheader()
            w.writerows(log)
        _save(rows)
    return finished


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
            w.writerow([f"株式会社{r['name']}", r.get("home_url", ""), r["name"], f"ベストベンチャー100 選出年度 {ys}"])
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(keep)}\n", encoding="utf-8")


def main(deadline_s: float = 4 * 3600) -> int:
    deadline = time.monotonic() + deadline_s
    rows = _load()
    a = crawl_site(rows, deadline)
    c = crawl_archive(rows, deadline)
    b = search_press(rows, deadline)
    write_outputs(rows, a and b and c)
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()
