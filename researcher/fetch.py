"""Read a page as text: HTTP first, headless browser when HTTP fails or the page is
JavaScript-only, PDF text via pypdf, images via tesseract OCR. robots.txt honored.
Runs on GitHub Actions, so no Claude session allowlist applies. No AI.
"""
from __future__ import annotations

import os
import io
import re
import subprocess
import tempfile
import time
import urllib.robotparser
from urllib.parse import urljoin as _raw_urljoin
def urljoin(base, href):
    """2026-11-05 HEALTH（setouchi_mfg・三王ハウジング）：ページ内の壊れたリンク（「http://[…」など）で urljoin が
    ValueError: Invalid IPv6 URL を出し、会社1社の取得が丸ごとエラーになった。壊れたリンクは空にして飛ばす。"""
    try:
        return _raw_urljoin(base, href)
    except ValueError:
        return ""
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
    try:
        return _robots[key].can_fetch(UA, url)
    except ValueError:  # 壊れたURL（Invalid IPv6 URL など）は取りに行かない
        return False


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
    """文字の層がある PDF は pypdf で読み、文字がほとんど取れない（スキャン画像の）PDF は先頭のページを画像にして OCR する。
    2026-11-07 METHOD.md 4-80（Tabula はテキストの PDF しか読めない）：HEALTH.md の「検索は当たったが本文から取れない」の層の一部は画像の PDF。"""
    t = _pdf_text_parse(data, timeout)
    if len(re.sub(r"\s", "", t)) >= 200:
        return t
    o = _pdf_ocr(data)
    if o:
        return (t + "\n[OCR] " + o).strip()  # OCR の文字は印を付けて、走査で【OCR】として扱える形にする
    return t


def _pdf_ocr(data: bytes, pages: int = 5) -> str:
    import shutil
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        return ""
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.pdf")
            with open(src, "wb") as f:
                f.write(data)
            subprocess.run(["pdftoppm", "-r", "200", "-l", str(pages), "-png", src, os.path.join(d, "p")], capture_output=True, timeout=120)
            outs = []
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".png"):
                    with open(os.path.join(d, fn), "rb") as g:
                        outs.append(ocr_bytes(g.read()))
            return "\n".join(o for o in outs if o)
    except Exception:
        return ""


def _pdf_text_parse(data: bytes, timeout: int = 60) -> str:
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


def _unit_of(col: str) -> str:
    """列の見出しから単位を取る（数字だけのセルに単位を付け、走査で金額・人数として読めるようにする）。"""
    m = re.search(r"[（(]\s*(兆円|億円|百万円|千万円|万円|千円|円|人|事業所|社|%|％)\s*[)）]", col)
    if m: return m.group(1)
    if "事業所数" in col: return "事業所"
    if "従業者数" in col or "従業員数" in col: return "人"
    return ""


def table_text(data: bytes, low: str = "", ctype: str = "", max_rows: int = 3000) -> str:
    import io
    try:
        import pandas as pd
        if low.endswith(".csv") or "csv" in ctype:
            for enc in ("utf-8-sig", "cp932"):
                try:
                    sheets = {"csv": pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding=enc)}; break
                except Exception:
                    sheets = {}
        else:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    except Exception:
        return ""
    lines = []
    for name, df in sheets.items():
        df = df.fillna("")
        rows = [[str(x).strip() for x in r] for r in df.values.tolist()[:max_rows]]
        # 見出しの行：数字でない文字が半分以上ある最初の行
        head = next((r for r in rows if sum(1 for x in r if x and not re.fullmatch(r"[\d,.\-△▲ ]+", x)) * 2 >= max(1, sum(1 for x in r if x))), [])
        # 2026-11-06 走査の失敗から（mfg_capacity：工業統計表 2020-k3-data.xls を取得できたのに、数字が1件も品目に入らなかった）：
        # 統計表の見出しは「表題→項目名（事業所数・従業者数・加工賃収入額）→単位（（人）（百万円））」の複数行で、セルの結合で左の列にだけ文字がある。
        # 最初の1行（表題）だけを見出しにすると、数字の行に「事業所数」などの項目名も単位も付かず、品目の言葉で拾われない。
        # 最初の数字の行より上の行（最大4行）を列ごとにつなぎ、結合セルは左から右へ埋めて、列の見出しにする
        numre = re.compile(r"[\d,.△▲-]+")
        first = next((i for i, r in enumerate(rows) if sum(1 for x in r if x and numre.fullmatch(x)) >= 2), None)
        if first:
            width = max(len(r) for r in rows[:first + 1])
            stack = []
            for r in rows[max(0, first - 4):first]:
                if sum(1 for x in r if x) <= 1 and len(rows[0]) > 2:  # 表題だけの行は列の見出しにしない
                    continue
                filled, last = [], ""
                for i in range(width):
                    x = r[i] if i < len(r) else ""
                    if x: last = x
                    filled.append(x or (last if i > 0 and not numre.fullmatch(last or "0") else ""))
                stack.append(filled)
            if stack:
                head = [" ".join(dict.fromkeys(h[i] for h in stack if h[i]))[:60] for i in range(width)]
        for r in rows:
            cells = [x for x in r if x]
            if not cells: continue
            if r is head or not any(re.fullmatch(r"[\d,.△▲-]+", x) for x in cells):
                lines.append(f"{name}：" + " ".join(cells)); continue
            label = " ".join(x for x in r if x and not re.fullmatch(r"[\d,.△▲-]+", x))[:60]
            # 2026-11-06：時系列表の左端の年（2019 など）は数字なので行の名前から落ちていた。左端が年なら「2019年」として行の名前に入れる
            if r and re.fullmatch(r"(19|20)\d\d(\.0)?", r[0] or ""):
                label = (r[0][:4] + "年 " + label).strip()
            for i, x in enumerate(r):
                if x and re.fullmatch(r"[\d,.△▲-]+", x) and x != "-" and not (i == 0 and re.fullmatch(r"(19|20)\d\d(\.0)?", x)):
                    col = head[i] if i < len(head) else ""
                    lines.append(f"{name}：{label}：{col} {x}{_unit_of(col)}")
    return "\n".join(lines)[:400000]


def parse_html(html: str, base: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    links = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0]
        if u: links.append((a.get_text(" ", strip=True)[:80], u))
    imgs = []
    for im in soup.find_all("img", src=True):
        u = urljoin(base, im["src"])
        if u: imgs.append((im.get("alt", "") or "", u))
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
    full = re.sub(r"[ \t　]+", " ", soup.get_text("\n", strip=True))
    # 2026-11-05 METHOD.md 4-35：本文に、同じサイトの関連記事・連載・ランキング・メニュー・フッターの文が混ざり、
    # 別の記事の文が元のURLの引用として数えられた（d-pops-group の1人あたり売上の記事に登山の連載・FDE の記事、
    # 電子決算公告ドットコムの記事に「1行3,589円」など関連記事の紹介文）。リンク・表は全体から取ったうえで、本文はそれらの枠を外して取る。
    # 外した結果が短すぎる（全体の3割未満か200字未満）ページは、枠の判定を誤ったとみなして全体を使う
    for t in soup.find_all(["nav", "aside", "footer", "header", "form"]):
        t.decompose()
    for t in soup.find_all(True, attrs={"class": True}):
        if getattr(t, "decomposed", False): continue
        c = " ".join(t.get("class") or []) + " " + (t.get("id") or "")
        if BOXES.search(c): t.decompose()
    body = re.sub(r"[ \t　]+", " ", soup.get_text("\n", strip=True))
    text = body if len(body) >= 200 and len(body) >= 0.3 * len(full) else full
    return {"title": title, "text": text, "links": links, "images": imgs, "pairs": pairs}


BOXES = re.compile(r"(^|[\s_-])(related|relation|recommend|popular|ranking|sidebar|side-bar|widget|breadcrumb|pickup|pick-up|banner|ad-|ads|advert|sns-share|share|footer|global-nav|gnav|menu|pager|pagination|tag-?list|category-list|newpost|new-post|archive)([\s_-]|$)", re.I)


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
    # 2026-11-05 METHOD.md 4-35：所管の統計表は Excel・CSV で置かれる（経産省の工業統計の結果表・時系列）。これまで HTML として読み、文字化けで数字が0件だった。
    # 表を「シート名：行の見出し：列の見出し 値単位」（｜は走査で題名扱いになるので使わない）の行に直して本文にする。表の上の単位の注記（百万円・人）も行として残す
    # e-Stat の file-download のように URL に拡張子が無い Excel もあるので、中身の先頭（xlsx=PK、xls=OLE）でも見分ける
    if low.endswith((".xlsx", ".xls", ".xlsm", ".csv")) or "spreadsheet" in ctype or "ms-excel" in ctype or "text/csv" in ctype \
            or body[:2] == b"PK" and "html" not in ctype or body[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        res.update(kind="table", method="table", text=table_text(body, low, ctype))
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
