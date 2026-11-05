"""走査（機械でできる部分）。トークンを使わずに毎回の結果から突き合わせ表と推定を作る。
out/<テーマ>/companies/*.json の引用から「年・数字・単位・出典」を抜き、品目×区分×単位ごとに並べる。
出典ごとの食い違い（最大÷最小）、一次情報かどうか、年ごとの中央値、10年の向き（上昇/下降/横ばい/判定不可）を出す。
同じ品目・同じ年に「金額」と「人数」の両方があれば、金額÷人数を【推定】1人あたりとして出す（式と出典件数つき）。
出力：out/<テーマ>/cross.csv（全数字）、out/<テーマ>/scan_auto.md（品目ごとの結論の下書き）。解釈の最終判断は scan.md（Claude）で行う。"""
import csv, glob, json, os, re, statistics, sys, unicodedata
from urllib.parse import urlparse

OUT = os.path.join(os.path.dirname(__file__), "out")
PRIMARY = re.compile(r"(\.go\.jp|xbrl|kanpo|houjin-bangou|\.lg\.jp|e-stat|edinet|jpx\.co\.jp|boj\.or\.jp|jil\.go|meti|mhlw|stat\.go|\.or\.jp|\.ac\.jp|ir\.|/ir/|disclosure)")
# 2026-11-05：ニュースサイトの「/ir/」カテゴリ（news.sharelab.jp/3dp-news/ir/…）を一次と判定した失敗から。報道・ブログのホストは一次にしない
NEWSHOST = re.compile(r"^(news\.|.*(note\.com|sharelab|nikkei|itmedia|monoist|prtimes|yahoo|newswitch|nikkan|livedoor|ameblo|hatena))", re.I)
def is_primary(url):
    return bool(PRIMARY.search(url)) and not NEWSHOST.search(urlparse(url).netloc)
# 2026-11-05 4-14：信用調査会社の売上は取材に基づく推定値を含む。一次に数えず「信用調査」とする（電子決算公告ドットコムの解説）
CREDIT = re.compile(r"(tdb\.co\.jp|tsr-net|tokyo-keizai|cosmonet|baseconnect|musubu|salesnow|\.dnb\.com|dun-bradstreet)")
# 2026-11-05 4-13／4-15：同じ欄でも定義が違う数字（単体と連結、売上高と取扱高、出荷と販売、会員企業だけの集計、規模の裾切り）
DEFN = [("連結", re.compile(r"連結")), ("単体", re.compile(r"単体|個別")), ("取扱高", re.compile(r"取扱高|流通総額|GMV", re.I)),
        ("出荷ベース", re.compile(r"出荷")), ("販売・小売ベース", re.compile(r"小売|販売額|販売ベース")),
        ("会員企業のみ", re.compile(r"会員企業|会員各社|加盟")), ("規模の裾切り", re.compile(r"(従業者|従業員)\s*\d+\s*人以上|\d+人以上の事業所"))]
NUM = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(兆円|億円|百万円|千万円|万円|千円|円|万人|千人|人|名|社|事業所|件|倍|％|%|台)")
YEAR = re.compile(r"(20[0-3]\d|19[89]\d)\s*年")
TO_MAN = {"兆円": 1e8, "億円": 1e4, "千万円": 1e3, "百万円": 100, "万円": 1, "千円": 0.1, "円": 1e-4}
TO_PERSON = {"万人": 1e4, "千人": 1e3, "人": 1, "名": 1}
NOW_YEAR = __import__("datetime").date.today().year
# 2026-11-05 METHOD.md 4-5：数字にしてはいけない引用（見出し・記事題名・関連記事リンク・パンくず・検索結果の抜粋）
HEADLINE = re.compile(r"(徹底解説|とは[？?]|まとめ|一覧|比較\d*選|\d+選|【20\d\d年[^】]*】|[｜|] ?[^｜|]{0,40}$| > | › |https?://|価格帯別|おすすめ|ランキング)")
# 調査会社の市場レポート（互いに写し合うので、何社あっても独立の裏付けに数えない）METHOD.md 4-5
REPORT = re.compile(r"(mordorintelligence|gminsights|sdki|marketsandmarkets|fortunebusinessinsights|grandviewresearch|precedenceresearch|globalinformation|gii\.co\.jp|researchandmarkets|imarcgroup|alliedmarketresearch|technavio|reportocean|marketresearchfuture|atpress|prtimes|dreamnews|kenresearch|expertmarketresearch|verifiedmarket)")
# 数字の文脈。同じ区分でも「装置の価格」「市場規模」「1件の単価」を混ぜない（am_ceiling で装置価格と受託単価が混ざった失敗から）
CONTEXT = [("市場規模", re.compile(r"(市場規模|市場は|市場が|market size|market (is|was|will)|CAGR|年平均成長率|億ドル|billion)", re.I)),
           # 2026-11-05 4-11：工場の新設・増産の計画は生産能力の信号で、装置1台の値段と混ぜない（crexgroup 有報の読み方）
           ("設備投資・増産計画", re.compile(r"(設備投資|新工場|増産|増設|生産能力|能力増強|capex|capacity expansion)", re.I)),
           # 2026-11-05：am_ceiling の「受託1個単価」に家庭用3Dプリンターの値段（1万円〜100万円〜）が入った失敗から、購入・値段・機種のクラスも装置価格にする
           ("装置価格", re.compile(r"(装置|本体|プリンターは|プリンターの(値段|価格)|プリンターを購入|購入|値段|業務用機|エントリークラス|ハイエンド|機械の価格|導入費|machine price|printer costs?)", re.I)),
           ("売上・決算", re.compile(r"(売上高|売上は|営業利益|経常利益|決算|revenue|sales of)", re.I)),
           ("統計の総額", re.compile(r"(出荷額|付加価値額|加工賃収入|総額|全国で|全体で|合計)")),
           # 2026-11-05：1人あたりの給与は生産能力（1人あたり加工賃）ではない（mfg_capacity で現金給与総額402万円が1人あたり金額に入った失敗から）
           ("給与・人件費", re.compile(r"(現金給与|給与|賃金|人件費|年収|月給|初任給|wage|salary)", re.I)),
           ("単価・料金", re.compile(r"(単価|1個|1件|1点|1人あたり|一人当たり|料金|費用|円〜|円から|月額|年額|per part|per hour)", re.I))]

# 2026-11-05 4-11：会社の見通し・計画・目標の数字は、年が過去でも予測扱い（有報の将来見通しの注意書きと同じ扱い）
OUTLOOK = re.compile(r"(見込み|見込む|見通し|計画|予定|目標|目指す|予想|予測|forecast|expected to|target)", re.I)

def context_of(q):
    for name, rx in CONTEXT:
        if rx.search(q): return name
    return "不明"

# 2026-11-05：「1,512億9,435万円」を「9,435万円」と切って拾った失敗から。億と万が続く数字は1つの万円にまとめる
OKUMAN = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*億\s*(\d{1,3}(?:,\d{3})+|\d+)\s*万\s*円")
# 2026-11-05：「6,771億63百万円」の「63百万円」だけが拾われた失敗から。億＋百万円も1つにまとめる
OKUHYAKU = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*億\s*(\d+)\s*百万\s*円")
# 2026-11-05：「1ドル約149円」の為替の換算レートを149円として拾った失敗から。換算レートの数字は外す
FXRATE = re.compile(r"1\s*(ドル|米ドル|ユーロ|元|USD|EUR)\s*(=|＝|約|あたり|当たり)?\s*約?\s*\d+(\.\d+)?\s*円")
def join_okuman(q):
    q = FXRATE.sub("", q)
    q = OKUHYAKU.sub(lambda m: f"{int(m.group(1).replace(',', '')) * 10000 + int(m.group(2)) * 100}万円", q)
    return OKUMAN.sub(lambda m: f"{int(m.group(1).replace(',', '')) * 10000 + int(m.group(2).replace(',', ''))}万円", q)

# 2026-11-05：「4〜9人」「30人以上」「100人未満」は規模の区分の境目で、人数の事実ではない（mfg_capacity で社員数4〜615人の幅になった失敗から）
def is_size_band(q, m):
    if m.group(2) not in ("人", "名", "万人", "千人"): return False
    # 2026-11-05：NFKC で「～」が半角の「~」になり「4人~29人」の4が残った失敗と、PDFの「4人 以 上」の字間の空白から、半角~と空白入りも境目にする
    after, before = q[m.end():m.end() + 6], q[max(0, m.start() - 3):m.start()]
    return bool(re.match(r"\s*(以\s*上|未\s*満|以\s*下|超|〜|～|~|-|−|―|から\s*\d|規模|の事業所|の企業)", after) or re.search(r"[〜～~\-−―]\s*$|から\s*$", before))

# 2026-11-05 4-20：同じ統計・同じ論文でも版で数字が違う（速報→確報→改定）。版を値に残し、確報・改定があれば速報を年の中央値から外す（Deskrex の版の照合）
VERSION = [("改定", re.compile(r"(改定値|改訂値|遡及改定|訂正)")), ("確報", re.compile(r"(確報|確定値|確定)")), ("速報", re.compile(r"(速報|概数|暫定|preliminary|advance estimate)", re.I))]
def version_of(q):
    for n, rx in VERSION:
        if rx.search(q): return n
    return ""

def defn_of(q):
    return "・".join(n for n, rx in DEFN if rx.search(q))

def norm(v, u):
    x = float(v.replace(",", ""))
    if u in TO_MAN: return x * TO_MAN[u], "万円"
    if u in TO_PERSON: return x * TO_PERSON[u], "人"
    if u == "％": u = "%"
    return x, u

def rows_for(topic):
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: d = json.load(open(f, encoding="utf-8"))
        except Exception: continue
        name = d.get("name", "")
        for cat, items in (d.get("evidence") or {}).items():
            for it in items or []:
                q, url = unicodedata.normalize("NFKC", it.get("quote", "") or ""), it.get("url", "")  # 全角数字・全角％を半角に（4-12）
                q = join_okuman(q)
                if HEADLINE.search(q) or "抜粋" in cat: continue  # 見出し・題名・検索結果の抜粋は数字にしない（METHOD.md 4-5）
                ys = YEAR.findall(q)
                ctx = context_of(q)
                for m in NUM.finditer(q):
                    if is_size_band(q, m): continue  # 規模区分の境目は数えない
                    v, u = norm(*m.groups())
                    if u == "万円" and v <= 0: continue
                    # 数字の近く（前40字）の年を優先
                    near = YEAR.findall(q[max(0, m.start() - 40):m.start() + 5])
                    y = (near or ys or [""])[-1]
                    yield {"品目": name, "区分": cat, "文脈": ctx, "年": y, "値": round(v, 4), "単位": u,
                           "一次": "調査会社" if REPORT.search(url) else "信用調査" if CREDIT.search(url) else "一次" if is_primary(url) else "記事",
                           "定義": defn_of(q), "版": version_of(q),
                           # 2026-11-05 4-19：出どころの記録（URL・取得日・取り方）。欠けた引用は後から確かめられない
                           "取得日": it.get("first_seen", ""),
                           "予測": "予測" if y and int(y) > NOW_YEAR else "見通し" if OUTLOOK.search(q) else "",
                           "出典": urlparse(url).netloc, "URL": url, "引用": q[:160]}

def trend(series):
    ys = sorted(series)
    if len(ys) < 3: return "判定不可（年が3点未満）"
    first, last = series[ys[0]], series[ys[-1]]
    if first == 0: return "判定不可"
    ch = (last - first) / abs(first)
    word = "上昇" if ch > 0.05 else "下降" if ch < -0.05 else "横ばい"
    return f"{word}（{ys[0]}→{ys[-1]}で{ch*100:+.0f}%）"

def dedupe(rows):
    """同じ出どころ（同じ値・単位・引用の頭40字）を1件にまとめる。METHOD.md 4-1。"""
    seen, out = set(), []
    for r in rows:
        k = (r["品目"], r["値"], r["単位"], re.sub(r"\s", "", r["引用"])[:40])
        if k in seen: continue
        seen.add(k); out.append(r)
    return out

def drop_boilerplate(rows):
    """同じ出典ドメインの同じ引用が3品目以上に出たら、サイト共通のメニュー・広告とみなして捨てる。METHOD.md 4-5。"""
    seen = {}
    for r in rows: seen.setdefault((r["出典"], re.sub(r"\s", "", r["引用"])[:40]), set()).add(r["品目"])
    return [r for r in rows if len(seen[(r["出典"], re.sub(r"\s", "", r["引用"])[:40])]) < 3]

def src_key(r):
    """独立性の単位。調査会社レポートはまとめて1つの出どころとして数える。"""
    return "調査会社" if r["一次"] == "調査会社" else r["出典"]

# 2026-11-05 4-23：こだまの判定（skillmd source-triangulation：独立は媒体の数でなく方法・データの違い。
# 「独立」のはずの出どころが同じ数字を一字一句同じに持つのは、1つの元を写した指紋）。
# 一次でない出どころどうしで値がぴったり同じなら、同じ元の写しとして1つに束ねる。一次は元そのものなのでホストで数える。
def indep_key(r):
    return src_key(r) if r["一次"] == "一次" else ("同じ値", r["値"])

def echoes(g):
    """一次でない2か所以上が同じ値を持つ値の一覧（値→ホスト数）。"""
    hv = {}
    for r in g:
        if r["一次"] != "一次": hv.setdefault(r["値"], set()).add(src_key(r))
    return {v: len(h) for v, h in hv.items() if len(h) >= 2}

# 2026-11-05 4-23：確度に見合った書き方（skillmd：裏付けあり→出典つきで言い切る／1か所→文中で出どころを名指し／食い違い→幅を両方の出典で／裏の取れないもの→使わない）
def phrasing(gr, g):
    hosts = sorted({r["出典"] for r in g if r["出典"]})
    if gr == "【裏付けあり】": return "出典を付けて言い切ってよい"
    if gr == "【食い違い】": return "幅で書き、両端の出典を並べる（どちらか1つを言い切らない）"
    if len(hosts) == 1: return f"「{hosts[0]}によると」と出どころを文中に名指しする（1か所だけ）"
    return "出どころを名指しし【未確認】と書く。結論の根拠にしない"

def grade(g):
    """確度4段階。METHOD.md 4-1／4-5（調査会社は束ねて1件、予測値は事実の裏付けにしない）／4-23（同じ値の写しは1件）。"""
    g = [r for r in g if not r.get("予測")] or g
    doms = {src_key(r) for r in g}
    vals = sorted(r["値"] for r in g)
    med = statistics.median(vals)
    near = {indep_key(r) for r in g if med and abs(r["値"] - med) <= 0.2 * abs(med)}
    if len(near) >= 2: return "【裏付けあり】"
    if vals and vals[0] > 0 and vals[-1] / vals[0] > 3 and len(doms) >= 2: return "【食い違い】"
    if any(r["一次"] == "一次" for r in g): return "【一次で確認】"
    return "【未確認】"

# 2026-11-05 4-21：代表値を採る規則（survivorship）を明示する（Data Ladder の entity resolution：元の記録は残し、代表値の選び方は別の段で規則にする）。
# 順：予測・見通しを外す → 一次があれば一次だけ → 確報・改定があれば速報を外す → いちばん新しい年 → その年の中央値。
# 食い違い・低確度のときは保守側（低い側＝第1四分位、4件未満は最小）も並べる（CUFinder：信号が合わなければ低い側に寄せ、低確度と印す）。
def pick(g):
    c = [r for r in g if not r.get("予測")] or []
    if not c: return None
    c = [r for r in c if r["一次"] == "一次"] or c
    fin = [r for r in c if r.get("版") in ("確報", "改定")]
    if fin: c = [r for r in c if r.get("版") != "速報"]
    yrs = [int(r["年"]) for r in c if r["年"]]
    if yrs:
        y = max(yrs); c = [r for r in c if r["年"] and int(r["年"]) == y]
    else: y = None
    vals = sorted(r["値"] for r in c)
    return y, statistics.median(vals), len(c), {r["出典"] for r in c}

def conservative(vals):
    vals = sorted(vals)
    return statistics.quantiles(vals, n=4)[0] if len(vals) >= 4 else vals[0]

def scan(topic):
    rows = drop_boilerplate(dedupe(list(rows_for(topic))))
    if not rows: return 0
    with open(os.path.join(OUT, topic, "cross.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    md = [f"# 走査（自動）— {topic}", "", "機械で出した突き合わせと【推定】。式と出典件数つき。最終の結論は scan.md。", ""]
    # 2026-11-05 4-19：出どころの記録（URL・取得日）が欠けた数字を数える。0件でなければ HEALTH と同じく直す対象
    nprov = sum(1 for r in rows if not r["URL"] or not r["取得日"])
    md += [f"- 出どころの記録が欠けた数字：{nprov}件／{len(rows)}件（URLか取得日が無い）"]
    # 2026-11-05 4-21：機械で決めきれず人（Claude の走査）の確認が要る件数（Data Ladder：保留の件数を品質の指標にする）。文脈不明か年が無い数字
    nhold = sum(1 for r in rows if r["文脈"] == "不明" or not r["年"])
    md += [f"- 要確認（文脈不明か年が無い）：{nhold}件／{len(rows)}件。この数字は結論に使う前に引用の本文で確かめる", ""]
    by_item = {}
    for r in rows: by_item.setdefault(r["品目"], []).append(r)
    thin = []  # 4-23：主張×独立の出どころの表（theosintvault の validation matrix）で、出どころ1か所の欄を最後に並べる
    for item, rs in sorted(by_item.items()):
        md.append(f"## {item}")
        groups = {}
        for r in rs: groups.setdefault((r["区分"], r["文脈"], r["単位"]), []).append(r)
        for (cat, ctx, u), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(g) < 2 or u in ("件",): continue
            vals = [r["値"] for r in g]; prim = [r for r in g if r["一次"] == "一次"]
            lo, hi = min(vals), max(vals)
            spread = f"{hi/lo:.1f}倍" if lo > 0 else "-"
            peryear = {}
            # 4-20：同じ年に確報・改定があれば速報を外す
            fin_years = {r["年"] for r in g if r.get("版") in ("確報", "改定")}
            for r in (prim or g):
                if r["年"] and not r["予測"] and not (r.get("版") == "速報" and r["年"] in fin_years): peryear.setdefault(int(r["年"]), []).append(r["値"])
            med = {y: statistics.median(v) for y, v in peryear.items()}
            fut = [r for r in g if r["予測"]]
            gr = grade(g)
            md.append(f"- {gr} {cat}［{ctx}］（{u}）：{len(g)}件・一次{len(prim)}件・出典{len({src_key(r) for r in g})}か所（調査会社は1つに束ねる）・予測値{len(fut)}件、幅 {lo:g}〜{hi:g}（食い違い{spread}）")
            md.append(f"  - 書き方（4-23）：{phrasing(gr, g)}")
            ec = echoes(g)
            if ec: md.append("  - ⚠ 写しの疑い（一次でない別々の出どころが同じ値をぴったり持つ。1件に束ねて数えた）：" + "、".join(f"{v:g}{u}が{n}か所" for v, n in sorted(ec.items())[:5]) + "。元（統計・開示・調査）までたどる")
            if len({src_key(r) for r in g}) == 1: thin.append(f"{item}｜{cat}［{ctx}］")
            pk = pick(g)
            if pk:
                py, pv, pn, pd = pk
                md.append(f"  - 採る値（規則：予測除外→一次優先→確報優先→最新年→中央値）：{pv:g}{u}（{py or '年不明'}年・{pn}件・出典{len(pd)}か所）")
                # 4-21：最新の年が3年以上前なら古い（Inven：公表データは古いことがある）。今の判断に使う前に新しい版を探す
                if py and NOW_YEAR - py >= 3: md.append(f"  - ⚠ 古い：採った値の最新の年が{py}年（{NOW_YEAR - py}年前）。新しい版・新しい年を探す")
            if gr in ("【食い違い】", "【未確認】") and len(vals) >= 2:
                md.append(f"  - 【推定】保守側の値（低い側に寄せる・低確度）：{conservative(vals):g}{u}")
            # 2026-11-05 4-15：外れ値に引っ張られた幅か、全体が散っているかを見るため四分位も出す（感度の確認）
            if len(vals) >= 4:
                qs = statistics.quantiles(vals, n=4)
                md.append(f"  - 【推定】中ほど半分の幅（第1〜第3四分位）：{qs[0]:g}〜{qs[2]:g}、中央値 {statistics.median(vals):g}")
            # 2026-11-05 4-13：100倍以上の開きは単位の読み違い（百万円の表の数字を万円と読む等）か別物の混入を先に疑う
            if lo > 0 and hi / lo >= 100: md.append("  - ⚠ 桁違いの疑い（100倍以上）：単位・対象（単体/連結、事業所/企業、装置/単価）の取り違えを先に確かめる")
            dc = {}
            for r in g:
                for d0 in (r.get("定義") or "").split("・"):
                    if d0: dc[d0] = dc.get(d0, 0) + 1
            if len(dc) >= 2: md.append("  - ⚠ 定義が混在：" + "、".join(f"{k}{v}件" for k, v in dc.items()) + "（同じ定義どうしで比べる）")
            vc = {}
            for r in g:
                if r.get("版"): vc[r["版"]] = vc.get(r["版"], 0) + 1
            if len(vc) >= 2: md.append("  - ⚠ 版が混在：" + "、".join(f"{k}{v}件" for k, v in vc.items()) + "（同じ年は確報・改定を採り、速報は年の中央値から外した）")
            # 4-19：一次の出どころが2か所以上で中央値の±20%に収まれば、いちばん確度が高い（一次どうしの裏付け）
            pmed = statistics.median(vals)
            pdoms = {r["出典"] for r in prim if pmed and abs(r["値"] - pmed) <= 0.2 * abs(pmed)}
            if len(pdoms) >= 2: md.append(f"  - 一次どうしの裏付け：一次{len(pdoms)}か所が中央値±20%で一致（確度がいちばん高い）")
            cr = sum(1 for r in g if r["一次"] == "信用調査")
            if cr: md.append(f"  - 信用調査会社の数字{cr}件（推定値を含むので【推定】扱い・一次に数えない）")
            if med: md.append(f"  - 【推定】年ごとの中央値（{'一次のみ' if prim else '記事含む'}）：" + "、".join(f"{y}年 {v:g}" for y, v in sorted(med.items())) + f" → 向き：{trend(med)}")
        # 1人あたり：同じ年に万円と人がある場合
        money = {}; people = {}
        for r in rs:
            if not r["年"] or r["予測"]: continue
            if r["単位"] == "万円": money.setdefault(int(r["年"]), []).append(r["値"])
            if r["単位"] == "人": people.setdefault(int(r["年"]), []).append(r["値"])
        per = {y: statistics.median(money[y]) / statistics.median(people[y]) for y in money if y in people and statistics.median(people[y]) > 0}
        if per:
            md.append("  - 【推定】1人あたり金額＝その年の金額の中央値÷人数の中央値（同じ品目の別出典をまたぐので桁違いに注意）：" + "、".join(f"{y}年 {v:,.1f}万円" for y, v in sorted(per.items())) + f" → 向き：{trend(per)}")
        # 2026-11-05：1人あたりの金額（給与以外）÷1人あたり給与＝受託の天井の厚み（H3）。同じ品目・同じ年にそろったときだけ
        pay = {}; cap = {}
        for r in rs:
            if not r["年"] or r["予測"] or r["単位"] != "万円" or not r["区分"].startswith("1人あたり"): continue
            (pay if r["文脈"] == "給与・人件費" else cap).setdefault(int(r["年"]), []).append(r["値"])
        ratio = {y: statistics.median(cap[y]) / statistics.median(pay[y]) for y in cap if y in pay and statistics.median(pay[y]) > 0}
        if ratio:
            md.append("  - 【推定】1人あたり金額÷1人あたり給与（同じ年の中央値どうし。給与の何倍を1人で稼いでいるか）：" + "、".join(f"{y}年 {v:.2f}倍" for y, v in sorted(ratio.items())) + f" → 向き：{trend(ratio)}")
            # 2026-11-05 4-17：黒字の会社の1人あたり売上・加工賃は1人あたり給与を下回らない（給与が下限）。下回る年は別物の混入か定義違い
            low = [y for y, v in ratio.items() if v < 1]
            if low: md.append("  - ⚠ 給与の下限割れ：" + "、".join(f"{y}年" for y in sorted(low)) + "（1人あたり金額が給与より小さい。付加価値・利益など別の欄の混入か、事業所と企業の取り違えを疑う）")
        md.append("")
    # 2026-11-05 4-19：何が見つからなかったかを書く（範囲の明示）。数字が1つも取れなかった品目を並べる
    names = set()
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: names.add(json.load(open(f, encoding="utf-8")).get("name", ""))
        except Exception: pass
    empty = sorted(n for n in names - set(by_item) if n)
    md += ["## 出どころが1か所だけの欄（裏が薄い。穴埋めの検索の対象・4-23）", *(f"- {t}" for t in thin or ["（なし）"]), ""]
    md += ["## 数字が1つも取れなかった品目（範囲の明示）", *(f"- {n}" for n in empty or ["（なし）"]), ""]
    open(os.path.join(OUT, topic, "scan_auto.md"), "w", encoding="utf-8").write("\n".join(md))
    return len(rows)

if __name__ == "__main__":
    topics = sys.argv[1:] or [os.path.basename(p.rstrip("/")) for p in glob.glob(os.path.join(OUT, "*/")) if not p.rstrip("/").endswith("brief")]
    for t in topics:
        n = scan(t)
        if n: print(t, n)
