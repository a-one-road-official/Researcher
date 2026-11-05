"""瀬戸内（＋全国の町工場）の製造業中小企業リスト作りと、1社ずつの求人・想定年収の突き合わせ。

本人の指示（2026-11-05）：
- 社名を洗う：タウンページ、地元の採用情報サイト、地元の大学・高専の合同企業説明会／採用セミナーの参加企業。
- マイナビ等の求人に書いてある「想定年収○○万円」を拾う。
- 社名は必ずググって照らし合わせ（実在・製造業・所在地）、整合したものだけを残す。

段階
  A. 社名集め：検索（Yahoo! JAPAN＋ddgs）の結果と、robots.txt が許すページ本文から「株式会社○○」等を抜く。
     どの出典（大学の説明会／タウンページ／求人サイト／県の企業ガイド）で何回出たかを残す。
  B. 照合：1社ずつ「社名＋県＋製造」で検索し、製造業の語と所在地が一致するかを見る。
     続けて「社名＋求人 年収」で検索し、スニペット（と robots が許す求人ページ1枚）から
     想定年収・月給・求人媒体（マイナビ／Wantedly／Indeed／ハローワーク等）を抜く。
  出力：out/setouchi_mfg/names.csv（段階A）、checked.csv（段階B）、stats.md（集計）、
        inputs/setouchi_mfg.csv（照合済みの製造業だけ。会社ごとの公式サイト調査は topics/setouchi_mfg.yaml）。
時間切れでも途中から再開する（キャッシュCSV）。求人サイトは1社1ページまで・ホスト毎に間隔を空ける。
"""
from __future__ import annotations

import csv
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlparse

from researcher.targets import ROOT

OUT = ROOT / "out" / "setouchi_mfg"
NAMES = OUT / "names.csv"
CHECKED = OUT / "checked.csv"
SOURCES_DONE = OUT / "sources_done.txt"
MAX_COMPANIES = 1500

PREFS = ["広島県", "岡山県", "山口県", "愛媛県", "香川県"]
CITIES = {
    "広島県": ["広島市", "福山市", "呉市", "東広島市", "尾道市", "三原市", "廿日市市", "府中市", "三次市", "竹原市", "大竹市", "安芸郡", "庄原市"],
    "岡山県": ["岡山市", "倉敷市", "津山市", "総社市", "井原市", "笠岡市", "玉野市", "赤磐市", "備前市", "瀬戸内市", "浅口市", "新見市", "真庭市"],
    "山口県": ["下関市", "宇部市", "山口市", "周南市", "防府市", "岩国市", "下松市", "光市", "山陽小野田市", "柳井市", "萩市", "長門市"],
    "愛媛県": ["松山市", "今治市", "新居浜市", "西条市", "四国中央市", "宇和島市", "大洲市", "伊予市", "東温市", "八幡浜市"],
    "香川県": ["高松市", "丸亀市", "坂出市", "観音寺市", "善通寺市", "三豊市", "さぬき市", "東かがわ市", "綾歌郡", "仲多度郡"],
}
SCHOOLS = {
    "広島県": ["広島大学", "広島工業大学", "県立広島大学", "近畿大学工学部", "福山大学", "広島国際大学", "呉工業高等専門学校", "広島商船高等専門学校"],
    "岡山県": ["岡山大学", "岡山理科大学", "岡山県立大学", "津山工業高等専門学校"],
    "山口県": ["山口大学", "山口東京理科大学", "宇部工業高等専門学校", "徳山工業高等専門学校", "大島商船高等専門学校"],
    "愛媛県": ["愛媛大学", "松山大学", "新居浜工業高等専門学校", "弓削商船高等専門学校"],
    "香川県": ["香川大学", "香川高等専門学校"],
}
TRADES = ["金属加工", "精密機械加工", "板金", "製缶", "溶接", "鋳造", "鍛造", "プレス加工", "樹脂成形", "金型",
          "造船", "舶用機器", "自動車部品", "産業機械", "電子部品", "めっき", "熱処理", "部品製造"]

MFG_WORDS = ["製造", "加工", "工場", "メーカー", "部品", "機械", "金属", "樹脂", "造船", "鋳", "鍛", "溶接", "板金",
             "製缶", "金型", "プレス", "めっき", "塗装", "組立", "舶用", "電子", "化学", "ゴム", "素材", "鉄工", "工業"]
MEDIA = {
    "マイナビ転職": "tenshoku.mynavi.jp", "マイナビ新卒": "job.mynavi.jp", "マイナビバイト": "baito.mynavi.jp",
    "Wantedly": "wantedly.com", "Indeed": "indeed.com", "ハローワーク": "hellowork.mhlw.go.jp",
    "doda": "doda.jp", "リクナビNEXT": "next.rikunabi.com", "リクナビ新卒": "job.rikunabi.com",
    "エン転職": "employment.en-japan.com", "engage": "en-gage.net", "type": "type.jp",
    "求人ボックス": "xn--pckua2a7gp15o89zb.com", "ジモティー": "jmty.jp", "キャリアインデックス": "careerindex.jp",
    "はたらいく": "hataraiku.jp", "タウンワーク": "townwork.net", "バイトル": "baitoru.com",
    "スタンバイ": "jp.stanby.com", "求人ジャーナル": "kyujin-journal.net", "地元求人(広島)": "hiroshima-kyujin",
    "工場ワークス": "factory-works.jp", "工場求人ナビ": "job-con.jp",
}
PORTALS = set(MEDIA.values()) | {
    "yelp.com", "yelp.co.jp", "compalyze.co.jp", "ipros.com", "tsujigawa.com", "yellowpages.net", "sanyonews.jp",
    "chugoku-np.co.jp", "ehime-np.co.jp", "shikoku-np.co.jp", "lg.jp", "go.jp", "or.jp", "chuokai", "jobhakase.com", "bizmap.jp",
    "jpnumber.com", "townpage", "biz-maps.com", "nikkan.co.jp", "zenkoku-houjin", "sokuho", "ekiten.jp", "goo.ne.jp", "jp.indeed.com",
    "stanby.com", "kyujinbox.com", "en-japan.com", "rikunabi.com", "doda.jp", "mynavi.jp", "wantedly.com", "hellowork",
    "itp.ne.jp", "baseconnect.in", "houjin.info", "houjin-bangou.nta.go.jp", "en-hyouban.com", "openwork.jp",
    "wikipedia.org", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com", "linkedin.com",
    "mapion.co.jp", "navitime.co.jp", "google.com", "yahoo.co.jp", "kaisharesearch.com", "salesnow.jp",
    "musubu.in", "ullet.com", "kabutan.jp", "minkabu.jp", "nikkei.com", "prtimes.jp", "jobtalk.jp", "careerconnection.jp",
    # 2026-11-05 追加（METHOD.md 4-8）：企業DB・電話帳・地図・口コミ・ブログ・まとめは公式にしない
    "catr.jp", "houjin.jp", "alarmbox.jp", "jpubb.com", "kigyou", "tdb.co.jp", "tsr-net.co.jp", "g-search", "imitsu.jp",
    "mapfan.com", "tabelog.com", "hotpepper.jp", "job-medley.com", "hatarako.net", "kakaku.com", "bing.com", "duckduckgo.com",
    "note.com", "ameblo.jp", "hatenablog", "fc2.com", "livedoor", "j-net21", "mirasapo", "smrj.go.jp", "jetro.go.jp",
    "nta.go.jp", "e-stat", "monotaro.com", "aperza.jp", "metoree.com", "emidas", "ncnet", "nc-net.or.jp", "mono.ipros.com",
    "buffett-code.com", "irbank.net", "kaisha-hyouban", "careerjet", "glassdoor", "jobcatalog", "arubaito-ex", "gakujo.ne.jp",
    "career-tasu.jp", "offerbox.jp", "onecareer.jp", "unistyle.jp", "shukatsu", "rikeinavi", "kosen", "jobway", "hellowork.careers",
}
# 大手のグループ会社（社名に親の名前が入る）。中小の一覧に入れない
BIG_GROUP = re.compile(r"^(マツダ|トヨタ|デンソー|三菱|日立|東芝|パナソニック|川崎重工|住友|三井|JFE|ＪＦＥ|日本製鉄|神戸製鋼|コベルコ|IHI|ＩＨＩ|"
                       r"ダイハツ|スズキ|ホンダ|本田|日産|いすゞ|ヤンマー|クボタ|コマツ|小松|ダイキン|帝人|東レ|旭化成|三井化学|宇部興産|UBE|"
                       r"トクヤマ|東ソー|クラレ|カネカ|今治造船|ジャパンマリンユナイテッド|常石|新来島|富士通|NEC|シャープ|京セラ|村田|オムロン|"
                       r"キヤノン|リコー|ブリヂストン|横浜ゴム|ENEOS|出光|中国電力|四国電力|ＪＲ|JR|日本郵船|商船三井)")
# 公式サイトの検索結果の題名に出る、ポータル・DB・求人の言葉
PORTAL_TITLE = re.compile(r"求人|採用情報[ -－|｜]+.*(マイナビ|リクナビ|doda|Indeed)|口コミ|評判|ランキング|一覧|企業情報[ -－|｜]|会社情報[ -－|｜]|"
                          r"の会社概要|決算|電話番号|地図|アクセス[ -－|｜]+.*(ナビ|マップ)|reviews|Yelp|法人番号|基本情報|企業データ|年収|就職")
NOT_COMPANY = re.compile(r"マイナビ|リクルート|パーソル|インディード|Indeed|ウォンテッド|エン・ジャパン|ディップ|ハローワーク|"
                         r"大学|高専|学校|銀行|信用金庫|信金|証券|保険|新聞|放送|テレビ|商工会|会議所|協会|組合|財団|機構|"
                         r"市役所|県庁|NTT|タウンページ|ヤフー|グーグル|楽天|求人|人材|派遣|紹介|コンサル|不動産|ホテル|病院|"
                         r"薬局|歯科|介護|保育|建設|工務店|住宅|塗装店|運輸|運送|物流|商事|商店|販売|ショップ|"
                         r"不要|歓迎|募集|未経験|経験者|年収|月給|時給|日給|賞与|土日|週休|残業|応募|自己PR|志望動機|面接|勤務|正社員|"
                         r"ソーシング|アウトソーシング|契約社員|アルバイト|パート|新着|会社情報|会社概要|採用情報|企業情報|特集|一覧|ランキング|おすすめ|まとめ|求人票|OK|ＯＫ")
CORP = r"(?:株式会社|有限会社|合同会社|㈱|㈲|\(株\)|（株）|\(有\)|（有）)"
NAME_CHARS = r"[一-龥ぁ-んァ-ヶーA-Za-zＡ-Ｚａ-ｚ0-9０-９・&＆\-]"
NAME_RX = re.compile(rf"{CORP}\s?({NAME_CHARS}{{2,18}})|({NAME_CHARS}{{2,18}})\s?{CORP}")
SALARY_RX = re.compile(r"(?:想定)?年収\s*[:：]?\s*(\d{3,4})(?:\.\d)?\s*万円?\s*(?:[〜~～\-－―]\s*(\d{3,4})(?:\.\d)?\s*万円)?")
MONTHLY_RX = re.compile(r"月給\s*[:：]?\s*(\d{2})(?:[.,]\d+)?\s*万")
MONTHLY_YEN_RX = re.compile(r"月給\s*[:：]?\s*(\d{3}),?(\d{3})\s*円")
EMP_RX = re.compile(r"従業員(?:数)?\s*[:：]?\s*(?:約)?\s*(\d[\d,]{0,5})\s*(?:名|人)")
CAP_RX = re.compile(r"資本金\s*[:：]?\s*(\d[\d,\.]*)\s*(億|千万|百万|万)円")
HIRE_RX = re.compile(r"(?:採用予定|募集)人数\s*[:：]?\s*(\d{1,3})\s*名")

NAME_FIELDS = ["name", "pref", "mentions", "source_types", "source_urls"]
CHECK_FIELDS = ["name", "pref", "status", "official_url", "city", "mfg_hits", "employees", "capital_man",
                "media", "salary_min_man", "salary_max_man", "monthly_man", "hire_count", "job_urls", "verify_quote", "tries"]


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_portal(url: str) -> bool:
    h = _host(url)
    if h in ("t.co", "bit.ly") or h.startswith(("pref.", "www.pref.", "city.", "www.city.", "town.", "www.town.")) or h.endswith((".ac.jp", ".ed.jp", ".lg.jp", ".go.jp")):
        return True
    return any(p in h for p in PORTALS)


def _is_official_hit(h: dict, name: str) -> bool:
    """公式サイトと認める検索結果：ポータルでない／題名に社名がある／題名がDB・求人・口コミの型でない。
    ドメインや電話番号の一致は「関連の候補」にすぎず、それだけで公式と決めない（METHOD.md 4-8）。"""
    if _is_portal(h["url"]):
        return False
    title = h.get("title", "")
    if name not in title or PORTAL_TITLE.search(title):
        return False
    path = urlparse(h["url"]).path.lower()
    return not re.search(r"/(company|corp|kigyo|kaisha|detail|shop|spot|biz)/\d", path)  # DBの個別ページの型


def _listed() -> set:
    """上場企業（out/ir1000/screen.csv の全上場一覧）。中小の一覧から外す。"""
    f = ROOT / "out" / "ir1000" / "screen.csv"
    if not f.exists():
        return set()
    with open(f, encoding="utf-8") as fh:
        return {norm(r["name"]) for r in csv.DictReader(fh)}


def norm(name: str) -> str:
    n = re.sub(CORP, "", name)
    n = n.translate(str.maketrans("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９＆",
                                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789&"))
    return n.strip(" ・-")


def valid_name(core: str) -> bool:
    """社名として通すか。求人サイトの文言・見出し・業種名・大手のグループ会社は通さない（METHOD.md 2章・4-8）。"""
    if core in TRADES or core in MFG_WORDS or core.endswith(("業", "業界")) and len(core) <= 4:
        return False
    if len(core) < 2 or NOT_COMPANY.search(core) or re.fullmatch(r"[0-9A-Za-z\-]+", core) and len(core) < 3:
        return False
    if re.search(r"[！!？?。、「」『』【】：:／/]", core) or re.search(r"(の|で|を|に|は|が|と)$", core):  # 文の切れ端
        return False
    if BIG_GROUP.match(core):
        return False
    return True


def names_in(text: str) -> list[str]:
    out = []
    for m in NAME_RX.finditer(text or ""):
        core = norm(m.group(1) or m.group(2) or "")
        if valid_name(core):
            out.append(core)
    return out


def source_plan() -> list[tuple[str, str, str]]:
    """(source_type, pref, query). 本人指定の出典の順：大学・高専の説明会 → タウンページ → 地元の採用情報 → 求人サイト。"""
    plan = []
    for pref, schools in SCHOOLS.items():
        for s in schools:
            plan.append(("大学・高専の企業説明会", pref, f"{s} 合同企業説明会 参加企業 一覧"))
            plan.append(("大学・高専の企業説明会", pref, f"{s} 学内企業説明会 参加企業 製造"))
    for pref in PREFS:
        plan += [
            ("地元の合同説明会", pref, f"{pref} 合同企業説明会 参加企業一覧 ものづくり"),
            ("地元の合同説明会", pref, f"{pref} 高校生 就職 企業説明会 参加企業 製造業"),
            ("県・市の企業ガイド", pref, f"{pref} ものづくり企業 ガイドブック 一覧"),
            ("県・市の企業ガイド", pref, f"{pref} 中小企業 製造業 企業紹介 就職"),
            ("県・市の企業ガイド", pref, f"{pref} 経営革新計画 承認企業 一覧 製造"),
            ("地元の採用情報", pref, f"{pref} 地元企業 就職 ナビ 製造業"),
            ("Wantedly", pref, f"wantedly {pref} 製造業 町工場"),
        ]
        for t in TRADES:
            plan.append(("タウンページ", pref, f"itp.ne.jp {pref} {t}"))
            plan.append(("マイナビ", pref, f"マイナビ転職 {pref} {t} 想定年収"))
        for c in CITIES[pref]:
            plan.append(("タウンページ", pref, f"タウンページ {c} 金属加工 OR 機械製造 OR 部品"))
            plan.append(("地元の採用情報", pref, f"{c} 製造業 求人 正社員 年収"))
    for t in TRADES:  # 瀬戸内以外（本人：「瀬戸内じゃなくてもいい、町工場とか製造業の中小企業」）
        plan.append(("マイナビ", "全国", f"マイナビ転職 町工場 {t} 想定年収"))
        plan.append(("Wantedly", "全国", f"wantedly 町工場 {t}"))
    return plan


FETCH_SOURCE_HOSTS = ("ac.jp", "go.jp", "lg.jp", "pref.", "city.", "itp.ne.jp", "mynavi.jp", "wantedly.com", "or.jp")


def collect_names(deadline: float) -> bool:
    from researcher.fetch import fetch
    from researcher.search import search

    OUT.mkdir(parents=True, exist_ok=True)
    done_q = set(SOURCES_DONE.read_text(encoding="utf-8").splitlines()) if SOURCES_DONE.exists() else set()
    rows: dict[tuple[str, str], dict] = {}
    if NAMES.exists():
        with open(NAMES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if valid_name(r["name"]):  # 前の版の甘い判定で入った社名を、読み込むたびに外す
                    rows[(r["name"], r["pref"])] = r
    plan = source_plan()
    fetched = set()

    def add(core, pref, stype, url):
        k = (core, pref)
        r = rows.setdefault(k, {"name": core, "pref": pref, "mentions": "0", "source_types": "", "source_urls": ""})
        r["mentions"] = str(int(r["mentions"]) + 1)
        st = set(filter(None, r["source_types"].split(";"))) | {stype}
        r["source_types"] = ";".join(sorted(st))
        us = [u for u in r["source_urls"].split(" ") if u]
        if url not in us and len(us) < 5:
            us.append(url)
        r["source_urls"] = " ".join(us)

    try:
        for stype, pref, q in plan:
            if q in done_q:
                continue
            if time.monotonic() > deadline:
                return False
            for res in search(q, 10):
                for core in set(names_in(res["title"] + " " + res["snippet"])):
                    add(core, pref, stype, res["url"])
                u = res["url"]
                if u in fetched or not any(h in _host(u) for h in FETCH_SOURCE_HOSTS):
                    continue
                fetched.add(u)
                page = fetch(u)  # robots.txt を守る（不許可なら本文は取らない）
                if page["status"] != "ok":
                    continue
                for core in set(names_in(page["text"])):
                    add(core, pref, stype, u)
            done_q.add(q)
    finally:
        with open(NAMES, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=NAME_FIELDS)
            w.writeheader()
            for r in sorted(rows.values(), key=lambda r: -int(r["mentions"])):
                w.writerow(r)
        SOURCES_DONE.write_text("\n".join(sorted(done_q)), encoding="utf-8")
    return True


def _media_of(url: str) -> str:
    h = _host(url)
    for k, v in MEDIA.items():
        if v in h:
            return k
    return ""


def check_one(name: str, pref: str) -> dict:
    from researcher.fetch import fetch
    from researcher.search import search

    rec = {k: "" for k in CHECK_FIELDS}
    rec.update(name=name, pref=pref)
    where = "" if pref == "全国" else pref
    hits = search(f'"{name}" {where} 会社概要 製造', 8)
    blob_hits = [h for h in hits if name in (h["title"] + h["snippet"])]
    text = " ".join(h["title"] + " " + h["snippet"] for h in blob_hits)
    mfg = sorted({w for w in MFG_WORDS if w in text})
    rec["mfg_hits"] = ";".join(mfg)
    cities = CITIES.get(pref, [])
    city = next((c for c in cities if c in text), "")
    rec["city"] = city
    if pref != "全国" and pref not in text and not city:
        rec["status"] = "pref_mismatch" if blob_hits else "not_found"
    elif len(mfg) < 2:
        rec["status"] = "not_mfg" if blob_hits else "not_found"
    else:
        rec["status"] = "ok"
    for h in blob_hits:
        if rec["official_url"]:
            break
        if _is_official_hit(h, name):
            rec["official_url"] = f"{urlparse(h['url']).scheme}://{_host(h['url'])}/"
    m = EMP_RX.search(text)
    rec["employees"] = m.group(1).replace(",", "") if m else ""
    m = CAP_RX.search(text)
    if m:
        v = float(m.group(1).replace(",", ""))
        rec["capital_man"] = str(round(v * {"億": 10000, "千万": 1000, "百万": 100, "万": 1}[m.group(2)]))
    rec["verify_quote"] = (blob_hits[0]["snippet"][:200] if blob_hits else "")
    if rec["status"] != "ok":
        return rec

    jobs = search(f'"{name}" 求人 年収', 8) + search(f'"{name}" マイナビ 想定年収', 5)
    media, lows, highs, monthly, urls, hires = set(), [], [], [], [], []
    page_done = False
    for j in jobs:
        if name not in (j["title"] + j["snippet"]):
            continue
        md = _media_of(j["url"])
        if md:
            media.add(md)
        elif rec["official_url"] and _host(j["url"]) == _host(rec["official_url"]):
            media.add("自社採用ページ")
        txt = j["title"] + " " + j["snippet"]
        if md and not page_done and md in ("マイナビ転職", "マイナビ新卒", "Wantedly", "ハローワーク", "Indeed"):
            page = fetch(j["url"])  # robots.txt が許す時だけ、1社1ページまで
            if page["status"] == "ok" and name in page["text"]:
                txt += " " + page["text"][:20000]
            page_done = True
        got = False
        for m in SALARY_RX.finditer(txt):
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            if 150 <= lo <= 3000 and lo <= hi <= 5000:
                lows.append(lo)
                highs.append(hi)
                got = True
        for m in MONTHLY_RX.finditer(txt):
            v = int(m.group(1))
            if 14 <= v <= 120:
                monthly.append(v)
                got = True
        for m in MONTHLY_YEN_RX.finditer(txt):
            v = int(m.group(1) + m.group(2)) // 10000
            if 14 <= v <= 120:
                monthly.append(v)
                got = True
        for m in HIRE_RX.finditer(txt):
            hires.append(int(m.group(1)))
        if (got or md) and j["url"] not in urls and len(urls) < 5:
            urls.append(j["url"])
    rec["media"] = ";".join(sorted(media))
    rec["salary_min_man"] = str(min(lows)) if lows else ""
    rec["salary_max_man"] = str(max(highs)) if highs else ""
    rec["monthly_man"] = str(round(statistics.median(monthly))) if monthly else ""
    rec["hire_count"] = str(max(hires)) if hires else ""
    rec["job_urls"] = " ".join(urls)
    return rec


def check_names(deadline: float) -> bool:
    names = []
    with open(NAMES, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            names.append(r)
    names = names[:MAX_COMPANIES]
    done: dict[tuple[str, str], dict] = {}
    if CHECKED.exists():
        with open(CHECKED, encoding="utf-8") as f:
            done = {(r["name"], r["pref"]): r for r in csv.DictReader(f)}
    finished = True
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = []
    for r in names:
        k = (r["name"], r["pref"])
        prev = done.get(k)
        stale = prev and prev["status"] == "ok" and (_is_portal(prev.get("official_url") or "") or not prev.get("official_url"))
        if not valid_name(r["name"]):
            continue
        if prev and not stale and (prev["status"] in ("ok", "not_mfg", "pref_mismatch", "not_company") or prev.get("tries") == "2"):
            continue
        todo.append(r)

    def _one(r):
        k = (r["name"], r["pref"])
        prev = done.get(k)
        try:
            rec = check_one(*k)  # 社名を見つけたページ（source_urls）は「名前が出たページ」であって公式サイトではないので使わない
        except Exception as e:  # 1社の失敗で全体を止めない
            rec = {**{f: "" for f in CHECK_FIELDS}, "name": k[0], "pref": k[1], "status": f"error:{type(e).__name__}"}
        rec["tries"] = str(int((prev or {}).get("tries") or 0) + 1)
        return k, rec

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = []
            for r in todo:
                if time.monotonic() > deadline:
                    finished = False
                    break
                futs.append(ex.submit(_one, r))
            for fu in as_completed(futs):
                k, rec = fu.result()
                done[k] = rec
                if len(done) % 50 == 0:
                    print(f"checked {len(done)}", flush=True)
        if time.monotonic() > deadline:
            finished = False
    finally:
        with open(CHECKED, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CHECK_FIELDS)
            w.writeheader()
            for rec in done.values():
                w.writerow({k: rec.get(k, "") for k in CHECK_FIELDS})
    return finished


def write_outputs(finished: bool = True) -> None:
    with open(CHECKED, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    src = {}
    with open(NAMES, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            src[(r["name"], r["pref"])] = r
    for r in rows:
        if r["status"] == "ok" and (not r.get("official_url") or _is_portal(r["official_url"])):
            r["official_url"] = ""  # 公式が確かめられない会社は一覧に入れない（出典ページで埋めない）
        if r["status"] == "ok" and not valid_name(r["name"]):
            r["status"] = "not_company"
        if r["status"] == "ok" and _is_portal(r.get("official_url") or ""):
            r["official_url"] = ""
    def _big(r):
        e = int(r["employees"]) if (r.get("employees") or "").isdigit() else 0
        c = int(r["capital_man"]) if (r.get("capital_man") or "").isdigit() else 0
        if BIG_GROUP.match(r["name"]) or re.search(r"東証|上場|グループ連結|連結従業員", r.get("verify_quote") or ""):
            return True
        return e > 300 and c > 30000 or e > 1000 or c > 100000
    listed = _listed()
    ok = [r for r in rows if r["status"] == "ok" and r.get("official_url") and not _is_portal(r["official_url"]) and not _big(r) and norm(r["name"]) not in listed]
    # 同じ会社が県違いで重複したら、出典の多い方を残す
    best: dict[str, dict] = {}
    for r in ok:
        cur = best.get(r["name"])
        if not cur or int(src.get((r["name"], r["pref"]), {}).get("mentions", 0)) > int(src.get((cur["name"], cur["pref"]), {}).get("mentions", 0)):
            best[r["name"]] = r
    ok = list(best.values())

    with open(ROOT / "inputs" / "setouchi_mfg.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in ok:
            s = src.get((r["name"], r["pref"]), {})
            sal = f"{r['salary_min_man']}〜{r['salary_max_man']}万円" if r["salary_min_man"] else "未取得"
            w.writerow([r["name"], r["official_url"], "", f"{r['pref']}{r['city']}／出典:{s.get('source_types','')}"
                        f"／求人媒体:{r['media'] or '見当たらず'}／想定年収:{sal}／従業員:{r['employees'] or '不明'}"])

    by_pref = defaultdict(list)
    for r in ok:
        by_pref[r["pref"]].append(r)
    st = Counter(r["status"] for r in rows)
    lines = ["# 瀬戸内（＋全国町工場）製造業 求人の突き合わせ集計", "",
             "出典：検索結果のスニペットと robots.txt が許すページだけ。社名は検索で製造業の語と所在地が一致したものだけ「ok」。", "",
             f"照合した社数 {len(rows)}：" + "、".join(f"{k} {v}" for k, v in st.most_common()), "",
             "| 県 | 製造業と確認 | 求人が見つかった | 自社採用ページ | マイナビ | Wantedly | Indeed | ハローワーク | 想定年収あり | 下限の中央値(万円) | 上限の中央値(万円) | 月給の中央値(万円) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for pref in PREFS + ["全国"]:
        rs = by_pref.get(pref, [])
        if not rs:
            continue
        has = lambda key: sum(1 for r in rs if key in r["media"])
        lo = [int(r["salary_min_man"]) for r in rs if r["salary_min_man"]]
        hi = [int(r["salary_max_man"]) for r in rs if r["salary_max_man"]]
        mo = [int(r["monthly_man"]) for r in rs if r["monthly_man"]]
        med = lambda xs: str(round(statistics.median(xs))) if xs else "-"
        lines.append(f"| {pref} | {len(rs)} | {sum(1 for r in rs if r['media'])} | {has('自社採用ページ')} | {has('マイナビ')} | "
                     f"{has('Wantedly')} | {has('Indeed')} | {has('ハローワーク')} | {len(lo)} | {med(lo)} | {med(hi)} | {med(mo)} |")
    media_all = Counter(m for r in ok for m in r["media"].split(";") if m)
    lines += ["", "## 求人媒体の出現社数（製造業と確認できた会社のうち）", ""] + [f"- {k}: {v}社" for k, v in media_all.most_common()]
    emp = [int(r["employees"]) for r in ok if r["employees"].isdigit()]
    if emp:
        bands = Counter("〜19" if e < 20 else "20〜99" if e < 100 else "100〜299" if e < 300 else "300〜" for e in emp)
        lines += ["", "## 従業員数（取れた会社だけ）", ""] + [f"- {k}名: {bands.get(k, 0)}社" for k in ["〜19", "20〜99", "100〜299", "300〜"]]
    (OUT / "stats.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(ok)}\n", encoding="utf-8")
    print(f"setouchi_mfg: {len(ok)} manufacturers written", flush=True)


def main(deadline_s: float = 4 * 3600) -> int:
    deadline = time.monotonic() + deadline_s
    if not collect_names(deadline):
        print("names: time up, continue next run", flush=True)
        return 0
    write_outputs(check_names(deadline))
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
