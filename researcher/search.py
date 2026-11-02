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
        if not h or not href.startswith("http") or "yahoo.co.jp" in urllib.parse.urlparse(href).netloc:
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


def search(query: str, n: int = 10) -> list[dict]:
    seen, merged = set(), []
    for r in yahoo_japan(query, n) + ddgs_search(query, n):
        key = r["url"].split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        r["query"] = query
        merged.append(r)
    return merged
