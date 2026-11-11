#!/usr/bin/env python3
"""昇格ゲート（検証器）: 日本の金属AM（PBF）保有者リスト用。

方針（audit-2026-11-11 の理想系）:
  - 1行 = 「同定された会社」×「その会社自身の公式ドメイン上の言葉による証拠」。
  - 同定: 候補公式URLを開き、会社概要ページで正式社名と所在地を照合する。
  - 設備証拠: 公式ドメイン内だけを辿り、具体的な型番（メーカー名だけの語は除く）を探す。
  - 法人番号: 国税庁Web-APIは事前申込制のアプリケーションIDが必須で機械利用できないため
    使わない。同定アンカーは自社公式サイトの会社概要（社名＋所在地）。法人番号欄は「未取得」。

使い方:
  python3 gate.py --input candidates.csv --output results.csv [--only 社名1,社名2] [--max-pages 14]
入力CSV列: 社名, 候補URL, 旧URL, URL判定
出力CSV列: 社名, 候補URL, 最終URL, ホスト, 結果(PASS/REJECT/REVIEW), 理由コード, 詳細,
           同定社名, 所在地, 型番, 設備証拠URL, 概要URL, 法人番号, 取得ページ数

理由コード:
  NOT_ENTITY          社名が法人として成立しない断片（パターンのみで即不合格）
  NO_OFFICIAL_URL     候補公式URLが無い
  FETCH_FAILED        候補URLが開けない（要手確認）
  FETCH_THIN          取得できた本文が薄すぎて判定不能（要手確認）
  IDENTITY_MISMATCH   会社概要の社名と一致しない
  IDENTITY_UNCONFIRMED 会社概要が見つからず同定できない（要手確認）
  ADDRESS_MISSING     社名は一致したが所在地が取得できない（要手確認）
  NO_EQUIPMENT_EVIDENCE 公式ドメイン内に型番つき設備証拠が無い
  PASS                合格
"""
from __future__ import annotations

import argparse
import csv
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 15
CHECK_DATE = "2026-11-11"

# ---------------------------------------------------------------- 型番リスト
# build_am_owners.py の PBF_MODELS から「メーカー名だけの語」（型番・シリーズ数字を
# 含まないもの）を除いたもの。除外した語: ソディック, DMG森精機, 日本電子, 松浦機械,
# マツウラ, Arcam, Farsoon, HBD, レニショー, Bright Laser, Velo3D, AddUp, Aconity,
# Freemelt, Wayland, Raplas, Sisma, Eplus3D, Additive Industries, One Click Metal,
# Prima Additive, ORLAS, Xact Metal, ニコンSLM, Nikon SLM, トゥループリント,
# 電子ビーム金属3D, 電子ビーム積層, 3D Systems DMP
MODELS = [
    "EOS M 290", "EOS M290", "EOS M 400", "EOS M400", "EOS M 300", "EOS M300",
    "EOS M 100", "EOS M100", "EOSINT M", "EOS M 280", "EOS M280",
    "SLM 125", "SLM125", "SLM 280", "SLM280", "SLM 500", "SLM500", "SLM 800", "NXG XII",
    "DMP Flex", "DMP Factory", "ProX DMP",
    "RenAM", "AM400", "AM 400", "AM250", "AM 250",
    "TruPrint",
    "Concept Laser", "M2 cusing", "Mlab", "Xline", "M Line",
    "Spectra H", "Q20plus", "Q20 plus", "Q10plus", "Q10 plus",
    "LUMEX",
    "OPM250", "OPM 250", "OPM350", "OPM 350", "OPM250L", "LPM325",
    "LASERTEC 30 SLM", "LASERTEC 12 SLM",
    "JAM-5200EBM", "JAM-5200",
    "FS271M", "FS121M", "FS301M", "FS421M", "FS721M",
    "BLT-S",
    "Sapphire",
    "XM200",
    "FormUp",
    "MetalFAB",
    "MYSINT",
    "Print Sharp",
    "EP-M",
    "AconityMINI", "Aconity MINI",
    "EZ300", "LAMDA200", "LAMDA 200",
    "Studio System", "Metal X",
    "Avance-25", "Avance 25", "Avance-37",
]


def _model_regex(m: str) -> re.Pattern:
    parts = [re.escape(p) for p in m.split(" ")]
    pat = r"\s*".join(parts)
    return re.compile(r"(?<![A-Za-z0-9])" + pat + r"(?![A-Za-z0-9])", re.I)


MODEL_RES = [(m, _model_regex(m)) for m in MODELS]

# ---------------------------------------------------------------- 社名の断片判定
LEGAL_FORMS = [
    "株式会社", "有限会社", "合同会社", "合資会社", "合名会社",
    "国立大学法人", "公立大学法人", "学校法人", "地方独立行政法人",
    "独立行政法人", "一般財団法人", "公益財団法人", "一般社団法人",
    "公益社団法人", "財団法人", "社団法人", "医療法人", "社会福祉法人",
    "国立研究開発法人", "大学共同利用機関法人",
    "（株）", "(株)", "（有）", "(有)",
]

DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
HEADLINE_MARKERS = ["最新", "国内初", "発表", "様"]
GENERIC_ONLY = {
    "株式会社", "有限会社", "合同会社", "国立大学", "公設試験研究所",
    "工業技術センター", "工業技術研究所", "産業技術研究センター",
    "産業技術センター", "工業株式会社", "技術センター", "研究所",
}


def core_name(name: str) -> str:
    """正規化した社名コア（法人格・空白・全半角・中黒を吸収）。"""
    n = unicodedata.normalize("NFKC", name or "")
    n = n.replace("・", "").replace("·", "").replace("‐", "-").replace("ー", "ー")
    for lf in LEGAL_FORMS:
        n = n.replace(unicodedata.normalize("NFKC", lf), "")
    n = re.sub(r"[\s　]+", "", n)
    return n.lower()


# ------------------------------------------------------- 会社種別サニティチェック
# Tamura基準（2026-11-11）: ベネッセ（教育）やディズニーのような、本業がAM・製造と
# 明らかに無関係な有名企業は、URLが照合できても合格させず OBVIOUS_NON_AM で落とす。
# 設備証拠の機械判定をすり抜けた「明らかに違う」行をここで落とすのが目的。
OBVIOUS_NON_AM_EXACT = {
    "野村證券", "野村アセットマネジメント", "野村ホールディングス",
    "ベネッセコーポレーション", "ベネッセホールディングス",
    "ウォルトディズニージャパン", "ディズニー",
}
# 社名に含まれれば本業が金融・放送と断定できる高精度パターン
OBVIOUS_NON_AM_PATTERNS = [
    (re.compile(r"証券"), "証券（金融）"),
    (re.compile(r"アセットマネジメント"), "資産運用（金融）"),
    (re.compile(r"生命保険|損害保険"), "保険（金融）"),
    (re.compile(r"テレビジョン放送|放送$|放送株式会社"), "放送"),
]


def obvious_non_am_reason(name: str) -> str | None:
    core = core_name(name)
    for ex in OBVIOUS_NON_AM_EXACT:
        if core == core_name(ex):
            return f"有名な非製造業（{name}）"
    for pat, sector in OBVIOUS_NON_AM_PATTERNS:
        if pat.search(name):
            return f"本業が{sector}でAM・製造と無関係"
    return None


def not_entity_reason(name: str) -> str | None:
    """社名が法人として成立しない断片なら理由を返す。"""
    raw = (name or "").strip()
    n = unicodedata.normalize("NFKC", raw)
    if DATE_RE.search(n):
        return "日付を含む断片"
    if "様" in n:
        return "「様」を含む見出し断片"
    if "http" in n.lower() or "www." in n.lower():
        return "URL断片"
    if "..." in raw or "…" in raw:
        return "省略記号を含む切断断片"
    if n in GENERIC_ONLY or core_name(n) == "":
        return "汎称のみで固有名がない"
    for mk in HEADLINE_MARKERS:
        if mk in n and mk != "様":
            return f"見出し語「{mk}」を含む断片"
    if n.endswith("製品") or n.endswith("製"):
        return "製品名の断片"
    if "金属3Dプリンタ" in n:
        return "製品カテゴリ名"
    # 株式会社 + ASCIIのみ（型番・略号の断片: SLM125, PDF, as, EBM, LUMEX 等）
    m = re.fullmatch(r"株式会社([A-Za-z0-9\-]+)", n)
    if m:
        return "株式会社＋型番/略号のみの断片"
    m = re.fullmatch(r"([A-Za-z0-9\-]+)株式会社", n)
    if m and len(m.group(1)) <= 3 and not re.search(r"[ぁ-んァ-ヶ一-龥]", n):
        return "略号のみの断片"
    return None


# ---------------------------------------------------------------- 取得
class _TextLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._ltext: list[str] = []
        self._skip = 0
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href", "") or ""
            self._ltext = []
        if tag == "title":
            self._ltext = []
            self._href = "__title__"

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, "".join(self._ltext).strip()))
            self._href = ""
        if tag == "title" and self._href == "__title__":
            self.title = "".join(self._ltext).strip()
            self._href = ""
        if tag in ("p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "td", "th", "dt", "dd"):
            self.text.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href:
            self._ltext.append(data)


_cache: dict[str, dict | None] = {}
_last_fetch: dict[str, float] = {}


def fetch(url: str) -> dict | None:
    """{url(最終), text, links[(href,text)], title} を返す。失敗時 None。"""
    if url in _cache:
        return _cache[url]
    host = urlparse(url).netloc
    wait = 0.4 - (time.monotonic() - _last_fetch.get(host, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_fetch[host] = time.monotonic()
    result = None
    data = None
    final = url
    ctype = ""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                final = r.geturl()
                ctype = r.headers.get("Content-Type", "")
                data = r.read(3_000_000)
            break
        except Exception:
            data = None
            if attempt == 0:
                time.sleep(2.0)
    try:
        if data is None:
            raise IOError("fetch failed after retry")
        if "pdf" in ctype.lower() or final.lower().endswith(".pdf"):
            ptext = _pdf_text(data)
            if ptext:
                result = {"url": final, "text": ptext, "links": [], "title": "", "pdf": False}
            else:
                result = {"url": final, "text": "", "links": [], "title": "", "pdf": True}
        else:
            text_html = _decode(data)
            p = _TextLinks()
            try:
                p.feed(text_html)
            except Exception:
                pass
            links = []
            for href, lt in p.links:
                if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    links.append((urljoin(final, href), lt))
            txt = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.text))
            txt = re.sub(r"\n\s*\n+", "\n", txt).strip()
            result = {"url": final, "text": txt, "links": links, "title": p.title, "pdf": False}
    except Exception:
        result = None
    _cache[url] = result
    return result


def _pdf_text(data: bytes) -> str:
    """pypdf があれば PDF 本文を抜く（公設試の設備一覧は PDF が多い）。無ければ空。"""
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(data))
        parts = []
        for pg in r.pages[:12]:
            try:
                parts.append(pg.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _decode(data: bytes) -> str:
    head = data[:4096].decode("ascii", "ignore").lower()
    m = re.search(r"charset=([a-z0-9_\-]+)", head)
    encs = []
    if m:
        encs.append(m.group(1))
    encs += ["utf-8", "shift_jis", "euc_jp"]
    for e in encs:
        try:
            return data.decode(e)
        except Exception:
            continue
    return data.decode("utf-8", "ignore")


def host_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def reg_domain(host: str) -> str:
    """登録ドメイン（co.jp/ac.jp 等は3ラベル、都道府県型JPは pref を含む4ラベル）。"""
    labels = host.split(".")
    if len(labels) >= 4 and labels[-3] == "pref":
        return ".".join(labels[-4:])
    if len(labels) >= 3 and labels[-1] == "jp" and labels[-2] in \
            ("co", "ac", "or", "go", "ne", "lg", "ed", "gr"):
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def same_family(host_a: str, host_b: str) -> bool:
    """同一登録ドメイン配下（大学・機関のサブドメイン掲載を許す）。"""
    return reg_domain(host_a) == reg_domain(host_b)


# ---------------------------------------------------------------- 同定
OVERVIEW_KEYS = ["会社概要", "会社情報", "企業情報", "会社案内", "会社紹介", "会社要覧",
                 "about", "company", "outline", "profile", "corporate", "gaiyou", "annai"]
PREFS = ("北海道|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|"
         "東京都|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|"
         "滋賀県|京都府|大阪府|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|"
         "香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県")
ADDR_RE = re.compile(r"〒?\s*\d{3}\s*[-−－]\s*\d{4}\s*(" + PREFS + r"[^\n\s、,，]{0,40})")
ADDR_RE2 = re.compile(r"(" + PREFS + r")([^\n\s、,，]{1,12}?(?:市|区|町|村|郡))")
NAME_LABEL_RE = re.compile(r"(?:会社名|商号|法人名|団体名|名称)\s*[：:]\s*([^\n]{2,60})")


def extract_address(text: str) -> str:
    m = ADDR_RE.search(text)
    if m:
        return m.group(1).strip()
    # 所在地/本社/住所ラベルの近傍
    for lab in ("所在地", "本社所在地", "本社", "住所", "事業所"):
        idx = text.find(lab)
        if idx >= 0:
            m2 = ADDR_RE2.search(text[idx:idx + 200])
            if m2:
                return (m2.group(1) + m2.group(2)).strip()
    m3 = ADDR_RE2.search(text)
    return (m3.group(1) + m3.group(2)).strip() if m3 else ""


def identify(candidate: str, pages: list[dict]) -> dict:
    """pages: 概要候補ページ群（概要ページ優先）。同定結果を返す。"""
    core = core_name(candidate)
    out = {"name_on_site": "", "address": "", "matched": False, "weak": False,
           "mismatch_name": "", "overview_url": ""}
    for pg in pages:
        text = pg["text"]
        m = NAME_LABEL_RE.search(text)
        if m and not out["name_on_site"]:
            out["name_on_site"] = m.group(1).strip()
            out["overview_url"] = pg["url"]
        if not out["address"]:
            a = extract_address(text)
            if a:
                out["address"] = a
                if not out["overview_url"]:
                    out["overview_url"] = pg["url"]
    site_core = core_name(out["name_on_site"]) if out["name_on_site"] else ""
    if site_core and core:
        if site_core == core or site_core in core or core in site_core:
            out["matched"] = True
        else:
            out["mismatch_name"] = out["name_on_site"]
    if not out["matched"] and not out["mismatch_name"]:
        # ラベル付き社名が無い/取れない場合は、概要ページ群の本文にコア名が出るか
        joined = "\n".join(pg["text"] for pg in pages)
        joined_core = core_name(joined)
        alts = [core]
        # 下部組織（〜大学○○センター等）は親組織名を落とした形でも照合する
        m = re.match(r"^.+?(?:大学|工業高等専門学校|大学院)(.+)$", core)
        if m and len(m.group(1)) >= 4:
            alts.append(m.group(1))
        if any(a and a in joined_core for a in alts):
            out["matched"] = True
            out["weak"] = True
            if not out["name_on_site"]:
                out["name_on_site"] = candidate
    return out


# ---------------------------------------------------------------- 設備証拠
EQUIP_LINK_KEYS = ["設備", "保有設備", "主要設備", "装置", "施設", "工場", "サービス", "事業",
                   "製品", "技術", "受託", "造形", "ラボ", "センター", "機器", "導入事例",
                   "3d", "3D", "プリンタ", "プリンター", "printer", "printing", "additive",
                   "facility", "facilities", "equipment", "service", "business", "technology",
                   "plant", "works", "lab", "利用案内", "機器利用", "開放", "共用"]
# 強いキー（設備ページらしいリンクを優先して辿る）
STRONG_LINK_KEYS = ["設備", "装置", "facility", "facilities", "equipment", "機器",
                    "3dprinter", "3d-printer", "3Dプリンタ", "金属3d", "保有"]


def find_models(text: str, strip_names: list[str]) -> list[str]:
    t = unicodedata.normalize("NFKC", text)
    for sn in strip_names:
        if sn:
            t = t.replace(sn, " ")
            t = t.replace(core_name(sn), " ")
    found = []
    for m, rx in MODEL_RES:
        if rx.search(t):
            found.append(m)
    return found


def crawl_equipment(start_pages: list[dict], candidate_name: str, max_pages: int) -> dict:
    """公式ドメイン内を辿り型番を探す。{models, evidence_url, pages_fetched}"""
    seen: set[str] = set()
    queue: list[tuple[str, int]] = []
    host = ""
    all_pages: list[dict] = []
    for pg in start_pages:
        if pg:
            all_pages.append(pg)
            seen.add(pg["url"])
            host = host_of(pg["url"])
    def _enqueue(pg, depth):
        cands = []
        for href, lt in pg["links"]:
            hay = (lt or "") + " " + href.lower()
            if same_family(host_of(href), host) and href not in seen and \
                    any(k in hay for k in EQUIP_LINK_KEYS):
                strong = any(k in hay for k in STRONG_LINK_KEYS)
                cands.append((0 if strong else 1, href))
        for _, href in sorted(cands):
            if href not in seen:
                queue.append((href, depth))
                seen.add(href)
    for pg in start_pages:
        if pg:
            _enqueue(pg, 1)
    # sitemap.xml を種にする（設備ページが通常のリンク辿りで届かないサイト対策）
    if start_pages and start_pages[0]:
        base = start_pages[0]["url"]
        sm_url = f"{urlparse(base).scheme}://{urlparse(base).netloc}/sitemap.xml"
        sm = fetch(sm_url)
        sm_texts = []
        if sm and not sm.get("pdf"):
            sm_texts.append(sm["text"])
            for child in re.findall(r"https?://[^\s<]+\.xml", sm["text"])[:3]:
                c = fetch(child)
                if c and not c.get("pdf"):
                    sm_texts.append(c["text"])
        locs = []
        for t in sm_texts:
            locs += re.findall(r"https?://[^\s<]+", t)
        for u in locs:
            if same_family(host_of(u), host) and u not in seen and \
                    any(k in u.lower() for k in STRONG_LINK_KEYS + ["setsubi", "kiki", "yumekobo", "3d"]):
                queue.insert(0, (u, 1))
                seen.add(u)
    fetched = len(all_pages)
    evidence: dict[str, str] = {}
    order: list[str] = []
    def scan(pg):
        for m in find_models(pg["text"] + " " + pg["title"], [candidate_name]):
            if m not in evidence:
                evidence[m] = pg["url"]
                order.append(m)
    for pg in all_pages:
        scan(pg)
    qi = 0
    while qi < len(queue) and fetched < max_pages:
        url, depth = queue[qi]
        qi += 1
        pg = fetch(url)
        if not pg or pg.get("pdf"):
            continue
        fetched += 1
        all_pages.append(pg)
        scan(pg)
        if depth < 2:
            _enqueue(pg, depth + 1)
    return {"models": order, "evidence": evidence, "pages_fetched": fetched}


# ---------------------------------------------------------------- 本体
def gate_row(row: dict, max_pages: int) -> dict:
    name = (row.get("社名") or "").strip()
    cand = (row.get("候補URL") or "").strip()
    out = {"社名": name, "候補URL": cand, "最終URL": "", "ホスト": "", "結果": "",
           "理由コード": "", "詳細": "", "同定社名": "", "所在地": "", "型番": "",
           "設備証拠URL": "", "概要URL": "", "法人番号": "未取得", "取得ページ数": 0}

    reason = not_entity_reason(name)
    if reason:
        out.update(結果="REJECT", 理由コード="NOT_ENTITY", 詳細=reason)
        return out
    reason = obvious_non_am_reason(name)
    if reason:
        out.update(結果="REJECT", 理由コード="OBVIOUS_NON_AM",
                   詳細=reason + "。URL照合・設備言及があっても合格させない")
        return out
    if not cand:
        out.update(結果="REJECT", 理由コード="NO_OFFICIAL_URL", 詳細="候補公式URLなし（url-verdicts=NOT_FOUND）")
        return out

    host = host_of(cand)
    out["ホスト"] = host
    top = fetch(cand)
    if not top:
        out.update(結果="REVIEW", 理由コード="FETCH_FAILED", 詳細="候補URLを開けなかった")
        return out
    out["最終URL"] = top["url"]
    out["ホスト"] = host_of(top["url"])

    # ホームページ
    root_url = f"{urlparse(top['url']).scheme}://{urlparse(top['url']).netloc}/"
    home = top if top["url"].rstrip("/") == root_url.rstrip("/") else fetch(root_url)
    base_pages = [p for p in (top, home) if p]

    # 会社概要ページ探索
    overview_pages: list[dict] = []
    cand_links: list[str] = []
    for pg in base_pages:
        for href, lt in pg["links"]:
            if same_family(host_of(href), out["ホスト"]) and \
                    any(k in (lt or "").lower() or k in href.lower() for k in OVERVIEW_KEYS):
                if href not in cand_links:
                    cand_links.append(href)
    for u in cand_links[:3]:
        pg = fetch(u)
        if pg and not pg.get("pdf"):
            overview_pages.append(pg)
    id_pages = overview_pages + base_pages
    total_text = sum(len(p["text"]) for p in id_pages)
    out["取得ページ数"] = len(id_pages)

    ident = identify(name, id_pages)
    out["同定社名"] = ident["name_on_site"]
    out["所在地"] = ident["address"]
    out["概要URL"] = ident["overview_url"]

    if ident["mismatch_name"] and not ident["matched"]:
        out.update(結果="REJECT", 理由コード="IDENTITY_MISMATCH",
                   詳細=f"会社概要の社名は「{ident['mismatch_name']}」で候補と一致しない")
        return out
    if not ident["matched"]:
        if total_text < 300:
            out.update(結果="REVIEW", 理由コード="FETCH_THIN",
                       詳細="取得できた本文が薄く同定不能（JS描画等の可能性）")
        else:
            code = "IDENTITY_UNCONFIRMED" if not overview_pages else "IDENTITY_MISMATCH"
            out.update(結果="REVIEW" if code == "IDENTITY_UNCONFIRMED" else "REJECT",
                       理由コード=code,
                       詳細="会社概要ページが見つからず社名を同定できない" if code == "IDENTITY_UNCONFIRMED"
                            else "会社概要はあるが社名が一致しない")
        return out

    equip = crawl_equipment(base_pages + overview_pages, name, max_pages)
    out["取得ページ数"] += equip["pages_fetched"] - len(base_pages)
    if not equip["models"]:
        out.update(結果="REJECT", 理由コード="NO_EQUIPMENT_EVIDENCE",
                   詳細="公式ドメイン内に型番つき設備証拠なし" +
                        ("（同定は弱い一致）" if ident["weak"] else ""))
        return out
    out["型番"] = ";".join(equip["models"])
    first = equip["models"][0]
    out["設備証拠URL"] = equip["evidence"].get(first, "")
    if not ident["address"]:
        out.update(結果="REVIEW", 理由コード="ADDRESS_MISSING",
                   詳細="社名は一致し型番証拠もあるが所在地を取得できなかった")
        return out
    out.update(結果="PASS", 理由コード="PASS",
               詳細="同定OK＋型番証拠あり" + ("（同定は弱い一致：本文内コア名）" if ident["weak"] else ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--max-pages", type=int, default=14)
    args = ap.parse_args()

    with open(args.input, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        rows = [r for r in rows if (r.get("社名") or "").strip() in only]
    fields = ["社名", "候補URL", "最終URL", "ホスト", "結果", "理由コード", "詳細",
              "同定社名", "所在地", "型番", "設備証拠URL", "概要URL", "法人番号", "取得ページ数"]
    done = 0
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            res = gate_row(r, args.max_pages)
            w.writerow(res)
            f.flush()
            done += 1
            print(f"[{done}/{len(rows)}] {res['社名']}: {res['結果']} ({res['理由コード']}) "
                  f"models={res['型番'][:60]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
