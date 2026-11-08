"""Search several engines and merge, so a company only one engine knows is not missed.

- ddgs (DuckDuckGo / Bing / Google / Brave / Yahoo / Mojeek backends, free, no key)
- Yahoo! JAPAN (results come from Google's index) scraped directly
Every result is {title, url, snippet, engine, query}. No AI, no paid API.
"""
from __future__ import annotations

import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DDGS_BACKENDS = ("google", "bing", "duckduckgo", "brave", "yahoo")
_last = [0.0]


def _pace(sec: float = 1.5) -> None:
    w = sec - (time.monotonic() - _last[0])
    if w > 0:
        time.sleep(w)
    _last[0] = time.monotonic()


def yahoo_japan(query: str, n: int = 10) -> list[dict]:
    _pace()
    try:
        r = requests.get("https://search.yahoo.co.jp/search", params={"p": query, "ei": "UTF-8"},
                         headers={"User-Agent": UA, "Accept-Language": "ja"}, timeout=15)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a"):
        h = a.find("h3")
        href = a.get("href", "")
        try:  # 2026-11-05 HEALTH（recruit_pr_verify）：「関連記事：…」のような全角文字のリンクで urlparse が ValueError を出して全体が落ちた。壊れたリンクは飛ばす
            bad = "yahoo.co.jp" in urllib.parse.urlparse(href).netloc
        except ValueError:
            bad = True
        if not h or not href.startswith("http") or bad:
            continue
        box = a.find_parent("div")
        snip = box.get_text(" ", strip=True)[:300] if box else ""
        out.append({"title": h.get_text(" ", strip=True), "url": href, "snippet": snip, "engine": "yahoo_jp"})
        if len(out) >= n:
            break
    return out


def ddgs_search(query: str, n: int = 10) -> list[dict]:
    try:
        from ddgs import DDGS
    except Exception:
        return []
    out = []
    for be in DDGS_BACKENDS:
        _pace(1.0)
        try:
            rows = DDGS().text(query, region="jp-jp", max_results=n, backend=be) or []
        except Exception:
            continue
        for r in rows:
            u = r.get("href") or r.get("url") or ""
            if u.startswith("http"):
                out.append({"title": r.get("title", ""), "url": u, "snippet": r.get("body", ""), "engine": f"ddgs:{be}"})
    return out


# 2026-11-07 METHOD 4-71（AIよろず室「RAG導入でよくある失敗7選」：失敗は層に分けて切り分ける。検索の層の失敗を答えの層で直さない）。
# 品目名に付けた手法の注記（「（市・県の表を使わない・4-30）」など）がそのまま検索語に入り、検索の層で外れていた。
# 注記の括弧（METHOD の番号・「使わない」「確かめ」などの指示語を含むもの）は検索エンジンに送る前に外す。元の品目名は記録に残す
import re as _re
_NOTE = _re.compile(r"[（(][^（）()]*(?:\d-\d{1,3}|使わない|確かめ|確認|取り直|ため|段差|構成変化|同じ年|そろう|原表|原典|要修正)[^（）()]*[）)]")


def clean_query(query: str) -> str:
    q = _NOTE.sub(" ", query)
    q = _re.sub(r"\s+", " ", q).strip()
    return q or query


# 2026-11-09 METHOD.md 4-112（走査で見つかった失敗）：ddgs:bing の結果に、ホストが生のIPアドレスの成人向けページや、
# 偽ブランド品のページ（emken2012 の chloe-handbags）が混ざり、research_method の未読URLに並んだ。
# ホストが数字だけのIP・ブランド品の偽物・成人向けの語を含むURLは結果に入れない
_SPAM_HOST = _re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$")
_SPAM_PATH = _re.compile(r"(bokep|porn|xxx|ngentot|toket|memek|hentai|jav-|chloe-(handbags|purses)|replica-|cheap-(louis|gucci|chanel)|casino|togel|slot-gacor)", _re.I)


def is_spam(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
    except Exception:
        return True
    return bool(_SPAM_HOST.match(u.netloc or "")) or bool(_SPAM_PATH.search(url or ""))


def search(query: str, n: int = 10) -> list[dict]:
    seen, merged = set(), []
    sent = clean_query(query)
    for r in yahoo_japan(sent, n) + ddgs_search(sent, n):
        key = r["url"].split("#")[0].rstrip("/")
        if key in seen or is_spam(r.get("url", "")):
            continue
        seen.add(key)
        r["query"] = query
        r["sent_query"] = sent
        merged.append(r)
    return merged
