"""一都三県の表彰・認定リストを種にした、芋づる式の企業リスト（資本金1億円以上・自動車を除く）。

本人の指示（2026-11-07〜09）：
- 種は大きな品質認証（IATF等）ではなく、地域の表彰・認定リスト。一都三県を地域ごとに
  （例：ベストベンチャー100、すごい町工場、大田の優工場、健康経営優良法人、ものづくり企業データベース）。
- 芋づる式：各社の会社概要・組織・取引先一覧・子会社一覧に出てくる名前を全部拾って、次の会社にする。
- 払う力が小さすぎる会社は切る（資本金1億円以下の目安）。自動車はA1の客から外す（2026-11-09）。
- 品質最優先：1社ずつ公式サイトの本文を読み、資本金・所在地・事業内容・取引先・子会社を引用とURLで残す。
  業種や自動車の判定はキーワード1つで決めない（2つの手がかりがそろったら確定、1つなら「要確認」で残す）。
  資本金が読めない会社は消さずに「資本金 未確認」で残す（3回の読み方：会社概要の本文→画像OCR→外部の検索結果）。

段階
  A. 種：表彰・認定の一覧（公式ページ・PDF・Excel）から社名を抜く。全国の一覧は、同じ行に一都三県の名がある社だけ取る。
     健康経営優良法人のように業種の列がある一覧は、同じ行に「製造」がある社だけ取る。
  B. 芋づる：1社ずつ公式サイトを探して会社概要を読み、資本金・所在地・設立・事業内容・主要取引先・子会社/グループ会社を取る。
     取引先・子会社に出てきた名前を次の段（深さ+1）に積む。次の段に積むのは、資本金1億円以上が確かめられた会社の名前だけ（深さ2まで）。
  C. 判定：対象＝一都三県に本社・資本金1億円以上・製造業（3つの手がかりのうち2つ・2026-11-09）・自動車の除外に当たらない。1億円ちょうどは「1億円ちょうど」と書いて残す。
  出力：out/award_chain/seeds.csv（種）、chain.csv（調べた全社と引用）、list.csv（対象だけ）、stats.md（集計）、
        inputs/award_chain.csv（対象。次の段の深掘り＝創業年度・株主構成は topics/award_chain.yaml）。
時間切れでも途中から再開する（キャッシュCSV）。
"""
from __future__ import annotations

import csv
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin

from researcher.build_setouchi_mfg import CORP, NAME_CHARS, NOT_COMPANY, PORTAL_TITLE, _is_portal, cmp_key, norm, urlparse
from researcher.targets import ROOT

OUT = ROOT / "out" / "award_chain"
SEEDS = OUT / "seeds.csv"
CHAIN = OUT / "chain.csv"
SRC_DONE = OUT / "sources_done.txt"
INPUT = ROOT / "inputs" / "award_chain.csv"
MAX_DEPTH = 2
MAX_COMPANIES = 6000

PREFS = ["東京都", "神奈川県", "埼玉県", "千葉県"]
PREF_SHORT = {"東京": "東京都", "神奈川": "神奈川県", "埼玉": "埼玉県", "千葉": "千葉県"}
# 一都三県の市区（同じ行に県名が無い一覧のため。区は東京23区）
_CITY_GROUPS = {
    "東京都": "千代田区 中央区 港区 新宿区 文京区 台東区 墨田区 江東区 品川区 目黒区 大田区 世田谷区 渋谷区 中野区 杉並区 豊島区 北区 荒川区 板橋区 練馬区 足立区 葛飾区 江戸川区 "
              "八王子市 立川市 武蔵野市 三鷹市 府中市 調布市 町田市 小金井市 日野市 国分寺市 青梅市 昭島市",
    "神奈川県": "横浜市 川崎市 相模原市 横須賀市 平塚市 藤沢市 厚木市 大和市 海老名市 小田原市 秦野市 綾瀬市 座間市",
    "埼玉県": "さいたま市 川口市 川越市 所沢市 越谷市 草加市 春日部市 上尾市 熊谷市 狭山市 入間市 戸田市 八潮市 三郷市",
    "千葉県": "千葉市 船橋市 市川市 松戸市 柏市 市原市 八千代市 習志野市 流山市 野田市 佐倉市 浦安市 木更津市",
}
CITY_PREF = {c: p for p, cs in _CITY_GROUPS.items() for c in cs.split()}
CITIES = list(CITY_PREF)

# 種の出典。(出典名, 範囲, 検索語の並び, 行の条件)。範囲「全国」は同じ行に一都三県の名がある社だけ取る。
# 行の条件「製造」は同じ行に「製造」の語がある社だけ取る（業種の列がある一覧）。
SOURCES: list[tuple[str, str, list[str], str]] = [
    ("健康経営優良法人（大規模）", "全国", ["健康経営優良法人 大規模法人部門 認定法人一覧 xlsx", "健康経営優良法人 2026 大規模法人部門 認定 一覧"], "製造"),
    ("健康経営優良法人（中小規模）", "全国", ["健康経営優良法人 中小規模法人部門 認定法人一覧 xlsx", "健康経営優良法人 2026 中小規模法人部門 認定 一覧"], "製造"),
    ("地域未来牽引企業", "全国", ["地域未来牽引企業 選定企業一覧 東京都", "地域未来牽引企業 選定企業一覧 神奈川県",
                                  "地域未来牽引企業 選定企業一覧 埼玉県", "地域未来牽引企業 選定企業一覧 千葉県"], ""),
    ("グローバルニッチトップ企業100選", "全国", ["グローバルニッチトップ企業100選 選定企業 一覧 経済産業省"], ""),
    ("はばたく中小企業・小規模事業者300社", "全国", ["はばたく中小企業・小規模事業者300社 一覧 東京都", "はばたく中小企業・小規模事業者300社 一覧 神奈川県"], ""),
    ("ものづくり日本大賞", "全国", ["ものづくり日本大賞 受賞者一覧 関東"], ""),
    ("東京都ベンチャー技術大賞", "東京都", ["東京都ベンチャー技術大賞 受賞企業 一覧"], ""),
    ("東京都 ものづくり企業データベース", "東京都", ["東京都 ものづくり企業 データベース 企業一覧"], ""),
    ("東京のすごい町工場", "東京都", ["すごい町工場 東京 一覧", "東京 町工場 表彰 企業一覧"], ""),
    ("大田区 優工場", "東京都", ["大田区 優工場 認定 一覧", "大田の工匠100人 一覧"], ""),
    ("葛飾ブランド・葛飾町工場物語", "東京都", ["葛飾町工場物語 認定企業", "葛飾ブランド 認定 企業"], ""),
    ("墨田区 すみだモダン", "東京都", ["すみだモダン 認証 事業者 一覧"], ""),
    ("板橋製品技術大賞", "東京都", ["板橋製品技術大賞 受賞 企業"], ""),
    ("神奈川工業技術開発大賞", "神奈川県", ["神奈川工業技術開発大賞 受賞 企業 一覧"], ""),
    ("横浜型地域貢献企業", "神奈川県", ["横浜型地域貢献企業 認定企業一覧 製造"], ""),
    ("川崎ものづくりブランド", "神奈川県", ["川崎ものづくりブランド 認定 一覧", "かわさき基準 KIS 認証 企業"], ""),
    ("さがみはら・ものづくり", "神奈川県", ["相模原 ものづくり 表彰 企業 一覧 さがみはら産業創造センター"], ""),
    ("彩の国工場", "埼玉県", ["彩の国工場 指定 一覧"], ""),
    ("渋沢栄一ビジネス大賞", "埼玉県", ["渋沢栄一ビジネス大賞 受賞 企業 一覧"], ""),
    ("埼玉ものづくり", "埼玉県", ["埼玉県 ものづくり 表彰 企業 一覧 経営革新"], ""),
    ("千葉ものづくり認定製品", "千葉県", ["千葉ものづくり認定製品 認定 企業 一覧"], ""),
    ("千葉県 経営革新・表彰", "千葉県", ["千葉県 ものづくり 表彰 企業 一覧"], ""),
]
LIST_LINK = re.compile(r"一覧|認定|選定|受賞|名簿|企業|事業者|リスト|list|\.xlsx?$|\.pdf$|\.csv$", re.I)
NAME_RX = re.compile(rf"{CORP}\s?({NAME_CHARS}{{2,25}})|({NAME_CHARS}{{2,25}})\s?{CORP}")

CAP_RX = re.compile(r"資本金[^0-9０-９]{0,8}([0-9０-９][0-9０-９,，\.．]*)\s*(億|千万|百万|万|千)?\s*([0-9０-９,，]*)\s*(万)?\s*円")
LABELS = {
    "所在地": r"所在地|本社所在地|本社|住所",
    "設立": r"設立|創業|創立",
    "事業内容": r"事業内容|事業概要|営業品目|主な事業|業務内容|主要製品|製品",
    "取引先": r"主要取引先|主な取引先|取引先|主要販売先|主な販売先|販売先|主要納入先|納入先|主要仕入先|仕入先|取引実績",
    "子会社": r"子会社|関連会社|関係会社|グループ会社|グループ企業|国内拠点|海外拠点",
    "従業員": r"従業員数|従業員|社員数",
}
ANY_LABEL = re.compile(r"^(?:" + "|".join(LABELS.values()) + r"|資本金|代表者|代表取締役|電話|TEL|FAX|URL|売上高|決算|許認可|取引銀行|主要取引銀行|加盟団体)")
SUBPAGE_RX = re.compile(r"会社概要|会社案内|企業情報|会社情報|企業概要|沿革|取引先|グループ|拠点|事業所|about|company|corporate|profile|outline|overview", re.I)
TOKEN_DROP = re.compile(r"様$|御中$|^他$|ほか|など|順不同|敬称略|五十音|以上|株式会社$|^\(|^（|社$")
AUTOMAKERS = ["トヨタ自動車", "日産自動車", "本田技研", "ホンダ", "マツダ", "SUBARU", "スバル", "スズキ", "ダイハツ", "三菱自動車", "いすゞ", "日野自動車",
              "三菱ふそう", "UDトラックス", "ヤマハ発動機", "デンソー", "アイシン", "豊田自動織機", "ジェイテクト", "トヨタ紡織", "日立Astemo", "ボッシュ"]
AUTO_WORDS = re.compile(r"自動車部品|車載|自動車用|自動車向け|カーメーカー|自動車メーカー|完成車|二輪車|自動車産業")

SEED_FIELDS = ["name", "key", "source", "pref", "line", "url"]
CHAIN_FIELDS = ["name", "key", "depth", "found_via", "status", "official_url", "pages_read", "capital_man", "capital_quote",
                "pref", "address_quote", "established_quote", "employees_quote", "business_quote", "partners", "subsidiaries",
                "partners_quote", "subsidiaries_quote", "auto_signals", "auto_judge",
                "mfg_industry", "mfg_signals", "mfg_judge", "judge"]

# 2026-11-09 本人指示：製造業かどうかを見ずに資本金と所在地だけで「対象」にしていた（58社の半分以上が不動産・IT・卸）。
# ベストベンチャー100と同じく中身で判定する。3つの手がかりのうち2つがそろって初めて「製造業」と確定する：
#  M1 産業の判定（classify_industry：取引先・客の呼び方・職種・許認可の2つ一致 →「製造業」。候補も1票）
#  M2 事業内容の欄に、自社で作る言い方（「○○の製造」「製造・販売」「加工」「成形」等）がある
#  M3 会社概要に工場・生産拠点・製造所の所在地が載っている
# 1つだけなら「製造業 要確認」（対象に入れず残す）、0なら「除外：製造業でない」。単語1つでは付けない。
OWN_PROD = re.compile(r"(?:の|を)(?:設計・)?(?:開発・)?(?:製造|生産|加工|成形|鋳造|鍛造|切削|研削|プレス|溶接|組立|めっき|塗装|熱処理)"
                      r"|(?:製造|生産|加工)(?:・|及び|および|、)(?:販売|加工|組立|修理)|(?:製造|加工|成形)業|受託(?:製造|加工|生産)")
FACTORY = re.compile(r"(?:^|\s)(?:[^\s]{0,10}工場|生産拠点|製造拠点|製造所|生産工場|事業所・工場)\s*[:：]?\s*(?:〒|[^\s]{0,6}[都道府県]|東京|神奈川|埼玉|千葉)")


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def valid(core: str) -> bool:
    if len(core) < 2 or len(core) > 25 or NOT_COMPANY.search(core):
        return False
    if re.fullmatch(r"[0-9\-\s\.]+", _nfkc(core)) or re.search(r"[！!？?。、「」『』【】：:／/]", core):
        return False
    return not re.search(r"(の|で|を|に|は|が|と|へ|や)$", core)


def names_in(text: str) -> list[str]:
    out = []
    for m in NAME_RX.finditer(text or ""):
        core = norm(m.group(1) or m.group(2) or "")
        if valid(core):
            out.append(core)
    return out


def pref_of(text: str) -> str:
    t = _nfkc(text)
    for p in PREFS:
        if p in t:
            return p
    for c in sorted(CITIES, key=lambda x: (x[-1] != "市", -len(x))):  # 市を先に見る（千葉市中央区・横浜市港北区を東京の区と取り違えない）
        if re.search(rf"(?<!市){c}", t) if c.endswith("区") else c in t:  # 「大阪市北区」を東京の北区にしない
            return _city_pref(c)
    for s, p in PREF_SHORT.items():
        if re.search(rf"(^|[\s　(（]){s}([\s　)）]|$)", t):
            return p
    return ""


def _city_pref(c: str) -> str:
    return CITY_PREF.get(c, "")


def capital_man(text: str) -> tuple[float | None, str]:
    """資本金の行 → 万円。「1億2,000万円」「100百万円」「3億円」「5,000万円」「10,000千円」。"""
    m = CAP_RX.search(_nfkc(text))
    if not m:
        return None, ""
    num = float(m.group(1).replace(",", "").replace("，", "").replace("．", "."))
    unit, rest = m.group(2) or "", (m.group(3) or "").replace(",", "")
    if unit == "億":
        v = num * 10000 + (float(rest) if rest and m.group(4) else 0)
    elif unit == "千万":
        v = num * 1000
    elif unit == "百万":
        v = num * 100
    elif unit == "万":
        v = num
    elif unit == "千":
        v = num / 10
    else:
        v = num / 10000
    return v, m.group(0)[:80]


def _read_csv(p) -> list[dict]:
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(p, rows: list[dict], fields: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _get_table(url: str) -> str:
    """Excel・CSVの一覧は行数が多い（健康経営は数万行）ので、行数の上限を上げて自分で読む。"""
    import requests
    from researcher.fetch import BROWSER_UA, table_text
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=90)
        return table_text(r.content, url.lower().split("?")[0], r.headers.get("Content-Type", "").lower(), max_rows=80000)
    except Exception:
        return ""


def _harvest(text: str, source: str, scope: str, need: str, url: str, seeds: dict) -> int:
    n = 0
    for line in (text or "").splitlines():
        if need and need not in line:
            continue
        p = pref_of(line)
        if scope == "全国" and not p:
            continue
        for nm in names_in(line):
            k = cmp_key(nm)
            if k in seeds:
                if source not in seeds[k]["source"].split(";"):
                    seeds[k]["source"] += ";" + source
                continue
            seeds[k] = {"name": nm, "key": k, "source": source, "pref": p or (scope if scope != "全国" else ""), "line": line.strip()[:160], "url": url}
            n += 1
    return n


def collect_seeds(deadline: float) -> bool:
    from researcher.fetch import fetch
    from researcher.search import search, is_spam
    seeds = {r["key"]: r for r in _read_csv(SEEDS)}
    done = set(SRC_DONE.read_text(encoding="utf-8").splitlines()) if SRC_DONE.exists() else set()
    # ベストベンチャー100（既にある名簿）も種に入れる
    for r in _read_csv(ROOT / "out" / "bv100" / "names.csv"):
        k = cmp_key(r.get("name", ""))
        if k and k not in seeds:
            seeds[k] = {"name": r["name"], "key": k, "source": "ベストベンチャー100", "pref": "", "line": "", "url": r.get("urls", "")[:200]}
    for source, scope, queries, need in SOURCES:
        for q in queries:
            if q in done:
                continue
            if time.monotonic() > deadline:
                _write_csv(SEEDS, list(seeds.values()), SEED_FIELDS)
                return False
            seen = set()
            for h in search(q, 10)[:8]:
                u = h["url"]
                if u in seen or is_spam(u):
                    continue
                seen.add(u)
                pages = [u]
                p = fetch(u)
                txt = _get_table(u) if p.get("kind") == "table" else p.get("text", "")
                got = _harvest(txt, source, scope, need, u, seeds)
                # 一覧へのリンク（同じサイトのPDF・Excel・一覧ページ）を8本までたどる
                host = urlparse(p.get("final_url") or u).netloc
                for label, href in (p.get("links") or [])[:400]:
                    if len(pages) > 8:
                        break
                    if urlparse(href).netloc != host or href in seen or not (LIST_LINK.search(label or "") or LIST_LINK.search(href)):
                        continue
                    seen.add(href)
                    pages.append(href)
                    low = href.lower().split("?")[0]
                    t2 = _get_table(href) if low.endswith((".xlsx", ".xls", ".csv")) else fetch(href).get("text", "")
                    got += _harvest(t2, source, scope, need, href, seeds)
                print(f"seed [{source}] {u} -> +{got}", flush=True)
            done.add(q)
            SRC_DONE.write_text("\n".join(sorted(done)), encoding="utf-8")
            _write_csv(SEEDS, list(seeds.values()), SEED_FIELDS)
    _write_csv(SEEDS, list(seeds.values()), SEED_FIELDS)
    return True


def _block(lines: list[str], i: int, label_rx: str) -> str:
    """ラベルの行とその後の行（次のラベルが来るまで、最大8行）をつなぐ。表の『ラベル 値』も同じ行で拾う。"""
    out = [re.sub(rf"^.*?(?:{label_rx})\s*[:：]?\s*", "", lines[i])]
    for ln in lines[i + 1:i + 9]:
        if ANY_LABEL.match(ln.strip()):
            break
        out.append(ln)
    return " ".join(x.strip() for x in out if x.strip())[:600]


def _tokens(block: str) -> list[str]:
    """取引先・子会社の欄の名前。『株式会社○○』の形に加え、読点・空白で区切られた名前も取る（本人：名前は全部拾う）。"""
    names = names_in(block)
    for tok in re.split(r"[、,，／/\s　・;；]+", re.sub(rf"{CORP}", "", block)):
        tok = norm(tok.strip("()（）「」[]【】"))
        if valid(tok) and not TOKEN_DROP.search(tok) and re.search(r"[一-龥ァ-ヶA-Za-z]", tok) and tok not in names:
            names.append(tok)
    return names[:40]


def find_official(name: str) -> str:
    from researcher.search import search
    for q in (f"{name} 会社概要", f"株式会社{name}"):
        for h in search(q, 10):
            title = h.get("title", "")
            if _is_portal(h["url"]) or PORTAL_TITLE.search(title) and "会社概要" not in title:
                continue
            if cmp_key(name) and cmp_key(name) in cmp_key(title):
                pr = urlparse(h["url"])
                return f"{pr.scheme}://{pr.netloc}/"
    return ""


def check_one(name: str, depth: int, via: str) -> dict:
    from researcher.fetch import fetch
    from researcher.search import search
    r = {"name": name, "key": cmp_key(name), "depth": depth, "found_via": via, "status": "", "official_url": "", "pages_read": ""}
    home = find_official(name)
    if not home:
        r["status"] = "公式サイト見つからず"
        return r
    r["official_url"] = home
    first = fetch(home)
    host = urlparse(first.get("final_url") or home).netloc
    pages, texts = [home], [first.get("text", "")]
    for label, href in (first.get("links") or [])[:300]:
        if len(pages) >= 7:
            break
        if urlparse(href).netloc == host and href not in pages and (SUBPAGE_RX.search(label or "") or SUBPAGE_RX.search(href)):
            pages.append(href)
            texts.append(fetch(href).get("text", ""))
    # 画像の会社概要（OCR）：本文に資本金が無いときだけ、会社概要らしいページの画像を2枚まで読む
    if not any("資本金" in t for t in texts):
        for href in pages[1:3]:
            p = fetch(href)
            for img in (p.get("images") or [])[:2]:
                src = img[1] if isinstance(img, (list, tuple)) else img
                t = fetch(urljoin(href, src)).get("text", "") if src else ""
                if "資本金" in t:
                    texts.append(t)
    r["pages_read"] = " ".join(pages)
    lines = [ln.strip() for t in texts for ln in t.splitlines() if ln.strip()]
    got = {k: "" for k in LABELS}
    for i, ln in enumerate(lines):
        for k, rx in LABELS.items():
            if not got[k] and re.match(rf"^(?:{rx})(?:\s|[:：]|$)", ln):
                got[k] = _block(lines, i, rx)
    cap, quote = None, ""
    for ln in lines:
        if "資本金" in ln:
            cap, quote = capital_man(ln if re.search(r"\d", _nfkc(ln)) else ln + " " + lines[min(len(lines) - 1, lines.index(ln) + 1)])
            if cap is not None:
                break
    if cap is None:  # 3回目：外部の検索結果の抜粋
        for h in search(f"{name} 資本金", 8):
            if cmp_key(name) in cmp_key(h.get("title", "") + h.get("snippet", "")):
                cap, quote = capital_man(h.get("snippet", ""))
                if cap is not None:
                    quote = f"{quote}（外部：{h['url'][:80]}）"
                    break
    r["capital_man"] = "" if cap is None else f"{cap:g}"
    r["capital_quote"] = quote
    r["address_quote"] = got["所在地"][:160]
    r["pref"] = pref_of(got["所在地"]) or pref_of(" ".join(lines[:5]))
    r["established_quote"] = got["設立"][:80]
    r["employees_quote"] = got["従業員"][:80]
    r["business_quote"] = got["事業内容"][:300]
    partners, subs = _tokens(got["取引先"]), _tokens(got["子会社"])
    r["partners"], r["subsidiaries"] = ";".join(partners), ";".join(subs)
    r["partners_quote"], r["subsidiaries_quote"] = got["取引先"][:400], got["子会社"][:400]
    sig = []
    if AUTO_WORDS.search(got["事業内容"]):
        sig.append("事業内容：" + AUTO_WORDS.search(got["事業内容"]).group(0))
    hit = [a for a in AUTOMAKERS if a in got["取引先"]]
    if hit:
        sig.append("取引先：" + "・".join(hit[:4]))
    r["auto_signals"] = " / ".join(sig)
    r["auto_judge"] = "自動車（除外）" if len(sig) >= 2 else ("自動車（要確認）" if sig else "")
    # 製造業かどうか（3つの手がかりのうち2つ）
    from researcher import classify_industry as ci
    text = "\n".join(texts)
    try:
        code, how = ci.judge(ci.score(text, ci._listed_map()))
    except Exception:
        code, how = "", "要読込"
    r["mfg_industry"] = f"{ci.JSIC.get(code, '不明')}（{how}）"
    sig = []
    if code == "E" and how in ("確定", "候補"):
        sig.append(f"M1 産業判定：製造業（{how}）")
    m = OWN_PROD.search(got["事業内容"])
    if m:
        sig.append("M2 事業内容：" + got["事業内容"][max(0, m.start() - 20): m.end() + 10].replace("\n", " "))
    m = FACTORY.search("\n".join(lines))
    if m:
        sig.append("M3 拠点：" + m.group(0).strip()[:40])
    r["mfg_signals"] = " / ".join(sig)
    r["mfg_judge"] = "製造業" if len(sig) >= 2 else ("製造業 要確認" if sig else "製造業でない")
    r["status"] = "読了"
    r["judge"] = judge(r)
    return r


def judge(r: dict) -> str:
    if r.get("status") != "読了":
        return "未判定（" + (r.get("status") or "未読") + "）"
    if r.get("auto_judge") == "自動車（除外）":
        return "除外：自動車"
    cap = r.get("capital_man")
    if not cap:
        return "資本金 未確認"
    c = float(cap)
    if c < 10000:
        return "除外：資本金1億円未満"
    if r.get("pref") not in PREFS:
        return "対象外：一都三県の外" if r.get("pref") else "所在地 未確認（資本金1億円以上）"
    mj = r.get("mfg_judge", "")
    if mj == "製造業でない":
        return "除外：製造業でない（" + (r.get("mfg_industry") or "産業不明") + "）"
    if mj != "製造業":
        return "製造業 要確認" if mj else "製造業 未判定"
    tag = "（1億円ちょうど）" if c == 10000 else ""
    return "対象" + tag + ("・自動車要確認" if r.get("auto_judge") else "")


def run_chain(deadline: float) -> bool:
    rows = {r["key"]: r for r in _read_csv(CHAIN)}
    # 2026-11-09 製造業の判定を足す前に読んだ会社のうち、資本金1億円以上・一都三県の会社は読み直す（他の会社は資本金で外れているので読まない）
    redo = [r for r in rows.values() if r.get("status") == "読了" and not r.get("mfg_judge")
            and r.get("capital_man") and float(r["capital_man"]) >= 10000 and r.get("pref") in PREFS]
    for r in redo:
        if time.monotonic() > deadline:
            _write_csv(CHAIN, list(rows.values()), CHAIN_FIELDS)
            return False
        try:
            nr = check_one(r["name"], int(r.get("depth") or 0), r.get("found_via", ""))
            rows[r["key"]] = nr
            print(f"redo {r['name']}: {nr.get('judge')} {nr.get('mfg_signals','')[:80]}", flush=True)
        except Exception as e:
            r["judge"] = f"未判定（読み直しエラー:{type(e).__name__}）"
    if redo:
        _write_csv(CHAIN, list(rows.values()), CHAIN_FIELDS)
    queue = [(s["name"], 0, s["source"]) for s in _read_csv(SEEDS)]
    for r in list(rows.values()):  # 資本金1億円以上が確かめられた会社の取引先・子会社だけ次の段へ
        d = int(r.get("depth") or 0)
        if d < MAX_DEPTH and r.get("capital_man") and float(r["capital_man"]) >= 10000:
            for nm in filter(None, (r.get("partners", "") + ";" + r.get("subsidiaries", "")).split(";")):
                queue.append((nm, d + 1, f"{r['name']}の取引先・子会社"))
    todo, seen = [], set(rows)
    for nm, d, via in queue:
        k = cmp_key(nm)
        if k and k not in seen:
            seen.add(k)
            todo.append((nm, d, via))
    for nm, d, via in todo:
        if len(rows) >= MAX_COMPANIES:
            break
        if time.monotonic() > deadline:
            _write_csv(CHAIN, list(rows.values()), CHAIN_FIELDS)
            return False
        try:
            r = check_one(nm, d, via)
        except Exception as e:
            r = {"name": nm, "key": cmp_key(nm), "depth": d, "found_via": via, "status": f"読み取りエラー:{type(e).__name__}"}
            r["judge"] = judge(r)
        rows[r["key"]] = r
        print(f"chain d{d} {nm}: {r.get('judge')} cap={r.get('capital_man')} pref={r.get('pref')}", flush=True)
        if len(rows) % 10 == 0:
            _write_csv(CHAIN, list(rows.values()), CHAIN_FIELDS)
        # 新しく1億円以上が確かめられた会社が出たら、その取引先も同じ回の後ろに積む
        if d < MAX_DEPTH and r.get("capital_man") and float(r["capital_man"]) >= 10000:
            for nm2 in filter(None, (r.get("partners", "") + ";" + r.get("subsidiaries", "")).split(";")):
                k2 = cmp_key(nm2)
                if k2 and k2 not in seen:
                    seen.add(k2)
                    todo.append((nm2, d + 1, f"{nm}の取引先・子会社"))
    _write_csv(CHAIN, list(rows.values()), CHAIN_FIELDS)
    return len(rows) >= MAX_COMPANIES or all(cmp_key(n) in rows for n, _, _ in todo)


def write_outputs(finished: bool) -> None:
    rows = _read_csv(CHAIN)
    seeds = _read_csv(SEEDS)
    ok = [r for r in rows if (r.get("judge") or "").startswith("対象") and not r["judge"].startswith("対象外")]
    _write_csv(OUT / "list.csv", ok, CHAIN_FIELDS)
    INPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in ok:
            w.writerow([r["name"], r.get("official_url", ""), "", f"{r.get('pref', '')}・資本金{r.get('capital_man', '')}万円・{r.get('found_via', '')}"])
    from collections import Counter
    jc = Counter(r.get("judge", "") for r in rows)
    sc = Counter(s for r in seeds for s in r.get("source", "").split(";") if s)
    pc = Counter(r.get("pref", "") for r in ok)
    dc = Counter(r.get("depth", "") for r in ok)
    mq = [r for r in rows if r.get("judge") == "製造業 要確認"]
    _write_csv(OUT / "mfg_review.csv", mq, CHAIN_FIELDS)  # 手がかりが1つだけの会社。人（または別のAI）が読んで決める
    md = [f"# 表彰・認定リスト起点の芋づる（一都三県・資本金1億円以上・製造業・自動車除く）{'（完了）' if finished else '（途中）'}", "",
          f"- 種：{len(seeds)}社　調べた会社：{len(rows)}社　対象：{len(ok)}社", "", "## 判定の内訳"]
    md += [f"- {k or '空'}：{v}" for k, v in jc.most_common()]
    md += ["", "## 対象の都県"] + [f"- {k or '空'}：{v}" for k, v in pc.most_common()]
    md += ["", "## 対象の深さ（0＝表彰リストにいた会社、1以上＝その取引先・子会社）"] + [f"- 深さ{k}：{v}" for k, v in sorted(dc.items())]
    md += ["", "## 種の出典ごとの社数"] + [f"- {k}：{v}" for k, v in sc.most_common()]
    (OUT / "stats.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(ok)}\n", encoding="utf-8")


def main(deadline_s: float = 3 * 3600) -> int:
    deadline = time.monotonic() + deadline_s
    OUT.mkdir(parents=True, exist_ok=True)
    if not collect_seeds(deadline):
        write_outputs(False)
        print("award_chain: seeds time up, continue next run", flush=True)
        return 0
    write_outputs(run_chain(deadline))
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
