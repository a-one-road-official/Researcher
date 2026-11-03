"""Researcher: one engine for every research topic (金属AM, 戦略17分野, 商社, ソフトウェア ...).

A topic is a YAML file in researcher/topics/: which company list to read, which
company-profile labels to pick up, which keywords mark the evidence lines to quote,
and which outside queries to try. The code never changes per topic.

Per company, three passes before anything is called 記載なし:
  1. official site text (top page + 会社概要/沿革/採用/事業/IR ... subpages)
  2. PDFs and images linked from those pages (pypdf / tesseract OCR)
  3. outside sources: several search engines x name variants (全角/半角, 別名, Japan/日本 ...)
Output: researcher/out/<topic>/companies/<slug>.json (facts with URL and pass only).

Usage: python -m researcher.run --topic am_trade --shard 0 --shards 6 --limit 15
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

from researcher import extract, fetch, search, variants
from researcher.targets import ROOT, done as _done, load_targets, load_topic, slug

load_companies = load_targets  # older name
AGGREGATORS = (
    "ipros", "baseconnect", "mynavi", "rikunabi", "doda", "indeed", "en-japan", "wantedly", "green-japan",
    "nikkei", "nikkan", "prtimes", "wikipedia", "linkedin", "facebook", "twitter", "x.com", "instagram",
    "youtube", "navitime", "yahoo", "google", "houjin", "salesnow", "musubu", "alarmbox", "nc-net",
    "metal-am", "3dprintingindustry", "3dadept", "voxelmatters", "sharelab", "fabcross", "monoist",
    "tsr-net", "tdb", "kaisharesearch", "openwork", "jobtalk", "hatalike", "townwork", "amazon", "rakuten",
)


def is_aggregator(url: str) -> bool:
    h = urlparse(url).netloc.lower()
    return any(a in h for a in AGGREGATORS)


def identify(c: dict, log: list) -> tuple[str, str]:
    """Find the official site. Returns (url, how)."""
    if c["url"].startswith("http") and not is_aggregator(c["url"]):
        return c["url"], "given"
    for q in variants.search_variants(c["name"], c["aliases"]):
        results = search.search(q)
        log.append({"query": q, "results": [r["url"] for r in results[:10]]})
        for r in results:
            if is_aggregator(r["url"]):
                continue
            if variants.name_hits(r["title"] + " " + r["snippet"], c["name"], c["aliases"]):
                p = urlparse(r["url"])
                home = f"{p.scheme}://{p.netloc}/"
                page = fetch.fetch(home)
                if page["status"] == "ok" and variants.name_hits(page["title"] + page["text"][:4000], c["name"], c["aliases"]):
                    return home, f"search:{r['engine']}:{q}"
    return "", "not_found"


def pick_links(page: dict, kws: list[str], domain: str, limit: int) -> tuple[list[str], list[str]]:
    pages, files = [], []
    for text, url in page.get("links", []):
        if urlparse(url).netloc.split(":")[0].removeprefix("www.") != domain:
            continue
        hay = (text + " " + url).lower()
        if any(k.lower() in hay for k in kws):
            if url.lower().split("?")[0].endswith(".pdf"):
                files.append(url)
            elif url not in pages:
                pages.append(url)
    return pages[:limit], files


def research(c: dict, topic: dict) -> dict:
    rec = {"name": c["name"], "aliases": c["aliases"], "context": c["context"],
           "researched_at": datetime.now(timezone.utc).isoformat(),
           "official_url": "", "identified_by": "", "profile": {}, "evidence": {},
           "checked": {"pass1": [], "pass2": [], "pass3": []}, "search_log": [], "missing": {}}
    url, how = identify(c, rec["search_log"])
    rec["official_url"], rec["identified_by"] = url, how
    keys, cats = topic["profile_keys"], topic["evidence"]
    sub_kws = topic["subpage_keywords"]
    lim = topic.get("limits", {})

    def take(page: dict, pass_no: int, need_name: bool = False):
        rec["checked"][f"pass{pass_no}"].append({"url": page["final_url"], "status": page["status"], "method": page.get("method", "")})
        if page["status"] != "ok" and not page.get("text"):
            return
        if need_name and not variants.name_hits(page.get("title", "") + page.get("text", ""), c["name"], c["aliases"]):
            return
        extract.merge_into(rec["profile"], extract.profile(page, keys, pass_no))
        extract.merge_into(rec["evidence"], extract.evidence(page, cats, pass_no))

    # pass 1: official site
    files: list[str] = []
    images: list[tuple[str, str]] = []
    if url:
        domain = urlparse(url).netloc.split(":")[0].removeprefix("www.")
        top = fetch.fetch(url)
        take(top, 1)
        queue, f1 = pick_links(top, sub_kws, domain, lim.get("subpages", 20))
        files += f1
        seen = {url}
        i = 0
        while i < len(queue) and len(seen) < lim.get("pages", 30):
            u = queue[i]; i += 1
            if u in seen:
                continue
            seen.add(u)
            pg = fetch.fetch(u)
            take(pg, 1)
            more, f2 = pick_links(pg, sub_kws, domain, 10)
            files += f2
            queue += [m for m in more if m not in seen and m not in queue]
            images += [(alt, src) for alt, src in pg.get("images", [])]
        images += top.get("images", [])
        # pass 2: PDFs + images (organisation charts, recruiting numbers are often pictures)
        img_kws = topic.get("image_keywords", [])
        pdf_done = 0
        for f in dict.fromkeys(files):
            if pdf_done >= lim.get("pdfs", 8):
                break
            take(fetch.fetch(f), 2); pdf_done += 1
        img_done = 0
        for alt, src in dict.fromkeys(images):
            if img_done >= lim.get("images", 12):
                break
            hay = (alt + " " + src).lower()
            if any(k.lower() in hay for k in img_kws):
                take(fetch.fetch(src), 2); img_done += 1
    # pass 3: outside sources, for every field / category still empty (and always for statements)
    names = variants.search_variants(c["name"], c["aliases"], limit=3)
    for tmpl in topic["external_queries"]:
        q = tmpl.format(name=names[0] if names else c["name"])
        results = search.search(q)
        rec["search_log"].append({"query": q, "results": [r["url"] for r in results[:10]]})
        for r in results[: lim.get("external_per_query", 3)]:
            if url and urlparse(r["url"]).netloc == urlparse(url).netloc:
                continue
            take(fetch.fetch(r["url"]), 3, need_name=True)
        # snippets are kept as pointers (marked as such), never as profile values
        for r in results[:5]:
            if variants.name_hits(r["title"] + r["snippet"], c["name"], c["aliases"]):
                extract.merge_into(rec["evidence"], {"検索結果の抜粋": [{"quote": f"{r['title']} — {r['snippet']}"[:400], "url": r["url"], "pass": 3}]})
    # 記載なし only after all three passes, saying where we looked
    where = lambda p: len(rec["checked"][p])
    for field in list(keys) + list(cats):
        if field not in rec["profile"] and field not in rec["evidence"]:
            rec["missing"][field] = (f"記載なし（3回確認済み：公式{where('pass1')}頁／PDF・画像{where('pass2')}件／"
                                     f"外部{where('pass3')}頁・検索{len(rec['search_log'])}回）")
    return rec


NUM_RE = re.compile(r"\d")
MARKET_SKIP = ("youtube.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com", "amazon.com", "amazon.co.jp", "rakuten.co.jp", "tiktok.com")


def research_market(c: dict, topic: dict) -> dict:
    """kind: market. One product category / market per target. No official site:
    every query (Japanese name and English name) goes to several engines, the top
    pages are read (HTML, JS, PDF), and only lines that name the item and carry the
    topic's keywords are quoted verbatim with their URL. Lines in `numeric` categories
    must contain a figure. Nothing is ranked or interpreted here."""
    rec = {"name": c["name"], "en": c.get("en", ""), "aliases": c["aliases"], "context": c["context"], "kind": "market",
           "researched_at": datetime.now(timezone.utc).isoformat(), "evidence": {}, "figures": {},
           "checked": {"pass3": []}, "search_log": [], "missing": {}}
    cats, numeric = topic["evidence"], set(topic.get("numeric", []))
    lim = topic.get("limits", {})
    names = [c["name"]] + c["aliases"] + ([c["en"]] if c.get("en") else [])
    seen: set[str] = set()
    for tmpl in topic["external_queries"]:
        if "{en}" in tmpl and not c.get("en"):
            continue
        q = tmpl.format(name=c["name"], en=c.get("en", ""))
        results = search.search(q)
        rec["search_log"].append({"query": q, "results": [r["url"] for r in results[:10]]})
        taken = 0
        for r in results:
            if taken >= lim.get("external_per_query", 4):
                break
            u = r["url"].split("#")[0]
            host = urlparse(u).netloc.lower().split(":")[0]
            if u in seen or any(host == k or host.endswith("." + k) or host.startswith(k + ".") for k in MARKET_SKIP):
                continue
            seen.add(u)
            if len(seen) > lim.get("pages", 40):
                break
            page = fetch.fetch(u)
            taken += 1
            rec["checked"]["pass3"].append({"url": page["final_url"], "status": page["status"], "method": page.get("method", "")})
            text = page.get("title", "") + " " + page.get("text", "")
            if not page.get("text") or not variants.phrase_hits(text, c["name"], names[1:]):
                continue
            ev = extract.evidence(page, cats, 3, per_cat=lim.get("per_cat", 15))
            ev = {k: [i for i in v if k not in numeric or NUM_RE.search(i["quote"])] for k, v in ev.items()}
            extract.merge_into(rec["evidence"], {k: v for k, v in ev.items() if v})
        for r in results[:6]:
            snip = f"{r['title']} — {r['snippet']}"
            if NUM_RE.search(snip) and variants.phrase_hits(snip, c["name"], names[1:]):
                extract.merge_into(rec["evidence"], {"検索結果の抜粋": [{"quote": snip[:400], "url": r["url"], "pass": 3}]})
    rec["figures"] = extract.figures(rec["evidence"], topic.get("figure_patterns", {}))
    for field in cats:
        if field not in rec["evidence"]:
            rec["missing"][field] = f"記載なし（読んだ{len(rec['checked']['pass3'])}頁・検索{len(rec['search_log'])}回）"
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default="")
    ap.add_argument("--redo", action="store_true", help="re-research companies already done")
    a = ap.parse_args(argv)
    topic = load_topic(a.topic)
    out = Path(a.out) if a.out else ROOT / "out" / a.topic / "companies"
    out.mkdir(parents=True, exist_ok=True)
    done_dir = ROOT / "out" / a.topic / "companies"
    todo = [c for i, c in enumerate(load_companies(topic)) if i % a.shards == a.shard]
    todo = [c for c in todo if a.redo or not _done(done_dir, c)]
    n = 0
    for c in todo[: a.limit]:
        t0 = time.time()
        try:
            rec = (research_market if topic["kind"] == "market" else research)(c, topic)
        except Exception as e:  # one bad company never stops the run
            rec = {"name": c["name"], "error": f"{type(e).__name__}: {e}"[:300]}
        (out / f"{slug(c['name'])}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
        print(f"[{n}/{min(len(todo), a.limit)}] {c['name']} {time.time()-t0:.0f}s", flush=True)
    fetch.BROWSER.close()
    print(f"remaining_after_this_run={max(0, len(todo) - n)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
