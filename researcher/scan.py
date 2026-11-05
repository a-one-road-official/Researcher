"""走査（機械でできる部分）。トークンを使わずに毎回の結果から突き合わせ表と推定を作る。
out/<テーマ>/companies/*.json の引用から「年・数字・単位・出典」を抜き、品目×区分×単位ごとに並べる。
出典ごとの食い違い（最大÷最小）、一次情報かどうか、年ごとの中央値、10年の向き（上昇/下降/横ばい/判定不可）を出す。
同じ品目・同じ年に「金額」と「人数」の両方があれば、金額÷人数を【推定】1人あたりとして出す（式と出典件数つき）。
出力：out/<テーマ>/cross.csv（全数字）、out/<テーマ>/scan_auto.md（品目ごとの結論の下書き）。解釈の最終判断は scan.md（Claude）で行う。"""
import csv, glob, json, os, re, statistics, sys, unicodedata
from urllib.parse import urlparse as _raw_urlparse
def urlparse(u):
    """全角文字入りの壊れたURLで落ちないようにする（search.py の ValueError と同じ失敗から）。"""
    try: return _raw_urlparse(u or "")
    except ValueError: return _raw_urlparse("")

OUT = os.path.join(os.path.dirname(__file__), "out")
PRIMARY = re.compile(r"(\.go\.jp|xbrl|kanpo|houjin-bangou|\.lg\.jp|e-stat|edinet|jpx\.co\.jp|boj\.or\.jp|jil\.go|meti|mhlw|stat\.go|\.or\.jp|\.ac\.jp|ir\.|/ir/|disclosure)")
# 2026-11-05：ニュースサイトの「/ir/」カテゴリ（news.sharelab.jp/3dp-news/ir/…）を一次と判定した失敗から。報道・ブログのホストは一次にしない
NEWSHOST = re.compile(r"^(news\.|.*(note\.com|sharelab|nikkei|itmedia|monoist|prtimes|yahoo|newswitch|nikkan|livedoor|ameblo|hatena))", re.I)
# 2026-11-05 4-24：登記・開示・判決の「写し」を載せたまとめサイトは一次にしない（Altss OSINT 検証基準：
# 登記の写しは、役所・登記所の仕組みから取り直すまで報道と同じ扱い）。EDINET・法人番号の写しのサイトが URL に edinet・houjin を含んでも一次にしない
COPYHOST = re.compile(r"(irbank|buffett-code|kabutan|minkabu|ullet|houjin\.info|houjin-info|corporate-number|alarmbox|catr\.jp|b2bportal|houjinbangou\.|stockclip|kabuyoho|yahoo)", re.I)
# 2026-11-05 走査の失敗から：神奈川県・京都府の統計（www.pref.kanagawa.jp・www.pref.kyoto.jp）が「.lg.jp」でないため一次0件と出た（mfg_capacity の工業統計表の品目）。
# 都道府県・市の公式ドメイン（pref.<名>.jp・city.<名>.<…>.jp・metro.tokyo）は自治体自身の統計なので一次にする
LOCALGOV = re.compile(r"(^|\.)(pref\.[a-z]+\.jp|city\.[a-z-]+(\.[a-z]+)?\.jp|metro\.tokyo(\.lg)?\.jp)$", re.I)
def is_primary(url):
    host = urlparse(url).netloc
    return (bool(PRIMARY.search(url)) or bool(LOCALGOV.search(host))) and not NEWSHOST.search(host) and not COPYHOST.search(host)
# 2026-11-05 走査の失敗から：奈良県と京都府の別々の県の統計が同じ欄で【裏付けあり】になった（mfg_capacity の規模別の品目）。地域が違えば同じ対象の裏付けではない（4-20 の条件違い）
def region_of(url):
    m = re.search(r"(?:^|\.)(pref|city)\.([a-z-]+)", urlparse(url).netloc, re.I)
    return f"{m.group(1)}.{m.group(2)}" if m else ""

# 2026-11-05 4-24：出どころの立場（Altss の source standing を日本向けに簡略化）。
# 記録者＝官公庁・登記・開示・統計の役所自身／写し＝報道・まとめ・調査会社・信用調査／本人以外の区別は付かないので残りは「その他」
def standing(url, kind):
    if kind == "一次": return "記録者・本人"
    if kind in ("調査会社", "信用調査") or COPYHOST.search(urlparse(url).netloc) or NEWSHOST.search(urlparse(url).netloc): return "写し・集計"
    return "その他"

# 2026-11-05 4-24：記事が一次（統計・有報・決算・官報・登記・会社発表）を名指ししている数字は、元へ戻って取り直す候補（iri-group デスクリサーチ：
# ニュースが紹介する数値を政府統計・決算資料に戻って確かめる／Altss：報道は「記者が書いたこと」の証拠で、届出そのものではない）
CITES_PRIMARY = re.compile(r"(e-Stat|経済センサス|工業統計|経済構造実態調査|中小企業実態基本調査|賃金構造基本統計|法人企業統計|有価証券報告書|有報|決算短信|決算説明|官報|登記|IR資料|統計によると|調査によると|発表した|公表した|によると)")
# 2026-11-05 4-14：信用調査会社の売上は取材に基づく推定値を含む。一次に数えず「信用調査」とする（電子決算公告ドットコムの解説）
CREDIT = re.compile(r"(tdb\.co\.jp|tsr-net|tokyo-keizai|cosmonet|baseconnect|musubu|salesnow|\.dnb\.com|dun-bradstreet)")
# 2026-11-05 4-13／4-15：同じ欄でも定義が違う数字（単体と連結、売上高と取扱高、出荷と販売、会員企業だけの集計、規模の裾切り）
DEFN = [("連結", re.compile(r"連結")), ("単体", re.compile(r"単体|個別")), ("取扱高", re.compile(r"取扱高|流通総額|GMV", re.I)),
        ("出荷ベース", re.compile(r"出荷")), ("販売・小売ベース", re.compile(r"小売|販売額|販売ベース")),
        ("会員企業のみ", re.compile(r"会員企業|会員各社|加盟")),
        # 2026-11-05 4-26：付加価値額は従業者規模で式が違う（30人以上は生産額から・減価償却を引く、9人以下は引かない。RIETI 05-P-001）。出荷額・加工賃と同じ欄で比べない
        ("付加価値", re.compile(r"付加価値")), ("加工賃", re.compile(r"加工賃")), ("規模の裾切り", re.compile(r"(従業者|従業員)\s*\d+\s*人以上|\d+人以上の事業所"))]
NUM = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(兆円|億円|百万円|千万円|万円|千円|円|万人|千人|人|名|社|事業所|件|倍|％|%|台)")
YEAR = re.compile(r"(20[0-3]\d|19[89]\d)\s*年")
TO_MAN = {"兆円": 1e8, "億円": 1e4, "千万円": 1e3, "百万円": 100, "万円": 1, "千円": 0.1, "円": 1e-4}
TO_PERSON = {"万人": 1e4, "千人": 1e3, "人": 1, "名": 1}
NOW_YEAR = __import__("datetime").date.today().year
# 2026-11-05 METHOD.md 4-5：数字にしてはいけない引用（見出し・記事題名・関連記事リンク・パンくず・検索結果の抜粋）
# 2026-11-05 4-35：関連記事の紹介文（「〜を解説します」「完全ガイド」「〜まで網羅」）の数字も事実にしない（電子決算公告ドットコムの「1行3,589円」の混入から）
HEADLINE = re.compile(r"(完全ガイド|を?(わかりやすく)?(解説|説明)します|を網羅|を徹底比較|徹底解説|とは[？?]|まとめ|一覧|比較\d*選|\d+選|【20\d\d年[^】]*】|[｜|] ?[^｜|]{0,40}$| > | › |https?://|価格帯別|おすすめ|ランキング)")
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
# 2026-11-05 4-37 走査の失敗から：「11万8213人」の後ろの「8213人」だけが拾われ、金型製造業の従業員数が1991年 8,213人と1/14になった。万＋人・事業所・社・件も1つの値にまとめる
MANCOUNT = re.compile(r"(\d{1,4})\s*万\s*(\d{1,3},\d{3}|\d{1,4})\s*(人|名|事業所|社|件|台)")
def join_okuman(q):
    q = FXRATE.sub("", q)
    q = MANCOUNT.sub(lambda m: f"{int(m.group(1)) * 10000 + int(m.group(2).replace(',', ''))}{m.group(3)}", q)
    q = OKUHYAKU.sub(lambda m: f"{int(m.group(1).replace(',', '')) * 10000 + int(m.group(2)) * 100}万円", q)
    return OKUMAN.sub(lambda m: f"{int(m.group(1).replace(',', '')) * 10000 + int(m.group(2).replace(',', ''))}万円", q)

# 2026-11-05：「4〜9人」「30人以上」「100人未満」は規模の区分の境目で、人数の事実ではない（mfg_capacity で社員数4〜615人の幅になった失敗から）
# 2026-11-05 4-31：求人検索の結果ページ（jp.stanby.com の検索）の本文「年間2,850万人が行き交う国際空港」が、
# 日本熱処理工業会の品目の社員数 2,850万人として数えられた失敗から。求人サイトの検索結果ページの数字は、統計・会社の事実にしない（4-9）
JOBSEARCH = re.compile(r"(stanby|indeed|doda|mynavi|rikunabi|en-japan|townwork|baitoru|careerjet|kyujinbox|hatarako|jobtalk|openwork|en-hyouban|tenshoku)", re.I)
def is_job_search(url):
    p = urlparse(url)
    return bool(JOBSEARCH.search(p.netloc)) and bool(re.search(r"(search|/q-|[?&](q|keyword|kw)=)", (p.path or "") + "?" + (p.query or ""), re.I))
# 2026-11-05 4-31：桁の感覚の上限（4-4 の最後の確かめを機械に落とす）。1業種・1品目の社員数・従業者数が1,000万人を超えることはない
# （日本の製造業の就業者全体より大きい）。超えた人数は別の文脈（乗降客・人口・閲覧数）の混入として数えず、件数だけ出す
MAXPERSON = 1e7
# 2026-11-05 4-31：規模の内側の向き（日本銀行 WP22-J-3：労働生産性の変化を Melitz-Polanec の分解で「企業内の改善」と「構成・再配分」「参入・退出」に分ける）。
# 引用の中の従業者規模の区分（4〜9人・10〜29人・300人以上など）を値に付け、同じ地域×同じ規模の中で年ごとの向きを出す
SIZEBAND = re.compile(r"(\d+)\s*人?\s*[〜～~\-−―]\s*(\d+)\s*人|(\d+)\s*人\s*以\s*上")
def size_class_of(q, pos):
    best = ""
    for m in SIZEBAND.finditer(q[:pos + 1]):
        best = f"{m.group(1)}〜{m.group(2)}人" if m.group(1) else f"{m.group(3)}人以上"
    return best

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

# 2026-11-05 4-34：経済センサス-活動調査は調査の年の前年の実績を聞く（経産省「経済産業統計まる分かり」Q8：平成24年の活動調査は平成23年1〜12月の実績）。
# 調査の年を値の年にすると、工業統計の実績年と1年ずれて同じ年に2つの値が並ぶ。「実績」の語が近くに無いときだけ1年戻し、印を残す
CENSUS_ACT = re.compile(r"経済センサス\s*[-‐ー－―]?\s*活動調査")
CENSUS_YEARS = {"2012", "2016", "2021"}
# 2026-11-05 4-34：レジスター統計の「試算値」・推計値は実測でない（総務省・経産省「経済構造統計の体系的整備について」2022-08-30）。見通しと同じく事実の向きに入れない
TRIAL = re.compile(r"(試算値|試算|推計値|推計した|レジスター統計)")
# 2026-11-05 4-34：決算の表は負の値を△・▲・括弧で書く（Bradshaw「company accounts」：括弧の数字は損失で、見落としやすい）。負の金額を捨てたり正にしたりしない
# 2026-11-05 4-35：ハイフン・マイナス記号は負にしない。統計表の「-」（該当なし・秘匿）や範囲の「10-19」まで負と読み、mfg_capacity の受注額・1人あたりの欄に
# 負の値が入って食い違いの判定が外れ、【食い違い】が【一次で確認】に上がった失敗から。負は日本の表の慣行の△・▲と括弧だけで読む
NEGMARK = re.compile(r"[△▲]\s*$")
FINTABLE = re.compile(r"(当期純|純損|損失|損益|貸借対照表|決算公告|営業利益|経常利益|純資産)")
GOINGCONCERN = re.compile(r"(継続企業の前提|重要な不確実性|going concern|material uncertainty)", re.I)
GC = {}

PERDENOM = re.compile(r"1\s*(人|名|社|事業所|件|台|企業)\s*(あたり|当たり|当り|につき|平均)")

def norm(v, u):
    x = float(v.replace(",", ""))
    if u in TO_MAN: return x * TO_MAN[u], "万円"
    if u in TO_PERSON: return x * TO_PERSON[u], "人"
    if u == "％": u = "%"
    return x, u

OUTLIERS = {}
def rows_for(topic):
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: d = json.load(open(f, encoding="utf-8"))
        except Exception: continue
        yield from rows_from_doc(topic, d)

def rows_from_doc(topic, d):
    if True:
        name = d.get("name", "")
        for cat, items in (d.get("evidence") or {}).items():
            for it in items or []:
                q, url = unicodedata.normalize("NFKC", it.get("quote", "") or ""), it.get("url", "")  # 全角数字・全角％を半角に（4-12）
                q = join_okuman(q)
                if is_job_search(url): continue  # 4-31 求人サイトの検索結果ページの数字は事実にしない
                if HEADLINE.search(q) or "抜粋" in cat: continue  # 見出し・題名・検索結果の抜粋は数字にしない（METHOD.md 4-5）
                ys = YEAR.findall(q)
                ctx = context_of(q)
                for m in NUM.finditer(q):
                    if is_size_band(q, m): continue  # 規模区分の境目は数えない
                    # 2026-11-05：「1人あたり」「1事業所当たり」の1を人数・事業所数として拾い、幅が「1〜3,952事業所」になった失敗から。割り算の分母の1は数えない
                    if PERDENOM.match(q, m.start()): continue
                    v, u = norm(*m.groups())
                    pre = q[max(0, m.start() - 2):m.start()]
                    neg = bool(NEGMARK.search(pre)) or (u == "万円" and pre.endswith("(") and q[m.end():m.end() + 1] == ")" and bool(FINTABLE.search(q)))  # 4-35：括弧は決算の表の文脈だけ
                    if u == "万円" and v <= 0: continue
                    if neg and u == "万円": v = -v  # 4-34 △・▲・括弧は負の値（損失・減少）
                    if u == "人" and v > MAXPERSON: OUTLIERS[topic] = OUTLIERS.get(topic, 0) + 1; continue  # 4-31 桁の感覚の上限
                    # 数字の近く（前40字）の年を優先
                    near = YEAR.findall(q[max(0, m.start() - 40):m.start() + 5])
                    y = (near or ys or [""])[-1]
                    yfix = ""
                    if y in CENSUS_YEARS and CENSUS_ACT.search(q) and "実績" not in q[max(0, m.start() - 40):m.end() + 20]:
                        y, yfix = str(int(y) - 1), "活動調査の調査年→実績年（-1年）"
                    if GOINGCONCERN.search(q): GC.setdefault(topic, set()).add((name, url))
                    kind = "調査会社" if REPORT.search(url) else "信用調査" if CREDIT.search(url) else "一次" if is_primary(url) else "記事"
                    yield {"品目": name, "区分": cat, "文脈": ctx, "年": y, "値": round(v, 4), "単位": u,
                           "一次": kind, "立場": standing(url, kind),
                           "元へ戻る": "要" if kind != "一次" and CITES_PRIMARY.search(q) else "",
                           "定義": defn_of(q), "版": version_of(q), "規模": size_class_of(q, m.start()),
                           # 2026-11-05 4-19：出どころの記録（URL・取得日・取り方）。欠けた引用は後から確かめられない
                           # 2026-11-05：mfg_capacity の引用は first_seen が無く「記録が欠けた数字 1488/1488」になった。ファイルの researched_at を取得日に使う
                           "取得日": it.get("first_seen", "") or (d.get("researched_at", "") or "")[:10],
                           "予測": "予測" if y and int(y) > NOW_YEAR else "見通し" if OUTLOOK.search(q) else "試算" if TRIAL.search(q) else "",
                           "年の補正": yfix, "符号": "負" if neg and u == "万円" else "",
                           "出典": urlparse(url).netloc, "URL": url, "引用": q[:160]}


# 2026-11-05 4-38：判定の正解例での点検（ripla「名寄せシステム開発の進め方」：判定は人が付けた正解例と照合してから使い、
# 失敗例・未判定例も示す）。走査で見つかった失敗（4-5・4-21・4-31・4-35・4-37）を正解例にし、毎回の実行で抜き出しの規則が
# まだ効いているかを機械で確かめる。期待どおりでない例は scan_auto.md の先頭に出す（規則を直したときの戻り＝デグレを防ぐ）
GOLD = [
    ("関連記事の題名の数字（4-5）", "1万円〜1億円まで価格帯別に徹底解説", "https://example.com/a", []),
    ("億＋万の1つの値（4-5）", "製造品出荷額は1,512億9,435万円だった", "https://example.go.jp/a", [(1512 * 10000 + 9435, "万円")]),
    ("億＋百万円の1つの値（4-5）", "売上高は6,771億63百万円", "https://example.go.jp/a", [(6771 * 10000 + 63 * 100, "万円")]),
    ("為替の換算レート（4-5）", "1ドル約149円で換算", "https://example.com/a", []),
    ("規模区分の境目・半角~（4-21）", "従業者4人~29人の事業所", "https://example.go.jp/a", []),
    ("規模区分の境目・以上（4-5）", "従業者30人以上の事業所", "https://example.go.jp/a", []),
    ("1人あたりの分母の1（4-5）", "1人あたり加工賃は500万円", "https://example.go.jp/a", [(500, "万円")]),
    ("万＋人の1つの値（4-37）", "従業者数は11万8213人", "https://example.go.jp/a", [(11 * 10000 + 8213, "人")]),
    ("△は負の金額（4-34）", "当期純損失 △1,200万円", "https://example.go.jp/a", [(-1200, "万円")]),
    ("ハイフンは負にしない（4-35）", "加工賃 -  300万円", "https://example.go.jp/a", [(300, "万円")]),
    ("求人検索ページの数字（4-31）", "年間2,850万人が行き交う国際空港", "https://jp.stanby.com/search?q=a", []),
    ("1,000万人超の人数（4-31）", "利用者は延べ3,000万人", "https://example.com/a", []),
]
def gold_check():
    bad = []
    for label, q, url, want in GOLD:
        d = {"name": "__gold__", "evidence": {"テスト": [{"quote": q, "url": url}]}}
        got = sorted((round(r["値"], 4), r["単位"]) for r in rows_from_doc("__gold__", d))
        if got != sorted((round(v, 4), u) for v, u in want): bad.append(f"- ✗ {label}：「{q}」→ 期待 {want}／実際 {got}")
    OUTLIERS.pop("__gold__", None)
    return bad

# 2026-11-05 4-26：工業統計は2022年から経済構造実態調査（製造業事業所調査）に包まれ、対象が「従業者4人以上の全数」から
# 「出荷額上位9割の実測＋残りは伸び率で推計、1人以上全数を集計」に変わった（総務省・経産省 2021-03-24 資料）。2021年以前と2022年以降は同じ系列でつながない
STATSRC = re.compile(r"(工業統計|経済構造実態調査|経済センサス|製造業事業所調査|e-stat|meti\.go\.jp/statistics)", re.I)
BREAK_YEAR = 2022
def split_trend(series, year=BREAK_YEAR, label="工業統計→経済構造実態調査、対象範囲が変わった"):
    a = {y: v for y, v in series.items() if y < year}; b = {y: v for y, v in series.items() if y >= year}
    if not a or not b: return ""
    return f"⚠ 系列の切れ目（{year}年に{label}）：切れ目の前 {trend(a)}／後 {trend(b)}。前後をまたぐ向きは使わない"
# 2026-11-05 4-37：賃金構造基本統計調査は令和2年（2020年）に推計方法（復元倍率）・短時間労働者の対象・新規学卒者の把握を変えた（厚労省「遡及集計について」「利用上の注意」）。
# 2019年以前の公表値と2020年以降を直接つながない。比べるなら厚労省の遡及集計（参考系列、平成18年〜令和元年）どうしで
WAGESRC = re.compile(r"(賃金構造基本統計|賃金センサス|賃金構造)")
WAGE_BREAK = 2020
WAGE_RETRO = re.compile(r"(遡及|参考系列|特別集計)")
# 2026-11-05 4-37：%どうしの差はポイント（Sarah Cohen "Danger! Numbers in the Newsroom"）。%の変化率と%ポイントの差を同じ欄で比べない
PPOINT = re.compile(r"\d+(?:\.\d+)?\s*(ポイント|pt|%ポイント)", re.I)
# 2026-11-05 4-37：回答が100未満の調査の%は偽りの精度（Cohen：20社なら1社で5ポイント動く）。実数で読む
SMALLN = re.compile(r"(n\s*=\s*\d{1,2}(?!\d)|回答\s*(数)?\s*\d{1,2}\s*(社|人|件|名)(?!\d))", re.I)
REAL = re.compile(r"(実質|物価調整|インフレ調整|デフレーター|消費者物価で割)")
# 2026-11-05 4-26：1981〜2019年の工業統計は西暦末尾0・3・5・8年が全数、他の年は小さい事業所の一部を調べない（RIETI 05-P-001）。事業所数が年ごとにノコギリ型に動く
def census_mix(years):
    old = [y for y in years if 1981 <= y <= 2019]
    full = {y for y in old if y % 10 in (0, 3, 5, 8)}
    return bool(full) and bool(set(old) - full)

def trend(series):
    ys = sorted(series)
    if len(ys) < 3: return "判定不可（年が3点未満）"
    first, last = series[ys[0]], series[ys[-1]]
    if first == 0: return "判定不可"
    ch = (last - first) / abs(first)
    word = "上昇" if ch > 0.05 else "下降" if ch < -0.05 else "横ばい"
    # 2026-11-05 4-37：年ごとの伸びは足し算でなく複利（Cohen：年3%の10年は30%でなく約34%）。年率は (最後÷最初)^(1/年数)-1 で出す
    span = ys[-1] - ys[0]
    cagr = f"、年率{((last / first) ** (1 / span) - 1) * 100:+.1f}%（複利）" if span > 0 and first > 0 and last > 0 else ""
    return f"{word}（{ys[0]}→{ys[-1]}で{ch*100:+.0f}%{cagr}）"

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
def indep_key(r, ec=None):
    # 2026-11-05 修正：前の版は一次でない数字を値ごとに別の出どころと数え、同じホスト1か所の近い値2つで【裏付けあり】になっていた
    # （mfg_capacity のめっき・金型で「一次0件・出典1か所」なのに【裏付けあり】）。独立の単位はホスト。写し（同じ値が一次でない2か所以上）だけ1つに束ねる
    if r["一次"] != "一次" and ec and r["値"] in ec: return ("同じ値", r["値"])
    return src_key(r)

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
    # 4-35：負の値（損失・減少）は幅・食い違いの判定に入れない。正の値だけで判定する
    vals = sorted(r["値"] for r in g if r["値"] > 0) or sorted(r["値"] for r in g)
    med = statistics.median(vals)
    ec = echoes(g)
    # 2026-11-05 走査の失敗から：横須賀市・会津若松市・大阪市・経産省（全国）の別々の対象の数字が、中央値の近さだけで【裏付けあり】になった
    # （mfg_capacity の「製造業の受託加工全体」）。「地域が違う」の警告は出ていたが確度に効いていなかった。裏付けは同じ地域（全国は全国どうし）の中だけで数える
    near = {}
    for r in g:
        if med and abs(r["値"] - med) <= 0.2 * abs(med): near.setdefault(region_of(r["URL"]) or "全国", set()).add(indep_key(r, ec))
    # 2026-11-05 4-31：矛盾に注目する（やさしいサイバーセキュリティ「OSINT 情報評価」の ACH の手順4：合う証拠の数より、合わない証拠を先に見る）。
    # mfg_hiring_seg の「採用単価［給与・人件費］」が幅 3〜100万倍なのに、中央値の近くの2か所だけで【裏付けあり】になった失敗から。
    # 幅が10倍を超え、中央値から離れた値しか持たない独立の出どころが1つでもあれば、裏付けではなく【食い違い】にする
    agree = {indep_key(r, ec) for r in g if med and abs(r["値"] - med) <= 0.2 * abs(med)}
    contra = {indep_key(r, ec) for r in g} - agree
    if vals and vals[0] > 0 and vals[-1] / vals[0] > 10 and contra and len(doms) >= 2: return "【食い違い】"
    if any(len(v) >= 2 for v in near.values()): return "【裏付けあり】"
    # 2026-11-05 4-37 走査の失敗から：mfg_capacity で1か所（1つのホスト）の欄の幅が 6,257倍・782,397倍なのに【一次で確認】と出た。
    # 1か所の中で100倍以上散るのは、同じ表の別の行・列（総額と1あたり、別の業種・規模）が1つの欄に混ざった印。確認ではなく【未確認】にする
    if vals and vals[0] > 0 and vals[-1] / vals[0] >= 100 and len(doms) == 1: return "【未確認】"
    if vals and vals[0] > 0 and vals[-1] / vals[0] > 3 and len(doms) >= 2: return "【食い違い】"
    if any(r["一次"] == "一次" for r in g): return "【一次で確認】"
    return "【未確認】"

# 2026-11-05 4-21：代表値を採る規則（survivorship）を明示する（Data Ladder の entity resolution：元の記録は残し、代表値の選び方は別の段で規則にする）。
# 順：予測・見通しを外す → 一次があれば一次だけ → 確報・改定があれば速報を外す → いちばん新しい年 → その年の中央値。
# 食い違い・低確度のときは保守側（低い側＝第1四分位、4件未満は最小）も並べる（CUFinder：信号が合わなければ低い側に寄せ、低確度と印す）。
# 2026-11-05 4-28：Admiralty（NATO）式の2軸の格付け（usersearch の Admiralty Code for OSINT）。出どころの信頼度 A〜F と情報の確からしさ 1〜6 を別に付け、
# 集めた時点で機械が付ける（結論を作ったあとに付けると見方が格付けに染みる）。過去の実績を持たないので出どころは A を使わず、一次=B・報道=C・調査会社/信用調査=D・その他=F。
# 情報：一次どうし2か所以上の一致=1、【裏付けあり】=2、【一次で確認】=3、【食い違い】=4、【未確認】=6。F6 は「判断材料がまだ無い」で誤りではない。捨てずに穴埋めの検索に回す
def admiralty(g, gr):
    kinds = {r["一次"] for r in g}
    hosts = {r["出典"] for r in g}
    src = "B" if "一次" in kinds else "C" if any(NEWSHOST.search(h or "") for h in hosts) else "D" if kinds & {"調査会社", "信用調査"} else "F"
    vals = [r["値"] for r in g]; pmed = statistics.median(vals)
    prim_ok = {r["出典"] for r in g if r["一次"] == "一次" and pmed and abs(r["値"] - pmed) <= 0.2 * abs(pmed)}
    info = "1" if len(prim_ok) >= 2 and gr == "【裏付けあり】" else {"【裏付けあり】": "2", "【一次で確認】": "3", "【食い違い】": "4"}.get(gr, "6")
    return src + info

# 2026-11-05 4-29：単位の読み違いの指紋（電子決算公告ドットコム「官報の決算公告の見方」：公告は百万円か千円で省略して載り、単位を見ずに読むと100倍・1,000倍を誤る）。
# 最大÷最小が10・100・1,000・10,000倍の±15%に入るときは、別物ではなく単位の読み違いを先に疑う
def unit_slip(lo, hi):
    if lo <= 0: return None
    r = hi / lo
    for k in (10, 100, 1000, 10000):
        if abs(r / k - 1) <= 0.15: return k
    return None

# 2026-11-05 4-30：統計の注記に出る「○○年から計算方法を変更」「区分の変更」（stats47「e-Stat の7つのテクニック」：メタ情報の注記を見てから系列をつなぐ）。
# 引用にこの型があれば、その年を品目の「定義の変わり目」として並べ、またいだ向きを使わない
DEFCHANGE = re.compile(r"((19[89]\d|20[0-3]\d)\s*年度?\s*(から|より|以降)[^。]{0,20}?(計算方法|算出方法|定義|区分|集計方法|調査方法|対象範囲|分類|基準)[^。]{0,8}?(を|が)?\s*(変更|改定|見直|改訂|移行))")

def pick(g):
    c = [r for r in g if not r.get("予測")] or []
    if not c: return None
    c = [r for r in c if r["一次"] == "一次"] or c
    # 2026-11-05 4-30：市・県の統計と全国の統計が同じ欄にあるときは、全国（region が空）の一次だけで代表値を採る。地域が混ざった中央値は何の値でもない
    nat = [r for r in c if r["一次"] == "一次" and not region_of(r["URL"])]
    if nat and len(nat) < len(c) and any(region_of(r["URL"]) for r in c): c = nat
    fin = [r for r in c if r.get("版") in ("確報", "改定")]
    if fin: c = [r for r in c if r.get("版") != "速報"]
    yrs = [int(r["年"]) for r in c if r["年"]]
    if yrs:
        y = max(yrs); c = [r for r in c if r["年"] and int(r["年"]) == y]
    else: y = None
    vals = sorted(r["値"] for r in c)
    return y, statistics.median(vals), len(c), {r["出典"] for r in c}, {region_of(r["URL"]) or "全国" for r in c}

def conservative(vals):
    vals = sorted(vals)
    return statistics.quantiles(vals, n=4)[0] if len(vals) >= 4 else vals[0]

def scan(topic):
    rows = drop_boilerplate(dedupe(list(rows_for(topic))))
    if not rows: return 0
    with open(os.path.join(OUT, topic, "cross.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    md = [f"# 走査（自動）— {topic}", "", "機械で出した突き合わせと【推定】。式と出典件数つき。最終の結論は scan.md。", ""]
    gb = gold_check()
    md += [f"- 判定の正解例の点検（4-38）：{len(GOLD) - len(gb)}／{len(GOLD)}件が期待どおり", *gb]
    picks = {}  # 4-38：前回からの変化を出すため、欄ごとの採る値を残す
    # 2026-11-05 4-19：出どころの記録（URL・取得日）が欠けた数字を数える。0件でなければ HEALTH と同じく直す対象
    nprov = sum(1 for r in rows if not r["URL"] or not r["取得日"])
    md += [f"- 出どころの記録が欠けた数字：{nprov}件／{len(rows)}件（URLか取得日が無い）"]
    # 2026-11-05 4-21：機械で決めきれず人（Claude の走査）の確認が要る件数（Data Ladder：保留の件数を品質の指標にする）。文脈不明か年が無い数字
    nhold = sum(1 for r in rows if r["文脈"] == "不明" or not r["年"])
    md += [f"- 桁の感覚の外れで外した人数（1,000万人超・4-31）：{OUTLIERS.get(topic, 0)}件"]
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
            # 2026-11-05 走査の失敗から：「社員数・平均社員数」の欄に万円・%、「事業所数・社数」の欄に%が入り、【裏付けあり】と出た（mfg_capacity の中小企業実態基本調査・金型）。
            # 欄の型と単位が合わない数字は、その欄の事実にしない（4-8 の欄ごとの型の確認を走査にも当てる）。件数だけ出して確度は付けない
            if (cat.startswith("社員数") and u not in ("人",)) or (cat.startswith("事業所数") and u not in ("事業所", "社")):
                md.append(f"- ⚠ 欄と単位が合わない：{cat}［{ctx}］に（{u}）が{len(g)}件。別の欄の混入なので確度を付けず、結論に使わない")
                continue
            vals = [r["値"] for r in g]; prim = [r for r in g if r["一次"] == "一次"]
            pos = [v for v in vals if v > 0] or vals  # 4-35：幅は正の値で取る。負の値は件数だけ別に出す
            lo, hi = min(pos), max(pos)
            nneg_g = len(vals) - len([v for v in vals if v > 0])
            if nneg_g: md.append(f"- （下の欄に負または0の値{nneg_g}件。幅・確度から外した）")
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
            md.append(f"  - 書き方（4-23）：{phrasing(gr, g)}／格付け（4-28・出どころA〜F×情報1〜6）：{admiralty(g, gr)}")
            # 2026-11-05 4-25：証拠の量に引っ張られない（Heuer の競合仮説分析：大量の証拠に見えても出どころが偏れば強さではない）。
            # 4件以上の欄で1か所のホストが半分超を出していれば、件数でなく出どころの数で読むよう印す
            hc = {}
            for r in g: hc[src_key(r)] = hc.get(src_key(r), 0) + 1
            top_h, top_n = max(hc.items(), key=lambda kv: kv[1])
            if len(g) >= 4 and top_n * 2 > len(g): md.append(f"  - ⚠ 1か所に偏り：{len(g)}件のうち{top_n}件が {top_h}。件数ではなく出どころの数（{len(hc)}か所）で確度を読む")
            regs = sorted({region_of(r["URL"]) for r in g} - {""})
            if len(regs) >= 2: md.append(f"  - ⚠ 地域が違う出どころ（{'・'.join(regs[:5])}）：別々の自治体の統計は同じ対象の裏付けではない。地域ごとに別の品目として読む（4-20）")
            ec = echoes(g)
            if ec: md.append("  - ⚠ 写しの疑い（一次でない別々の出どころが同じ値をぴったり持つ。1件に束ねて数えた）：" + "、".join(f"{v:g}{u}が{n}か所" for v, n in sorted(ec.items())[:5]) + "。元（統計・開示・調査）までたどる")
            if len({src_key(r) for r in g}) == 1: thin.append(f"{item}｜{cat}［{ctx}］")
            pk = pick(g)
            if pk:
                py, pv, pn, pd, preg = pk
                picks[f"{item}｜{cat}［{ctx}］（{u}）"] = {"年": py, "値": pv, "件": pn, "確度": gr}
                md.append(f"  - 採る値（規則：予測除外→一次優先→全国優先→確報優先→最新年→中央値）：{pv:g}{u}（{py or '年不明'}年・{pn}件・出典{len(pd)}か所）")
                if len(preg) >= 2: md.append(f"  - ⚠ 採る値に地域が混ざる（{'・'.join(sorted(preg)[:5])}）：この中央値は使わず、地域ごとに読む（4-30）")
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
            us = unit_slip(lo, hi)
            if us and len(g) >= 2: md.append(f"  - ⚠ 単位の読み違いの指紋：最大と最小がほぼ{us:,}倍（千円↔百万円＝1,000倍、百万円↔万円＝100倍）。表の単位の欄を本文で確かめる（4-29）")
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
            # 2026-11-05 4-37 走査の失敗から：京都府と奈良県の一次2か所で「一次どうしの裏付け」と出た。地域が違う統計は同じ対象の裏付けではない（4-30）。地域ごとに数える
            preg_d = {}
            for r in prim:
                if pmed and abs(r["値"] - pmed) <= 0.2 * abs(pmed): preg_d.setdefault(region_of(r["URL"]) or "全国", set()).add(r["出典"])
            pdoms = max(preg_d.values(), key=len) if preg_d else set()
            if len(pdoms) >= 2: md.append(f"  - 一次どうしの裏付け：一次{len(pdoms)}か所が中央値±20%で一致（確度がいちばん高い）")
            cr = sum(1 for r in g if r["一次"] == "信用調査")
            if cr: md.append(f"  - 信用調査会社の数字{cr}件（推定値を含むので【推定】扱い・一次に数えない）")
            if med: md.append(f"  - 【推定】年ごとの中央値（{'一次のみ' if prim else '記事含む'}）：" + "、".join(f"{y}年 {v:g}" for y, v in sorted(med.items())) + f" → 向き：{trend(med)}")
            if med and any(STATSRC.search(r["引用"] + r["URL"]) for r in g):
                st = split_trend(med)
                if st: md.append(f"  - {st}")
                if u in ("事業所", "社") and census_mix(med): md.append("  - ⚠ 全数年と裾切り年が混在（1981〜2019年は末尾0・3・5・8年だけ全数）：事業所数の増減は全数年どうしで比べる")
            # 4-37：賃金構造基本統計調査の2020年の推計方法の変更をまたがない（遡及集計の参考系列どうしなら比べてよい）
            if med and any(WAGESRC.search(r["引用"]) for r in g):
                st = split_trend(med, WAGE_BREAK, "賃金構造基本統計調査の推計方法・短時間労働者の対象が変わった")
                if st: md.append(f"  - {st}" + ("（引用に遡及集計・参考系列の語あり：その系列どうしなら比べてよい）" if any(WAGE_RETRO.search(r["引用"]) for r in g) else "（厚労省の遡及集計＝参考系列 平成18年〜令和元年で取り直す）"))
            # 4-37：長期の金額の向きは名目。実質（物価で割った値）の語が無ければ名目と印す
            if u == "万円" and med and max(med) - min(med) >= 5 and not any(REAL.search(r["引用"]) for r in g):
                md.append("  - 名目の向き：物価で割っていない金額の向き。10年の伸びの一部は物価の上昇で、能力の伸びと書く前に実質と並べる（4-37）")
            # 4-37：%の欄に「ポイント」の差が混ざる／回答100未満の調査の%
            if u == "%" and any(PPOINT.search(r["引用"]) for r in g):
                md.append("  - ⚠ %とポイントの混在：%どうしの差（ポイント）と%の変化率を同じ欄で比べない。引用ごとにどちらかを確かめる（4-37）")
            nsm = sum(1 for r in g if u == "%" and SMALLN.search(r["引用"]))
            if nsm: md.append(f"  - ⚠ 回答100未満の調査の%：{nsm}件（1件で数ポイント動く偽りの精度。実数で読み、裏付けに数えない。4-37）")
        # 4-30：定義・計算方法の変わり目の注記
        dch = sorted({m.group(2) for r in rs for m in DEFCHANGE.finditer(r["引用"])})
        if dch: md.append("- ⚠ 定義・計算方法の変わり目の注記（" + "・".join(f"{y}年" for y in dch) + "）：この年をまたいだ向きは使わない。メタ情報・注記で中身を確かめる（4-30）")
        # 1人あたり：同じ年に万円と人がある場合
        money = {}; people = {}
        for r in rs:
            if not r["年"] or r["予測"]: continue
            if r["単位"] == "万円": money.setdefault(int(r["年"]), []).append(r["値"])
            if r["単位"] == "人": people.setdefault(int(r["年"]), []).append(r["値"])
        per = {y: statistics.median(money[y]) / statistics.median(people[y]) for y in money if y in people and statistics.median(people[y]) > 0}
        # 2026-11-05 4-37：物理的にありえない1人あたり（年0.1万円＝1,000円未満）は別の欄の割り算。向きに入れない（金属切削で「2024年 0.0万円→-100%」と出た失敗から。4-35 の物理的上限・下限）
        per = {y: v for y, v in per.items() if v >= 0.1}
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
        # 2026-11-05 4-25：生き残りの偏り・構成の変化（社会構想デザイン機構の統計リテラシー：廃業した先は統計から消える）。
        # 1あたりの金額が上がり、同じ期間に事業所・社の数が下がっていれば、能力が上がったのではなく小さい先が退出しただけの可能性がある
        cnt = {}; unit = {}
        for r in rs:
            if not r["年"] or r["予測"]: continue
            if r["単位"] in ("事業所", "社"): cnt.setdefault(int(r["年"]), []).append(r["値"])
            if r["単位"] == "万円" and ("あたり" in r["区分"] or re.search(r"(あたり|当たり|平均)", r["引用"])): unit.setdefault(int(r["年"]), []).append(r["値"])
        cm = {y: statistics.median(v) for y, v in cnt.items()}; um = {y: statistics.median(v) for y, v in unit.items()}
        tc, tu = trend(cm), trend(um)
        # 2026-11-05 4-26：事業所数の「減少」は廃業だけではない（転業・他市区町村への転出・3人以下への規模下がりも含む。RIETI 05-P-001）。
        # 2022年の対象の変更をまたぐ事業所数は比べない
        if split_trend(cm): md.append(f"  - 事業所・社の数：{split_trend(cm)}")
        if tc.startswith("下降") and tu.startswith("上昇"):
            md.append(f"  - ⚠ 生き残り・構成変化の疑い：1あたり金額は{tu}、同じ品目の事業所・社の数は{tc}。規模別に分けた同じ表で、規模の内側でも上がっているかを確かめるまで「能力が上がった」と書かない（数の減少には転業・転出・規模下がりも含む。4-26）")
        # 2026-11-05 4-31：規模の内側の向き（日銀 WP22-J-3 の Melitz-Polanec 分解の「内側の変化」にあたる部分だけを機械で出す）。
        # 同じ地域×同じ規模区分×1あたり（万円）の年ごとの中央値で向きを出す。全区分で同じ向きなら構成変化だけでは説明できない。区分が混ざれば「判定できない」
        within = {}
        for r in rs:
            if not r["年"] or r["予測"] or not r.get("規模") or r["単位"] != "万円": continue
            if not ("あたり" in r["区分"] or re.search(r"(あたり|当たり|平均)", r["引用"])): continue
            within.setdefault((region_of(r["URL"]) or "全国・不明", r["規模"]), {}).setdefault(int(r["年"]), []).append(r["値"])
        wt = {k: trend({y: statistics.median(v) for y, v in ser.items()}) for k, ser in within.items()}
        if wt:
            md.append("  - 【推定】規模の内側の向き（同じ地域×同じ規模区分の1あたり金額・4-31）：" + "、".join(f"{reg}｜{band}：{t}" for (reg, band), t in sorted(wt.items())))
            words = {t[:2] for t in wt.values() if t[:2] in ("上昇", "下降", "横ばい")}
            md.append("  - 規模の内側の判定：" + ("全区分で" + next(iter(words)) + "（構成変化だけでは説明できない）" if len(words) == 1 and all(t[:2] in ("上昇", "下降", "横ばい") for t in wt.values()) else "判定できない（3年以上そろった区分が足りないか、区分で向きが割れる）"))
        # 2026-11-05 4-35：配分の効き（Olley-Pakes の分解。財務総研 PPR 2017 の分解手法の整理より）。
        # 同じ年×同じ地域で、規模区分ごとの総額（万円・1あたりでない）と人数がそろえば、
        # 全体の1人あたり（総額の和÷人数の和＝人数で重みづけ）と、区分ごとの1人あたりの単純平均を並べ、差を「配分の効き」とする。
        # 差が正なら1人あたりの高い区分に人が多く集まっている（大きい先が平均を押し上げる）。差の向きが年で変われば構成の移り変わり
        tot = {}
        for r in rs:
            if not r["年"] or r["予測"] or not r.get("規模") or r["単位"] not in ("万円", "人"): continue
            if "あたり" in r["区分"] or re.search(r"(あたり|当たり|平均)", r["引用"]): continue
            k = (int(r["年"]), region_of(r["URL"]) or "全国・不明", r["規模"])
            tot.setdefault(k, {}).setdefault(r["単位"], []).append(r["値"])
        opd = {}
        for (y, reg, band), d0 in tot.items():
            if "万円" in d0 and "人" in d0 and statistics.median(d0["人"]) > 0:
                opd.setdefault((y, reg), {})[band] = (statistics.median(d0["万円"]), statistics.median(d0["人"]))
        oline = []
        for (y, reg), bands in sorted(opd.items()):
            if len(bands) < 2: continue
            w = sum(m for m, _ in bands.values()) / sum(p for _, p in bands.values())
            uw = statistics.mean(m / p for m, p in bands.values())
            oline.append(f"{y}年・{reg}（{len(bands)}区分）：加重 {w:,.1f}／単純平均 {uw:,.1f}／配分の効き {w - uw:+,.1f}万円")
        if oline: md.append("  - 【推定】配分の効き（規模区分の総額÷人数。加重の1人あたり−区分の単純平均。4-35）：" + "、".join(oline))
        md.append("")
    # 2026-11-05 4-19：何が見つからなかったかを書く（範囲の明示）。数字が1つも取れなかった品目を並べる
    names = set()
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: names.add(json.load(open(f, encoding="utf-8")).get("name", ""))
        except Exception: pass
    empty = sorted(n for n in names - set(by_item) if n)
    # 2026-11-05 4-24：記事・調査会社が一次を名指しして紹介している数字。元の統計・開示を取り直すまで「記者が書いたこと」として扱う
    back = [r for r in rows if r.get("元へ戻る")]
    md += [f"## 元へ戻って取り直す数字（記事が一次を名指し・{len(back)}件・4-24）",
           *(f"- {r['品目']}｜{r['値']:g}{r['単位']}（{r['年'] or '年不明'}年）｜{r['出典']}：{r['引用'][:60]}" for r in back[:15]), *(["（なし）"] if not back else []), ""]
    # 2026-11-05 4-25：再調査の条件を結果と一緒に残す（Deskrex のデスクリサーチ自動化：見直し期限と再調査条件を台帳に書く）。
    # 取得から7日を超えた品目は取り直しの対象として並べる（control.yaml の run: true にする目安）
    import datetime
    today = datetime.date.today(); stale = []
    for item, rs in sorted(by_item.items()):
        ds = [r["取得日"] for r in rs if re.match(r"\d{4}-\d{2}-\d{2}$", r["取得日"] or "")]
        if ds:
            age = (today - datetime.date.fromisoformat(max(ds))).days
            if age > 7: stale.append(f"- {item}：最後の取得 {max(ds)}（{age}日前）")
    md += ["## 再調査の条件（取得から7日超・4-25）", *(stale or ["（なし）"]), ""]
    # 2026-11-05 4-34：年の補正・負の値・継続企業の前提の注記を数える（見落とし防止）
    nfix = sum(1 for r in rows if r.get("年の補正")); nneg = sum(1 for r in rows if r.get("符号"))
    ntrial = sum(1 for r in rows if r.get("予測") == "試算")
    md += ["## 年・符号・試算の点検（4-34）", f"- 経済センサス-活動調査の調査年を実績年に1年戻した数字：{nfix}件",
           f"- △・▲・括弧の負の金額：{nneg}件（損失・減少。正の値と同じ欄で中央値を取るときは本文で確かめる）",
           f"- 試算値・推計値として事実の向きから外した数字：{ntrial}件",
           *(f"- ⚠ 継続企業の前提・重要な不確実性の注記：{n}（{u}）" for n, u in sorted(GC.get(topic, set()))[:10]), ""]
    # 2026-11-05 4-38：前回からの変化（ICD 203：新しい情報や推論がどう判断を変えたかを書き、定型の繰り返しを避ける）。
    # 前回の採る値（picks.json）と比べ、値が±20%を超えて動いた欄・確度が変わった欄・新しく出た欄・消えた欄を並べる
    pf = os.path.join(OUT, topic, "picks.json")
    try: prev = json.load(open(pf, encoding="utf-8"))
    except Exception: prev = None
    ch = []
    if prev is not None:
        for k, v in sorted(picks.items()):
            o = prev.get(k)
            if o is None: ch.append(f"- 新しく出た：{k} {v['値']:g}（{v['年'] or '年不明'}年）{v['確度']}"); continue
            moved = o.get("値") and abs(v["値"] - o["値"]) > abs(o["値"]) * 0.2
            if moved or o.get("確度") != v["確度"] or o.get("年") != v["年"]:
                ch.append(f"- 変わった：{k} {o.get('値', 0):g}（{o.get('年') or '年不明'}年）{o.get('確度', '')} → {v['値']:g}（{v['年'] or '年不明'}年）{v['確度']}")
        ch += [f"- 消えた：{k}" for k in sorted(set(prev) - set(picks))]
    md += ["## 前回からの変化（採る値・確度・年。4-38）", *(ch[:40] or (["（変化なし）"] if prev is not None else ["（初回。次の実行から比べる）"])), *([f"- ほか{len(ch) - 40}件"] if len(ch) > 40 else []), ""]
    json.dump(picks, open(pf, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    md += ["## 出どころが1か所だけの欄（裏が薄い。穴埋めの検索の対象・4-23）", *(f"- {t}" for t in thin or ["（なし）"]), ""]
    md += ["## 数字が1つも取れなかった品目（範囲の明示）", *(f"- {n}" for n in empty or ["（なし）"]), ""]
    open(os.path.join(OUT, topic, "scan_auto.md"), "w", encoding="utf-8").write("\n".join(md))
    return len(rows)

if __name__ == "__main__":
    topics = sys.argv[1:] or [os.path.basename(p.rstrip("/")) for p in glob.glob(os.path.join(OUT, "*/")) if not p.rstrip("/").endswith("brief")]
    for t in topics:
        n = scan(t)
        if n: print(t, n)
