"""世界のAM原料メーカーの名簿を、業界の企業ディレクトリから作る（feedstock_jp_check の入力に足す）。

本人の指示（2026-11-09）：Formnext出展社だけでなく、世界中のあらゆるリストから原料メーカー
（樹脂ペレット・エンプラ/スーパーエンプラのフィラメント・レジン・樹脂粉末・セラミック・複合材・金属粉・ワイヤ・フィードストック）を洗う。
feedstock_makers_world（市場型）の引用は文の羅列で社名の一覧にならなかったため、社名が1社1ページで並ぶディレクトリから直接取る。

段階
  A. ディレクトリのカテゴリ一覧から、原料に当たるカテゴリ（名前に powder / filament / resin / pellet / material / wire /
     feedstock / polymer / ceramic / metal を含む）だけを選ぶ。カテゴリ名は機械的に選ぶが、会社は1社ずつ企業ページで確かめる。
  B. カテゴリのページ送りを最後までたどり、企業ページ（/company/<slug>/）を集める。
  C. 企業ページ1枚ずつ：社名（見出し・題名）、公式サイト（ディレクトリ以外の最初の外部リンク）、載っていたカテゴリ。
  出力：out/feedstock_dir/companies.csv（全社）、inputs/feedstock_jp_check.csv に未登録の会社を追記（文脈＝カテゴリ）。
時間切れでも途中から再開する（companies.csv をキャッシュに使う）。判定（日本の担い手・大手）は feedstock_jp_check でする。
"""
from __future__ import annotations

import csv
import re
import sys
import time

from researcher.fetch import fetch, urlparse
from researcher.targets import ROOT

OUT = ROOT / "out" / "feedstock_dir"
COMPANIES = OUT / "companies.csv"
INPUT = ROOT / "inputs" / "feedstock_jp_check.csv"
FIELDS = ["slug", "name", "home_url", "categories", "directory", "page_url"]

DIRECTORIES = [
    # (ディレクトリ名, トップURL, カテゴリURLの印, 企業URLの印)
    ("voxelmatters", "https://www.voxelmatters.directory/", "/company-category/", "/company/"),
]
MATERIAL_CAT = re.compile(r"powder|filament|resin|pellet|granul|material|wire|feedstock|polymer|plastic|ceramic|metal|composite|fiber|fibre|ink|paste|binder", re.I)
NOT_COMPANY = re.compile(r"/(?:company-category|category|tag|page|feed|author)/", re.I)
SNS = ("facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "linkedin.com", "google.", "wa.me", "t.me",
       "pinterest.", "tiktok.com", "voxelmatters", "wordpress.org", "addtoany", "mailto:", "tel:")


def _load() -> dict:
    rows = {}
    if COMPANIES.exists():
        with open(COMPANIES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["slug"]] = r
    return rows


def _save(rows: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(COMPANIES, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for sl in sorted(rows):
            w.writerow({**rows[sl], "slug": sl})


def _categories(top: str, cat_mark: str) -> list[str]:
    p = fetch(top)
    cats = {u.split("?")[0] for _, u in p.get("links", []) if cat_mark in u}
    # カテゴリページの中のカテゴリ一覧も拾う（トップに全部は出ていないことがある）
    for u in list(cats)[:5]:
        cats |= {v.split("?")[0] for _, v in fetch(u).get("links", []) if cat_mark in v}
    return sorted(u for u in cats if MATERIAL_CAT.search(u.rstrip("/").rsplit("/", 1)[-1]))


def _company_links(cat_url: str, comp_mark: str, deadline: float) -> set[str]:
    found, page = set(), 1
    while time.time() < deadline and page <= 60:
        url = cat_url if page == 1 else cat_url.rstrip("/") + f"/page/{page}/"
        p = fetch(url)
        if p.get("status") != "ok":
            break
        new = {u.split("?")[0] for _, u in p.get("links", []) if comp_mark in u and not NOT_COMPANY.search(u)}
        if not new - found:
            break
        found |= new
        page += 1
    return found


def _slug(u: str) -> str:
    return urlparse(u).path.rstrip("/").rsplit("/", 1)[-1].lower()


def _record(u: str, cat: str, dname: str, rows: dict) -> None:
    sl = _slug(u)
    if not sl:
        return
    r = rows.get(sl)
    if r:
        if cat not in r["categories"].split(";"):
            r["categories"] = ";".join(filter(None, [r["categories"], cat]))
        return
    p = fetch(u)
    title = re.split(r"\s[|–\-]\s", p.get("title", ""))[0].strip()
    home = ""
    host = urlparse(u).netloc
    for _, href in p.get("links", []):
        h = urlparse(href).netloc
        if href.startswith("http") and h and h != host and not any(s in href for s in SNS):
            home = href
            break
    rows[sl] = {"name": title or sl, "home_url": home, "categories": cat, "directory": dname, "page_url": u}


def _append_input(rows: dict) -> int:
    have = set()
    if INPUT.exists():
        with open(INPUT, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                have.add(re.sub(r"[^a-z0-9]", "", (r.get("社名") or "").lower())[:12])
    add = []
    for r in rows.values():
        key = re.sub(r"[^a-z0-9]", "", r["name"].lower())[:12]
        if key and key not in have:
            have.add(key)
            add.append([r["name"], r["home_url"], "", "ディレクトリ:" + r["categories"]])
    if add:
        with open(INPUT, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(add)
    return len(add)


def main(deadline_s: float = 3 * 3600) -> int:
    deadline = time.time() + deadline_s
    rows = _load()
    for dname, top, cat_mark, comp_mark in DIRECTORIES:
        for cat_url in _categories(top, cat_mark):
            cat = cat_url.rstrip("/").rsplit("/", 1)[-1]
            for u in sorted(_company_links(cat_url, comp_mark, deadline)):
                if time.time() > deadline:
                    break
                _record(u, cat, dname, rows)
                if len(rows) % 25 == 0:
                    _save(rows)
            _save(rows)
            print(f"{dname} {cat}: total {len(rows)}", flush=True)
    _save(rows)
    added = _append_input(rows)
    print(f"appended {added} companies to {INPUT.name}", flush=True)
    if time.time() < deadline:
        (OUT / "BUILD_DONE").write_text(f"{len(rows)}\n", encoding="utf-8")
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
