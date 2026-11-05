"""Read a page as text: HTTP first, headless browser when HTTP fails or the page is
JavaScript-only, PDF text via pypdf, images via tesseract OCR. robots.txt honored.
Runs on GitHub Actions, so no Claude session allowlist applies. No AI.
"""
from __future__ import annotations

import io
import re
import subprocess
import tempfile
import time
import urllib.robotparser
from urllib.parse import urljoin
from urllib.parse import urlparse as _safe_urlparse


def urlparse(u):
    """全角記号などで urlparse が落ちる URL（例：netloc に「＃」）を空として扱う。"""
    try:
        return _safe_urlparse(u)
    except ValueError:
        return _safe_urlparse("")


import requests
from bs4 import BeautifulSoup

UA = "A-one-road-researcher/1.0 (+https://a-oneroad.com/)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 20
MAX_BYTES = 8_000_000
MIN_TEXT = 200
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_last: dict[str, float] = {}


def _allowed(url: str) -> bool:
    p = urlparse(url)
    key = f"{p.scheme}://{p.netloc}"
    if key not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(key + "/robots.txt", headers={"User-Agent": UA}, timeout=10)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[key] = rp
    return _robots[key].can_fetch(UA, url)


def _pace(host: str) -> None:
    w = 1.0 - (time.monotonic() - _last.get(host, 0))
    if w > 0:
        time.sleep(w)
    _last[host] = time.monotonic()


def ocr_bytes(img: bytes) -> str:
    try:
        with tempfile.NamedTemporaryFile(suffix=".img") as f:
            f.write(img)
            f.flush()
            out = subprocess.run(["tesseract", f.name, "-", "-l", "jpn+eng"], capture_output=True, timeout=120)
            return out.stdout.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def _pdf_text_inner(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:60])
    except Exception:
        return ""


def _pdf_child(data: bytes, q) -> None:
    import resource
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    except Exception:
        pass
    try:
        q.put(_pdf_text_inner(data))
    except BaseException:
        q.put("")


def pdf_text(data: bytes, timeout: int = 60) -> str:
    """Parse in a child process with a memory cap and a time limit: one broken or huge
    PDF (e.g. Japanese CMap fonts) must cost only that PDF, never the whole shard."""
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    pr = ctx.Process(target=_pdf_child, args=(data, q), daemon=True)
    pr.start()
    try:
        return q.get(timeout=timeout) or ""
    except Exception:
        return ""
    finally:
        if pr.is_alive():
            pr.kill()
        pr.join(5)


def parse_html(html: str, base: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    links = []
    for a in soup.find_all("a", href=True):
        links.append((a.get_text(" ", strip=True)[:80], urljoin(base, a["href"]).split("#")[0]))
    imgs = []
    for im in soup.find_all("img", src=True):
        imgs.append((im.get("alt", "") or "", urljoin(base, im["src"])))
    pairs = []
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            pairs.append((cells[0].get_text(" ", strip=True), " ".join(c.get_text(" ", strip=True) for c in cells[1:])))
    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                pairs.append((dt.get_text(" ", strip=True), dd.get_text(" ", strip=True)))
    text = re.sub(r"[ \t　]+", " ", soup.get_text("\n", strip=True))
    return {"title": title, "text": text, "links": links, "images": imgs, "pairs": pairs}


class Browser:
    def __init__(self):
        self.pw = self.b = None

    def html(self, url: str) -> str:
        try:
            if self.b is None:
                from playwright.sync_api import sync_playwright
                self.pw = sync_playwright().start()
                self.b = self.pw.chromium.launch()
            pg = self.b.new_page(user_agent=BROWSER_UA, locale="ja-JP")
            pg.goto(url, timeout=30000, wait_until="networkidle")
            h = pg.content()
            pg.close()
            return h
        except Exception:
            return ""

    def close(self):
        try:
            if self.b:
                self.b.close()
            if self.pw:
                self.pw.stop()
        except Exception:
            pass


BROWSER = Browser()


def fetch(url: str) -> dict:
    """Returns {url, final_url, status, kind(html|pdf|image), method, title, text, links, images, pairs}."""
    res = {"url": url, "final_url": url, "status": "error", "kind": "", "method": "", "title": "", "text": "",
           "links": [], "images": [], "pairs": []}
    if not url.startswith("http"):
        return res
    if not _allowed(url):
        res["status"] = "robots_disallowed"
        return res
    host = urlparse(url).netloc
    _pace(host)
    body, ctype = b"", ""
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "ja,en;q=0.5"}, timeout=TIMEOUT, stream=True)
        res["final_url"] = r.url
        ctype = r.headers.get("Content-Type", "").lower()
        body = r.raw.read(MAX_BYTES, decode_content=True)
        res["status"] = f"http_{r.status_code}" if r.status_code != 200 else "ok"
    except Exception as e:
        res["status"] = f"network_error:{type(e).__name__}"
    low = url.lower().split("?")[0]
    if body.startswith(b"%PDF") or "pdf" in ctype or low.endswith(".pdf"):
        res.update(kind="pdf", method="pdf", text=pdf_text(body))
        return res
    if ctype.startswith("image/") or low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        res.update(kind="image", method="ocr", text=ocr_bytes(body))
        return res
    html = ""
    if body:
        enc = None
        m = re.search(rb'charset=["\']?([\w-]+)', body[:3000])
        if m:
            enc = m.group(1).decode()
        try:
            html = body.decode(enc or "utf-8", "replace")
        except LookupError:
            html = body.decode("utf-8", "replace")
    parsed = parse_html(html, res["final_url"]) if html else None
    res["kind"], res["method"] = "html", "http"
    if not parsed or len(parsed["text"]) < MIN_TEXT:
        h2 = BROWSER.html(url)
        if h2:
            parsed = parse_html(h2, url)
            res["method"], res["status"] = "browser", "ok"
    if parsed:
        res.update(parsed)
    return res
