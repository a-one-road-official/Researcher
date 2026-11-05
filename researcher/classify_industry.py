"""会社が「どの産業のお金で食っているか」を、表紙（業界ラベル・社名・キャッチコピー）でなく中身で判定する。AIは使わない。

本人の指示（2026-11-05）：「ITとか広告じゃなくて、製造とか物流とか農業とかで見る」「PLEXは確実に物流。人間はそう認識する。
AIがそのルートで会社を識別するロジックまで組め」「文字や言葉通りに受け取らない。Don't judge a book by its cover.」

人が会社の産業を見抜くときの道筋を、独立した手がかりに分けて別々に数える：
  S1 取引先・導入企業：誰からお金をもらっているか。上場企業なら東証33業種 → 日本標準産業分類（経済センサス）の大分類に写す。
  S2 客の呼び方：「○○向け」「○○業界」「○○会社様」「○○事業者」「○○の皆様」「○○の課題」の○○（型にはまった時だけ数える）。
  S3 扱う人の職種：人材・教育・求人の文脈で出る職種（ドライバー・看護師・施工管理…）→ その人が働く産業。
  S4 許認可・登録：自分で営む産業（一般貨物自動車運送事業・宅建業免許・建設業許可…）。
  S5 機能：人材・広告・SaaS・コンサル・EC など「何をするか」。産業とは別の列（PLEXなら機能＝人材＋SaaS、産業＝運輸業）。
判定：S1〜S4のうち2種類以上が同じ大分類を指す → 確定／1種類だけ → 候補／S2・S1が3つ以上の大分類にばらける → 横断（全業種向け）／
何もない → 要読込（引用を残すので人が読んで決める）。単語が出ただけでは数えない（型・文脈・社名の照合を通ったものだけ）。
"""
from __future__ import annotations

import csv
import re
import time
from collections import Counter, defaultdict

from researcher.targets import ROOT

# 日本標準産業分類（経済センサスの大分類）
JSIC = {
    "A": "農業・林業", "B": "漁業", "C": "鉱業", "D": "建設業", "E": "製造業", "F": "電気・ガス・熱供給・水道業",
    "G": "情報通信業", "H": "運輸業・郵便業", "I": "卸売業・小売業", "J": "金融業・保険業", "K": "不動産業・物品賃貸業",
    "L": "学術研究・専門・技術サービス業", "M": "宿泊業・飲食サービス業", "N": "生活関連サービス業・娯楽業",
    "O": "教育・学習支援業", "P": "医療・福祉", "Q": "複合サービス事業", "R": "サービス業（他に分類されないもの）", "S": "公務",
}

# 東証33業種 → 大分類（S1：上場している取引先の業種）
TSE33 = {
    "水産・農林業": "A", "鉱業": "C", "建設業": "D", "食料品": "E", "繊維製品": "E", "パルプ・紙": "E", "化学": "E",
    "医薬品": "E", "石油・石炭製品": "E", "ゴム製品": "E", "ガラス・土石製品": "E", "鉄鋼": "E", "非鉄金属": "E",
    "金属製品": "E", "機械": "E", "電気機器": "E", "輸送用機器": "E", "精密機器": "E", "その他製品": "E",
    "電気・ガス業": "F", "陸運業": "H", "海運業": "H", "空運業": "H", "倉庫・運輸関連業": "H", "情報・通信業": "G",
    "卸売業": "I", "小売業": "I", "銀行業": "J", "証券、商品先物取引業": "J", "保険業": "J", "その他金融業": "J",
    "不動産業": "K", "サービス業": "R",
}

# S2：客の呼び方の○○（その産業の事業者を指す名詞だけ。機能語＝「人材」「広告」は入れない）
CUSTOMER_NOUNS = {
    "A": ["農家", "農業法人", "生産者", "林業", "畜産", "酪農", "農協", "JA"],
    "B": ["漁業者", "漁協", "水産業", "養殖"],
    "D": ["建設会社", "建設業", "工務店", "ゼネコン", "施工会社", "リフォーム会社", "設備工事", "電気工事", "解体業", "建設現場", "職人"],
    "E": ["製造業", "メーカー", "工場", "町工場", "ものづくり企業", "製造現場", "食品メーカー", "部品メーカー"],
    "F": ["電力会社", "ガス会社", "発電事業者", "水道事業"],
    "H": ["運送会社", "運送業", "物流会社", "物流業", "物流事業者", "トラック", "運輸業", "倉庫", "配送", "タクシー会社", "バス会社", "引越"],
    "I": ["小売店", "小売業", "販売店", "卸売業", "商社", "ドラッグストア", "スーパー", "EC事業者", "ネットショップ", "アパレル", "店舗"],
    "J": ["金融機関", "銀行", "証券会社", "保険会社", "信用金庫", "保険代理店"],
    "K": ["不動産会社", "不動産業", "管理会社", "賃貸管理", "仲介会社", "オーナー", "デベロッパー", "大家"],
    "L": ["士業", "税理士", "会計事務所", "弁護士", "法律事務所", "社労士", "設計事務所", "研究機関", "デザイン事務所"],
    "M": ["飲食店", "飲食業", "レストラン", "ホテル", "旅館", "宿泊施設", "居酒屋", "カフェ"],
    "N": ["美容室", "サロン", "エステ", "理容", "クリーニング", "冠婚葬祭", "葬儀", "ジム", "フィットネス", "旅行会社", "パチンコ"],
    "O": ["学校", "学習塾", "塾", "教育機関", "大学", "保育園", "幼稚園", "スクール"],
    "P": ["病院", "クリニック", "医療機関", "介護施設", "介護事業者", "歯科医院", "薬局", "調剤薬局", "福祉施設", "訪問看護", "保育所"],
    "R": ["警備会社", "ビルメンテナンス", "清掃会社", "整備工場", "産業廃棄物"],
    "S": ["自治体", "官公庁", "市町村", "行政"],
}
CUSTOMER_PATTERNS = [
    r"{n}(?:様)?向け", r"{n}業界", r"{n}(?:様|さま)(?:の|へ|に|から|が)", r"{n}(?:事業者|企業)(?:様)?(?:の|へ|に|向け|を)",
    r"{n}の(?:皆様|皆さま|方々|経営者|課題|人手不足|DX|業務)", r"(?:導入|取引|お客様|顧客|クライアント)[^。]{{0,20}}{n}",
    r"{n}(?:を|に)(?:支援|サポート|専門|特化)", r"{n}専門", r"{n}特化",
]

# S3：扱う人の職種 → 働く産業（人材・教育・求人の文脈に限る）
OCCUPATIONS = {
    "H": ["ドライバー", "運転手", "配送員", "倉庫作業", "フォークリフト", "物流スタッフ", "トラック運転"],
    "P": ["看護師", "介護士", "介護職", "保育士", "薬剤師", "医師", "理学療法士", "作業療法士", "歯科衛生士", "ケアマネ"],
    "D": ["施工管理", "職人", "現場監督", "大工", "電気工事士", "建設技術者", "設備管理"],
    "E": ["製造スタッフ", "工場勤務", "機械オペレーター", "溶接", "技能工", "生産技術", "品質管理"],
    "M": ["調理師", "ホールスタッフ", "料理人", "シェフ", "ホテルスタッフ"],
    "N": ["美容師", "理容師", "エステティシャン", "ネイリスト", "トレーナー"],
    "O": ["講師", "教員", "保育者"],
    "G": ["エンジニア", "プログラマー", "ITエンジニア", "SE"],
    "I": ["販売スタッフ", "店長", "販売員"],
    "R": ["警備員", "清掃スタッフ", "整備士"],
    "A": ["農業従事者", "新規就農"],
    "J": ["ファイナンシャルプランナー", "保険営業"],
}
OCC_CONTEXT = r"(?:求人|転職|採用|人材|派遣|紹介|育成|研修|教育|スクール|資格|マッチング|キャリア|働く|職場)"

# S4：許認可・登録 → 自分で営む産業
LICENSES = {
    "H": ["一般貨物自動車運送事業", "貨物利用運送事業", "倉庫業登録", "一般乗用旅客自動車運送事業", "第一種貨物利用運送"],
    "K": ["宅地建物取引業", "宅建業免許", "賃貸住宅管理業", "マンション管理業"],
    "D": ["建設業許可", "一般建設業", "特定建設業", "国土交通大臣許可（般", "知事許可（般"],
    "P": ["医療法人", "介護事業所番号", "指定居宅", "保険医療機関", "薬局開設許可"],
    "J": ["金融商品取引業", "貸金業", "保険代理店", "第二種金融商品", "電子決済等代行業", "資金移動業"],
    "M": ["飲食店営業許可", "旅館業"],
    "N": ["旅行業登録", "古物商", "美容所"],
    "O": ["学校法人"],
    "E": ["製造業許可", "工場", "ISO9001"],
    "I": ["酒類販売業免許", "医薬品販売業", "古物商許可"],
    "F": ["小売電気事業者", "発電事業"],
}
# S5：機能（何をするか）
FUNCTIONS = {
    "人材紹介・派遣・求人": ["有料職業紹介", "労働者派遣", "人材紹介", "人材派遣", "求人サイト", "求人メディア", "採用支援"],
    "広告・マーケティング": ["広告代理", "Web広告", "マーケティング支援", "集客支援", "プロモーション", "SNS運用"],
    "SaaS・ソフト": ["SaaS", "クラウドサービス", "システム開発", "アプリ開発", "ソフトウェア"],
    "コンサル": ["コンサルティング", "経営支援", "伴走支援"],
    "EC・小売": ["ECサイト運営", "通信販売", "D2C"],
    "メディア": ["メディア運営", "ポータルサイト", "比較サイト"],
    "仲介・マッチング": ["マッチング", "プラットフォーム", "仲介"],
}
PREF_PAGES = r"事業|サービス|service|business|導入|事例|case|実績|works|取引先|お客様|client|customer|会社概要|company|about|採用|recruit|ソリューション|solution|対象"
FIELDS = ["name", "home_url", "site_label", "industry", "industry_name", "judge", "functions", "S1", "S2", "S3", "S4",
          "pages_read", "quotes", "tries"]


def _listed_map() -> dict[str, str]:
    f = ROOT / "out" / "ir1000" / "screen.csv"
    out = {}
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                code = TSE33.get(r.get("industry", ""))
                if code and len(r["name"]) >= 2:
                    out[r["name"]] = code
    return out


_LRX = {}


def _listed_rx(listed: dict):
    if "rx" not in _LRX:
        names = sorted(listed, key=len, reverse=True)
        b = r"[ァ-ヶー一-龥ぁ-んA-Za-z0-9]"
        _LRX["rx"] = re.compile(rf"(?<!{b})(" + "|".join(map(re.escape, names)) + rf")(?!{b})") if names else re.compile(r"(?!x)x")
    return _LRX["rx"]


_CUST_RX = {c: [re.compile(p.format(n=re.escape(n))) for n in ns for p in CUSTOMER_PATTERNS] for c, ns in CUSTOMER_NOUNS.items()}


def _quote(text: str, m: re.Match, w: int = 40) -> str:
    return text[max(0, m.start() - w): m.end() + w].replace("\n", " ")


def score(text: str, listed: dict) -> dict:
    """本文1社分 → 手がかりごとの {大分類: 件数} と引用。"""
    s1, s2, s3, s4, fn = Counter(), Counter(), Counter(), Counter(), Counter()
    quotes = defaultdict(list)
    # S1：上場会社の社名が「取引先・導入・実績・お客様」の近くに出たもの
    for blk in re.finditer(r"(?:主要取引先|取引先|導入企業|導入実績|お客様|クライアント|納入先|販売先|主な顧客)[^\n]{0,400}", text):
        seg = blk.group(0)
        for m in _listed_rx(listed).finditer(seg):  # 社名の前後が文字の続きでないときだけ（「ヤマト」が「ヤマト運輸」に当たらないように）
            nm = m.group(1)
            code = listed[nm]
            s1[code] += 1
            if len(quotes["S1"]) < 6:
                quotes["S1"].append(f"{nm}（{JSIC[code]}）")
    for code, rxs in _CUST_RX.items():
        for rx in rxs:
            for m in rx.finditer(text):
                s2[code] += 1
                if len(quotes["S2"]) < 8:
                    quotes["S2"].append(_quote(text, m))
    for code, occs in OCCUPATIONS.items():
        for o in occs:
            for m in re.finditer(re.escape(o), text):
                ctx = text[max(0, m.start() - 30): m.end() + 30]
                if re.search(OCC_CONTEXT, ctx):
                    s3[code] += 1
                    if len(quotes["S3"]) < 6:
                        quotes["S3"].append(ctx.replace("\n", " "))
    for code, lics in LICENSES.items():
        for l in lics:
            if l in ("工場", "ISO9001", "古物商"):  # 単独では弱い語：会社概要の表の中に限る
                continue
            for m in re.finditer(re.escape(l), text):
                s4[code] += 1
                if len(quotes["S4"]) < 4:
                    quotes["S4"].append(_quote(text, m, 30))
    for f, ws in FUNCTIONS.items():
        fn[f] = sum(text.count(w) for w in ws)
    return {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "fn": fn, "quotes": quotes}


def judge(sc: dict) -> tuple[str, str]:
    """(大分類コード, 判定)。2種類以上の手がかりが一致＝確定。"""
    votes = Counter()
    for k in ("S1", "S2", "S3", "S4"):
        c = sc[k]
        if not c:
            continue
        top, n = c.most_common(1)[0]
        if n >= (2 if k == "S2" else 1) and n >= 0.5 * sum(c.values()):  # その手がかりの中で過半
            votes[top] += 1
    spread = [k for k in ("S1", "S2") if len(sc[k]) >= 3 and sc[k].most_common(1)[0][1] < 0.5 * sum(sc[k].values())]
    if votes:
        top, n = votes.most_common(1)[0]
        if n >= 2:
            return top, "確定"
        if spread:
            return "", "横断"
        return top, "候補"
    if spread:
        return "", "横断"
    return "", "要読込"


def _read_site(home: str, name: str) -> tuple[str, int]:
    from researcher.fetch import fetch
    from researcher.search import search

    if not home:
        for h in search(f"{name} 会社概要", 8):
            host = h["url"].split("/")[2] if h["url"].count("/") >= 2 else ""
            if name in h.get("title", "") and not re.search(r"wantedly|prtimes|v-tsushin|baseconnect|houjin|mynavi|doda|en-japan|indeed|wikipedia|nikkei|note\.com|facebook|x\.com|twitter", host):
                home = "/".join(h["url"].split("/")[:3]) + "/"
                break
    if not home:
        return "", 0
    top = fetch(home)
    if top["status"] != "ok":
        return "", 0
    texts, n = [top["text"]], 1
    seen = {home}
    host = home.split("/")[2]
    subs = [u for t, u in top.get("links", []) if host in u and re.search(PREF_PAGES, t + " " + u, re.I)]
    for u in subs:
        if u in seen or n >= 10:
            continue
        seen.add(u)
        p = fetch(u)
        if p["status"] == "ok":
            texts.append(p["text"])
            # 会社概要の表（pairs）は許認可・取引先が入る場所なので、そのまま足す
            texts.append("\n".join(f"{a} {b}" for a, b in p.get("pairs", [])))
            n += 1
    return "\n".join(texts), n


def classify_all(rows: dict, out_csv, deadline: float) -> bool:
    done = {}
    if out_csv.exists():
        with open(out_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                done[r["name"]] = r
    listed = _listed_map()
    finished = True
    try:
        for name, r in sorted(rows.items()):
            if not (r.get("official_years") or int(r.get("mentions", "0")) >= 2):
                continue
            if name in done and (done[name]["judge"] != "要読込" or int(done[name]["tries"] or 0) >= 2):
                continue
            if time.monotonic() > deadline:
                finished = False
                break
            text, n = _read_site(r.get("home_url", ""), name)
            sc = score(text, listed)
            code, j = judge(sc)
            fmt = lambda c: ";".join(f"{k}:{v}" for k, v in c.most_common())
            done[name] = {
                "name": name, "home_url": r.get("home_url", ""), "site_label": r.get("site_label", ""),
                "industry": code, "industry_name": JSIC.get(code, ""), "judge": j,
                "functions": ";".join(f for f, v in sc["fn"].most_common() if v),
                "S1": fmt(sc["S1"]), "S2": fmt(sc["S2"]), "S3": fmt(sc["S3"]), "S4": fmt(sc["S4"]),
                "pages_read": str(n), "quotes": " ｜ ".join(f"{k}: {q}" for k, qs in sc["quotes"].items() for q in qs)[:1500],
                "tries": str(int(done.get(name, {}).get("tries") or 0) + 1),
            }
    finally:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(done.values())
        _summary(done, out_csv.parent / "industry_count.md")
    return finished


def _summary(done: dict, path) -> None:
    """大分類ごとの社数（確定・候補・横断・要読込を分けて数える。混ぜない）。"""
    cnt = defaultdict(Counter)
    for r in done.values():
        cnt[r["industry"] or "-"][r["judge"]] += 1
    lines = ["# ベストベンチャー100 — どの産業のお金で食っているか（大分類ごとの社数）", "",
             "| 大分類 | 確定 | 候補 | 計 |", "|---|---|---|---|"]
    for code, name in JSIC.items():
        c = cnt.get(code, Counter())
        lines.append(f"| {code} {name} | {c['確定']} | {c['候補']} | {c['確定'] + c['候補']} |")
    rest = cnt.get("-", Counter())
    lines += ["", f"- 横断（全業種向け）：{rest['横断']}社", f"- 要読込（手がかりなし。引用を人が読む）：{rest['要読込']}社",
              "", "判定のしかたは researcher/classify_industry.py の冒頭。サイトの業界ラベル（IT・Web／人材／広告…）は使っていない。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
