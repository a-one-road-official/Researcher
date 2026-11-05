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
NAME_FIELDS = ["slug", "name", "first_listed", "years", "official_years", "sources", "urls", "mentions", "site_label", "home_url"]


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
                if r.get("slug"):
                    r.setdefault("first_listed", "")
                    rows[r["slug"]] = r
    return rows


def _save(rows: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(NAMES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NAME_FIELDS, extrasaction="ignore")
        w.writeheader()
        for sl, r in sorted(rows.items(), key=lambda kv: (-int(kv[1]["mentions"] or 0), kv[0])):
            r["slug"] = sl
            w.writerow(r)


SNS = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "linkedin.com", "line.me", "note.com",
       "v-tsushin.jp", "jo-katsu.com", "archive.org", "google.", "prtimes.jp", "wantedly.com", "addtoany", "hatena")
# 企業ページは /company/<slug>/ の1社1ページ。年・カテゴリ・ページ送りは企業ページではない
COMPANY_RX = re.compile(r"/company/([a-z0-9][a-z0-9\-_]*)/?$", re.I)
NOT_SLUG = re.compile(r"^(?:20\d\d|category|page|feed|tag)$")
CAT_RX = re.compile(r"/company/category/([a-z0-9\-_]+)/", re.I)


def _slug(url: str) -> str:
    m = COMPANY_RX.search(urlparse(url).path)
    return m.group(1).lower() if m and not NOT_SLUG.match(m.group(1)) else ""


def _members(p: dict) -> set[str]:
    """一覧・カテゴリ・トップのページから、載っている企業ページ（slug）を集める。本文の文字からは社名を取らない。"""
    return {sl for _, href in p.get("links", []) if SITE in href and (sl := _slug(href))}


def _company_record(rows: dict, slug: str, p: dict, url: str) -> None:
    """企業ページ1枚 → 社名（題名の最初の社名、なければ題名の「|」より前）と公式サイト（外部リンクの最初）。"""
    r = rows.setdefault(slug, {"name": "", "years": "", "official_years": "", "sources": "", "urls": "", "mentions": "0",
                               "site_label": "", "home_url": ""})
    if not r["name"]:
        title = p.get("title", "")
        t = names_in(title)
        r["name"] = t[0] if t else _norm(re.split(r"[|｜]", title)[0]).strip()
    if not r["home_url"]:
        for _, href in p.get("links", []):
            h = urlparse(href).netloc.lower()
            if href.startswith("http") and h and not any(x in h for x in SNS):
                r["home_url"] = href
                break
    us = [u for u in r["urls"].split(" ") if u]
    if url not in us and len(us) < 4:
        us.append(url)
    r["urls"] = " ".join(us)


def _mark(rows: dict, slug: str, year: str, src: str, label: str = "") -> None:
    r = rows.setdefault(slug, {"name": "", "years": "", "official_years": "", "sources": "", "urls": "", "mentions": "0",
                               "site_label": "", "home_url": "", "first_listed": ""})
    r["official_years"] = ";".join(sorted(set(filter(None, r["official_years"].split(";"))) | {year}))
    r["years"] = r["official_years"]
    r["sources"] = ";".join(sorted(set(filter(None, r["sources"].split(";"))) | {src}))
    r["mentions"] = str(len(r["official_years"].split(";")))
    if label and label not in r["site_label"].split(";"):
        r["site_label"] = ";".join(filter(None, [r["site_label"], label]))


def _list_pages(p: dict, base_host_filter) -> list[str]:
    """一覧のページ送り・カテゴリのリンク。"""
    out = []
    for _, href in p.get("links", []):
        path = urlparse(href).path
        if base_host_filter(href) and (CAT_RX.search(path) or re.search(r"/company/(?:page/\d+/)?$|/page/\d+/$", path)):
            out.append(href.split("#")[0])
    return out


def crawl_site(rows: dict, deadline: float) -> bool:
    """今のサイト（2026年の掲載）。WordPress の公開API（/wp-json/wp/v2/company）で全企業ページを取る。
    公開日（date）＝そのサイトに最初に載った日。カテゴリ（業界ラベル）は機能の軸として残すだけ。
    2026-11-05 点検：サイトに残っているのは今の掲載98社だけ（2018〜2026年に公開された記事）。過去の掲載社は消えている。"""
    import requests
    from researcher.fetch import fetch

    api = f"https://{SITE}/wp-json/wp/v2"
    hdr = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
    cats = {}
    try:
        for c in requests.get(f"{api}/company_category", params={"per_page": 100}, headers=hdr, timeout=60).json():
            cats[c["id"]] = c["name"]
    except Exception:
        pass
    posts, page = [], 1
    while True:
        r = requests.get(f"{api}/company", params={"per_page": 100, "page": page,
                         "_fields": "id,date,slug,link,title,company_category"}, headers=hdr, timeout=60)
        if r.status_code != 200:
            break
        posts += r.json()
        if page >= int(r.headers.get("X-WP-TotalPages") or 1):
            break
        page += 1
    log = [{"url": f"{api}/company", "status": f"{len(posts)}件", "title": "WordPress API", "names": len(posts)}]
    for j in posts:
        sl = j["slug"]
        label = ";".join(cats.get(c, str(c)) for c in j.get("company_category") or [])
        _mark(rows, sl, "2026", "公式サイト（今の掲載）", label)
        r0 = rows[sl]
        r0["first_listed"] = j["date"][:10]
        title = re.sub(r"<[^>]+>", "", (j.get("title") or {}).get("rendered", ""))
        if not r0["name"]:
            t = names_in(title)
            r0["name"] = t[0] if t else _norm(title)
    for j in posts:
        if time.monotonic() > deadline:
            return False
        sl = j["slug"]
        if rows[sl]["home_url"]:
            continue
        p = fetch(j["link"])
        if p["status"] == "ok":
            _company_record(rows, sl, p, j["link"])
    _write_pages(log)
    _save(rows)
    return bool(posts)


def _write_pages(log: list) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    new = not PAGES.exists()
    with open(PAGES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["url", "status", "title", "names"])
        if new:
            w.writeheader()
        w.writerows(log)


def _cdx(params: dict) -> list:
    import requests
    for _ in range(3):
        try:
            r = requests.get("https://web.archive.org/cdx/search/cdx", params=params, timeout=90)
            if r.status_code == 200:
                return r.json()[1:] if r.text.strip() else []
        except Exception:
            time.sleep(5)
    return []


def crawl_wayback(rows: dict, deadline: float) -> bool:
    """年ごと：その年に Wayback Machine に残る一覧・カテゴリ・トップのページ（各URLのその年の最後の版）を開き、
    リンクされている企業ページ＝その年の掲載社とする。社名は企業ページ（今のサイトかその年の版）の題名から取る。"""
    from researcher.fetch import fetch

    done_f = OUT / "wayback_done.txt"
    done = set(done_f.read_text(encoding="utf-8").splitlines()) if done_f.exists() else set()
    log = []
    try:
        for y in YEARS[:-1]:  # 2026年は今のサイト
            if str(y) in done:
                continue
            cdx = _cdx({"url": f"{SITE}/*", "from": str(y), "to": str(y), "output": "json",
                        "filter": ["statuscode:200", "mimetype:text/html"], "collapse": "urlkey", "limit": "20000",
                        "fl": "timestamp,original"})
            if not cdx:
                # 2026-11-05 点検：GitHub Actions から web.archive.org は 403（締め出し）、CDX は空で返る。取れなかった年として記録だけ残す
                log.append({"url": f"cdx {y}", "status": "no_snapshot_or_blocked", "title": "", "names": 0})
                done.add(str(y))
                continue
            lists = [(ts, o) for ts, o in cdx if re.search(r"/company/(?:category/[^/]+/)?(?:page/\d+/)?$|v-tsushin\.jp/?$", urlparse(o).path or "/")
                     or urlparse(o).path in ("", "/")]
            slugs_snap = {}
            for ts, o in cdx:
                sl = _slug(o)
                if sl:
                    slugs_snap.setdefault(sl, (ts, o))
            members = {}
            for ts, o in lists[:200]:
                if time.monotonic() > deadline:
                    return False
                p = fetch(f"https://web.archive.org/web/{ts}id_/{o}")
                log.append({"url": f"{ts} {o}", "status": p["status"], "title": p.get("title", "")[:120], "names": 0})
                if p["status"] != "ok":
                    continue
                cat = CAT_RX.search(urlparse(o).path)
                ms = _members(p)
                log[-1]["names"] = len(ms)
                for sl in ms:
                    members.setdefault(sl, set())
                    if cat:
                        members[sl].add(cat.group(1))
            if not members:  # 一覧が残っていない年：その年に保存された企業ページそのものを掲載の証拠にする（出典を分けて記録）
                for sl in slugs_snap:
                    _mark(rows, sl, str(y), f"アーカイブ{y}年（企業ページの保存のみ）")
            for sl, labels in members.items():
                _mark(rows, sl, str(y), f"アーカイブ{y}年（一覧）", ";".join(sorted(labels)))
            for sl in set(members) | (set(slugs_snap) if not members else set()):
                if rows[sl]["name"] or time.monotonic() > deadline:
                    continue
                ts, o = slugs_snap.get(sl, (f"{y}1231", f"https://{SITE}/company/{sl}/"))
                p = fetch(f"https://web.archive.org/web/{ts}id_/{o}")
                if p["status"] == "ok":
                    _company_record(rows, sl, p, o)
            done.add(str(y))
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        done_f.write_text("\n".join(sorted(done)), encoding="utf-8")
        _write_pages(log)
        _save(rows)
    return True


def write_outputs(rows: dict, finished: bool) -> None:
    by_year, src_year = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for r in rows.values():
        for y in filter(None, r["official_years"].split(";")):
            by_year[y] += 1
        for sname in filter(None, r["sources"].split(";")):
            m = re.search(r"20\d\d", sname)
            src_year[m.group(0) if m else "2026"][sname] += 1
    lines = ["# ベストベンチャー100 掲載企業の名簿（年ごと）", "",
             f"- 企業ページの数（重複なし）：{len(rows)}社　うち社名が取れた社：{sum(1 for r in rows.values() if r['name'])}社", "",
             "| 年 | 掲載社数 | 出典 |", "|---|---|---|"]
    for y in YEARS:
        srcs = "／".join(f"{k} {v}" for k, v in src_year.get(str(y), {}).items())
        lines.append(f"| {y} | {by_year.get(str(y), 0)} | {srcs} |")
    fy = defaultdict(int)
    for r in rows.values():
        if r.get("first_listed"):
            fy[r["first_listed"][:4]] += 1
    lines += ["", "## 今の掲載社が最初にサイトに載った年（公開日）", "", "| 年 | 社数 |", "|---|---|"]
    lines += [f"| {y} | {n} |" for y, n in sorted(fy.items())]
    lines += ["", "2026年＝今のサイト。2016〜2025年＝Wayback Machine のその年の一覧・カテゴリページにリンクされた企業ページ。",
              "一覧が残っていない年は「企業ページの保存のみ」と出典を分けて数えている（掲載の確かさが一段低い）。"]
    (OUT / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    keep = [r for r in rows.values() if r["name"]]
    INPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in sorted(keep, key=lambda r: r["name"]):
            w.writerow([r["name"], r.get("home_url", ""), r["slug"], f"ベストベンチャー100 掲載年 {r['official_years']}"])
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(keep)}\n", encoding="utf-8")


def main(deadline_s: float = 4 * 3600) -> int:
    deadline = time.monotonic() + deadline_s
    rows = _load()
    a = crawl_site(rows, deadline)
    crawl_wayback(rows, deadline)  # 取れなくても止めない（取れた年だけ足す）
    write_outputs(rows, False)
    if not a:
        return 0
    from researcher.classify_industry import classify_all
    c = classify_all({r["name"]: r for r in rows.values() if r["name"]}, CLASSIFIED, deadline)
    write_outputs(rows, c)
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()
