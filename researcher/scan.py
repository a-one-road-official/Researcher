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
COPYHOST = re.compile(r"(irbank|buffett-code|kabutan|minkabu|ullet|houjin\.info|houjin-info|corporate-number|alarmbox|catr\.jp|kanpoo\.jp|kanpo-kensaku|b2bportal|houjinbangou\.|stockclip|kabuyoho|yahoo)", re.I)
# 2026-11-05 走査の失敗から：神奈川県・京都府の統計（www.pref.kanagawa.jp・www.pref.kyoto.jp）が「.lg.jp」でないため一次0件と出た（mfg_capacity の工業統計表の品目）。
# 都道府県・市の公式ドメイン（pref.<名>.jp・city.<名>.<…>.jp・metro.tokyo）は自治体自身の統計なので一次にする
LOCALGOV = re.compile(r"(^|\.)(pref\.[a-z]+\.jp|city\.[a-z-]+(\.[a-z]+)?\.jp|metro\.tokyo(\.lg)?\.jp)$", re.I)
# 2026-11-06 4-49：範囲の違いの語（グロービス「マーケットシェア」・電子決算公告ドットコム「官報の決算公告の閲覧」）
# 2026-11-06 4-50：品目が名指しする指標の語（結果の差し替えの点検に使う）
INDICATOR = ["加工賃", "出荷額", "付加価値", "従業者", "事業所数", "受注", "給与", "採用単価", "採用費", "有効求人倍率", "売上高", "生産額"]
SCOPEMIX = re.compile(r"(合算|を含む|を含めた|含まない|を除く|連結ベース|全事業|グループ全体|広義|狭義)")
# 2026-11-06 4-53（一田和樹のメモ帳のファクトチェック事例：一次資料の留保語を1語落とすだけで主張が反転する／週間と月間の違う指標が並べられる）。
# 数字の前後に上限・留保の語（最大・最高・理論上・ピーク時・上限・最低・最小）があれば、典型の値でなく端の値として印す。
# 同じ欄に、集計の期間（週・月・四半期・年・累計・延べ）が違う数字が混ざれば、期間をそろえるまで突き合わせない
HEDGE = re.compile(r"(最大|最高|理論上|理論値|ピーク時?|上限|最低|最小|最も多い|過去最高|過去最多)")
PERIOD = {"週": re.compile(r"(週間|毎週|週あたり|週当たり|1週)"), "月": re.compile(r"(月間|毎月|月あたり|月当たり|月次|1か月|1カ月|ひと月)"),
          "四半期": re.compile(r"(四半期|3か月間|3カ月間)"), "年": re.compile(r"(年間|毎年|年あたり|年当たり|年次|通年|1年間)"),
          "累計": re.compile(r"(累計|累積|延べ|これまでに|創業以来)")}
def period_of(q):
    hit = [k for k, rx in PERIOD.items() if rx.search(q)]
    return hit[0] if len(hit) == 1 else ""
# 2026-11-06 4-53（インティメート・マージャー「市場規模の調べ方」：公的統計を使うときは資料名・統計表番号・対象年・URLを記録する）。
# 一次の統計の値で、URL が表を指していない（e-Stat の表番号 sid・statdisp_id・ファイル、xlsx・csv が無い）ものは、どの表の値か後から引けない
STATTABLEID = re.compile(r"(sid=|statdisp_id=|stat_infid=|toukei=|tstat=|file-download|\.xlsx?($|\?)|\.csv($|\?)|/excel/|/xls/)", re.I)
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
        ("付加価値", re.compile(r"付加価値")), ("加工賃", re.compile(r"加工賃")), ("規模の裾切り", re.compile(r"(従業者|従業員)\s*\d+\s*人以上|\d+人以上の事業所")),
        # 2026-11-06 4-58（ゴムマリ「経済センサスの統計表の取り方と活用法」：令和6年基礎調査と令和3年活動調査は対象の範囲が違い、統計局が比較に留意としている。
        # 売上（収入）金額は必要な事項が得られた事業所だけの集計で、事業所数の全体とは分母が違う）。調査の種類と売上の集計範囲を定義に残し、混ざれば「定義が混在」で出す
        ("センサス基礎調査", re.compile(r"経済センサス\s*[-‐ー－―]?\s*基礎調査")), ("センサス活動調査", re.compile(r"経済センサス\s*[-‐ー－―]?\s*活動調査")),
        ("売上は回答事業所のみ", re.compile(r"(必要な事項の数値が得られた|売上\s*[（(]?収入[）)]?\s*金額.{0,20}(集計対象|得られた事業所))"))]
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
# 2026-11-06 4-54 走査の失敗から：am_ceiling の「公式料金表（国内各社）」で、比較サイトの「729c㎥×600円＝437,400円」の 600円/cm3（体積あたり）と
# 合計 437,400円が同じ「1件の単価」の欄に並び、幅 0.06〜43.74万円・食い違い729倍と出た（729倍は体積そのもの）。体積・重さ・時間あたりの値段は1件の単価と分ける
CONTEXT = [("体積・重さ・時間あたりの単価", re.compile(r"(c㎥|cm3|cm³|立方センチ|1\s*g(あたり|当たり)|グラムあたり|/kg|kgあたり|1時間あたり|時間単価|per cm|per gram|per hour)\s*(あたり|当たり|×|x)?", re.I)),
           ("市場規模", re.compile(r"(市場規模|市場は|市場が|market size|market (is|was|will)|CAGR|年平均成長率|億ドル|billion)", re.I)),
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

# 2026-11-06 4-40 走査の失敗から：e-Stat のデータベース表示（dbview）の選択肢「500万円超~1千万円」「1億円超~5億円」が、
# 中小企業実態基本調査の品目の受注金額として13件数えられ、幅200倍の【食い違い】になった。金額の階級（売上高・資本金の区分）の境目は値にしない
def is_money_band(q, m):
    if not m.group(2).endswith("円"): return False
    after, before = q[m.end():m.end() + 6], q[max(0, m.start() - 3):m.start()]
    if re.match(r"\s*(以\s*上|未\s*満|以\s*下|超|〜|～|~|から\s*\d)", after) or re.search(r"[〜～~]\s*$|超\s*[〜～~]\s*$|から\s*$", before): return True
    return bool(re.fullmatch(r"\s*[\d,.千百万億兆]+円(以下|未満|以上|超)?\s*([〜～~]\s*[\d,.千百万億兆]+円(以下|未満)?)?\s*", q))

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
# 2026-11-06 4-56（イノベーション総合研究所「市場規模の調べ方」：公開統計・自社データ・仮置きを区別して計算し、仮置きは仮定と明記して検証後に更新する）。
# 引用の書き手が「仮に」「と仮定」「と置く」「想定すると」で置いた数字は、実測でも統計でもない仮置き。見通し・試算と同じく事実の向き・裏付けに入れない
# 2026-11-06 走査の失敗から（4-57）：mfg_capacity の造船の就労者数で、国交省の「2025年に22.5百万トンを建造する前提とした場合…13.5万人となる」
# （2014年ごろの計画の前提の値）が2025年の実績として採る値になり、「2014→2025で上昇」と出た。「前提とした場合」「を前提に」の型も仮置きに入れる（同じ資料の表の行「2025年 13.5万人」は語が無く残るので scan.md で外す）
ASSUME = re.compile(r"(仮に|と仮定|を仮定|仮定する|仮置き|と置く|と置いて|想定すると|と想定して|前提とした場合|前提とすると|を前提に|とした場合|の場合の想定|assum(e|ing) that|suppose)", re.I)
# 2026-11-06 4-56（角渕由英 note「生成AIで特許侵害を見つけられるか？」：出力した各引用が元の本文の中に、空白・改行をそろえたうえで実在するかをコードで確かめる。
# 判定と抜き出しを分け、引用の機械検証を必ず挟む）。cross.csv の引用は頭160字なので、値が161字目より後ろにあると、表を見た人が値の出どころを引用で確かめられない。
# そのときは頭100字（重複除去・共通文の判定に使う頭40字は変えない）＋「…」＋値の前後の窓にし、値の文字列が引用の中に見えるかを scan() で数える
QHEAD = 160
QCUT = {}
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
# 2026-11-07 4-61：【食い違い】の欄のうち、記録した次元（期間・定義・版・規模・種類）で分かれた欄と分かれなかった欄の数。
# 分かれない欄が多いほど、同じ対象か・単位の読みの誤りか（4-1 の③④）を本文で確かめる必要が大きい
DIMSPLIT = {}
# 2026-11-05 4-39：除外の点検（Kasica ら「Dirty Data in the Newsroom」CHI'23 の filter check：行を落としたら要約を取り直す。
# 落とした件数を見ないと、見かけの完全さだけが上がる）。規則ごとに外した数字の件数を数えて scan_auto.md に出す
DROPS = {}
def drop(topic, why): DROPS.setdefault(topic, {}); DROPS[topic][why] = DROPS[topic].get(why, 0) + 1
# 2026-11-05 4-39：値の長さの外れ（School of Data「Avoiding mistakes when cleaning your data」）。12字未満の引用は
# メニュー・見出し・表の切れ端の疑い（brief の「BRAND VOICE・Paid Program」が事実に入った失敗と同じ型）
SHORTQ = 12
AMITEM = re.compile(r"金属AM|AM受託|積層造形|3Dプリン|WAAM|DED", re.I)
AMWORD = re.compile(r"積層|3D|ＡＭ|\bAM\b|アディティブ|additive|プリンタ", re.I)
MTORDER = re.compile(r"工作機械(の)?受注|日工会|日本工作機械工業会")
PRICEIDX = re.compile(r"(物価指数|デフレーター|価格指数)")
PRICEWORD = re.compile(r"(指数|物価|価格|デフレーター|前年比|騰落)")
SHIPWORK = re.compile(r"造船.{0,12}(就労|従業|人手|働く)")
SEAFARER = re.compile(r"(船員|外航|内航|定員|乗組)")
SHIPYARD = re.compile(r"(造船|造船所|舶用|建造)")
# 4-83（58回目の走査の失敗）：取り違えた図表・行・地域
FIGFILE = re.compile(r"(?<![a-z0-9])b\d+_\d+_\d+(?![0-9])", re.I)
FIGNO = re.compile(r"第(\d)-(\d)-(\d+)図")
SHEETROW = re.compile(r"^\s*Sheet\d*\s*:\s*([^:]{1,40}):")
INDROW = re.compile(r"(業|サービス|福祉|産業)")
MFGITEM = re.compile(r"(製造業|めっき|鍍金|金型|熱処理|プレス|鋳造|鍛造|受託加工)")
NOLOCAL = re.compile(r"(県や市の表を使わない|市・県の表を使わない|全国の表|全国・e-Stat|（全国）|全国×)")
def rows_for(topic):
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: d = json.load(open(f, encoding="utf-8"))
        except Exception: continue
        yield from rows_from_doc(topic, d)

DIPOINT = re.compile(r"\s*(%|％)?\s*(ポイント|pt\b|ﾎﾟｲﾝﾄ)")
DIWORD = re.compile(r"(DI|判断指数|ディフュージョン|不足超|過剰超)")
# 2026-11-07 4-60（Thunderbit「trafilatura レビュー」：本文と定型文は手書きのセレクタでなく中身の手がかりで分ける。
# 走査の失敗から：research_method の 64p.org の記事の引用8件が全部サイドバーの記事一覧の題名（句読点も数字も無い短い行）だった）。
# 1つのURLの引用が5件以上あり、その6割以上が「40字以下・句読点なし・数字なし」の題名の型なら、そのURLは一覧・目次のページとみなし、
# 同じURLの句読点の無い短い数字入りの行も事実にしない
LISTLINE = re.compile(r"^[^。、,.，．]{1,40}$")
def listpage_urls(d):
    per = {}
    for items in (d.get("evidence") or {}).values():
        for it in items or []:
            q = unicodedata.normalize("NFKC", it.get("quote", "") or "").strip()
            per.setdefault(it.get("url", ""), []).append(bool(LISTLINE.match(q)) and not NUM.search(q))
    return {u for u, f in per.items() if u and len(f) >= 5 and sum(f) / len(f) >= 0.6}

# 2026-11-07 4-61（jack4manastalk note の AI との対話の監査：利用者が「〜なのでしょうか？」と疑問文で尋ねた内容を、AI が
# 「統計的に少数派」と断定に書き換え、存在しない統計が事実として残った）。疑問文の中の数字は問いであって事実ではない。
# また AI の対話の出力をそのまま載せたページ（「ユーザーのご指摘」「結論から申し上げますと」「[一般知識」「推論合成」の型）は
# 出典ではなく生成物なので、その型の引用が同じURLに2件以上あれば、そのURLの数字をすべて事実にしない（4-16 生成と検証を分ける）
QUESTION = re.compile(r"(でしょうか|ですか|ませんか|のか|だろうか)\s*[？?]?\s*[」』)）]?\s*$|[？?]\s*[」』)）]?\s*$")
AIGEN = re.compile(r"(ユーザーの(ご指摘|仮説|ご質問)|結論から申し上げますと|\[一般知識|推論合成|知識再生\]|AIの回答|ChatGPT(の|に)(回答|答え))")
def aigen_urls(d):
    per = {}
    for items in (d.get("evidence") or {}).values():
        for it in items or []:
            q = unicodedata.normalize("NFKC", it.get("quote", "") or "")
            if AIGEN.search(q): per[it.get("url", "")] = per.get(it.get("url", ""), 0) + 1
    return {u for u, c in per.items() if u and c >= 2}

def rows_from_doc(topic, d):
    if True:
        name = d.get("name", "")
        lpu = listpage_urls(d)
        agu = aigen_urls(d)
        for cat, items in (d.get("evidence") or {}).items():
            for it in items or []:
                q, url = unicodedata.normalize("NFKC", it.get("quote", "") or ""), it.get("url", "")  # 全角数字・全角％を半角に（4-12）
                q = join_okuman(q)
                if is_job_search(url): drop(topic, "求人サイトの検索結果ページ（4-31）"); continue
                if url in agu:
                    if NUM.search(q): drop(topic, "AIの対話の出力をそのまま載せたページ（4-61）")
                    continue
                if QUESTION.search(q.strip()):
                    if NUM.search(q): drop(topic, "疑問文の中の数字（4-61）")
                    continue
                if url in lpu and LISTLINE.match(q.strip()):
                    if NUM.search(q): drop(topic, "一覧・目次のページの題名の行（4-60）")
                    continue  # 4-31 求人サイトの検索結果ページの数字は事実にしない
                if HEADLINE.search(q) or "抜粋" in cat:
                    if NUM.search(q): drop(topic, "見出し・題名・検索結果の抜粋（4-5）")
                    continue  # 見出し・題名・検索結果の抜粋は数字にしない（METHOD.md 4-5）
                # 2026-11-06 走査の失敗から（4-5 主語の一致）：am_ceiling の「金属AM受託の大型案件・最大受注（国内）」に、日本工作機械工業会の
                # 工作機械受注総額（月次の業界統計・約1兆円）が「1件の単価」として5件入った。品目が積層造形なのに、引用が工作機械受注の統計で
                # 積層造形・3Dプリンターの語を含まないものは、その品目の事実にしない
                if AMITEM.search(name) and MTORDER.search(q) and not AMWORD.search(q):
                    if NUM.search(q): drop(topic, "主語が別の統計（工作機械受注・4-5）")
                    continue
                # 2026-11-06 走査の失敗から（4-58 主語の一致）：mfg_capacity の「造船業の就労者数」の品目に、国交省の資料の船員の人数
                # （外航・内航・日本人・外国人の船員、練習船の定員）が社員数として入った。造船所で働く人と船に乗る船員は別の対象。
                # 品目が造船の就労者・従業員なのに、引用が船員・外航・内航・定員の語を含み造船・造船所の語を含まないものは、その品目の事実にしない
                if SHIPWORK.search(name) and SEAFARER.search(q) and not SHIPYARD.search(q):
                    if NUM.search(q): drop(topic, "主語が別の対象（船員・定員・4-58）")
                    continue
                # 2026-11-07 走査の失敗から（4-61 主語の一致）：mfg_capacity の「日本銀行 企業物価指数 金属製品の年平均指数」の品目に、
                # 指数の水準（単位の無い数字）は1つも入らず、日銀のページの別の統計の金額（万円・幅 2.15e+06〜2.2e+10）が15件入った。
                # 品目が物価指数なのに、引用が指数・物価の語を含まない金額・人数は、その品目の事実にしない
                if PRICEIDX.search(name) and not PRICEWORD.search(q):
                    if NUM.search(q): drop(topic, "主語が別の統計（物価指数の品目に金額・4-61）")
                    continue
                # 2026-11-07 走査の失敗から（58回目・4-83 取り違えた表）：mfg_capacity の「中小企業白書2020 付属統計資料 規模別 労働生産性
                # 上位10%・中央値・下位10% 製造業の表」に、第1-2-5図（b1_2_05.xlsx）でなく第1-2-4図の b1_2_04.xlsx の行
                # （宿泊業・小売業・医療福祉など製造業以外の業種平均）が16件入り、【一次で確認】になった。
                # ①品目が付属統計のファイル番号（bN_N_NN）を名指しするのに、URLのファイル番号が違えば別の図表なので事実にしない
                fwant = {x.lower() for x in FIGFILE.findall(name)} or {f"b{a}_{b}_{int(c):02d}" for a, b, c in FIGNO.findall(name)[:1]}; fgot = [x.lower() for x in FIGFILE.findall(url)]
                if fwant and fgot and not (set(fgot) & fwant):
                    if NUM.search(q): drop(topic, "品目が名指す図表と別のファイル（4-83）")
                    continue
                # ②品目が製造業（か製造業の細分類）を名指しするのに、表の行の見出し（「Sheet1:<行>:」）が製造業以外の産業なら事実にしない
                sm = SHEETROW.match(q)
                if sm and MFGITEM.search(name) and INDROW.search(sm.group(1)) and not re.search(r"製造|全産業|産業計|合計", sm.group(1)):
                    if NUM.search(q): drop(topic, "表の行の見出しが別の産業（4-83）")
                    continue
                # ③品目が「全国」「県や市の表を使わない」と名指しするのに、自治体（pref.・city.）の表から来た数字は事実にしない
                # （同じ回、工業統計の消費税の扱いの品目に横須賀市・長野県の表が入った）
                if NOLOCAL.search(name) and region_of(url):
                    if NUM.search(q): drop(topic, "全国の品目に自治体の表（4-83）")
                    continue
                ys = YEAR.findall(q)
                ctx = context_of(q)
                for m in NUM.finditer(q):
                    if is_size_band(q, m): drop(topic, "規模区分の境目（4-5）"); continue  # 規模区分の境目は数えない
                    if is_money_band(q, m): drop(topic, "金額の階級の境目（4-40）"); continue
                    # 2026-11-06 4-40 走査の失敗から：神奈川県の工業統計の結果表の目次「表 33 事業所数、…」の33が事業所数として数えられた。表・図の番号は値にしない
                    if re.search(r"((?<!代)表|図|第)\s*$", q[max(0, m.start() - 3):m.start()]): drop(topic, "表・図の番号（4-40）"); continue
                    # 2026-11-05：「1人あたり」「1事業所当たり」の1を人数・事業所数として拾い、幅が「1〜3,952事業所」になった失敗から。割り算の分母の1は数えない
                    if PERDENOM.match(q, m.start()): drop(topic, "1あたりの分母の1（4-5）"); continue
                    # 2026-11-06 走査の失敗から（4-48）：mfg_capacity の「人手不足で受注を断った企業の割合」に、日銀短観の雇用人員判断DI
                    # （「不足」と答えた割合−「過剰」の割合、%ポイント）が企業の割合・社員数として13件入った。差の指数（DI・%ポイント・ポイント）は割合にしない
                    if DIPOINT.match(q, m.end()) or (DIWORD.search(q[max(0, m.start() - 30):m.start()]) and "ポイント" in q[m.end():m.end() + 6]):
                        drop(topic, "DI・%ポイントの差の指数（4-48）"); continue
                    # 2026-11-07 56回目の走査の失敗から：中小企業白書の「上位10％、中央値、下位10％」の10が1人あたりの欄に % の値として3件入り
                    # 【一次で確認】と出た。分位の名前（上位・下位・トップ・ボトム＋%）は値にしない
                    if re.search(r"(上位|下位|トップ|ボトム|top|bottom)\s*$", q[max(0, m.start() - 5):m.start()], re.I) and m.group(2) in ("%", "％"):
                        drop(topic, "分位の名前（上位・下位10%・4-81）"); continue
                    v, u = norm(*m.groups())
                    # 2026-11-06 4-56 走査の失敗から：mfg_capacity の「日本造船工業会・国交省の年ごとの新造船受注量」の %（10年推移）の欄に、
                    # 世界の建造量の国別シェア（中国 約50%・日本 8%〜16%）と前年比の増減（12%減・15%増・バルカー45%増）が同じ欄で並び、【裏付けあり】と出た。
                    # % は数字の直後の語で「増減率」「シェア・構成比」に分け、同じ欄で突き合わせない
                    ctx_n = ctx
                    if u == "%":
                        aft, bef = q[m.end():m.end() + 6], q[max(0, m.start() - 25):m.start()]
                        if re.match(r"\s*\)?\s*(増|減|の増|の減|上昇|下落|低下|拡大|縮小|up|down|increase|decrease|growth)", aft, re.I) or re.search(r"(前年比|前年同期比|年比|比べ|伸び率|増加率|減少率|増減率)", bef):
                            ctx_n = "増減率"
                        elif re.search(r"(シェア|構成比|割合|占め|のうち|share)", bef + aft, re.I):
                            ctx_n = "シェア・構成比"
                    pre = q[max(0, m.start() - 2):m.start()]
                    neg = bool(NEGMARK.search(pre)) or (u == "万円" and pre.endswith("(") and q[m.end():m.end() + 1] == ")" and bool(FINTABLE.search(q)))  # 4-35：括弧は決算の表の文脈だけ
                    if u == "万円" and v <= 0: drop(topic, "0以下の金額"); continue
                    if neg and u == "万円": v = -v  # 4-34 △・▲・括弧は負の値（損失・減少）
                    if u == "人" and v > MAXPERSON: OUTLIERS[topic] = OUTLIERS.get(topic, 0) + 1; continue  # 4-31 桁の感覚の上限
                    # 数字の近く（前40字）の年を優先
                    near = YEAR.findall(q[max(0, m.start() - 40):m.start() + 5])
                    y = (near or ys or [""])[-1]
                    # 2026-11-07 56回目の走査の失敗から：mfg_capacity の「離職率は36.7％（2005年）から11.4％（2025年）に低下」で、
                    # 前40字の年を採ったため 11.4 に2005年・36.7 に2025年が付き、下がった系列が上がった向きに読めた。値の直後の括弧の年はその値の年
                    _post = re.match(r"\s*[（(]\s*(20[0-3]\d|19[89]\d)\s*年", q[m.end():m.end() + 12])
                    if _post: y = _post.group(1)
                    yfix = ""
                    if y in CENSUS_YEARS and CENSUS_ACT.search(q) and "実績" not in q[max(0, m.start() - 40):m.end() + 20]:
                        y, yfix = str(int(y) - 1), "活動調査の調査年→実績年（-1年）"
                    if GOINGCONCERN.search(q): GC.setdefault(topic, set()).add((name, url))
                    kind = "調査会社" if REPORT.search(url) else "信用調査" if CREDIT.search(url) else "一次" if is_primary(url) else "記事"
                    yield {"品目": name, "区分": cat, "文脈": ctx_n, "年": y, "値": round(v, 4), "単位": u,
                           "一次": kind, "立場": standing(url, kind),
                           "元へ戻る": "要" if kind != "一次" and CITES_PRIMARY.search(q) else "",
                           "定義": defn_of(q), "版": version_of(q), "規模": size_class_of(q, m.start()),
                           # 2026-11-05 4-19：出どころの記録（URL・取得日・取り方）。欠けた引用は後から確かめられない
                           # 2026-11-05：mfg_capacity の引用は first_seen が無く「記録が欠けた数字 1488/1488」になった。ファイルの researched_at を取得日に使う
                           "取得日": it.get("first_seen", "") or (d.get("researched_at", "") or "")[:10],
                           "予測": "予測" if y and int(y) > NOW_YEAR else "見通し" if OUTLOOK.search(q) else "試算" if TRIAL.search(q) else "仮置き" if ASSUME.search(q) else "",
                           "年の補正": yfix, "符号": "負" if neg and u == "万円" else "",
                           "出典": urlparse(url).netloc, "URL": url,
                           "原文の数字": m.group(0).strip()[:40],
                           "引用": q[:QHEAD] if m.end() <= QHEAD else (QCUT.__setitem__(topic, QCUT.get(topic, 0) + 1) or q[:100] + " … " + q[max(100, m.start() - 50):m.end() + 10])}


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
    ("金額の階級の境目（4-40）", "500万円超~1千万円", "https://www.e-stat.go.jp/dbview?sid=1", []),
    ("金額の階級の境目・以下（4-40）", "500万円以下", "https://www.e-stat.go.jp/dbview?sid=1", []),
    ("表の番号（4-40）", "表 33 事業所数、従業者数", "https://www.pref.kanagawa.jp/a", []),
    ("階級でない金額は残す（4-40）", "売上高は5,000万円だった", "https://example.go.jp/a", [(5000, "万円")]),
    ("疑問文の数字は事実にしない（4-61）", "製造業の3割が受注を断ったのでしょうか？", "https://example.com/a", []),
    ("断定文の数字は残す（4-61）", "製造業の受注は3,000万円だった", "https://example.go.jp/a", [(3000, "万円")]),
    ("DIの%ポイントは割合にしない（4-48）", "雇用人員判断DIはマイナス35%ポイントと不足超", "https://www5.cao.go.jp/a", []),
]
GOLD_DOCS = [  # 2026-11-07 4-60：1つの文書の中で決まる判定（一覧・目次のページ）
    ("AIの対話の出力のページ（4-61）", ["結論から申し上げますと、約3割の企業です。", "ユーザーのご指摘は鋭く、売上は5,000万円規模です。", "従業者は12人である。"], "https://example.com/ai", []),
    ("一覧ページの題名の数字（4-60）", ["記事A 正規表現の実装", "記事B LSM フレームワーク", "記事C 仮想化の手引き", "記事D 3つの設定方法", "記事E 入力メソッド"], "https://example.org/notes/x", []),
    ("本文の数字は残す（4-60）", ["加工賃収入は3,000万円だった。", "従業者は12人である。", "設備は2台で、稼働は昼だけ。", "取引先は5社に増えた。", "受注の半分は県外から。"], "https://example.go.jp/a", [(3000, "万円"), (12, "人")]),
]
# 2026-11-06 4-47：正解例との照合を「通った件数」だけでなく、拾った値ごとに TP（拾うべきを拾った）・FP（拾ってはいけない混入）・
# FN（拾うべきを落とした）に分け、精度＝TP/(TP+FP)・再現率＝TP/(TP+FN)・F1 を出す（AWS Entity Resolution の精度の測り方）。
# A1 では混入（FP）のほうが落とし（FN）より害が大きい（4-8）ので、精度が1を割ったら先に直す
GOLD_PRF = {}
def gold_check():
    bad = []; tp = fp = fn = 0
    for label, q, url, want in GOLD:
        d = {"name": "__gold__", "evidence": {"テスト": [{"quote": q, "url": url}]}}
        got = sorted((round(r["値"], 4), r["単位"]) for r in rows_from_doc("__gold__", d))
        exp = sorted((round(v, 4), u) for v, u in want)
        rest = list(exp)
        for g in got:
            if g in rest: tp += 1; rest.remove(g)
            else: fp += 1
        fn += len(rest)
        if got != exp: bad.append(f"- ✗ {label}：「{q}」→ 期待 {want}／実際 {got}")
    for label, qs, url, want in GOLD_DOCS:
        d = {"name": "__gold__", "evidence": {"テスト": [{"quote": q, "url": url} for q in qs]}}
        got = sorted((round(r["値"], 4), r["単位"]) for r in rows_from_doc("__gold__", d) if r["単位"] in ("万円", "人"))
        exp = sorted((round(v, 4), u) for v, u in want)
        rest = list(exp)
        for g0 in got:
            if g0 in rest: tp += 1; rest.remove(g0)
            else: fp += 1
        fn += len(rest)
        if got != exp: bad.append(f"- ✗ {label}：{qs[0]} ほか → 期待 {want}／実際 {got}")
    OUTLIERS.pop("__gold__", None); DROPS.pop("__gold__", None)
    pr = tp / (tp + fp) if tp + fp else 1.0; rc = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * pr * rc / (pr + rc) if pr + rc else 0.0
    GOLD_PRF.update({"TP": tp, "FP": fp, "FN": fn, "精度": round(pr, 3), "再現率": round(rc, 3), "F1": round(f1, 3)})
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

# 2026-11-06 4-40：端の1点に依存する向き（reading_monkey note「景気変動」：トレンドの推計は標本の末端で大きく動き、
# その時点の推計と後の改訂後の推計の差は主に末端から生じる）。最初か最後の年を1つ外して向きの語が変われば、向きは端の1点に依存している
def end_sensitivity(series):
    ys = sorted(series)
    if len(ys) < 4: return None
    w = lambda t: t[:2]
    base = w(trend(series))
    alt = {"最初の年を外す": w(trend({y: series[y] for y in ys[1:]})), "最後の年を外す": w(trend({y: series[y] for y in ys[:-1]}))}
    flip = [k for k, v in alt.items() if v != base]
    return "・".join(f"{k}と{alt[k]}" for k in flip) if flip else None

# 2026-11-06 4-40：向きと大きさを分けて突き合わせる（M&E Studio「Triangulation」：自己申告と行政記録が同じ向きで大きさが3分の1、
# という食い違いは幅と理由を書く）。出どころごとに2年以上あれば最初→最後の変化率を出し、向きは一致・大きさが2倍超ずれるなら印す
def dir_magnitude(g):
    by = {}
    for r in g:
        if r["年"] and not r.get("予測") and r["値"] > 0: by.setdefault(src_key(r), {}).setdefault(int(r["年"]), []).append(r["値"])
    ch = {}
    for k, ser in by.items():
        if len(ser) < 2: continue
        ys = sorted(ser); a, b = statistics.median(ser[ys[0]]), statistics.median(ser[ys[-1]])
        if a > 0: ch[k] = (b - a) / a
    if len(ch) < 2: return None
    signs = {(v > 0.05) - (v < -0.05) for v in ch.values()}
    if len(signs) > 1: return "向きが出どころで割れる：" + "、".join(f"{k} {v*100:+.0f}%" for k, v in ch.items())
    mags = sorted(abs(v) for v in ch.values())
    if mags[0] > 0 and mags[-1] / mags[0] > 2: return "向きは一致・大きさが2倍超ずれる：" + "、".join(f"{k} {v*100:+.0f}%" for k, v in ch.items())
    return None

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

# 2026-11-07 4-64：平均への回帰（BBC Editorial Guidelines「Reporting statistics and data」：山の時点から始めた減少は、
# 手を打たなくても平均に戻るだけのことがある）。系列の最初の年が全期間の最大か最小なら、起点が山・谷として印す
def extreme_start(series):
    ys = sorted(series)
    if len(ys) < 4: return None
    v0 = series[ys[0]]; rest = [series[y] for y in ys[1:]]
    if v0 > max(rest): return "山"
    if v0 < min(rest): return "谷"
    return None

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
# 2026-11-06 4-57：同じ文書の別の版・写し（deskrex「AI検索と自前の調査ライブラリの比べ方」：同じ研究の別の版を独立の研究として数えない）。
# 一次でも、別々のホストの引用が頭60字まで一字一句同じなら、同じ文書の写し・転載とみなし1つの出どころに束ねる
def _txt(r):
    t = re.sub(r"\s", "", r.get("引用", ""))
    return t[:60] if len(t) >= 30 else ""

def indep_key(r, ec=None):
    if ec and _txt(r) and ("文", _txt(r)) in ec: return ("同じ文", _txt(r))
    # 2026-11-05 修正：前の版は一次でない数字を値ごとに別の出どころと数え、同じホスト1か所の近い値2つで【裏付けあり】になっていた
    # （mfg_capacity のめっき・金型で「一次0件・出典1か所」なのに【裏付けあり】）。独立の単位はホスト。写し（同じ値が一次でない2か所以上）だけ1つに束ねる
    if r["一次"] != "一次" and ec and r["値"] in ec: return ("同じ値", r["値"])
    return src_key(r)

def echoes(g):
    """一次でない2か所以上が同じ値を持つ値の一覧（値→ホスト数）。"""
    hv = {}
    for r in g:
        if r["一次"] != "一次": hv.setdefault(r["値"], set()).add(src_key(r))
    out = {v: len(h) for v, h in hv.items() if len(h) >= 2}
    th = {}
    for r in g:
        if _txt(r): th.setdefault(("文", _txt(r)), set()).add(src_key(r))
    out.update({k: len(h) for k, h in th.items() if len(h) >= 2})  # 4-57 同じ文の写し（一次も含む）
    return out

# 2026-11-05 4-23：確度に見合った書き方（skillmd：裏付けあり→出典つきで言い切る／1か所→文中で出どころを名指し／食い違い→幅を両方の出典で／裏の取れないもの→使わない）
# 2026-11-05 4-39：閉じた母集団か開いた母集団か（Kasica ら CHI'23）。一次（統計の全数）でない数え上げの社数・件数は
# 漏れがありうるので「少なくとも」を付けて書く
OPENCOUNT = ("社", "事業所", "件", "台")
def phrasing(gr, g):
    base = _phrasing(gr, g)
    if g and g[0]["単位"] in OPENCOUNT and not any(r["一次"] == "一次" for r in g): base += "。数え上げは漏れがありうるので「少なくとも」を付ける"
    return base

# 2026-11-05 4-39：可能性の言葉を1つの組に固定する（ICD 203 の likelihood の表。言葉と確率の幅を対にし、組を混ぜない。
# 確信の度合い（確度）と可能性の言葉を同じ文に並べない）。scan.md の結論の文はこの7語だけを使う
LIKELIHOOD = [("ほぼありえない", "1〜5%"), ("かなり低い", "5〜20%"), ("低い", "20〜45%"), ("五分五分", "45〜55%"),
              ("高い", "55〜80%"), ("かなり高い", "80〜95%"), ("ほぼ確実", "95〜99%")]

def _phrasing(gr, g):
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

# 2026-11-06 4-41：取り方の点検（英国下院図書館「How to check facts」：統計の発表の要約・報道発表より、データの表・法令などの原典を引く）。
# 一次の数字でも、統計の概要・報道発表のページから取ったものは表で取り直す。URL で表（xlsx/csv/e-Stat の表）か要約かを分ける
SUMMARYPAGE = re.compile(r"(gaiyo|gaiyou|kekka_?gaiyo|press|houdou|/news|summary|topics|happyou|kouhyou|pdf/.*(gaiyo|youyaku))", re.I)
TABLEPAGE = re.compile(r"(\.xlsx?($|\?)|\.csv($|\?)|e-stat\.go\.jp/(dbview|stat-search/(files|database))|statdisp|/data/|toukeihyo|hyo)", re.I)
NATSTAT_ITEM = re.compile(r"(工業統計|経済構造実態調査|経済センサス|e-Stat|法人企業統計|中小企業実態基本調査|鉱工業指数|賃金構造基本統計|生産動態統計)")
NATSTAT_HOST = re.compile(r"(\.go\.jp$|e-stat\.go\.jp|\.lg\.jp$|^www\.pref\.|^www\.city\.)", re.I)
def take_kind(url):
    return "表" if TABLEPAGE.search(url or "") else "要約" if SUMMARYPAGE.search(url or "") else ""

EMPMIX = re.compile(r"(パート|臨時|派遣|非正規|短時間労働|アルバイト|常用雇用者|常用労働者)")
MERGER = re.compile(r"(合併|経営統合|事業譲渡|買収|M&A|子会社化|吸収)", re.I)
# 2026-11-06 4-43（毎月勤労統計調査の集計方法の解説：標本の抽出替え・母集団労働者数のベンチマーク更新・復元の有無で、同じ統計の値に段差が出る。
# 「推計母集団労働者数」は推計比率を作るための重みで、ふつうの意味の人数ではない）
SAMPLEROT = re.compile(r"(抽出替え|標本の?入れ替え|サンプル替え|ベンチマーク(の)?更新|ギャップ修正|復元(倍率)?|再集計|遡及(改訂|集計))")
POPWEIGHT = re.compile(r"(推計母集団労働者数|推計比率|母集団労働者数)")
# 2026-11-06 4-46（Maastricht 大学の専門家インタビューの方法論：1人の証言の証拠の重みはほぼ0で、他の証言かデータと突き合わせて初めて効く。
# 話し手の選び方・個人の偏り・社会的に望ましい答えへの偏りがある）。引用が「関係者・取材・インタビュー・〇〇氏によると」の型なら証言として印を付ける
TESTIMONY = re.compile(r"(関係者(によると|の話)|業界関係者|取材(によると|に対し|で)|インタビュー|聞き取り|ヒアリング|氏(は|によると)[^。]{0,20}(話す|語る|述べ))")
# 2026-11-06 4-44（Stray「The Curious Journalist's Guide to Data」CJR Tow Center：大きな数は推定で誤差の幅がある。誤差の幅より小さい変化は記事にしない。
# NIRA「統計改革」報告：自己評価式の調査は偏りやすい・回収率の低さが偏りを生む・入札で委託先が変わると系列の継続性が落ちる。
# delight-solutions「競合の売上の調べ方」：セグメント売上は外部顧客向けと内部（セグメント間）を分ける）。引用にこの語があれば品目ごとに印す
SAMPLEERR = re.compile(r"(標準誤差|標本誤差|誤差率|信頼区間|margin of error|standard error)", re.I)
LOWRESP = re.compile(r"(回収率|回答率)\s*(は|が|：|:)?\s*(約)?\s*(\d{1,2}(?:\.\d+)?)\s*[%％]")
SELFREPORT = re.compile(r"(自己評価|自己申告|自己採点|自社調べ|自社調査)")
CONTRACTOR = re.compile(r"(委託先(の|が)?(変更|変わ)|調査(の)?委託先|入札(により|で)[^。]{0,10}委託)")
INTERSEG = re.compile(r"(セグメント間|内部売上|内部取引|社内売上|グループ内(の)?(売上|取引))")
# 2026-11-06 4-51（Flares「How to Find Competitor Revenue」：数字ごとに Filed／Stated／Estimated を付ける。監査済みの値と推定を同じ書き方で並べない）。
# 届出＝統計・開示・官報・自治体の一次／言明＝会社・団体などが自分で言った値／報道＝記事が書いた値（言明か推定の写し）／推定＝予測・試算・調査会社・信用調査
# 2026-11-07 4-79（データビズラボ「国勢調査データのクレンジング手順」：粗い単位から細かい単位への按分は推計値を作る行為）。按分・案分・按分推計の語がある値も【推定】に回す
ESTWORD = re.compile(r"(推計|推定|試算|概算|と見られる|とみられる|と推測|按分|案分|あん分)")
def fse_of(r):
    if r.get("予測") or r["一次"] in ("調査会社", "信用調査") or ESTWORD.search(r.get("引用", "")): return "推定"
    if r["一次"] == "一次": return "届出"
    if NEWSHOST.search(urlparse(r.get("URL", "")).netloc): return "報道"
    return "言明"
# 2026-11-06 4-51（Flares：外貨の決算は期中平均のレートで換算し、今日のレートで換算しない）。引用に外貨と円の両方の金額があれば換算の時点を確かめる印
FXMIX = re.compile(r"(\d[\d,.]*\s*(億|百万|万)?\s*(ドル|米ドル|ユーロ|ポンド|元|USD|EUR|\$))", re.I)
# 2026-11-06 4-51（edukatesingapore「統計の改訂・データの版・ベンチマーク改定」：指数の基準年を変えると全部の値の水準が変わる。
# 遡って作り直した系列は当時の公表値と同じものではない）。基準年の違う指数・遡及の作り直しを当時の値と同じ欄で混ぜない
REBASE = re.compile(r"((19|20)\d\d\s*年\s*[=＝]\s*100|基準年(の)?(改定|変更|改訂)|基準改定|指数の基準|遡って(作り直|再計算|改定)|遡及(推計|系列))")
def chance_band(series):
    # 4-44：件数（事業所・社・件）の最初と最後の年の差が、偶然のばらつき（ポアソンの目安：差が2×√(a+b) 以内）に収まるか。
    # 小さい件数の増減（Stray の例：信号を付けた交差点の事故9件→少ない件数）は偶然でも起きる。収まれば向きを言い切らない
    ys = sorted(series)
    if len(ys) < 2: return None
    a, b = series[ys[0]], series[ys[-1]]
    if a < 0 or b < 0 or a + b == 0 or max(a, b) >= 1000: return None
    lim = 2 * (a + b) ** 0.5
    return (abs(b - a) <= lim, a, b, lim, ys[0], ys[-1])
RATES = {}  # 4-44：比較の相手（差の差）。テーマの中の品目×欄ごとの年率（複利）を集める
def cagr_of(series):
    ys = sorted(series)
    if len(ys) < 3: return None
    a, b = series[ys[0]], series[ys[-1]]; span = ys[-1] - ys[0]
    if span <= 0 or a <= 0 or b <= 0: return None
    return (b / a) ** (1 / span) - 1

def conservative(vals):
    vals = sorted(vals)
    return statistics.quantiles(vals, n=4)[0] if len(vals) >= 4 else vals[0]

# 2026-11-06 走査で見つかった失敗から：検索で拾った工業統計の行は「年不明」「製造業計（750万人・18.5万事業所）」が細分類の品目に混ざった。
# build_kougyo.py が所管の Excel から直接読んだ kougyo_series.csv（産業細分類×年、同じ表・同じ年の組だけで割った値）があれば、
# 番号ごとに最初の年・最後の年・複利の年率（4-37）・端の1点を外したときの向き（4-40）・統計の切れ目（2016年・2021年〜・4-26）を機械で出す
KOUGYO_FOCUS = re.compile(r"めっき|金型|機械加工|熱処理|金属プレス|プレス|鍛造|鋳|溶接|塗装|研磨|切削|表面処理|製缶|板金|ねじ|ばね")
# 2026-11-07 走査で見つかった失敗から（45回目・4-70）：KOUGYO_FOCUS の「鋳・研磨・溶接・めっき」が、受託（賃加工）でない製品の製造業
# （2293 鋳鉄管・2252 可鍛鋳鉄・2171 研磨材・2921 電気溶接機・2411 ブリキ缶・めっき板・2249 表面処理鋼材）も拾い、平均並みの幅の母数と
# 「幅より上」の一覧を占めていた。比べる母数（幅・製造業計との上下の数・分子分母の型）からは製品の製造業を外す。本文の一覧には残す
KOUGYO_PRODUCT = re.compile(r"鋳鉄管|可鍛鋳鉄|銑鉄鋳物|鋼鋳鉄|研磨材|研磨布紙|溶接機|ブリキ|めっき板|表面処理鋼材|鋼材|鋳型製造")
# 2026-11-07 走査で見つかった失敗から（46回目）：「2464 電気めっき業（表面処理鋼材製造業を除く）」が、除く側の括弧の「表面処理鋼材」で
# 製品の製造業とみなされ、平均並みの幅・分子分母の型の母数から電気めっき（2464）と溶融めっき（2462）が落ちていた。
# 製品の製造業の判定は「（…を除く）」の括弧を外した名前で行う
_KEXCL = re.compile(r"[（(][^（）()]*を除く[）)]")
def _kfocus(name):
    n = name or ""
    return bool(KOUGYO_FOCUS.search(n)) and not KOUGYO_PRODUCT.search(_KEXCL.sub("", n))
def _kf(x):
    try: return float(str(x).replace(",", "").replace("△", "-").replace("▲", "-"))
    except Exception: return None
def _cagr(a, b, n):
    return (b / a) ** (1 / n) - 1 if a and b and a > 0 and b > 0 and n > 0 else None
# 2026-11-06 4-50（4-25・4-31 の規模の内側の向きを、所管の表の規模別の行で機械に出す）：kougyo_size.csv（build_kougyo.py が
# 産業別統計表の従業者規模別のシートから取った行）で、細分類×規模区分ごとに最初と最後の年の1人あたり（付加価値・出荷額）の向きを並べる。
# 秘匿（X）・注記（***）の付いた行は値にしない（4-26：秘匿を0として扱わない）。全区分で同じ向きのときだけ「構成変化だけでは説明できない」とする。
# あわせて規模区分ごとの従業者の割合（構成）の最初と最後を出し、構成の移り変わりを見えるようにする（4-35 の配分の効き）
# 2026-11-07 走査で見つかった失敗から（46回目・4-70・4-71）：品目「工業統計表 2019年実績 産業細分類別 加工賃収入額と製造品出荷額等の内訳」を
# 検索で取ったら、16件すべてが愛知県の表（www.pref.aichi.jp・年不明）で、全国の細分類の値にならなかった（4-30：市・県の表を使わない）。
# 全国の内訳は、build_kougyo.py が所管の Excel から既に読んだ kougyo_rows.csv のシート 1200（英語の見出し
# 「Receipts for production done for others」＝加工賃収入額）にある。検索でなくこの表から、細分類ごとの
# 賃加工の重さ＝加工賃収入額 ÷（製造品出荷額＋加工賃収入額＋くず・廃物＋その他収入）を同じ行・同じ年の組で出す
_KC = {"製造品": "Total Value of manufactured goods shipment", "加工賃": "Receipts for production done for oth",
       "くず": "Shipments for scraps", "その他": "Other Revenue"}
def kougyo_contract_section(topic):
    f = os.path.join(OUT, topic, "kougyo_rows.csv")
    if not os.path.exists(f): return []
    acc = {}
    for r in csv.DictReader(open(f, encoding="utf-8-sig")):
        if str(r.get("sheet")) != "1200" or len(r.get("code") or "") != 4: continue
        col = r.get("column") or ""
        if "Value of manufactured goods shipments" not in col: continue
        k = next((k for k, v in _KC.items() if v in col), None)
        if not k: continue
        key = (r["code"], r["year"])
        d = acc.setdefault(key, {"name": r.get("name", ""), "fy": r.get("file_year") or r["year"], "note": "*" in (r.get("row") or "")})
        if (r.get("file_year") or r["year"]) < d["fy"]: continue
        if k not in d: d[k] = _kf(r.get("value"))
    if not acc: return []
    ly = max(y for _, y in acc)
    out = []
    for (code, y), d in acc.items():
        if y != ly or any(d.get(k) is None for k in _KC): continue
        tot = sum(d[k] for k in _KC)
        if tot <= 0: continue
        out.append((d["加工賃"] / tot, code, d["name"], d["加工賃"], tot, d["note"]))
    if not out: return []
    out.sort(reverse=True)
    allc = [o for o in out if o[1] != "0000" and o[1][-1:] != "0"]
    md = ["", f"## 工業統計表 {ly}年実績 産業細分類別 賃加工の重さ（全国・kougyo_rows.csv のシート1200・同じ行・同じ年の組・名目・機械・4-70）",
          "- 賃加工の重さ＝加工賃収入額 ÷（製造品出荷額＋加工賃収入額＋くず・廃物＋その他収入）。単位は表のまま（百万円）。県の表（愛知県など）は使わない（4-30）",
          f"- 細分類の数：{len(allc)}業種。賃加工の重さ50%以上：{sum(1 for o in allc if o[0] >= 0.5)}業種、20%未満：{sum(1 for o in allc if o[0] < 0.2)}業種"]
    tot0 = next((o for o in out if o[1] == "0000"), None)
    if tot0: md.append(f"- 0000 製造業計：賃加工の重さ {tot0[0]:.1%}（加工賃収入額 {tot0[3]:,.0f}・合計 {tot0[4]:,.0f}）")
    for o in allc[:25]:
        md.append(f"- {o[1]} {o[2]}：賃加工の重さ {o[0]:.1%}（加工賃収入額 {o[3]:,.0f}・合計 {o[4]:,.0f}）" + ("・注記つきの行（本文で確かめる）" if o[5] else ""))
    foc = [o for o in allc if _kfocus(o[2]) and o not in allc[:25]]
    if foc:
        md.append("- 注目の細分類で上の25業種に入らないもの：" + "、".join(f"{o[1]} {o[2]} {o[0]:.1%}" for o in foc[:20]))
    md.append("- 受託の業種どうしの1人あたりの比較は、賃加工の重さが高い業種（加工賃が売上の大半）だけを母数にするのが筋。重さの線（何%以上を受託とみなすか）は scan.md で決めて書く")
    return md

def kougyo_size_section(topic):
    f = os.path.join(OUT, topic, "kougyo_size.csv")
    if not os.path.exists(f): return []
    by = {}
    for r in csv.DictReader(open(f, encoding="utf-8-sig")):
        y = _kf(r.get("year")); band = unicodedata.normalize("NFKC", r.get("規模", "") or "")
        if y is None or not KOUGYO_FOCUS.search(r.get("name", "") or ""): continue
        flag = "X" in band.split() or "***" in band
        band = re.sub(r"\s*(X|\*+)(\s+X)*\s*$", "", band).strip()
        by.setdefault((r["code"], r["name"]), {}).setdefault(band, {})[int(y)] = (r, flag)
    if not by: return []
    md = ["## 工業統計表 産業細分類×従業者規模の規模の内側の向き（kougyo_size.csv・同じ表・同じ年の組・名目・4-25・4-31・4-50）",
          "- 秘匿（X）・注記（***）の付いた年の行は値にしない。年は実績の年。区分ごとに最初と最後の年だけを比べる（年率は複利）"]
    for (code, name), bands in sorted(by.items()):
        out = []; spans = set(); dirs = {"従業者1人あたり付加価値額_万円": set(), "従業者1人あたり製造品出荷額等_万円": set()}
        tot = bands.get("計", {})
        for band, ys in sorted(bands.items()):
            if band == "計": continue
            ok = {y: r for y, (r, fl) in ys.items() if not fl}
            if len(ok) < 2: out.append(f"{band}：秘匿・注記で比べられない"); continue
            y0, y1 = min(ok), max(ok); parts = []; spans.add((y0, y1))
            for k in dirs:
                a, b = _kf(ok[y0].get(k)), _kf(ok[y1].get(k))
                if a and b and a > 0:
                    g = _cagr(a, b, y1 - y0); dirs[k].add("上" if b > a else "下" if b < a else "横")
                    parts.append(f"{k.replace('従業者1人あたり', '1人あたり').replace('_万円', '')} {a:,.1f}→{b:,.1f}万円（年率 {g * 100:+.1f}%）")
            sh = []
            for yy in (y0, y1):
                e, t = _kf(ok[yy].get("従業者数")), _kf((tot.get(yy) or ({}, False))[0].get("従業者数") if tot.get(yy) else None)
                if e and t: sh.append(f"{e * 100 / t:.1f}%")
            out.append(f"{band} {y0}→{y1}年：" + "／".join(parts) + (f"／従業者の割合 {'→'.join(sh)}" if len(sh) == 2 else ""))
        verdict = []
        for k, d in dirs.items():
            lab = k.replace("従業者1人あたり", "1人あたり").replace("_万円", "")
            if not d: continue
            verdict.append(f"{lab}：全区分で{'上向き' if d == {'上'} else '下向き' if d == {'下'} else ''}（構成変化だけでは説明できない）" if len(d) == 1 and d != {"横"} else f"{lab}：区分で向きが割れる（平均の向きは構成変化を含みうる・判定できない）")
        if len(spans) >= 2: verdict.append("⚠ 区分ごとに比べた年が違う（" + "・".join(f"{a}→{b}" for a, b in sorted(spans)) + "）：同じ年どうしになるまで言い切らない")
        # 2026-11-06 走査の失敗から（mfg_capacity 26回目）：小さい区分は2017→2018年、30人以上は2017→2019年で比べたまま「全区分で上向き」と出た。
        # 全区分に共通する年だけで比べ直した向きを別に出す。共通の年の間が1年だけなら「1回の変化」で傾向ではない（4-37）
        okyears = []
        for band, ys in bands.items():
            if band == "計": continue
            oy = {y for y, (r, fl) in ys.items() if not fl}
            if len(oy) >= 2: okyears.append((band, oy))
        if len(okyears) >= 2:
            com = set.intersection(*(oy for _b, oy in okyears))
            if len(com) >= 2:
                c0, c1 = min(com), max(com)
                for k in ("従業者1人あたり付加価値額_万円", "従業者1人あたり製造品出荷額等_万円"):
                    dd = set(); n = 0
                    for band, _oy in okyears:
                        a0, a1 = _kf(bands[band][c0][0].get(k)), _kf(bands[band][c1][0].get(k))
                        if a0 and a1 and a0 > 0: dd.add("上" if a1 > a0 else "下" if a1 < a0 else "横"); n += 1
                    lab = k.replace("従業者1人あたり", "1人あたり").replace("_万円", "")
                    one = "・1回の変化で傾向ではない（4-37）" if c1 - c0 == 1 else ""
                    if dd: verdict.append(f"同じ年どうし（{c0}→{c1}年・{n}区分）の{lab}：" + ("全区分で" + {"上": "上向き", "下": "下向き", "横": "横ばい"}[next(iter(dd))] if len(dd) == 1 else "区分で向きが割れる") + one)
            else: verdict.append("全区分に共通する年が2つ未満：同じ年どうしの向きは出せない")
        md.append(f"- {code} {name}：" + "／".join(verdict or ["比べられる区分なし"]))
        md += [f"  - {o}" for o in out]
    # 2026-11-07 48回目の走査から（4-32 の代わりの仮説「規模の構成で業種の平均が下がっているだけ」を見分ける）：
    # 業種をまたいで同じ規模区分・同じ年どうしで1人あたり付加価値額を並べる。業種の平均が低くても、同じ規模の中で中ほどなら構成の効き
    md.append("### 同じ規模区分・同じ年どうしの業種の並び（1人あたり付加価値額・万円・名目・秘匿と注記の行を除く）")
    bandvals = {}
    for (code, name), bands in by.items():
        for band, ys in bands.items():
            if band == "計": continue
            for y, (r, fl) in ys.items():
                v = _kf(r.get("従業者1人あたり付加価値額_万円"))
                if not fl and v and v > 0: bandvals.setdefault((band, y), []).append((v, code, name))
    latest = {}
    for (band, y), lst in bandvals.items():
        if len(lst) >= 4 and y >= latest.get(band, (0, []))[0]: latest[band] = (y, lst)
    for band, (y, lst) in sorted(latest.items()):
        lst = sorted(lst, reverse=True); n = len(lst)
        rk = "、".join(f"{c} {nm[:10]} {v:,.1f}（{i + 1}/{n}位）" for i, (v, c, nm) in enumerate(lst) if c in ("2461", "2462", "2464", "2465", "2469", "2691", "2692", "2446", "2452"))
        md.append(f"- {band}・{y}年（{n}業種・中央の値 {lst[n // 2][0]:,.1f}）：{rk or '注目の業種なし'}")
    # 2026-11-07 51回目の走査から（49回目の代わりの仮説3「並びの母数に製品の製造業が混ざり、受託の業種が下に見えるだけ」を見分ける）：
    # 賃加工の重さ（kougyo_rows.csv シート1200・最新年）が線以上の細分類だけを母数にした並びを、同じ規模区分・同じ年で出す。線は50%と20%の2本
    cw = _contract_weights(topic)
    if cw:
        for line in (0.5, 0.2):
            md.append(f"### 受託の業種だけを母数にした並び（賃加工の重さ {line:.0%}以上・{max(cw.values(), key=lambda t: t[1])[1]}年の重さで判定・1人あたり付加価値額・万円・名目）")
            for band, (y, lst) in sorted(latest.items()):
                sub = sorted([t for t in lst if cw.get(t[1], (0, 0))[0] >= line], reverse=True); n = len(sub)
                if n == 0: md.append(f"- {band}・{y}年：線以上の業種なし"); continue
                rk = "、".join(f"{c} {nm[:10]} {v:,.1f}（{i + 1}/{n}位・重さ {cw[c][0]:.0%}）" for i, (v, c, nm) in enumerate(sub))
                md.append(f"- {band}・{y}年（{n}業種）：{rk}" + ("・⚠ 3業種未満で順位に意味が薄い" if n < 3 else ""))
        md.append("- 賃加工の重さが線より下の業種（金属プレス・金型など）は、統計の上では製品の出荷が大半で、受託の比べの母数に入れない。ただし材料を自分で買う下請は出荷額に数えられるので、重さが低い＝下請でない、ではない（4-70）")
    md.append("")
    return md

def _contract_weights(topic):
    """kougyo_rows.csv のシート1200から、細分類ごとの最新年の賃加工の重さ {code: (重さ, 年)} を返す（kougyo_contract_section と同じ式）"""
    f = os.path.join(OUT, topic, "kougyo_rows.csv")
    if not os.path.exists(f): return {}
    acc = {}
    for r in csv.DictReader(open(f, encoding="utf-8-sig")):
        if str(r.get("sheet")) != "1200" or len(r.get("code") or "") != 4: continue
        col = r.get("column") or ""
        if "Value of manufactured goods shipments" not in col: continue
        k = next((k for k, v in _KC.items() if v in col), None)
        if not k: continue
        d = acc.setdefault((r["code"], r["year"]), {"fy": r.get("file_year") or r["year"]})
        if (r.get("file_year") or r["year"]) < d["fy"]: continue
        if k not in d: d[k] = _kf(r.get("value"))
    if not acc: return {}
    ly = max(y for _, y in acc); out = {}
    for (code, y), d in acc.items():
        if y != ly or any(d.get(k) is None for k in _KC): continue
        tot = sum(d[k] for k in _KC)
        if tot > 0: out[code] = (d["加工賃"] / tot, y)
    return out

# 2026-11-07 4-68（Journalist's Resource「Basic data analysis: making the call」：比べる単位の平均の周りの95%の幅を出し、
# その中に入る単位は「平均並み」として高い・低いと書かない）。kougyo_series.csv の最新の同じ年で、注目の細分類の
# 従業者1人あたり製造品出荷額等の平均・標準誤差・95%の幅を出し、幅の外の細分類だけを高い・低いとして挙げる
METHOD_DISC = re.compile(r"(n\s*[=＝]\s*\d|回答(数|者数|企業数|率)|有効回答|社を対象|名を対象|人を対象|調査方法|調査手法|調査概要|標本|サンプル(数|サイズ)|調査期間|調査対象|sample size|respondents|methodology)", re.I)
def kougyo_callband(by, fld="従業者1人あたり製造品出荷額等", unit="百万円"):
    # 2026-11-07 47回目の走査から：出荷額等は材料費を含むので、材料を客が出す賃加工の業種（めっきなど・賃加工の重さ90%前後）は
    # 構造的に低く出る。受託の業種どうしの比較は材料の入り方に左右されにくい付加価値額でも同じ幅を出し、2つで並べる（4-35 業態をそろえる）
    pts = {}
    # 2026-11-07 走査で見つかった失敗から（44回目）：この幅の母数「35業種」に、42回目に直した上下の数え上げと同じく
    # 小分類（末尾0の 2410・2440・2460 など）と、子が1つだけで値が同じ番号（2410＝2411、2480＝2481）が入っていた。
    # 母数は細分類（末尾が0でない番号）だけにし、同じ年に同じ値の番号は1つとして数える
    for (code, name), rs in sorted(by.items()):
        if not _kfocus(name) or len(str(code)) != 4 or str(code)[-1:] == "0": continue
        for y, r in rs:
            v = _kf(r.get(fld))
            if v is not None and v > 0 and v not in pts.get(y, {}).values(): pts.setdefault(y, {})[(code, name)] = v
    ys = [y for y in sorted(pts) if len(pts[y]) >= 5]
    if not ys: return []
    y = ys[-1]; vals = list(pts[y].values()); n = len(vals)
    m = statistics.mean(vals); sd = statistics.stdev(vals); se = sd / n ** 0.5; ci = 1.96 * se
    hi = [f"{c} {nm} {v:,.1f}" for (c, nm), v in sorted(pts[y].items(), key=lambda kv: -kv[1]) if v > m + ci]
    lo = [f"{c} {nm} {v:,.1f}" for (c, nm), v in sorted(pts[y].items(), key=lambda kv: kv[1]) if v < m - ci]
    mid = n - len(hi) - len(lo)
    return [f"- 平均並みの幅（4-68・{y}年・注目の細分類{n}業種・{fld}・{unit}）：平均 {m:,.2f}・標準誤差 {se:,.3f}・95%の幅 {m - ci:,.2f}〜{m + ci:,.2f}。"
            f"幅の中の{mid}業種は「平均並み」として高い・低いと書かない。母数から受託でない製品の製造業（鋳鉄管・研磨材・溶接機・めっき板など）を外した（4-70）。"
            "この幅は平均の不確かさの幅で、業種1つ1つの散らばりの幅ではない（業種の多くが幅の外に出るのは普通）",
            "  - 幅より上：" + ("、".join(hi[:8]) or "なし"), "  - 幅より下：" + ("、".join(lo[:8]) or "なし")]

def kougyo_section(topic):
    f = os.path.join(OUT, topic, "kougyo_series.csv")
    if not os.path.exists(f): return []
    by = {}
    for r in csv.DictReader(open(f, encoding="utf-8-sig")):
        y = _kf(r.get("year"))
        if y is None: continue
        by.setdefault((r["code"], r["name"]), []).append((int(y), r))
    md = ["## 工業統計表 産業別統計表（確報）の直接読み：産業細分類×年（kougyo_series.csv・同じ表・同じ年の組だけで割った値）",
          "- 年は実績の年（表の年次の欄。2026-11-06 までの版はファイルの年＝調査の年を付けていて、2020年ファイルの最初の行＝2015年実績を「2020年」と誤って並べていた・4-34）",
          "- 年の切れ目：実績2015年は経済センサス‐活動調査（2016年調査）の値で、工業統計の他の年と調査が違う。実績2020年以降は経済構造実態調査。この表は実績2019年までで、それ以降とつながない（4-26）。金額は名目・単位は表の欄の見出しのまま（百万円）",
          "- 年率は複利（(最後÷最初)^(1/年数)−1・4-37）。「端の1点に依存」は最初か最後の年を外すと向きが変わるもの（4-40）"]
    # 2026-11-07 4-64：取った表の行数がちょうど1,000・10,000などの切りのいい数なら、絞り込み済みか途中で切れた版を落とした疑い
    # （Journalist's Resource「10 simple data errors」）。年ごとの行数も出し、ある年だけ少なければ取り落としを疑う
    _rows = sum(len(v) for v in by.values())
    if _rows and _rows % 1000 == 0: md.append(f"- ⚠ 表の行数がちょうど{_rows}行（4-64）：絞り込み済み・途中で切れた版を取った疑い。所管の表の総数と照合する")
    _ny = {}
    for v in by.values():
        for y, _ in v: _ny[y] = _ny.get(y, 0) + 1
    if len(_ny) >= 3:
        _m = statistics.median(_ny.values())
        _low = [f"{y}年 {n}行" for y, n in sorted(_ny.items()) if n < 0.5 * _m]
        if _low: md.append("- ⚠ 行数が他の年の半分未満の年（4-64・取り落としの疑い）：" + "、".join(_low) + f"（年ごとの行数の中央値 {_m:g}）")
    keys = sorted(by, key=lambda k: (0 if KOUGYO_FOCUS.search(k[1] or "") else 1, k[0]))
    shown = 0
    for (code, name) in keys:
        rs = sorted(by[(code, name)], key=lambda t: t[0])
        if not KOUGYO_FOCUS.search(name or "") and shown >= 40: continue
        parts = []
        if any("file_year" not in r for _, r in rs): continue  # 古い版（file_year の欄が無い＝年が調査の年で、最初の行だけ）は並べない
        for fld in ("従業者1人あたり加工賃収入額", "従業者1人あたり製造品出荷額等", "1事業所あたり従業者数", "加工賃収入額", "事業所数", "従業者数"):
            pts = [(y, _kf(r.get(fld))) for y, r in rs]
            pts = [(y, v) for y, v in pts if v is not None]
            if len(pts) < 2: continue
            (y0, v0), (y1, v1) = pts[0], pts[-1]
            g = _cagr(v0, v1, y1 - y0)
            sgn = lambda a, b: (b > a) - (b < a)
            d = sgn(v0, v1)
            edge = len(pts) >= 3 and (sgn(pts[1][1], v1) != d or sgn(v0, pts[-2][1]) != d)
            cen = any(y == 2015 for y, _ in pts) and y0 != 2015 or y0 == 2015 or y1 == 2015
            # 2026-11-07 4-66（走査で見つかった失敗）：この節の1人あたりは百万円（工業統計の金額欄のまま）で単位を書いていず、
            # 次の節の _万円 と100倍違う値が並んでいた。単位を値に付ける（build_kougyo.py の _ratios：金額欄は百万円）
            _u = "百万円" if fld.startswith("従業者1人あたり") else ("人" if fld in ("1事業所あたり従業者数", "従業者数") else "百万円" if fld == "加工賃収入額" else "")
            parts.append(f"{fld} {y0}年 {v0:,.1f}→{y1}年 {v1:,.1f}{_u}（{len(pts)}点" + (f"・年率 {g:+.1%}" if g is not None else "") + ("・⚠ 端の1点に依存" if edge else "")
                         + ("・⚠ 2015年は経済センサスの値を含む" if cen else "") + "）")
        if parts:
            md.append(f"- 【一次で確認】{code} {name}：" + "／".join(parts))
            shown += 1
    if shown == 0: md.append("- （番号つきの行が年2点以上そろわない）")
    md += kougyo_segments(by)
    md += kougyo_callband(by)
    md += kougyo_callband(by, "従業者1人あたり付加価値額_万円", "万円")
    md += kougyo_decompose(by)
    return md + [""]

# 2026-11-07 4-70（hrbench「Revenue per employee」：1人あたりが動いた理由は分子と分母のどちらが動いたかで別物。
# 下がったときは「人が売上より速く増えた」か「売上が減って人が据え置き」のほぼ2つ）。段差をまたがない最長の区間で、
# 1人あたり製造品出荷額等の年率を、分子（製造品出荷額等）と分母（従業者数）の年率に分けて並べ、どちらが効いたかを機械で書く。
# 分子・分母は同じ表・同じ年の組だけ（4-5 の「別々の出典の値どうしを割らない」）。名目のまま
def kougyo_decompose(by):
    sheet = "1100"
    def segs(ys):
        out, cur = [], []
        for y in ys:
            if cur and (y in (2015, 2016) or y != cur[-1] + 1): out.append(cur); cur = []
            cur.append(y)
        if cur: out.append(cur)
        return [s for s in out if len(s) >= 2]
    md = []
    for (code, name), rs in sorted(by.items()):
        if code != "0000" and (not _kfocus(name) or code[-1:] == "0"): continue
        num = dict(_kseries(rs, "製造品出荷額等", sheet)); den = dict(_kseries(rs, "従業者数", sheet))
        ys = sorted(set(num) & set(den))
        ss = segs(ys)
        if not ss: continue
        s = max(ss, key=len); a, b = s[0], s[-1]
        gn = _cagr(num[a], num[b], b - a); gd = _cagr(den[a], den[b], b - a)
        if gn is None or gd is None: continue
        gp = (1 + gn) / (1 + gd) - 1
        if gp >= 0:
            # 2026-11-07 走査で見つかった失敗から（46回目）：出荷額も人も減った業種（2691 金属用金型 出荷額 -0.6%・従業者 -0.9%）が
            # 「出荷額が伸び、人が減った」と書かれていた。出荷額の向きを先に見る
            why = ("人が出荷額より速く減った（縮みながらの上昇・生き残りを疑う 4-25）" if gn < 0 else
                   "出荷額が伸び、人が減った（両方が上げた）" if gd < 0 else "出荷額が人より速く伸びた")
        else:
            why = ("人が出荷額より速く増えた（需要より先に人を増やした型）" if gd > 0 and gn >= 0 else
                   "出荷額が減り、人は増えたか据え置き" if gd >= 0 else "出荷額が人より速く減った")
        md.append(f"- {code} {name} {a}→{b}年：1人あたり出荷額 年率 {gp:+.1%} ＝ 出荷額 {gn:+.1%} ／ 従業者 {gd:+.1%}。{why}")
        if len(md) >= 30: break
    if not md: return []
    return ["", "### 1人あたりの向きを分子と分母に分ける（4-70・段差をまたがない最長の区間・同じ表の組・名目・機械）",
            "- 1人あたりの年率は (1+出荷額の年率)÷(1+従業者の年率)−1。同じ1人あたりの上昇でも、出荷額が伸びた型と人が減った型は意味が逆なので、scan.md ではどちらの型かを書く"] + md

# 2026-11-06 走査で見つかった失敗から（4-26・4-37・4-44）：上の年率は実績2013→2019年を1本で結ぶので、
# 経済センサスの2015年と、製造業計で1事業所あたり従業者が段差を作る2015→2016年（事業所の数え方の違いの疑い）をまたぐ。
# ①製造業計の隣り合う年で事業所数か1事業所あたり従業者が10%以上動いた境目を「段差」として機械で見つける
# ②段差をまたがない最長の区間（同じ調査の続き）だけで年率を出す ③同じ区間の製造業計の年率と並べ、差（ポイント）を出す（比べる相手）
KOUGYO_SEG_FIELDS = ("従業者1人あたり加工賃収入額", "従業者1人あたり製造品出荷額等_万円", "従業者1人あたり付加価値額_万円",
                     "従業者1人あたり現金給与総額_万円", "1事業所あたり従業者数", "事業所数", "従業者数")
def _kseries(rs, fld, sheet):
    pts = {}
    for y, r in rs:
        if sheet and r.get("sheet") != sheet: continue
        v = _kf(r.get(fld))
        if v is not None and y not in pts: pts[y] = v
    return sorted(pts.items())
def kougyo_segments(by):
    tot = next((rs for (c, n), rs in by.items() if c == "0000"), None)
    if not tot: return []
    sheet = "1100"
    breaks = set()
    for fld in ("事業所数", "1事業所あたり従業者数"):
        pts = _kseries(tot, fld, sheet)
        for (y0, v0), (y1, v1) in zip(pts, pts[1:]):
            if v0 and abs(v1 / v0 - 1) >= 0.10: breaks.add(y1)
    breaks.add(2015); breaks.add(2016)  # 2015＝経済センサス、その次の年から元の調査に戻る
    def segs(years):
        out, cur = [], []
        for y in years:
            # 2026-11-06 走査の失敗から：段差の年 y は「y−1 と y の間が切れる」の意味。前は段差の年の後ろでも切っていて、
            # 2016年が1点だけの区間になり、同じ工業統計の続きの 2016→2019年が 2017→2019年（3点）に縮んでいた
            if cur and (y in breaks or y != cur[-1] + 1):
                out.append(cur); cur = []
            cur.append(y)
        if cur: out.append(cur)
        return [s for s in out if len(s) >= 2]
    md = ["", "### 段差をまたがない区間だけの年率と、製造業計との差（4-26・4-37・4-44・機械）",
          f"- 段差とみなした年（製造業計で事業所数か1事業所あたり従業者が前年から10%以上動いた年・2015年・2016年）：{', '.join(str(y) for y in sorted(breaks))}。段差の年の前後はつながない",
          "- 金額は名目（物価で割っていない）。製造業計との差は「年率の差（ポイント）」で、構成変化（小さい事業所の退出）を除いた値ではない（規模別は kougyo_size.csv・4-25）"]
    base = {}
    for fld in KOUGYO_SEG_FIELDS:
        pts = dict(_kseries(tot, fld, sheet))
        for s in segs(sorted(pts)):
            base[(fld, s[0], s[-1])] = _cagr(pts[s[0]], pts[s[-1]], s[-1] - s[0])
    n = 0; tally = {}
    # 2026-11-07 走査で見つかった失敗から（42回目・4-67）：「35業種中 上 20・下 15」の母数に、小分類（末尾0の 2410・2440・2460 など）と
    # その下の細分類が両方入り、子が1つだけの小分類（2410＝2411、2480＝2481）は同じ値が2回数えられていた。数える母数は細分類（末尾が0でない番号）だけにし、
    # 値の並びがすでに数えた番号と同じなら数えない。小分類の行は本文には残す
    _seen_sig = set(); _dup = 0; _agg = 0; _prod_n = [0]
    for (code, name), rs in sorted(by.items()):
        if code == "0000" or not KOUGYO_FOCUS.search(name or ""): continue
        if KOUGYO_PRODUCT.search(name or ""): _agg += 0; _prod_n[0] += 1
        _sig = tuple(tuple(_kseries(rs, f, sheet)) for f in KOUGYO_SEG_FIELDS)
        _count = code[-1:] != "0" and _sig not in _seen_sig and not KOUGYO_PRODUCT.search(name or "")
        if code[-1:] == "0": _agg += 1
        elif _sig in _seen_sig: _dup += 1
        _seen_sig.add(_sig)
        parts = []
        for fld in KOUGYO_SEG_FIELDS:
            pts = dict(_kseries(rs, fld, sheet))
            ss = segs(sorted(pts))
            if not ss: continue
            s = max(ss, key=len)
            g = _cagr(pts[s[0]], pts[s[-1]], s[-1] - s[0])
            if g is None: continue
            b = base.get((fld, s[0], s[-1]))
            if b is not None and _count:
                t_ = tally.setdefault(fld, [0, 0, 0]); t_[0 if g > b else 1 if g < b else 2] += 1
            parts.append(f"{fld} {s[0]}→{s[-1]}年 {pts[s[0]]:,.1f}→{pts[s[-1]]:,.1f}（年率 {g:+.1%}"
                         + (f"・製造業計 {b:+.1%}・差 {(g - b) * 100:+.1f}ポイント" if b is not None else "") + "）")
        if parts:
            md.append(f"- 【一次で確認】{code} {name}：" + "／".join(parts)); n += 1
        if n >= 40: break
    if n == 0: md.append("- （段差をまたがない2点以上の区間が無い）")
    # 2026-11-07 4-65（QiQUMO「アンケートのバイアス」：切り口を細かく切って目立った切り口だけを採ると、当たりを拾っただけになる）。
    # 見た細分類の数と、製造業計より上・下の数を欄ごとに添える。scan.md で1業種の差を書くときは、この母数と並べる
    if tally:
        md.append("- 見た細分類の数と、製造業計との差の向き（4-65・探索の数を添える）：" + "／".join(
            f"{f} {a + b + c}業種中 上 {a}・下 {b}・同じ {c}" for f, (a, b, c) in tally.items())
            + f"（母数は細分類だけ。小分類の行 {_agg}件と、値の並びが同じ番号 {_dup}件、受託でない製品の製造業 {_prod_n[0]}件は数えない・4-70）")
    return md

MOCHIBUN = re.compile(r"(合同会社|合名会社|合資会社|LLC|\(同\)|（同）)")
NORMAL_ABSENT_SCAN = re.compile(r"(決算公告|官報|貸借対照表)")

def scan(topic):
    rows = drop_boilerplate(dedupe(list(rows_for(topic))))
    if not rows: return 0
    with open(os.path.join(OUT, topic, "cross.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    md = [f"# 走査（自動）— {topic}", "", "機械で出した突き合わせと【推定】。式と出典件数つき。最終の結論は scan.md。",
          "可能性の言葉（4-39・ICD 203。この7語だけを使い、確度の印と同じ文に並べない）：" + "／".join(f"{w}={p}" for w, p in LIKELIHOOD), ""]
    gb = gold_check()
    md += [f"- 判定の正解例の点検（4-38）：{len(GOLD) - len(gb)}／{len(GOLD)}件が期待どおり", *gb]
    md += [f"- 正解例の照合（4-47）：TP {GOLD_PRF['TP']}・混入FP {GOLD_PRF['FP']}・落としFN {GOLD_PRF['FN']}／精度 {GOLD_PRF['精度']}・再現率 {GOLD_PRF['再現率']}・F1 {GOLD_PRF['F1']}"
           + ("（混入あり：精度を先に直す）" if GOLD_PRF['FP'] else "")]
    picks = {}  # 4-38：前回からの変化を出すため、欄ごとの採る値を残す
    # 2026-11-05 4-19：出どころの記録（URL・取得日）が欠けた数字を数える。0件でなければ HEALTH と同じく直す対象
    nprov = sum(1 for r in rows if not r["URL"] or not r["取得日"])
    md += [f"- 出どころの記録が欠けた数字：{nprov}件／{len(rows)}件（URLか取得日が無い）"]
    # 2026-11-05 4-21：機械で決めきれず人（Claude の走査）の確認が要る件数（Data Ladder：保留の件数を品質の指標にする）。文脈不明か年が無い数字
    nhold = sum(1 for r in rows if r["文脈"] == "不明" or not r["年"])
    md += [f"- 桁の感覚の外れで外した人数（1,000万人超・4-31）：{OUTLIERS.get(topic, 0)}件"]
    md += [f"- 要確認（文脈不明か年が無い）：{nhold}件／{len(rows)}件。この数字は結論に使う前に引用の本文で確かめる"]
    # 4-39：除外の点検（どの規則で何件外したか）と、短すぎる引用（メニュー・切れ端の疑い）
    dr = DROPS.get(topic, {})
    md += ["- 除外の点検（規則ごとに外した数字の件数・4-39）：" + ("、".join(f"{k} {v}件" for k, v in sorted(dr.items(), key=lambda kv: -kv[1])) or "（なし）")]
    nshort = sum(1 for r in rows if len(re.sub(r"\s", "", r["引用"])) < SHORTQ)
    md += [f"- 短すぎる引用（{SHORTQ}字未満・メニューや表の切れ端の疑い・4-39）から取った数字：{nshort}件。結論に使う前に本文で確かめる"]
    # 2026-11-06 4-42：照合の前の棚卸し（GENIEE の名寄せロジック：キーごとの欠損率を先に測り、欠けが多いキーは補助のキーで補う）。
    # 突き合わせの鍵（年・規模・文脈・取得日）ごとに欠けた数字の割合を出す。欠けの多い鍵で突き合わせた向き・裏付けは弱い
    keys = [("年", lambda r: not r["年"]), ("規模区分", lambda r: not r.get("規模")), ("文脈", lambda r: r["文脈"] == "不明"), ("取得日", lambda r: not r["取得日"])]
    # 2026-11-06 4-50（Binette ら「How to Evaluate Entity Resolution Systems」arXiv 2404.05622：数字の組を均一に抜いて目で確かめると精度が偏る。
    # まとまり（ここでは品目）ごと、件数に比例した確率で抜き、まとまりの中を全部確かめる）。走査で本文を目で確かめる品目を、その日の日付を種に3つ選ぶ
    import random as _rnd
    _items = {}
    for r in rows: _items[r["品目"]] = _items.get(r["品目"], 0) + 1
    _rg = _rnd.Random(str(__import__("datetime").date.today()) + topic); _pool = dict(_items); _pick = []
    while _pool and len(_pick) < 3:
        it0 = _rg.choices(list(_pool), weights=list(_pool.values()))[0]; _pick.append(f"{it0}（{_pool.pop(it0)}件）")
    md += ["- 目で確かめる抜き取り（品目ごと・件数に比例・4-50）：" + "、".join(_pick) + "。この品目の引用は全件を本文と照らす"]
    # 2026-11-06 4-55（fungry C-NAPS「ファクトチェックとは」：引用・要約・翻訳の段で元の文脈から離れる。一次を元にした記事でも一次そのものと見比べる）。
    # 引用が翻訳・要約・抄訳・海外報道の紹介の型なら、文脈が離れた疑いとして数える。一次の表と見比べるまで採る値・裏付けにしない
    TRANS = re.compile(r"(翻訳|抄訳|和訳|英訳|要約すると|まとめると|海外メディア|英紙|米紙|外電|報じた|reportedly|according to)", re.I)
    ntr = [r for r in rows if TRANS.search(r["引用"]) and not PRIMARY.search(urlparse(r["URL"]).netloc + urlparse(r["URL"]).path if r["URL"] else "")]
    if ntr: md.append(f"  - ⚠ 翻訳・要約・又聞きの型の引用から取った数字 {len(ntr)}件（例：{ntr[0]['品目']}）：元の一次と見比べるまで採る値・裏付けにしない（4-55）")
    # 2026-11-06 4-55（Altss の entity resolution methodology：同じ番号でも、発行元の仕組みから取ったか・本人のサイトに書いてあるか・他者が引いたかで出どころが違う。
    # 発行元で確かめるまで同じ会社と決めない）。引用に13桁の法人番号があり、URL が国税庁の法人番号公表サイトでなければ「番号の出どころ：他者の引用」
    HB = re.compile(r"(?<!\d)\d{13}(?!\d)")
    nhb = [r for r in rows if HB.search(re.sub(r"[\s\-]", "", r["引用"])) and "houjin-bangou.nta.go.jp" not in (r["URL"] or "")]
    if nhb: md.append(f"  - ⚠ 法人番号の出どころが発行元でない数字 {len(nhb)}件（例：{urlparse(nhb[0]['URL']).netloc}）：国税庁の法人番号公表サイトで取り直すまで、同じ会社の判定・名寄せに使わない（4-55）")
    # 2026-11-07 4-71（SalesRadar note「法人番号を名寄せの主キーに」：法人番号は先頭1桁が検査用の数字の13桁。個人事業主と一部の外資には無い）。
    # 引用の13桁の数字を国税庁の検査用数字の式（9 − (下位から奇数桁×1・偶数桁×2 の和 mod 9)）で確かめ、合わないものは法人番号でない
    # （電話番号・注文番号・桁の取り違え）として数え、名寄せのキーに使わない
    def _hb_ok(n):
        base = n[1:]
        t = sum(int(c) * (1 if i % 2 == 0 else 2) for i, c in enumerate(reversed(base)))
        return int(n[0]) == 9 - (t % 9)
    _hbs = [m for r in rows for m in HB.findall(re.sub(r"[\s\-]", "", r["引用"]))]
    _hbad = [m for m in _hbs if not _hb_ok(m)]
    if _hbs: md.append(f"  - 法人番号の形の点検（4-71）：13桁の数字 {len(_hbs)}件のうち検査用の数字が合わない {len(_hbad)}件。合わないものは法人番号として名寄せに使わない")
    # 2026-11-07 4-72（知って得する研究所「AIの出典を検証する7つの方法」：料金などの数字は、会社のトップページを示すだけでは裏付けにならない。
    # その数字が載った個別のページが要る）。出典URLがサイトの入口（パスが空・/・index）の数字を数え、個別のページで取り直す対象にする
    _TOPP = re.compile(r"^/?(index\.(html?|php|aspx?))?$", re.I)
    ntop = [r for r in rows if r["URL"] and _TOPP.match(urlparse(r["URL"]).path or "")]
    if ntop: md.append(f"  - ⚠ 出典がサイトの入口（トップページ）の数字 {len(ntop)}件（例：{urlparse(ntop[0]['URL']).netloc}）：数字が載った個別のページで取り直すまで裏付けに数えない（4-72）")
    # 2026-11-07 4-73（Quartz「The Quartz guide to bad data」：空の代わりに入れた決まった値、表計算の行数の上限、型の上限は、
    # 本物の値ではなく「欠け・切れ」の指紋）。値がこの型（65535・65536・1048576・32767・2147483647・99999・999999・9999999）なら数え、採る値・裏付けにしない
    _SENT = {65535, 65536, 1048576, 1048575, 32767, 32768, 2147483647, 99999, 999999, 9999999, 99999999}
    def _num(v):
        try: return float(str(v).replace(",", ""))
        except Exception: return None
    nsen = [r for r in rows if _num(r.get("値")) in _SENT]
    if nsen: md.append(f"  - ⚠ 欠け・切れの指紋の値（65535・1048576・99999 など）{len(nsen)}件（例：{nsen[0]['品目']}）：空の代わりの値か表の行数の上限で、本物の値にしない（4-73）")
    # 同（Quartz：工学の外で小数第3位より細かく測るものはまず無い。細かすぎる桁は計算や推計の出力の指紋）。
    # 引用の原文の数字に小数4桁以上があれば「偽りの精度」として数え、測った値でなく推計・計算の値として【推定】側で読む
    _FP = re.compile(r"\d\.\d{4,}")
    nfp = [r for r in rows if _FP.search(str(r.get("原文の数字") or ""))]
    if nfp: md.append(f"  - ⚠ 小数4桁以上の数字 {len(nfp)}件（例：{nfp[0]['品目']}）：測った値でなく推計・計算の出力の疑い。【推定】側で読み、出どころの算出方法を確かめる（4-73）")
    # 同（Quartz：表計算が先頭の0を落とし、番号が数字に化ける）。産業分類（製造業の食料品は09で始まる）・市区町村コード・郵便番号が
    # 3桁・5桁などの数字だけで出てきたら先頭の0が落ちた疑い。build_kougyo.py の番号は文字として読む
    _Z = re.compile(r"(産業分類|分類番号|産業番号|市区町村コード|団体コード|郵便番号)\D{0,6}(\d{3}|\d{5}|\d{6})(?!\d)")
    nz = [r for r in rows if _Z.search(r["引用"])]
    if nz: md.append(f"  - ⚠ 番号の先頭の0が落ちた疑い {len(nz)}件（例：{nz[0]['品目']}）：番号は文字として照合し、数字として突き合わせない（4-73）")
    # 2026-11-07 4-74（コプラスワン「特許調査のAI活用」：番号が実在しても中身が違う・権利が消えていることがある。ゴムマリ「官公データで企業リスト」：
    # リストには閉鎖・移転・重複が混ざり、運用で洗い直すのが本体）。引用に会社の閉鎖・解散・清算・合併による消滅・廃業の語があれば、
    # その会社の数字を今の事実として使わない（法人番号公表サイトの変更履歴・閉鎖で確かめる）
    _GONE = re.compile(r"(登記記録の閉鎖|閉鎖登記|解散し|解散した|清算結了|清算手続|吸収合併により消滅|合併により消滅|廃業し|廃業した|事業を停止|破産手続開始|民事再生)")
    ngone = [r for r in rows if _GONE.search(r["引用"])]
    if ngone: md.append(f"  - ⚠ 会社の閉鎖・解散・消滅の語がある引用の数字 {len(ngone)}件（例：{ngone[0]['品目']}）：今の事実として使わず、法人番号公表サイトの変更履歴（閉鎖・合併）で時点を確かめる（4-74）")
    # 同（コプラスワン：件数は一覧から数えた数だけを書き、推計しない）。社・件・事業所の件数の引用に「推計・推定・見込み・とみられる」があれば、
    # 数え上げでなく推計の件数として分けて数え、事業所数・社数の事実の欄に使わない
    _CNTEST = re.compile(r"(推計|推定|見込み|とみられ|と見られ|試算|概算)")
    ncnt = [r for r in rows if r["単位"] in ("社", "件", "事業所") and _CNTEST.search(r["引用"])]
    if ncnt: md.append(f"  - ⚠ 推計された件数（社・件・事業所）{len(ncnt)}件（例：{ncnt[0]['品目']}）：一覧・名簿から数えた数ではないので、件数の事実の欄に入れず【推定】で読む（4-74）")
    # 同（GENAI「AIのハルシネーション」：引用元のない統計を公的な数値のように示す型が最も見抜きにくい）。一次でない引用が
    # 「調査によると・統計によると・データによると」と書きながら、調査・統計の名（省庁・白書・「」の調査名・団体名）を持たないものを数える
    _ANON = re.compile(r"(調査によると|統計によると|データによると|調査では|統計では|調べによると)")
    _NAMED = re.compile(r"(省|庁|白書|「[^」]{2,}」|『[^』]{2,}』|協会|工業会|連合会|総研|研究所|センサス|基本調査|実態調査|統計調査|日本銀行|日銀|帝国データバンク|東京商工リサーチ)")
    nanon = [r for r in rows if r["一次"] != "一次" and _ANON.search(r["引用"]) and not _NAMED.search(r["引用"])]
    if nanon: md.append(f"  - ⚠ 名の無い調査・統計を根拠にした数字 {len(nanon)}件（例：{nanon[0]['品目']}）：どの調査かを名指しした元へ戻るまで、採る値・裏付けに使わない（4-74）")
    # 2026-11-07 4-75（データビズラボ「データ統合の問題」：在庫がマイナスのような実態とかけ離れた値は、取り込み・結合の誤りの指紋）。
    # 数えるもの（人・社・事業所・件・台）が負の値なら、本物の値でなく取り込みの誤り（△の読み違い・差の取り違え）として数え、採る値にしない
    _CNT = ("人", "名", "社", "事業所", "件", "台")
    nneg = [r for r in rows if r["単位"] in _CNT and (_num(r.get("値")) or 0) < 0]
    if nneg: md.append(f"  - ⚠ 数えるもの（人・社・事業所・件）が負の値 {len(nneg)}件（例：{nneg[0]['品目']}）：ありえない値で、取り込み・差の取り違えの誤り。採る値にしない（4-75）")
    # 同（一田和樹のメモ帳の紹介する検証の手順：要約の段で1件の事例が「複数」「相次ぐ」に広げられる誇張が混ざる）。
    # 引用が「相次ぐ・多くの企業・各社・続々・複数の」と広げて言うのに、その品目の出どころが1つのホストしかないものを数える
    _GEN = re.compile(r"(相次い|相次ぐ|多くの(企業|会社|工場|事業者)|各社が|続々|軒並み|複数の(企業|会社|工場|事業者))")
    _hosts = {}
    for r in rows: _hosts.setdefault(r["品目"], set()).add(urlparse(r["URL"]).netloc if r["URL"] else "")
    ngen = [r for r in rows if _GEN.search(r["引用"]) and len(_hosts.get(r["品目"], ())) <= 1]
    if ngen: md.append(f"  - ⚠ 「相次ぐ・多くの企業」と広げて言うのに出どころが1つ {len(ngen)}件（例：{ngen[0]['品目']}）：1件の事例の一般化の疑い。事例の数を数えるまで向き・広がりの根拠にしない（4-75）")
    # 同（データビズラボ：「髙」「﨑」のような異体字・機種依存の字で顧客名の照合が落ちる。SalesNow「名寄せ」：照合の前に表記をそろえる）。
    # 引用に異体字を含む行を数え、社名の突き合わせは build_setouchi_mfg.py の KYUJI と同じ置き換えをしてから行う目印にする
    _KY = re.compile(r"[髙﨑德濵邊邉澤齋齊廣國櫻龍會實與壽榮檜]")
    nky = [r for r in rows if _KY.search(r["引用"]) or _KY.search(r["品目"])]
    if nky: md.append(f"  - ⚠ 異体字（髙・﨑など）を含む引用 {len(nky)}件（例：{nky[0]['品目']}）：社名の突き合わせは字をそろえてから。そろえずに「別の会社」「見つからない」としない（4-75）")
    # 2026-11-07 4-77（介護のコミミ「常勤換算とは」：常勤換算は勤務延時間÷常勤の所定時間で、所定時間（週35か40時間）が違えば同じ人でも値が変わる。
    # 兼務は職種ごとに按分し、端数は切り捨てる）。人数の引用に常勤換算・FTE・延べ・人月の語があれば、頭数でない人数として数え、
    # 頭数（従業者数）の系列と同じ分母にしない（4-70 の分母の固定）
    _FTE = re.compile(r"(常勤換算|FTE|フルタイム換算|延べ人数|延べ\d|延人員|人月|人日|勤務延時間)", re.I)
    nfte = [r for r in rows if r["単位"] in ("人", "名") and _FTE.search(r["引用"])]
    # 同（走査52回目の失敗：mfg_capacity の「材料支給の割合」の品目に 2〜100 の値が万円の欄で入った）。品目が割合・比率を聞くのに
    # 金額の単位で拾った数字を数え、%の読み違いの疑いとして採る値・裏付けにしない
    _RATQ = re.compile(r"(割合|比率|シェア|構成比|の重さ)")
    nrat = [r for r in rows if _RATQ.search(r["品目"]) and r["単位"] in ("万円", "円", "億円", "百万円", "千円") and (_num(r.get("値")) or 0) <= 100]
    if nrat: md.append(f"  - ⚠ 割合を聞く品目に金額の単位で100以下の数字 {len(nrat)}件（例：{nrat[0]['品目']}）：%を金額として読んだ疑い。原表の単位を確かめるまで使わない（4-77）")
    if nfte: md.append(f"  - ⚠ 常勤換算・延べの人数 {len(nfte)}件（例：{nfte[0]['品目']}）：頭数の従業者数と同じ分母にしない。換算の基準時間（所定労働時間）が分かるまで1人あたりの割り算に使わない（4-77）")
    # 2026-11-07 4-76（電子決算公告ドットコム「決算公告のやり方」：官報の決算公告は貸借対照表の要旨が中心で売上高・営業利益はほぼ載らない。
    # 合同会社には決算公告の義務が無い）。①「官報」「決算公告」と同じ引用の売上高の数字は、要旨の外（別の資料・記事の取り違え）の疑いとして数える
    # ②合同会社の決算公告の品目が空・0件でも失敗に数えない目印を出す
    _KANPO = re.compile(r"(官報|決算公告)"); _URI = re.compile(r"(売上高|売上収益|営業収益|営業利益)")
    nkp = [r for r in rows if _KANPO.search(r["引用"]) and _URI.search(r["引用"]) and not re.search(r"(損益計算書|P/L|PL)", r["引用"])]
    if nkp: md.append(f"  - ⚠ 官報・決算公告の引用に売上高・営業利益 {len(nkp)}件（例：{nkp[0]['品目']}）：官報の公告は貸借対照表の要旨が中心で売上は普通載らない。要旨の外の数字の取り違えを疑い、公告の原本（官報・電子公告のページ）で欄を確かめる（4-76）")
    ngk = [r for r in rows if "合同会社" in (r["引用"] + r["品目"]) and "公告" in (r["引用"] + r["品目"])]
    if ngk: md.append(f"  - ⚠ 合同会社の決算公告に触れた引用 {len(ngk)}件（例：{ngk[0]['品目']}）：合同会社には決算公告の義務が無い。公告が見つからないことを欠け・失敗に数えず、売上は別の切り口（4-4）で出す（4-76）")
    # 同（角渕由英 note「生成AIで特許侵害を見つけられるか？」：検索・引用の失敗の多くは表の構造が崩れて渡ることから起き、引用の照合に失敗したら
    # まず抜き出した文の側（改行・表の展開）を疑う）。数字が6つ以上並ぶのに、かな・述語がほとんど無い引用は「表を平たくした文」の疑いとして数え、
    # どの列・どの行の値かが分かるまで採る値にしない（表は表のファイルから取る・4-41）
    _NUMS = re.compile(r"\d[\d,\.]*"); _KANA = re.compile(r"[ぁ-ん]")
    nflat = [r for r in rows if len(_NUMS.findall(r["引用"])) >= 6 and len(_KANA.findall(r["引用"])) < 4]
    if nflat: md.append(f"  - ⚠ 表を平たくした文の疑い（数字6つ以上・かなほぼ無し）{len(nflat)}件（例：{nflat[0]['品目']}）：列・行の見出しとの対応が崩れている恐れ。表のファイル・原文の表で列を確かめるまで採る値にしない（4-76）")
    # 2026-11-07 4-78（Aura「LinkedIn Employee Count vs Real Headcount」・People Data Labs「Exploring Employee Headcount Accuracy」：
    # SNS の社員数は本人の申告で、辞めた人・外注・別の同名の会社の人を含み、子会社を合算する。製造業は SNS に載らない人が多く少なく出る）。
    # 人数の引用が LinkedIn・SNS・Wantedly の申告の数字なら頭数の一次にせず、別の切り口の幅の端としてだけ使う
    _SNSHC = re.compile(r"(LinkedIn|リンクトイン|Wantedly|ウォンテッドリー|社員数（LinkedIn|フォロワー)", re.I)
    nsns = [r for r in rows if r["単位"] in ("人", "名") and (_SNSHC.search(r["引用"]) or re.search(r"(linkedin\.com|wantedly\.com)", r["URL"] or ""))]
    if nsns: md.append(f"  - ⚠ SNS申告の社員数 {len(nsns)}件（例：{nsns[0]['品目']}）：本人の申告で、退職者・外注・同名の別会社・子会社の合算を含む。製造業は載らない人が多い。頭数の一次にせず、登記・開示・採用ページの人数と幅で並べる（4-78）")
    # 同（People Data Labs：親会社の人数に子会社を足した数は親自身の大きさと伸びを隠す）。人数の引用に「グループ」「連結」があれば、
    # 単体・事業所の人数と同じ系列で比べない目印を出す（4-13 の単体と連結を人数にも当てる）
    _GRPHC = re.compile(r"(グループ(全体|計|合計|従業員)|連結(従業員|社員|ベース|で)|グループ会社を含)")
    ngrp = [r for r in rows if r["単位"] in ("人", "名") and _GRPHC.search(r["引用"])]
    if ngrp: md.append(f"  - ⚠ グループ・連結の人数 {len(ngrp)}件（例：{ngrp[0]['品目']}）：子会社を足した人数。単体・事業所の人数や1人あたりの分母と同じ系列にしない（4-78）")
    # 同（JobsPikr「Estimate a Company's Employee Count with Job-Posting Math」：求人の件数は同じ職の複数媒体・複数地域の掲載、常時募集の再掲を含み、
    # 3〜4割が重複とされる。求人数は空席の在庫で人数ではない）。求人の件数（件・職種）の引用を数え、人数や伸びの事実にしない目印にする
    _JOBN = re.compile(r"(求人|募集)(数|件数|中|掲載)?[^。]{0,12}\d+\s*(件|職種|ポジション)")
    njob = [r for r in rows if _JOBN.search(r["引用"])]
    if njob: md.append(f"  - ⚠ 求人の件数 {len(njob)}件（例：{njob[0]['品目']}）：複数媒体・複数地域の重複と常時募集の再掲を含む空席の在庫。人数・伸びの事実にせず、重複を除いた件数の推移としてだけ読む（4-78）")
    # 同（IRE「How to bulletproof your data story」：記事の割合はいくつの件数で計算したかを必ず答える）。%の値の引用に、母数（n=・〇社・〇人・〇件・回答）が
    # 無いものを数え、母数が分かるまで向き・比較の根拠にしない
    _BASE = re.compile(r"(n\s*=\s*\d|N\s*=\s*\d|\d[\d,]*\s*(社|人|名|件|事業所|回答|サンプル))")
    npct = [r for r in rows if r["単位"] in ("%", "％") and not _BASE.search(r["引用"])]
    if npct: md.append(f"  - ⚠ 母数の書かれていない割合 {len(npct)}件（例：{npct[0]['品目']}）：何件で計算した割合か分からない。母数を原表で確かめるまで向き・比較の根拠にしない（4-78）")
    # 2026-11-08 4-84（Sansan「名寄せとは？」：生年月日が未来・郵便番号と住所が合わないなど論理的に矛盾する値を、照合の前に洗い出す）。
    # 引用の設立・創業の年が今年より後なら、ありえない値（年の読み違い・和暦の換算違い）として数え、会社の年齢・社歴の事実にしない
    _FOUND = re.compile(r"(設立|創業|創立)[^。]{0,6}?((?:19|20)\d{2})\s*年")
    nfut = [r for r in rows if any(int(m.group(2)) > NOW_YEAR for m in _FOUND.finditer(r["引用"]))]
    if nfut: md.append(f"  - ⚠ 設立・創業の年が今年より後 {len(nfut)}件（例：{nfut[0]['品目']}）：論理的に矛盾する値で、年の読み違い・和暦の換算違いの疑い。登記・会社概要で確かめるまで使わない（4-84）")
    # 同（Sansan：社名は合併による変更・略称・前株後株で別の記録になる）。引用に旧社名・社名変更・商号変更の語があれば、
    # 旧名と新名を同じ会社の別名として名寄せの辞書（aliases）に入れるまで、片方だけの検索の0件を「無い」としない
    _RENAME = re.compile(r"(旧社名|旧商号|社名変更|商号変更|社名を変更|商号を変更|に社名変更|(?:より|から)改称)")
    nren = [r for r in rows if _RENAME.search(r["引用"])]
    if nren: md.append(f"  - ⚠ 社名変更・旧社名の語がある引用 {len(nren)}件（例：{nren[0]['品目']}）：旧名と新名を同じ会社の別名として照合するまで、片方の名での0件・食い違いを確定しない（4-84）")
    # 2026-11-08 4-84（TargetCompanyResearch「How To Estimate A Private Company's Revenue (2026)」：①人数×1人あたり ②業界の基準
    # ③データベース・信用調査 ④資金調達・評価額からの逆算 ⑤拠点・床面積・設備などの物理の足跡、の5つを重ね、1つの点でなく幅で出す。
    # ④は直近の調達がある若い会社だけ）。売上の推定に使える「物理の足跡」（延床面積・敷地面積・工場数・拠点数・設備台数・生産ライン）の
    # 引用がある品目を数え、2つの切り口（4-4）の3本目の材料として出す。資金調達・評価額の語と売上が同じ引用にあれば、古い中小には使わない目印を出す
    _FOOT = re.compile(r"(延床面積|延べ床面積|敷地面積|工場面積|建屋|工場数|拠点数|事業所数|設備台数|保有台数|生産ライン|ライン数|\d+\s*台(を|の)?(保有|導入|稼働))")
    nfoot = sorted({r["品目"] for r in rows if _FOOT.search(r["引用"])})
    if nfoot: md.append(f"  - 推定の3本目の切り口（物理の足跡：床面積・拠点数・設備台数）の材料がある品目 {len(nfoot)}（例：{nfoot[0]}）：人数×1人あたり・人件費からの逆算と並べ、幅の確かめに使う（4-84）")
    _FUND = re.compile(r"(資金調達|調達額|評価額|バリュエーション|時価総額|シリーズ[A-EＡ-Ｅ]|第三者割当)")
    nfund = [r for r in rows if _FUND.search(r["引用"]) and _URI.search(r["引用"])]
    if nfund: md.append(f"  - ⚠ 資金調達・評価額と売上が同じ引用 {len(nfund)}件（例：{nfund[0]['品目']}）：評価額からの売上の逆算は直近に調達した若い会社だけ。設立の古い中小の受託加工には当てない（4-84）")
    # 2026-11-06 4-55（ProofSnap の OSINT 証拠保全：取得の各工程を時刻・対象・結果と一緒に記録し、各記録が直前の記録のハッシュを含む連鎖にすると、
    # 途中の書き換えが後から分かる）。cross.csv の行（URL・取得日・引用・値）を順にハッシュでつなぎ、最後の値を chain.txt に残す。前回と違えば行が変わった
    import hashlib as _hl
    _h = ""
    for r in sorted(rows, key=lambda r: (r["URL"] or "", r["取得日"] or "", r["引用"])):
        _h = _hl.sha256((_h + "|" + (r["URL"] or "") + "|" + (r["取得日"] or "") + "|" + r["引用"] + "|" + str(r.get("値", ""))).encode("utf-8")).hexdigest()
    _cp = os.path.join(OUT, topic, "chain.txt"); _prev = open(_cp).read().split()[0] if os.path.exists(_cp) else ""
    open(_cp, "w").write(_h + "\n")
    # 2026-11-06 4-56（角渕由英 note：引用の機械検証）。拾った値の原文の数字（空白・カンマを除く）が、cross.csv に残した引用の中に見えるかを確かめる。
    # 見えない行は表だけでは出どころを確かめられないので、結論に使う前に本文で確かめる
    _sq = lambda s: re.sub(r"[\s,，]", "", s or "")
    nnot = [r for r in rows if r.get("原文の数字") and _sq(r["原文の数字"]) not in _sq(r["引用"])]
    md.append(f"  - 引用の機械検証（4-56）：値の原文の数字が引用の中に見えない行 {len(nnot)}件／{len(rows)}件"
              + (f"（例：{nnot[0]['品目'][:30]}）" if nnot else "") + f"。161字目より後ろの値で引用を値の前後の窓にした行 {QCUT.get(topic, 0)}件")
    # 2026-11-06 4-56（イノベーション総合研究所：仮置きは仮定と明記して分ける）。書き手が仮に置いた数字の件数
    nas = sum(1 for r in rows if r["予測"] == "仮置き")
    if nas: md.append(f"  - ⚠ 書き手の仮置き（「仮に」「と仮定」など）から取った数字 {nas}件：実測・統計でないので、向き・裏付け・採る値に入れない（4-56）")
    # 2026-11-06 4-56（OSINT Blog「Cognitive Biases That Derail Investigations」：検索は問いへの関連で並ぶので、偏った検索語は偏った結果を返す。
    # 反証の証拠を先に探す）。inputs/<テーマ>.csv の品目・別名・英語名が一方の向きの語（増加・拡大・不足など）だけを含み、反対の向きの語を含まない品目を数える。
    # その品目で集まった事実は、仮説を支える側に片寄りうる。反対の向きの別名（減少・縮小・過剰・横ばい）を足して取り直す
    try:
        import csv as _csv2
        UPW = re.compile(r"(増加|拡大|急増|伸び|成長|上昇|好調|不足|人手不足|growth|increase|shortage|rising)", re.I)
        DNW = re.compile(r"(減少|縮小|低下|減|下落|過剰|横ばい|停滞|廃業|退出|decline|decrease|surplus|flat)", re.I)
        _inp = list(_csv2.DictReader(open(os.path.join(os.path.dirname(__file__), "inputs", topic + ".csv"), encoding="utf-8-sig")))
        _one = [r.get("品目", "") for r in _inp if UPW.search(" ".join(r.get(k, "") or "" for k in ("品目", "英語名", "別名"))) and not DNW.search(" ".join(r.get(k, "") or "" for k in ("品目", "英語名", "別名")))]
        md.append(f"  - 検索語の向きの片寄り（4-56）：一方の向きの語だけで探している品目 {len(_one)}件／{len(_inp)}件" + (f"（例：{_one[0][:40]}）。反対の向きの別名を足して取り直す" if _one else ""))
    except Exception: pass
    # 2026-11-07 4-62（Stanford RegLab「Legal RAG Hallucinations」：前提の誤った問いに検索・生成は同調しやすい。Berkeley Protocol：
    # 検索エンジンと検索語の選び方が結果を片寄らせる）。品目名が「なぜ・理由・背景・原因・ため」など答えの向きを前提に置く問いの形なら、
    # 集まった引用は前提を支える側に寄る。前提を外した中立の言い方（「〜の推移」「〜の件数」）の別名を足して取り直す
    try:
        import csv as _csv3
        PREM = re.compile(r"(なぜ|理由|背景|原因|のため|による|せい|why|because|reason)", re.I)
        _inp3 = list(_csv3.DictReader(open(os.path.join(os.path.dirname(__file__), "inputs", topic + ".csv"), encoding="utf-8-sig")))
        _pr = [r.get("品目", "") for r in _inp3 if PREM.search(r.get("品目", "") or "")]
        _prn = {n: sum(1 for r in rows if r["品目"] == n) for n in _pr}
        md.append(f"  - 前提を置いた問いの形の品目（4-62）：{len(_pr)}件／{len(_inp3)}件" + ("（" + "、".join(f"{n[:30]}＝{c}行" for n, c in list(_prn.items())[:5]) + "）。中立の言い方の別名を足し、前提に反する引用があるかを先に見る" if _pr else ""))
    except Exception: pass
    # 2026-11-07 4-63（koji.so「Anchoring Bias in Research」：目の前の数字に見積もりが引き寄せられ、そこからの修正が足りない。
    # 「X より多いか少ないか」の聞き方をやめ、数字を置かずに聞く）。品目・別名に具体的な数字を入れた検索は、その数字を含むページを拾い、
    # 集まった値がその数字に寄る。品目の文字にある数字（年を除く）と±1%で一致する値を「検索語の数字のこだま」として数える。こだまは裏付けに数えない
    try:
        import csv as _csv4
        _inp4 = list(_csv4.DictReader(open(os.path.join(os.path.dirname(__file__), "inputs", topic + ".csv"), encoding="utf-8-sig")))
        _qn = {}
        for r0 in _inp4:
            t0 = unicodedata.normalize("NFKC", " ".join(r0.get(k, "") or "" for k in ("品目", "英語名", "別名"))).replace(",", "")
            ns = {float(x) for x in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", t0) if not (1990 <= float(x) <= 2040) and float(x) >= 2}
            if ns: _qn[r0.get("品目", "")] = ns
        _echo = {}
        for r in rows:
            ns = _qn.get(r["品目"])
            if ns and r["値"] and any(abs(r["値"] - n) <= 0.01 * n for n in ns): _echo[r["品目"]] = _echo.get(r["品目"], 0) + 1
        md.append(f"  - 検索語の数字のこだま（4-63）：数字を入れた品目 {len(_qn)}件／{len(_inp4)}件、そのうち検索語の数字と同じ値を拾った品目 {len(_echo)}件" + ("（" + "、".join(f"{n[:28]}＝{c}行" for n, c in list(_echo.items())[:5]) + "）。数字を外した別名で取り直し、こだまは裏付けに数えない" if _echo else ""))
    except Exception: pass
    md.append(f"  - 取得の記録の連鎖（4-55）：{_h[:12]}" + ("（前回と同じ）" if _prev == _h else f"（前回 {_prev[:12]} から変わった：足された・消えた・書き換えられた行がある）" if _prev else "（初回）"))
    # 2026-11-06 4-52（Binette ら arXiv 2210.01230：件数に比例して抜いた品目で誤りを数えたら、素朴に「誤り合計÷確かめた合計」とせず、
    # 抜かれやすさで重みを戻す（件数比例なら品目ごとの誤り率の平均）。素朴な割合は大きい品目に引っ張られる）。
    # Claude が走査で本文と照らした結果を out/<テーマ>/spot_check.csv（品目,確かめた件数,誤りの件数）に残すと、ここで両方を出す
    try:
        import csv as _csv
        sc = [r for r in _csv.DictReader(open(os.path.join(OUT, topic, "spot_check.csv"), encoding="utf-8")) if int(r.get("確かめた件数") or 0) > 0]
    except Exception: sc = []
    if sc:
        n_ = sum(int(r["確かめた件数"]) for r in sc); e_ = sum(int(r["誤りの件数"] or 0) for r in sc)
        pps = statistics.mean(int(r["誤りの件数"] or 0) / int(r["確かめた件数"]) for r in sc)
        md += [f"- 抜き取りの誤り率（4-52）：素朴 {e_}/{n_}＝{e_ / n_:.1%}／抜かれやすさで重みを戻した値 {pps:.1%}（{len(sc)}品目）。結論には後者を使い、品目が5未満なら幅が広いと書く"]
        # 2026-11-06 4-54（Marchant の照合の評価の博士論文：精度の推定は少ない正解例だと散らばりが大きいので幅で書く。
        # 採った側だけ確かめると「混入」しか測れず、落とした側を確かめないと「落とし」は測れない）。
        # Wilson の95%の幅を出し、spot_check.csv に「落とした側を確かめた件数」「落とした側の誤りの件数」（本当は採るべきだった件数）があれば落としの率も出す
        def _wilson(e, n, z=1.96):
            if n <= 0: return (0.0, 0.0)
            ph = e / n; den = 1 + z * z / n
            c = (ph + z * z / (2 * n)) / den; h = z * ((ph * (1 - ph) / n + z * z / (4 * n * n)) ** 0.5) / den
            return (max(0.0, c - h), min(1.0, c + h))
        lo_, hi_ = _wilson(e_, n_)
        md += [f"- 誤り率の95%の幅（Wilson・4-54）：{lo_:.1%}〜{hi_:.1%}（確かめた{n_}件）。幅の上端で結論が変わるなら、確かめる件数を増やしてから言い切る"]
        dn_ = sum(int(r.get("落とした側を確かめた件数") or 0) for r in sc); de_ = sum(int(r.get("落とした側の誤りの件数") or 0) for r in sc)
        if dn_ > 0:
            dlo, dhi = _wilson(de_, dn_)
            md += [f"- 落とした側の抜き取り（落としの率・4-54）：{de_}/{dn_}＝{de_ / dn_:.1%}（95%の幅 {dlo:.1%}〜{dhi:.1%}）。高ければ除外の決まりが厳しすぎる"]
        else:
            md += ["- 落とした側の抜き取り（4-54）：まだ0件。採った側だけでは混入しか測れない。除外の理由の多い順に数件ずつ本文と照らし、spot_check.csv の「落とした側を確かめた件数」「落とした側の誤りの件数」に残す"]
    md += ["- 鍵の欠け率（照合の前の棚卸し・4-42）：" + "、".join(f"{k} {sum(1 for r in rows if f(r))}/{len(rows)}件（{sum(1 for r in rows if f(r)) * 100 / len(rows):.0f}%）" for k, f in keys), ""]
    by_item = {}
    for r in rows: by_item.setdefault(r["品目"], []).append(r)
    onlymix = []
    thin = []  # 4-23：主張×独立の出どころの表（theosintvault の validation matrix）で、出どころ1か所の欄を最後に並べる
    for item, rs in sorted(by_item.items()):
        md.append(f"## {item}")
        # 2026-11-06 4-41 走査の失敗から：品目「工業統計 全国 産業細分類×従業者規模…」の数字4件が全部 www.jama.or.jp（日本自動車工業会）で、
        # .or.jp なので一次に数えられ【一次で確認】B3 と出た。品目が国の統計を名指しするのに、所管（国の .go.jp・e-Stat）以外から来た数字は、その統計の値ではない
        # 2026-11-06 4-42：品目の主語が業界団体（「日本金型工業会 …（工業統計の金型製造業と並べる）」）なら、団体のサイトの数字は品目どおりの出どころ。括弧の前の主語で判定する
        if NATSTAT_ITEM.search(item.split("（")[0]) and not re.search(r"(工業会|協会|連合会|組合)", item.split("（")[0]):
            off = {}
            for r in rs:
                h = urlparse(r["URL"]).netloc if r["URL"] else ""
                if h and not NATSTAT_HOST.search(h): off[h] = off.get(h, 0) + 1
            if off: md.append(f"- ⚠ 品目が名指しする国の統計と出どころが違う（4-41）：{sum(off.values())}件／{len(rs)}件が " + "・".join(f"{h} {n}件" for h, n in sorted(off.items(), key=lambda kv: -kv[1])[:4]) + "。その統計の値として使わず、所管（経産省・総務省・e-Stat）の表から取り直す")
        # 2026-11-06 4-43 走査の失敗から：品目「全国鍍金工業組合連合会 …」の数字が syntaxpartners.com・wakayamapp.jp（団体でないサイト）だけで、
        # wakayamapp.jp の1か所で幅795万倍になった。品目の主語が業界団体なのに、団体らしいドメイン（.or.jp・.go.jp・.lg.jp）以外から来た数字は、その団体の統計の値として使わない
        subj = item.split("（")[0]
        if re.search(r"(工業会|協会|連合会|組合)", subj):
            offa = {}
            for r in rs:
                h = urlparse(r["URL"]).netloc if r["URL"] else ""
                if h and not re.search(r"\.(or|go|lg)\.jp$", h): offa[h] = offa.get(h, 0) + 1
            if offa and sum(offa.values()) * 2 > len(rs):
                md.append(f"- ⚠ 品目が名指しする団体と出どころが違う疑い（4-43）：{sum(offa.values())}件／{len(rs)}件が " + "・".join(f"{h} {n}件" for h, n in sorted(offa.items(), key=lambda kv: -kv[1])[:4]) + "。団体の統計の値として使わず、団体自身の公表（.or.jp など）か国の表から取り直す")
        # 2026-11-06 4-42（日銀 WP24-J-17 の生産性の分解：非正規雇用の広がりで労働投入の測り方が動き、合併で企業間の再配分の項が動く）。
        # 引用に雇用形態・合併の語があれば、1人あたり・社数の向きを「内側の改善」と読む前に分母と構成を確かめる印を出す
        nemp = sum(1 for r in rs if EMPMIX.search(r["引用"])); nmer = sum(1 for r in rs if MERGER.search(r["引用"]))
        if nemp: md.append(f"- ⚠ 分母の雇用形態（パート・臨時・派遣・非正規）の語が{nemp}件：1人あたりの分母が常用だけか全員かを確かめ、混ぜて向きを出さない（4-42）")
        if nmer: md.append(f"- ⚠ 合併・統合・事業譲渡の語が{nmer}件：1社・1事業所あたりの向きは再配分・構成の変化を含む。内側の改善と書かない（4-42）")
        nrot = sum(1 for r in rs if SAMPLEROT.search(r["引用"])); npw = sum(1 for r in rs if POPWEIGHT.search(r["引用"]))
        if nrot: md.append(f"- ⚠ 抽出替え・ベンチマーク更新・復元・再集計の語が{nrot}件：その月・年の前後で値に段差が出る。段差をまたいだ向きを使わず、遡及・再集計の系列どうしで比べる（4-43）")
        if npw: md.append(f"- ⚠ 推計母集団労働者数・推計比率の語が{npw}件：集計の重みで人数の事実ではない。社員数・従業者数の欄に入れない（4-43）")
        nfx = sum(1 for r in rs if FXMIX.search(r["引用"]) and re.search(r"\d\s*(億円|万円|百万円|円)", r["引用"]))
        if nfx: md.append(f"- ⚠ 外貨と円の金額が同じ引用に{nfx}件：換算に使ったレートの時点（期中平均か取得日か）を確かめ、決算期の平均で換算した値だけを採る（4-51）")
        nreb = sum(1 for r in rs if REBASE.search(r["引用"]))
        if nreb: md.append(f"- ⚠ 指数の基準年・遡及の作り直しの語が{nreb}件：基準年の違う指数、遡って作り直した系列と当時の公表値を同じ欄でつながない。同じ版の系列どうしで向きを見る（4-51）")
        ntes = sum(1 for r in rs if TESTIMONY.search(r["引用"])); htes = {urlparse(r["URL"]).netloc for r in rs if TESTIMONY.search(r["引用"]) and r["URL"]}
        if ntes: md.append(f"- ⚠ 証言（関係者・取材・インタビュー）からの数字が{ntes}件・{len(htes)}か所：1人の証言は1件の手がかりで裏付けにならない。別の証言かデータと突き合わせるまで【未確認】のまま読む（4-46）")
        # 2026-11-06 4-44：誤差の幅・回収率・自己評価・委託先の変更・内部売上の印
        nse = sum(1 for r in rs if SAMPLEERR.search(r["引用"]))
        if nse: md.append(f"- 標準誤差・標本誤差の語が{nse}件：変化がその誤差の幅より小さければ向きにしない（4-44）")
        lowr = sorted({float(m.group(4)) for r in rs for m in LOWRESP.finditer(r["引用"]) if float(m.group(4)) < 50})
        if lowr: md.append("- ⚠ 回収率・回答率が5割未満（" + "・".join(f"{v:g}%" for v in lowr[:5]) + "）：答えた先と答えない先が違えば偏る（無回答の偏り）。全体の値として言い切らない（4-44）")
        nsr = sum(1 for r in rs if SELFREPORT.search(r["引用"]))
        if nsr: md.append(f"- ⚠ 自己評価・自己申告・自社調べの語が{nsr}件：答える側が良く見せる向きに偏りやすい。記録（統計・開示）と並べる（4-44）")
        nct = sum(1 for r in rs if CONTRACTOR.search(r["引用"]))
        if nct: md.append(f"- ⚠ 調査の委託先の変更の語が{nct}件：集め方が変わり系列の継続性が落ちる。その年を切れ目として扱う（4-44）")
        nis = sum(1 for r in rs if INTERSEG.search(r["引用"]))
        if nis: md.append(f"- ⚠ セグメント間・内部売上の語が{nis}件：外部の客への売上と内部の売上を分け、合計に内部を足さない（4-44）")
        # 2026-11-06 4-50（BMJ Open 2023 の登録と論文の食い違いのメタ分析：主要な結果の差し替えが3割前後の研究で起きていた）。
        # 品目が名指しした指標（加工賃・出荷額・付加価値・従業者・事業所数・受注・給与など）が、取れた引用のどれにも出てこなければ、
        # 別の指標で答えた（結果の差し替え）疑い。その数字で品目の問いに答えたと書かず、計画どおりの指標を取り直す
        named = [k for k in INDICATOR if k in subj]
        if named and rs and not any(any(k in r["引用"] for k in named) for r in rs):
            md.append(f"- ⚠ 品目が名指しした指標（{'・'.join(named)}）が引用に1件も無い：{len(rs)}件は別の指標の数字。品目の問いの答えとして使わず、名指しした指標を取り直す（結果の差し替えの疑い・4-50）")
        # 2026-11-06 4-45（iri-group 新規事業の市場調査：工場向けなら会社数と工場数のどちらを数えているかを確かめる）：
        # 同じ品目の数え上げに「社」と「事業所」が両方あれば、企業と事業所（工場）が混ざっている。1社が複数の工場を持つので、同じ欄として比べない
        ucnt = {}
        for r in rs:
            if r["区分"].startswith("事業所数"): ucnt[r["単位"]] = ucnt.get(r["単位"], 0) + 1
        if ucnt.get("社") and ucnt.get("事業所"): md.append(f"- ⚠ 企業と事業所が混在：数え上げに（社）{ucnt['社']}件と（事業所）{ucnt['事業所']}件。会社数と工場数は別の対象なので、割り算・比較は同じ単位どうしでだけ行う（4-45）")
        # 2026-11-06 4-45 走査の失敗から：めっきの「社員数・平均社員数」の欄に 5,626〜31,487人（業界全体の従業者数）が入り、1社の平均として読める形で出た。
        # 同じ品目の事業所数の中央値より「平均社員数」の中央値が大きければ、平均ではなく業界の総数の疑いとして印す（平均×事業所数＝総数なので、平均が事業所数を超えるのは中小の受託ではまず無い）
        emp = [r["値"] for r in rs if r["区分"].startswith("社員数") and r["単位"] == "人" and r["値"] > 0]
        est = [r["値"] for r in rs if r["区分"].startswith("事業所数") and r["単位"] in ("事業所", "社") and r["値"] > 0]
        if emp and est and statistics.median(est) >= 50 and statistics.median(emp) > statistics.median(est):
            md.append(f"- ⚠ 平均でなく総数の疑い：社員数の欄の中央値 {statistics.median(emp):g}人が、同じ品目の事業所数の中央値 {statistics.median(est):g}を超える。業界全体の人数として読み、1社あたりの平均にしない（4-45）")
            # 2026-11-06 走査の失敗から：別々の出典の中央値どうしを割り、「製造業の受託加工全体」で1事業所あたり8544.3人、EMS で2.5人と桁の外れた値が出た。
            # 割り算は同じ出典（ホスト）・同じ年の組がある時だけ行い、無ければ割らない。結果が中小の受託の桁（1〜1,000人）を外れれば桁違いとして印す（4-4 の桁の感覚・4-41 の欄どうしの整合）
            pe = {}
            for r in rs:
                if r["年"] and r["値"] > 0 and r["区分"].startswith("社員数") and r["単位"] == "人": pe.setdefault((r["出典"], str(r["年"])), {}).setdefault("e", []).append(r["値"])
                if r["年"] and r["値"] > 0 and r["区分"].startswith("事業所数") and r["単位"] in ("事業所", "社"): pe.setdefault((r["出典"], str(r["年"])), {}).setdefault("s", []).append(r["値"])
            pairs = sorted((k, statistics.median(v["e"]) / statistics.median(v["s"])) for k, v in pe.items() if v.get("e") and v.get("s"))
            if not pairs:
                md.append("  - 1事業所あたりは割らない：社員数と事業所数が同じ出典・同じ年で揃う組が0件。別々の出典の中央値どうしの割り算は桁が外れる（4-4・4-41）")
            for (h, y), q in pairs[:6]:
                odd = "・⚠ 桁違いの疑い（中小の受託の1〜1,000人を外れる）" if not (1 <= q <= 1000) else ""
                md.append(f"  - 【推定】同じ出典・同じ年で割った1事業所あたり（{h}・{y}年）：{q:.1f}人{odd}")
        # 2026-11-06 走査の失敗から（mfg_hiring_seg：品目「従業員300〜999人の製造業」に、規模を限らない全体の「新卒1人当たり約93.6万円」が入り、
        # 規模別の採用単価150.5万円として2回続けて出た）。品目が従業員規模を名指しするのに、引用に規模の区分が無い数字は、全体の数字の疑いとして件数を出す（4-5 の最後の行・4-7）
        if re.search(r"(従業員|従業者)\s*\d+\s*(人|名)?\s*([〜～~\-−]|以上|未満|以下)", subj):
            nosz = [r for r in rs if not r.get("規模")]
            if nosz:
                hz = sorted({r["出典"] for r in nosz})
                md.append(f"- ⚠ 品目の規模と引用の主語が合わない疑い：{len(nosz)}件／{len(rs)}件の引用に従業員規模の区分が無い（" + "・".join(hz[:4]) + "）。規模を限らない全体の数字として読み、この規模の値にしない")
        groups = {}
        for r in rs: groups.setdefault((r["区分"], r["文脈"], r["単位"]), []).append(r)
        # 2026-11-06 4-46 走査の失敗から：品目「標準誤差率と回収率」「毎月勤労統計 事業所規模別 現金給与総額」で取れたのは欄と単位の合わない混入だけだった。
        # どの欄も単位が合わない品目は「数字が取れた」と読まず、数字が1つも取れなかった品目と同じに並べる
        _mis = lambda cat, u: (cat.startswith("社員数") and u not in ("人",)) or (cat.startswith("事業所数") and u not in ("事業所", "社")) or (cat.startswith(("受注金額", "1人あたり")) and u in ("人", "事業所", "社", "台", "名"))
        if groups and all(_mis(c, u) for (c, _x, u) in groups): onlymix.append(item)
        for (cat, ctx, u), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(g) < 2 or u in ("件",): continue
            # 2026-11-05 走査の失敗から：「社員数・平均社員数」の欄に万円・%、「事業所数・社数」の欄に%が入り、【裏付けあり】と出た（mfg_capacity の中小企業実態基本調査・金型）。
            # 欄の型と単位が合わない数字は、その欄の事実にしない（4-8 の欄ごとの型の確認を走査にも当てる）。件数だけ出して確度は付けない
            # 2026-11-06 4-44 走査の失敗から：金型の取り直しの品目で「受注金額・加工賃収入」の欄に（事業所）が入り、幅12815倍になった。金額・1あたりの欄も金額の単位だけにする
            if (cat.startswith("社員数") and u not in ("人",)) or (cat.startswith("事業所数") and u not in ("事業所", "社")) or (cat.startswith(("受注金額", "1人あたり")) and u in ("人", "事業所", "社", "台", "名")):
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
            pmid = statistics.median([v for v in vals if v > 0] or [0])
            md.append(f"- {gr} {cat}［{ctx}］（{u}）：{len(g)}件・一次{len(prim)}件・出典{len({src_key(r) for r in g})}か所（調査会社は1つに束ねる）・予測値{len(fut)}件、幅 {lo:g}〜{hi:g}（食い違い{spread}）")
            md.append(f"  - 書き方（4-23）：{phrasing(gr, g)}／格付け（4-28・出どころA〜F×情報1〜6）：{admiralty(g, gr)}")
            fse = {}
            for r in g: fse[fse_of(r)] = fse.get(fse_of(r), 0) + 1
            # 2026-11-07 4-68（HR Fraternity「conflicting market research」：方法・標本数・利害を開示しない推計は注意して扱う）。
            # 調査会社の値があるのに、方法・標本・対象の開示の語を含む引用が1件も無ければ印を付ける
            _rc = [r for r in g if r["一次"] == "調査会社"]
            if _rc and not any(METHOD_DISC.search(r.get("引用", "")) for r in _rc):
                md.append(f"  - ⚠ 方法の開示が見えない推計 {len(_rc)}件（4-68）：標本数・調査方法・調査対象の語が引用に無い。方法を開示した版（報告書の概要・調査概要）を取るまで、採る値・裏付けにしない")
            md.append("  - 数字の種類（4-51・届出／言明／報道／推定）：" + "・".join(f"{k}{fse.get(k, 0)}" for k in ("届出", "言明", "報道", "推定")) + ("。届出0件：「〇〇によると」で書き、確かめた値のように書かない" if not fse.get("届出") else ""))
            # 2026-11-06 4-44 走査の失敗から：金型の従業者数で、金型工業会の「就労者12万人」（年なし・昔の値）と「約7万人」（年なし）と記事の2024年69,553人が
            # 同じ欄で【裏付けあり】になった。時点の違う値の一致・年の無い値は、同じ対象の裏付けではない（4-1 の①時点の違い）
            if gr == "【裏付けあり】":
                yrs = sorted({int(r["年"]) for r in g if r["年"]}); noy = sum(1 for r in g if not r["年"])
                # 2026-11-07 4-67（Simply Psychology「triangulation in qualitative research」：一致が1つの場面・1つの時点だけに乗っていないかを見る。
                # 共通の偏りを持つ出どころの一致は独立の裏付けに見えるだけ）。一致が1つの年・1つの規模区分だけなら、その年・その区分の水準の裏付けで、向きや他の区分には広げない
                szs = {r.get("規模") for r in g}
                if not noy and len(yrs) == 1 and len(g) >= 2:
                    md.append(f"  - ⚠ 1時点だけの裏付け（4-67）：一致した値が全部{yrs[0]}年。{yrs[0]}年の水準の裏付けで、向き・他の年には広げない")
                if len(szs) == 1 and next(iter(szs)) and len(g) >= 2:
                    md.append(f"  - ⚠ 1つの規模区分だけの裏付け（4-67）：全部「{next(iter(szs))}」。他の規模区分・業種全体の値として書かない")
                if noy or (yrs and yrs[-1] - yrs[0] >= 3):
                    md.append(f"  - ⚠ 時点が揃わない裏付け（4-44）：年の無い値{noy}件" + (f"・年の幅{yrs[0]}〜{yrs[-1]}年" if yrs else "") + "。同じ年どうしで一致を見るまで、出典を付けて言い切らない（「〇〇によると」で書く）")
            # 2026-11-05 4-25：証拠の量に引っ張られない（Heuer の競合仮説分析：大量の証拠に見えても出どころが偏れば強さではない）。
            # 4件以上の欄で1か所のホストが半分超を出していれば、件数でなく出どころの数で読むよう印す
            hc = {}
            for r in g: hc[src_key(r)] = hc.get(src_key(r), 0) + 1
            top_h, top_n = max(hc.items(), key=lambda kv: kv[1])
            if len(g) >= 4 and top_n * 2 > len(g): md.append(f"  - ⚠ 1か所に偏り：{len(g)}件のうち{top_n}件が {top_h}。件数ではなく出どころの数（{len(hc)}か所）で確度を読む")
            regs = sorted({region_of(r["URL"]) for r in g} - {""})
            if len(regs) >= 2: md.append(f"  - ⚠ 地域が違う出どころ（{'・'.join(regs[:5])}）：別々の自治体の統計は同じ対象の裏付けではない。地域ごとに別の品目として読む（4-20）")
            ec = echoes(g)
            ecv = {k: n for k, n in ec.items() if not isinstance(k, tuple)}
            ect = {k: n for k, n in ec.items() if isinstance(k, tuple)}
            if ecv: md.append("  - ⚠ 写しの疑い（一次でない別々の出どころが同じ値をぴったり持つ。1件に束ねて数えた）：" + "、".join(f"{v:g}{u}が{n}か所" for v, n in sorted(ecv.items())[:5]) + "。元（統計・開示・調査）までたどる")
            if ect: md.append(f"  - ⚠ 同じ文の写し（4-57）：別々のホストに頭60字まで同じ引用が{len(ect)}組（最大{max(ect.values())}か所）。同じ文書の版・転載として1件に束ねて数えた")
            # 2026-11-06 4-57：裏付けの種類（Denzin のトライアンギュレーションの型：方法内か方法間か。scholarcompass「トライアンギュレーションとは」）。
            # 一致した出どころが全部同じ種類（記事だけ、一次だけ）なら「方法内」の裏付けで、同じ見落としを共有しうる。種類の違う出どころ（統計と開示と報道）が合えば強い
            if gr == "【裏付けあり】":
                ak = {r["一次"] for r in g if pmid and abs(r["値"] - pmid) <= 0.2 * abs(pmid)}
                if len(ak) == 1: md.append(f"  - ⚠ 方法内の裏付け（4-57）：一致した出どころが全部「{next(iter(ak))}」。種類の違う出どころ（統計・開示・報道）で合うまで、強い裏付けとして書かない")
            if len({src_key(r) for r in g}) == 1: thin.append(f"{item}｜{cat}［{ctx}］")
            pk = pick(g)
            # 4-41：一次の数字の取り方（表か要約か）。一次が要約・報道発表だけなら表で取り直す
            tk = {}
            for r in prim: tk[take_kind(r["URL"]) or "不明"] = tk.get(take_kind(r["URL"]) or "不明", 0) + 1
            if prim and tk.get("要約") and not tk.get("表"): md.append(f"  - ⚠ 一次{len(prim)}件のうち{tk['要約']}件が統計の概要・報道発表のページから、表から0件：同じ数字を統計の表（xlsx・csv・e-Stat の表）で取り直す（4-41）")
            if pk:
                py, pv, pn, pd, preg = pk
                picks[f"{item}｜{cat}［{ctx}］（{u}）"] = {"年": py, "値": pv, "件": pn, "確度": gr,
                    "版": "・".join(sorted({r.get("版") for r in g if r.get("版")})), "出典数": len(pd),
                    "定義変更": bool(any(DEFCHANGE.search(r["引用"]) for r in g))}
                md.append(f"  - 採る値（規則：予測除外→一次優先→全国優先→確報優先→最新年→中央値）：{pv:g}{u}（{py or '年不明'}年・{pn}件・出典{len(pd)}か所）")
                if len(preg) >= 2: md.append(f"  - ⚠ 採る値に地域が混ざる（{'・'.join(sorted(preg)[:5])}）：この中央値は使わず、地域ごとに読む（4-30）")
                # 4-21：最新の年が3年以上前なら古い（Inven：公表データは古いことがある）。今の判断に使う前に新しい版を探す
                if py and NOW_YEAR - py >= 3: md.append(f"  - ⚠ 古い：採った値の最新の年が{py}年（{NOW_YEAR - py}年前）。新しい版・新しい年を探す")
                # 2026-11-06 4-48：鮮度は「今から何年」でなく「手に入る最新の版より古くないか」で見る（tickerplace の株式調査の手順：入力は直近の開示より古くしない）。
                # 一次優先で古い年を採ったとき、同じ品目に新しい年の値（予測を除く）があれば、その年の一次の版を探す印を出す
                ally = [int(r["年"]) for r in g if r.get("年") and not r.get("予測")]
                if py and ally and max(ally) > py:
                    md.append(f"  - ⚠ より新しい年がある：採った値は{py}年、同じ品目に{max(ally)}年の値（一次とは限らない）。{max(ally)}年の一次の版を探して差し替える（4-48）")
                # 2026-11-06 4-48：採る値が全体の中ほど半分の幅の外なら、代表値の選び方で結論が変わりうる（結論は基準の1点でなく幅で成り立つかを見る）
                if len(vals) >= 4:
                    q1, _, q3 = statistics.quantiles(vals, n=4)
                    if pv < q1 or pv > q3: md.append(f"  - ⚠ 採る値が中ほど半分の幅（{q1:g}〜{q3:g}）の外：代表値の選び方で結論が変わりうる。幅の両端で同じ結論になるかを確かめてから書く（4-48）")
                # 2026-11-07 4-77（FUKUSHIMA note「三角測量」：採った点を全部の観測に戻して誤差を測り、閾値の外の観測は印を付ける＝再投影の点検。
                # なば note「メタ・クロスチェック」：重大な外れの検出は1つでも反応したら拾う、値の採用は多数で決める）。
                # 独立の出どころ（src_key）ごとの代表値を採る値に戻し、0.5〜2倍の外の出どころを数える。一次が1つでも外れたら印（検出は OR）、
                # 外れた出どころが半分以上なら採る値そのものを疑う
                if pv and pv > 0:
                    _bys = {}
                    for r in g:
                        if r.get("予測") or not r.get("値") or r["値"] <= 0: continue
                        _bys.setdefault(src_key(r), []).append(r)
                    _out = [k for k, rs in _bys.items() if not (0.5 <= statistics.median(x["値"] for x in rs) / pv <= 2)]
                    _pout = [k for k in _out if any(x["一次"] == "一次" for x in _bys[k])]
                    if len(_bys) >= 2 and _out:
                        md.append(f"  - 再投影の点検（4-77）：独立の出どころ{len(_bys)}のうち採る値の0.5〜2倍の外{len(_out)}（うち一次{len(_pout)}）"
                                  + ("。⚠ 半分以上が外れる：採る値そのものを疑い、定義・単位・年の違い（4-1）を先に解く" if len(_out) * 2 >= len(_bys) else "")
                                  + ("。⚠ 一次が外れる：1つでも一次が外れたら食い違いとして解くまで【裏付けあり】と書かない" if _pout else ""))
                # 2026-11-06 4-49：最初に見つけた数字に引っ張られていないか（アンカリング。haurix「バイアスと競合仮説分析の心理学」）。
                # 採る値が最初に取得した日の値だけと同じで、あとの日に取った値の半分以上が採る値から2倍以上離れていれば、最初の数字に寄った疑いを出す
                dated = sorted((r for r in g if r.get("取得日")), key=lambda r: r["取得日"])
                if len(dated) >= 4 and pv:
                    d0 = dated[0]["取得日"]; first = {r["値"] for r in dated if r["取得日"] == d0}; later = [r["値"] for r in dated if r["取得日"] > d0]
                    far = [v for v in later if v and (v / pv >= 2 or pv / v >= 2)] if pv > 0 else []
                    if pv in first and later and len(far) * 2 >= len(later):
                        md.append(f"  - ⚠ 最初の数字に寄った疑い（アンカリング）：採る値は最初の取得日（{d0}）の値で、あとで取った{len(later)}件のうち{len(far)}件が2倍以上離れる。あとの値の定義・年を先に確かめる（4-49）")
            # 2026-11-06 4-49：市場の定義・範囲が違う数字を同じ欄で比べない（グロービス「マーケットシェア」：定義を変えたのに過去の数値と比べるのがよくある失敗／
            # 決算が複数事業の合算なら対象の売上を切り分ける）。引用に合算・含む・範囲の語があれば件数を印す
            # 2026-11-06 4-50（Authentic8「OSINT chain of custody」：画面の写しは見た時点の証拠で、元のページがいつ変わったかは示さない。
            # URL・取得日時・取り方を値ごとに残す）。同じURLが取得日の違う回で、この欄に1つも共通の値を持たなければ、ページの中身が変わった疑い。
            # どの日の版から取った値かを書き、古い日の値を今の事実として使わない
            byu = {}
            for r in g:
                if r.get("URL") and r.get("取得日"): byu.setdefault(r["URL"], {}).setdefault(r["取得日"], set()).add(r["値"])
            chg = [u0 for u0, dd in byu.items() if len(dd) >= 2 and not (dd[min(dd)] & dd[max(dd)])]
            if chg: md.append(f"  - ⚠ ページの中身が変わった疑い {len(chg)}URL（同じURLで最初と最後の取得日に共通の値が無い。例 {urlparse(chg[0]).netloc}）：どの取得日の版の値かを書き、古い版の値を今の事実にしない（4-50）")
            # 2026-11-06 4-53：上限・留保の語の付いた値と、集計の期間が違う値の混在
            hed = [r for r in g if HEDGE.search(r["引用"])]
            if hed and len(hed) < len(g): md.append(f"  - ⚠ 上限・留保つきの値 {len(hed)}件（最大・最高・理論上・ピーク時など）：端の値で典型ではない。採る値・中央値の根拠にせず、留保語を落として写していないかを本文で確かめる（4-53）")
            elif hed: md.append(f"  - ⚠ この欄は全件が上限・留保つきの値（{len(hed)}件）：「最大で」「理論上」を付けて書き、典型の値として書かない（4-53）")
            # 2026-11-07 4-72（同上：「最大30%」と「平均30%」は別の主張）。同じ欄に平均・中央値の値と上限・下限の値が並んだら、別の主張として分けて読む
            _avg = [r for r in g if re.search(r"(平均|中央値|一般的に|標準的)", r["引用"]) and not HEDGE.search(r["引用"])]
            if hed and _avg: md.append(f"  - ⚠ 平均の値 {len(_avg)}件と上限・下限の値 {len(hed)}件が同じ欄に混在：別の主張として分け、上限の値で平均を裏付けない（4-72）")
            per = {}
            for r in g:
                k0 = period_of(r["引用"])
                if k0: per[k0] = per.get(k0, 0) + 1
            if len(per) >= 2: md.append("  - ⚠ 集計の期間が違う値が混在（" + "・".join(f"{k}{n}件" for k, n in per.items()) + "）：週・月・年・累計をそろえてから突き合わせ、中央値を取らない（4-53）")
            pst = [r for r in prim if STATSRC.search(r["引用"] + r["URL"]) and not STATTABLEID.search(r["URL"])]
            if pst: md.append(f"  - ⚠ 表の番号が引けない統計の一次 {len(pst)}件：URLが統計表（e-Stat の表番号・xlsx・csv）を指していない。資料名・統計表番号・対象年・URLの4点で取り直す（4-53）")
            mix = [r for r in g if SCOPEMIX.search(r["引用"])]
            if mix: md.append(f"  - ⚠ 範囲が違う数字の疑い {len(mix)}件（合算・〜を含む・範囲の語）：何を含む数字かをそろえてから突き合わせる。合算の決算は対象の事業を切り分ける（4-49）")
            if gr in ("【食い違い】", "【未確認】") and len(vals) >= 2:
                md.append(f"  - 【推定】保守側の値（低い側に寄せる・低確度）：{conservative(vals):g}{u}")
            # 2026-11-07 4-60（SellersGPT「食い違う市場データを確かさを作らずに突き合わせる」：食い違いは誤りの印でなく診断の手がかり。
            # 元の値を残したまま、どの次元（期間・定義・範囲・版・方法）で食い違うかを先に名指しする）。
            # 中央値より下の値の組と上の値の組で、年・定義・版・規模・出どころの種類・予測の印の集合が重ならない次元を出す
            if gr == "【食い違い】" and len(g) >= 2:
                pv = sorted(r["値"] for r in g if r["値"] > 0)
                if pv:
                    md0 = statistics.median(pv)
                    lo_g = [r for r in g if 0 < r["値"] <= md0]; hi_g = [r for r in g if r["値"] > md0]
                    dims = []
                    for fld, lab in (("年", "期間"), ("定義", "定義"), ("版", "版"), ("規模", "規模の範囲"), ("一次", "出どころの種類"), ("予測", "実績か見通しか")):
                        a0 = {r.get(fld) or "（無印）" for r in lo_g}; b0 = {r.get(fld) or "（無印）" for r in hi_g}
                        if lo_g and hi_g and not (a0 & b0): dims.append(f"{lab}（低い側 {'/'.join(sorted(a0))[:40]}｜高い側 {'/'.join(sorted(b0))[:40]}）")
                    DIMSPLIT.setdefault(topic, [0, 0])[0 if dims else 1] += 1
                    md.append("  - 食い違いの次元（4-60）：" + ("、".join(dims) + "で分かれる。そろう組どうしでだけ比べる" if dims else "期間・定義・版・規模・種類では分かれない。同じ対象かと単位を本文で確かめ、解けるまで両端を残す"))
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
            # 2026-11-07 4-63（ネクストイノベーションベース「市場規模の調べ方」：食い違う数字は対象・地域・時点・取引段階・単位・推計方法を横に並べる。
            # 世帯支出と小売販売額、出荷と販売は同じ取引段階ではない。税込・税抜もそろえる）。引用の語から取引段階と税の扱いを拾い、2つ以上混ざれば印す
            _stg = {}; _tax = {}
            for r in g:
                q0 = r.get("引用", "")
                for lab, rx in (("生産・出荷", r"出荷|生産額|生産高"), ("卸", r"卸売|卸"), ("小売・販売", r"小売|販売額|売上高"), ("支出・購入", r"支出|購入額|消費額"), ("輸出入", r"輸出|輸入")):
                    if re.search(rx, q0): _stg[lab] = _stg.get(lab, 0) + 1; break
                if re.search(r"税込|消費税込", q0): _tax["税込"] = _tax.get("税込", 0) + 1
                elif re.search(r"税抜|税別", q0): _tax["税抜"] = _tax.get("税抜", 0) + 1
            if len(_stg) >= 2: md.append("  - ⚠ 取引段階が混在（4-63）：" + "、".join(f"{k}{v}件" for k, v in _stg.items()) + "（同じ段階どうしで比べる。上の段階の総額を対象の市場にしない）")
            if len(_tax) >= 2: md.append("  - ⚠ 税込と税抜が混在（4-63）：" + "、".join(f"{k}{v}件" for k, v in _tax.items()))
            if len(dc) >= 2: md.append("  - ⚠ 定義が混在：" + "、".join(f"{k}{v}件" for k, v in dc.items()) + "（同じ定義どうしで比べる）")
            vc = {}
            for r in g:
                if r.get("版"): vc[r["版"]] = vc.get(r["版"], 0) + 1
            # 2026-11-07 4-62（Koenig・Dolmas・Piger「real-time data」：系列の中で、ある年は最新の改定値、ある年は速報と版がばらばらだと、
            # 向きに改定の差が混ざる。年ごとに同じ版（すべて確報、またはすべてその時点の初出）でそろえてから向きを読む）
            yv = {}
            for r in g:
                if r["年"] and not r["予測"] and r.get("版"): yv.setdefault(int(r["年"]), set()).add(r["版"])
            _vs = {y: ("確報・改定" if (v & {"確報", "改定"}) else "速報") for y, v in yv.items()}
            if len(set(_vs.values())) >= 2:
                md.append("  - ⚠ 年ごとの版がそろわない（4-62）：" + "、".join(f"{y}年＝{k}" for y, k in sorted(_vs.items())) + "。速報の年は確報が出るまで向きの端に使わない")
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
            if med and u in ("事業所", "社", "件", "台"):
                cb = chance_band(med)
                if cb and cb[0]: md.append(f"  - ⚠ 偶然の幅の中（4-44）：{cb[4]}年 {cb[1]:g}→{cb[5]}年 {cb[2]:g} の差は偶然のばらつきの目安（2×√合計＝{cb[3]:.1f}）以内。小さい件数の増減で向きを言い切らない")
            cg = cagr_of(med) if med else None
            if cg is not None and u in ("万円", "人", "事業所", "社"): RATES.setdefault(topic, []).append((f"{item}｜{cat}［{ctx}］（{u}）", u, cg))
            es = end_sensitivity(med) if med else None
            if es: md.append(f"  - ⚠ 端の1点に依存：{es}向きの語が変わる。端の年の値（速報・改訂前・1か所だけ）を本文で確かめるまで向きを言い切らない（4-40）")
            xs = extreme_start(med) if med else None
            if xs: md.append(f"  - ⚠ 起点が全期間の{xs}（4-64）：最初の年が系列の{'最大' if xs == '山' else '最小'}。向きの一部は平均への回帰かもしれないので、起点を1つずらした向きと並べて書く")
            dm = dir_magnitude(g)
            if dm: md.append(f"  - ⚠ {dm}。幅で書き、ずれの理由（定義・地域・取り方）を添える（4-40）")
            # 2026-11-06 4-40：二つのしきい値（Joffe ら 2014 JAMIA の重複照合：上のしきい値より上は一致、下より下は不一致、間は人が確かめる）。
            # 中央値±20%（一致）と3倍超（食い違い）の間にある値は、機械で一致とも食い違いとも決めず、件数を出して本文で確かめる
            mid_n = sum(1 for v in vals if pmid and v > 0 and 1.2 < max(v / pmid, pmid / v) <= 3)
            if mid_n: md.append(f"  - 中間帯（中央値の1.2〜3倍・機械で決めない）：{mid_n}件。本文で定義・年・地域を確かめる（4-40）")
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
        # 2026-11-06 4-41：規模区分ごとの1人あたりの「山」（peppereffect の1人あたり売上の規模別ベンチマーク：効率は規模に比例せず中ほどの帯で山になり、
        # 大きい先は管理の層で下がる。帯だけでは弱い信号）。大きいほど高いと決めず、どの区分が最大かを機械で出す
        def _lb(band):
            m0 = re.search(r"\d+", band or ""); return int(m0.group()) if m0 else 10**9
        pk_line = []
        for (y, reg), bands in sorted(opd.items()):
            if len(bands) < 3: continue
            order = sorted(bands, key=_lb); pp = [bands[b0][0] / bands[b0][1] for b0 in order]
            top = max(range(len(pp)), key=lambda i0: pp[i0])
            shape = "規模とともに上がる" if all(pp[i0] <= pp[i0 + 1] for i0 in range(len(pp) - 1)) else "規模とともに下がる" if all(pp[i0] >= pp[i0 + 1] for i0 in range(len(pp) - 1)) else ("中ほどの区分が山" if 0 < top < len(pp) - 1 else "向きが揃わない")
            pk_line.append(f"{y}年・{reg}：最大は{order[top]}（{shape}）")
        if pk_line: md.append("  - 【推定】規模区分ごとの1人あたりの形（4-41。大きいほど高いと決めない）：" + "、".join(pk_line))
        # 2026-11-06 4-41：欄どうしの整合（Crystal Lewis のデータ清掃：同じ対象の別の欄が互いに合うかを確かめる）。
        # 同じ品目・同じ年で 人数の中央値 ÷ 事業所数の中央値 が1未満なら、事業所数と人数のどちらかが別物（総数と1あたり、全国と県）の混入
        ppl = {}; est = {}
        for r in rs:
            if not r["年"] or r["予測"] or "あたり" in r["区分"]: continue
            if r["単位"] == "人" and r["区分"].startswith("社員数"): ppl.setdefault(int(r["年"]), []).append(r["値"])
            if r["単位"] in ("事業所", "社") and r["区分"].startswith("事業所数"): est.setdefault(int(r["年"]), []).append(r["値"])
        bad = [y for y in ppl if y in est and statistics.median(est[y]) > 0 and statistics.median(ppl[y]) < statistics.median(est[y])]
        if bad: md.append("  - ⚠ 欄どうしが合わない（4-41）：" + "、".join(f"{y}年" for y in sorted(bad)) + "は人数の中央値が事業所数の中央値より小さい（1事業所1人未満）。どちらかの欄に別物が混ざっている。結論に使わない")
        md.append("")
    # 2026-11-05 4-19：何が見つからなかったかを書く（範囲の明示）。数字が1つも取れなかった品目を並べる
    names = set()
    for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
        try: names.add(json.load(open(f, encoding="utf-8")).get("name", ""))
        except Exception: pass
    empty = sorted(n for n in names - set(by_item) if n)
    empty += [f"{n}（取れたのは欄と単位の合わない混入だけ・4-46）" for n in onlymix]
    # 2026-11-07 4-65（ひぐらし司法書士 note「会社の公告方法」・isa99999 note「非上場会社の決算公告」）：合同会社・合名会社・合資会社（持分会社）は
    # 決算公告の義務が無い。決算公告・官報の品目で空でも「欠け」に数えず、印を付けて分ける
    empty = [f"{n}（持分会社：決算公告の義務なし・欠けに数えない・4-65）" if MOCHIBUN.search(n) and NORMAL_ABSENT_SCAN.search(n) else n for n in empty]
    # 2026-11-07 4-59（deskrex「学会発表前の引用ネットワーク調査」：PRISMA-S の考え方で、調べる仕様（inputs）と実行の記録（companies/*.json）を
    # 突き合わせ、仕様にあって一度も回っていない品目と、仕様から消えたのに結果だけ残る品目を分けて出す。数字0件と「未実行」を混ぜない）
    spec = set()
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "inputs", f"{topic}.csv"), encoding="utf-8-sig") as fh:
            spec = {(r.get("品目") or r.get("name") or "").strip() for r in csv.DictReader(fh)} - {""}
    except Exception: pass
    notrun = sorted(spec - names); orphan = sorted(n for n in names - spec if n) if spec else []
    # 2026-11-07 4-59（グロービス「名寄せとは」：年度のラベルだけで機械的に比べず、決算期・対象期間・基準日をそろえる）。
    # 同じ品目・同じ年に「年度」の引用と暦年（「年末」「年平均」「暦年」「1〜12月」）の引用が並ぶ欄を数える。向きや食い違いを読む前に期間をそろえる
    fymix = {}
    for r in rows:
        if not r["年"]: continue
        q = r["引用"]
        k = "年度" if "年度" in q else ("暦年" if re.search(r"(年末|年平均|暦年|1\s*[〜~～-]\s*12\s*月|年間累計)", q) else None)
        if k: fymix.setdefault((r["品目"], r["年"]), set()).add(k)
    fymix = sorted(f"{it}（{y}年）" for (it, y), s in fymix.items() if len(s) > 1)
    # 2026-11-05 4-24：記事・調査会社が一次を名指しして紹介している数字。元の統計・開示を取り直すまで「記者が書いたこと」として扱う
    # 2026-11-06 4-44：比較の相手（Stray：増えた・減っただけでは原因も意味も言えない。比べる相手＝起きなかった世界の近似が要る）。
    # 同じテーマ・同じ単位の欄の年率（複利）の中央値を「相手」にして、各欄の年率との差（差の差）を出す。単位が違う欄どうしは比べない
    cmp_lines = []
    byu_r = {}
    for k, u0, cg in RATES.get(topic, []): byu_r.setdefault(u0, []).append((k, cg))
    for u0, lst in sorted(byu_r.items()):
        if len(lst) < 3: continue
        base = statistics.median(c for _, c in lst)
        for k, cg in sorted(lst, key=lambda t: -(t[1] - base))[:12]:
            cmp_lines.append(f"- 【推定】{k}：年率{cg * 100:+.1f}%／同じ単位の{len(lst)}欄の中央値{base * 100:+.1f}%／差{(cg - base) * 100:+.1f}ポイント")
    md += ["## 比較の相手（同じ単位の欄の年率の中央値との差・4-44）", *(cmp_lines or ["（3欄以上そろった単位なし）"]), ""]
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
                # 2026-11-06 4-42（Bundesbank DP 32/2004・FRB FEDS 2020-102：改訂は「定義の変更」と「新しい情報」で出どころが違い、
                # その時点の推計と事後の推計は大きく違いうる）。変わった欄に、変わった理由の型を機械で付ける
                why = ("定義・版の変更" if v.get("定義変更") or (v.get("版") and v.get("版") != o.get("版")) else
                       "年が新しくなった" if v["年"] != o.get("年") else
                       "出どころの数が変わった" if o.get("出典数") is not None and v.get("出典数") != o.get("出典数") else "同じ年・同じ出どころ数で値が動いた（取り違え・混入を疑う）")
                ch.append(f"- 変わった：{k} {o.get('値', 0):g}（{o.get('年') or '年不明'}年）{o.get('確度', '')} → {v['値']:g}（{v['年'] or '年不明'}年）{v['確度']}［理由の型：{why}］")
        ch += [f"- 消えた：{k}" for k in sorted(set(prev) - set(picks))]
    # 2026-11-06 4-43（OECD の改訂分析の指針：平均改訂＝片寄りの向き、平均絶対改訂＝大きさ、上方・下方・ゼロの割合）。
    # 同じ欄・同じ年のまま値が動いた分を毎回 revisions.json に積み、改訂が一方向に偏っていないか（早い値が過小・過大か）を出す
    rf = os.path.join(OUT, topic, "revisions.json")
    try: revs = json.load(open(rf, encoding="utf-8"))
    except Exception: revs = []
    if prev is not None:
        for k, v in picks.items():
            o = prev.get(k)
            if o and o.get("値") and o.get("年") == v["年"] and v["値"] != o["値"]:
                revs.append({"欄": k, "年": v["年"], "前": o["値"], "後": v["値"], "率": (v["値"] - o["値"]) / abs(o["値"])})
    revs = revs[-2000:]
    json.dump(revs, open(rf, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    rv = [x["率"] for x in revs if abs(x["率"]) < 100]
    if rv:
        mr = statistics.mean(rv); mar = statistics.mean(abs(x) for x in rv)
        up = sum(1 for x in rv if x > 0); dn = sum(1 for x in rv if x < 0)
        # 2026-11-06 4-46（アイルランド中銀の四半期国民経済計算の改訂の分析）：平均改訂が0でないことは、偶然の幅を超えるか
        # （平均改訂÷標準誤差＝t が2以上）を見てから片寄りと呼ぶ。直しが一度でも起きた割合を「改訂の起きやすさ」の粗い目安として出す
        tval = (mr / (statistics.stdev(rv) / len(rv) ** 0.5)) if len(rv) >= 2 and statistics.stdev(rv) > 0 else 0.0
        sig = abs(tval) >= 2
        verdict = ("（5件未満：判定しない）" if len(rv) < 5 else
                   "⚠ 上に直る側に片寄り（偶然の幅を超える）：早い値は過小に出やすい。最初の値で向きを言い切らない" if sig and mr > 0 and abs(mr) > 0.5 * mar else
                   "⚠ 下に直る側に片寄り（偶然の幅を超える）：早い値は過大に出やすい。最初の値で向きを言い切らない" if sig and mr < 0 and abs(mr) > 0.5 * mar else
                   "向きの片寄りに見えるが偶然の幅の中（|t|<2）：片寄りと書かない" if abs(mr) > 0.5 * mar else
                   "片寄りは目立たない（上下の直しが打ち消し合う）")
        md += ["## 改訂の分析（同じ欄・同じ年で値が直った分・4-43）",
               f"- 件数 {len(rv)}／平均改訂（向き）{mr:+.1%}／平均絶対改訂（大きさ）{mar:.1%}／上方 {up / len(rv):.0%}・下方 {dn / len(rv):.0%}・ゼロ {1 - (up + dn) / len(rv):.0%}／片寄りの t {tval:+.2f}（|t|≥2 で偶然の幅を超える・4-46）",
               f"- {verdict}"]
        # 2026-11-06 4-52（rOpenSci reviser：改訂の要約は平均だけでなく分位・ばらつき・続けて同じ向きに直るか（系列相関）を出し、
        # 何回目の公表から先は直しが小さくなるか（効率的な版）を選ぶ）。欄×年ごとに直しの順番を数え、順番ごとの平均絶対改訂を出す
        if len(rv) >= 5:
            q = statistics.quantiles(rv, n=10)
            md += [f"- 改訂の分位（4-52）：下10% {q[0]:+.1%}／中央 {statistics.median(rv):+.1%}／上10% {q[-1]:+.1%}。中央と平均が大きく離れたら、少数の大きな直しが平均を動かしている"]
        seq = {}
        for x in revs:
            if abs(x["率"]) < 100: seq.setdefault((x["欄"], x["年"]), []).append(x["率"])
        byord = {}
        for v in seq.values():
            for i, r_ in enumerate(v): byord.setdefault(min(i + 1, 3), []).append(abs(r_))
        if byord:
            md += ["- 直しの順番ごとの平均絶対改訂（4-52・効率的な版）：" + "、".join(f"{'3回目以降' if k == 3 else f'{k}回目'} {statistics.mean(v):.1%}（{len(v)}件）" for k, v in sorted(byord.items()))
                   + "。後の回で小さくなっていれば、その回の値から採る。小さくならなければ版を待っても締まらない"]
        # 2026-11-07 4-65（ONS「GDP revisions in Blue Book 2026」：不確かな時期（コロナ期）や方法の変わった年は直しが大きく、
        # 期間を通した平均は時期で変わる。同じ年が何年にもわたって直り続けることがある）。①実績の年で2020年前と以後に分けた平均絶対改訂
        # ②同じ欄・同じ年で3回以上直った欄の、最初の値から最新の値までの累積の直し
        per = {}
        for x in revs:
            try: yy = int(str(x.get("年") or "")[:4])
            except Exception: continue
            if abs(x["率"]) < 100: per.setdefault("2019年以前" if yy < 2020 else "2020年以後", []).append(abs(x["率"]))
        if per:
            md += ["- 時期で分けた平均絶対改訂（4-65）：" + "、".join(f"{k} {statistics.mean(v):.1%}（{len(v)}件）" for k, v in sorted(per.items()))
                   + "。片方が大きければ、その時期の値は早い版で言い切らない"]
        cum = {}
        for x in revs:
            if abs(x["率"]) < 100: cum.setdefault((x["欄"], x["年"]), []).append(x)
        many = sorted(((k, v) for k, v in cum.items() if len(v) >= 3), key=lambda kv: -len(kv[1]))
        if many:
            md += ["- 3回以上直った欄の累積の直し（4-65・直り続ける欄は確度を1段下げる）："] + [
                f"  - {k[0]}（{k[1] or '年不明'}年）{len(v)}回：{v[0]['前']:g}→{v[-1]['後']:g}（累積 {(v[-1]['後'] - v[0]['前']) / abs(v[0]['前']):+.1%}）"
                for k, v in many[:10] if v[0]["前"]]
        # 2026-11-07 4-66（ESS guidelines on revision policy：誤りの訂正は「ふだんの直しの幅」と比べて大きさを判定し、
        # 理由を添えて出す。版の識別が無い直しは改訂の分析に混ぜない／ai_love_009 note：本文にない年を補わない）。
        # ①ふだんの幅（絶対改訂の中央値）の3倍を超える直しを、誤りの訂正か方法の変更の疑いとして一覧にする
        # ②年の無い欄の直しは、時点違いの入れ替わりと区別できないので件数を出し、本文の改訂の分析では言い切らない
        if len(rv) >= 5:
            medabs = statistics.median(abs(x) for x in rv)
            big = [x for x in revs if abs(x["率"]) < 100 and medabs > 0 and abs(x["率"]) > 3 * medabs]
            if big:
                md += [f"- ふだんの直しの幅（絶対改訂の中央値 {medabs:.1%}）の3倍を超える直し {len(big)}件（4-66・誤りの訂正か方法・定義の変更の疑い。理由の型を確かめるまで前後の値で向きを比べない）："] + [
                    f"  - {x['欄'][:80]}（{x.get('年') or '年不明'}年）{x['前']:g}→{x['後']:g}（{x['率']:+.1%}）" for x in sorted(big, key=lambda x: -abs(x["率"]))[:8]]
        noyr = sum(1 for x in revs if not x.get("年"))
        if noyr:
            md += [f"- 年の無い欄の直し {noyr}件／{len(revs)}件（4-66）：時点の違う値の入れ替わりかもしれず、改訂とは言い切れない。年を本文で取れるまで、片寄り・大きさの判断はこの分を除いて読む"]
        pairs =[(v[i], v[i + 1]) for v in seq.values() for i in range(len(v) - 1)]
        if len(pairs) >= 5 and len({a for a, _ in pairs}) > 1 and len({b for _, b in pairs}) > 1:
            rho = statistics.correlation([a for a, _ in pairs], [b for _, b in pairs])
            md += [f"- 続けての直しの相関（4-52）：{rho:+.2f}（{len(pairs)}組）。" + ("正で大きい（0.3以上）：同じ欄が同じ向きに直り続ける。途中の値で言い切らず、直りが止まるまで待つ" if rho >= 0.3 else "負で大きい（-0.3以下）：直しが行き過ぎて戻る。直後の値を採らない" if rho <= -0.3 else "目立たない")]
        md += [""]
    md += ["## 前回からの変化（採る値・確度・年。4-38）", *(ch[:40] or (["（変化なし）"] if prev is not None else ["（初回。次の実行から比べる）"])), *([f"- ほか{len(ch) - 40}件"] if len(ch) > 40 else []), ""]
    json.dump(picks, open(pf, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    # 2026-11-05 4-39：同じ文の中の総額と人数（例「年間の中途採用費用総額は平均650.6万円、採用人数の平均は20.8人」）から
    # 1人あたりを割り出す。同じ文・同じ出どころの2つの数字なので、別の出典をまたぐ割り算より桁違いが起きにくい（1章の割り出し）
    sq = {}
    for r in rows:
        if r["予測"]: continue
        sq.setdefault((r["品目"], r["URL"], r["引用"]), {}).setdefault(r["単位"], []).append(r["値"])
    sl = []
    for (it, url, q), byu in sq.items():
        if len(byu.get("万円", [])) == 1 and len(byu.get("人", [])) == 1 and byu["人"][0] > 0 and "総額" in q:
            sl.append(f"- 【推定】{it}｜{byu['万円'][0]:g}万円÷{byu['人'][0]:g}人＝{byu['万円'][0] / byu['人'][0]:,.1f}万円/人（同じ文の総額÷人数）｜{urlparse(url).netloc}：{q[:60]}")
    md += ["## 同じ文の中の割り出し（総額÷人数・4-39）", *(sl[:30] or ["（なし）"]), ""]
    # 2026-11-07 4-79（ivaluate「Calendarization in Comps」：決算期が違う値は実績の月と見込みの月の合成になり、見込みの割合で確かさが変わる。
    # データビズラボ「国勢調査データのクレンジング手順」：区分・地域コードが年で変わる・秘匿や不詳を埋めない・総数と突き合わせる。
    # Q「Data preparation」：割合は無回答を母数に含むかで値が変わる（再ベース）・問の文言の版が変わると同じ系列にならない）。
    # ①同じ品目・同じ年・同じ単位に実績と見込み（予測・見通し）の値が並ぶ欄 ②区分・定義・コードの変更や合併に触れた引用
    # ③無回答・不明・わからないを含む割合の引用（母数の取り方を確かめるまで他の割合と並べない） ④問の文言・設問の変更に触れた引用
    _mix = {}
    for r in rows:
        if r.get("年"): _mix.setdefault((r["品目"], r["年"], r["単位"]), set()).add("見込" if r.get("予測") else "実績")
    _mix = sorted(f"{it}（{y}年・{u}）" for (it, y, u), v in _mix.items() if len(v) > 1)
    _RECODE = re.compile(r"(区分(の)?(変更|見直し|改定)|定義(の)?(変更|見直し)|分類(の)?(改定|変更)|市町村合併|コード(の)?(変更|改定)|組み替え|組替)")
    _NOANS = re.compile(r"(無回答|不明|わからない|分からない|don.?t know|no answer)", re.I)
    _QWORD = re.compile(r"(設問(の)?(変更|見直し)|質問(文|項目)?(の)?(変更|見直し)|調査票(の)?(変更|改定|見直し)|選択肢(の)?(変更|見直し))")
    _rc = sorted({r["品目"] for r in rows if _RECODE.search(r.get("引用", ""))})
    _na = sorted({r["品目"] for r in rows if r["単位"] in ("%", "％") and _NOANS.search(r.get("引用", ""))})
    _qw = sorted({r["品目"] for r in rows if _QWORD.search(r.get("引用", ""))})
    md += ["## 期間・区分・母数の点検（4-79）",
           f"- 同じ品目・同じ年・同じ単位に実績と見込みの値が並ぶ欄：{len(_mix)}件（見込みの値を実績の向きに混ぜない。決算期の違う会社は実績の月と見込みの月の内訳を書く）", *(f"  - {n}" for n in _mix[:15]),
           f"- 区分・定義・分類・地域コードの変更や合併に触れた引用のある品目：{len(_rc)}件（変更の前後で一致する範囲だけを比べる・増減率を出す前に区分が両年で同じかを見る）", *(f"  - {n}" for n in _rc[:15]),
           f"- 無回答・不明を含む割合の引用のある品目：{len(_na)}件（母数に無回答を含むか除くかを確かめるまで、他の調査の割合と並べない）", *(f"  - {n}" for n in _na[:15]),
           f"- 設問・調査票・選択肢の変更に触れた引用のある品目：{len(_qw)}件（変更の前後を同じ系列につながない）", *(f"  - {n}" for n in _qw[:10]), ""]
    # 2026-11-07 4-80（Sourcescrub「How to Find Private Company Revenue Information」：人数×平均年収は人件費を賄う最低額で売上の下限、
    # 販売数量×単価は割引・卸値を無視して上に振れ、上場の同業の比は非上場では上限。中小企業白書2020 第1部2章2節：労働生産性は平均でなく
    # 上位10%・中央値・下位10%で比べ、同じ規模の中のばらつきが規模の間の差より大きいことがある。IJNet「Tabula」：表の取り出しは文字の層のある PDF だけ）。
    # ①下限・上限の型の推定（点の推定として採らない） ②平均だけの1人あたり・生産性（分位・中央値が無い） ③分母の書かれていない1人あたり ④OCR 由来の引用
    _LOWB = re.compile(r"(人件費|給与(総額|支払)|賃金総額|平均年収)[^。]{0,30}(売上|売り上げ)|(売上|売り上げ)[^。]{0,30}(人件費|給与(総額|支払)|賃金総額)")
    _UPB = re.compile(r"(販売(数|台数|個数)|出荷(数|台数|個数)|生産台数)[^。]{0,30}(単価|価格|円/個|円/台)|上場(企業)?(の)?(同業|類似)[^。]{0,20}(比|倍率|比率)")
    _PER = re.compile(r"(1人(当|あ)たり|一人(当|あ)たり|従業(者|員)1人|労働生産性|per (employee|worker))", re.I)
    _DIST = re.compile(r"(中央値|上位\s*\d+\s*[%％]|下位\s*\d+\s*[%％]|分位|パーセンタイル|ばらつき|分布|median|percentile)", re.I)
    _DEN = re.compile(r"(従業者|従業員|社員|就業者|常用|雇用者|頭数|常勤換算|FTE|人員)", re.I)
    _lb = sorted({r["品目"] for r in rows if _LOWB.search(r.get("引用", ""))})
    _ub = sorted({r["品目"] for r in rows if _UPB.search(r.get("引用", ""))})
    _per = [r for r in rows if _PER.search(r.get("引用", ""))]
    _avg_only = sorted({r["品目"] for r in _per if "平均" in r["引用"] and not _DIST.search(r["引用"])})
    _noden = sorted({r["品目"] for r in _per if not _DEN.search(r["引用"])})
    _ocr = sorted({r["品目"] for r in rows if "[OCR]" in r.get("引用", "") or "【OCR】" in r.get("引用", "")})
    md += ["## 推定の向き・分布・分母・取り方の点検（4-80）",
           f"- 人件費・年収から売上に触れた引用のある品目：{len(_lb)}件（売上の下限。点の推定として採らず、幅の下の端にだけ置く）", *(f"  - {n}" for n in _lb[:10]),
           f"- 数量×単価・上場の同業比から規模に触れた引用のある品目：{len(_ub)}件（割引・卸値・非上場の割り引きで上に振れる。幅の上の端にだけ置く）", *(f"  - {n}" for n in _ub[:10]),
           f"- 1人あたり・生産性の引用 {len(_per)}行のうち、平均だけで分位・中央値の無い品目：{len(_avg_only)}件（規模の差より規模の中のばらつきが大きいことがある。規模別の向きを書く前に分位を取る）", *(f"  - {n}" for n in _avg_only[:15]),
           f"- 1人あたりの引用に分母（従業者・従業員・常用など）の書かれていない品目：{len(_noden)}件（分母が分かるまで他の1人あたりと並べない・4-70）", *(f"  - {n}" for n in _noden[:15]),
           f"- 画像の PDF を OCR で読んだ引用のある品目：{len(_ocr)}件（OCR は桁・小数点・カンマを取り違える。原表で数字を確かめるまで【事実】に上げない）", *(f"  - {n}" for n in _ocr[:10]), ""]
    # 2026-11-07 4-81（兵庫県「経済構造実態調査（製造業事業所調査）の概要・利用上の注意」令和5年：2022年以降は出荷額の上位から累積9割の事業所を
    # 調べて全体を推計する・従業者4〜29人の付加価値は粗付加価値で30人以上と算式が違う・出荷額は消費税込みに補正し旧年は当時の税率のまま・
    # 1〜2事業所の欄と差引きで分かる欄は X・四捨五入で内訳と合計が合わない・従業上の地位の区分が無期／有期に変わった。
    # wphillipmoore「ICD 203 tradecraft standards」：検索ごとに当たった件数と採った件数を残し、一次の検索が0件なら省かず「不達」と書く）。
    # ①秘匿（X）に触れた引用 ②粗付加価値・付加価値の算式に触れた引用 ③消費税の込み・抜きに触れた引用と、円の系列が税率の変わった年（2014・2019）をまたぐ品目
    # ④抽出・推計の調査に触れた引用 ⑤従業上の地位・雇用区分の変更に触れた引用
    _HIDE = re.compile(r"(秘匿|(?<![A-Za-z])X(?![A-Za-z])\s*(は|：|=|＝)|「X」|“X”|数値を(秘匿|伏せ))")
    _GVA = re.compile(r"(粗付加価値|付加価値額?の(算式|計算|定義)|4\s*[〜～~-]\s*29\s*人)")
    _TAX = re.compile(r"(消費税(込|抜|を含|を除|額を含)|税込|税抜|内税|外税)")
    _SAMP = re.compile(r"(上位[^。]{0,10}9\s*割|累積[^。]{0,10}(9割|90\s*[%％])|裾切り|抽出(調査)?[^。]{0,15}推計|推計値)")
    _EMP = re.compile(r"(無期雇用|有期雇用|従業上の地位[^。]{0,10}(変更|区分)|雇用形態[^。]{0,6}区分[^。]{0,6}変)")
    _hide = sorted({r["品目"] for r in rows if _HIDE.search(r.get("引用", ""))})
    _gva = sorted({r["品目"] for r in rows if _GVA.search(r.get("引用", ""))})
    _tax = sorted({r["品目"] for r in rows if _TAX.search(r.get("引用", ""))})
    _samp = sorted({r["品目"] for r in rows if _SAMP.search(r.get("引用", ""))})
    _emp = sorted({r["品目"] for r in rows if _EMP.search(r.get("引用", ""))})
    _yen = {}
    for r in rows:
        if r.get("予測") or "円" not in str(r.get("単位", "")): continue
        try: _y = int(str(r.get("年", ""))[:4])
        except Exception: continue
        _yen.setdefault(r["品目"], set()).add(_y)
    _taxcross = sorted(f"{it}（{min(ys)}〜{max(ys)}年・税率の変わった{'・'.join(str(t) for t in (2014, 2019) if min(ys) < t <= max(ys))}年をまたぐ）"
                       for it, ys in _yen.items() if any(min(ys) < t <= max(ys) for t in (2014, 2019)))
    md += ["## 統計の作り方の点検（秘匿・算式・税・抽出・雇用区分・4-81）",
           f"- 秘匿（X）に触れた引用のある品目：{len(_hide)}件（X を0にしない。X を除いた足し上げは下限。差引きで X が分かる欄も X）", *(f"  - {n}" for n in _hide[:10]),
           f"- 粗付加価値・付加価値の算式に触れた引用のある品目：{len(_gva)}件（4〜29人と30人以上は算式が違う。規模をまたいで付加価値の1人あたりを並べない）", *(f"  - {n}" for n in _gva[:10]),
           f"- 消費税の込み・抜きに触れた引用のある品目：{len(_tax)}件（込みと抜きの値を同じ系列に並べない）", *(f"  - {n}" for n in _tax[:10]),
           f"- 円の系列が消費税率の変わった年をまたぐ品目：{len(_taxcross)}件（税込みの出荷額は税率の段差を含む。向きを書く前に込み・抜きを確かめる）", *(f"  - {n}" for n in _taxcross[:20]),
           f"- 抽出・推計（上位9割など）の調査に触れた引用のある品目：{len(_samp)}件（2022年以降の推計値と全数の工業統計を同じ線につながない）", *(f"  - {n}" for n in _samp[:10]),
           f"- 従業上の地位・雇用区分の変更に触れた引用のある品目：{len(_emp)}件（分母の数え方が変わる。変更の前後で1人あたりの向きを書かない・4-70）", *(f"  - {n}" for n in _emp[:10]), ""]
    # 2026-11-07 4-82（lifexrees「AIでPDFの表が読めない・数字が違う？」：注記の番号が金額に混ざる・小数点やマイナスが抜ける・文字認識の誤りと
    # 行列・単位・注記の取り違えを別に確かめる。TIMEWELL「市場規模の算出方法 TAM・SAM・SOM」：二重計上・桁違い・頻度の掛け忘れ。
    # PLOS ONE 2023 の論文「Cross-checking journalistic fact-checkers」：2つの確かめ手が同じ主張を選んで確かめた割合（選択の一致）を、
    # 判定の一致より先に測る。選ばれ方が違えば、判定の食い違いは比べられない）。
    # ①注記の記号（※・注・＊）の直後に数字がある引用 ②延べ・二重計上・重複の語の引用 ③選択の重なり：独立のホスト2か所以上が同じ品目に数字を持つ割合と、
    # 品目数の多いホストどうしの品目の重なり
    _FN = re.compile(r"([※＊*†]\s*\d|[（(]\s*注\s*\d|注\s*\d\s*[)）:：]|\d[※＊†])")
    _DUP = re.compile(r"(延べ|延人数|二重(計上|カウント)|重複(計上|して計上|を含)|ダブルカウント)")
    _fn = sorted({r["品目"] for r in rows if _FN.search(r.get("引用", ""))})
    _dup = sorted({r["品目"] for r in rows if _DUP.search(r.get("引用", ""))})
    _ih = {}
    for r in rows:
        if r.get("予測"): continue
        _ih.setdefault(r["品目"], set()).add(src_key(r))
    _multi = sorted(n for n, hs in _ih.items() if len(hs) >= 2)
    _hi = {}
    for n, hs in _ih.items():
        for h in hs: _hi.setdefault(h, set()).add(n)
    _top = sorted(_hi, key=lambda h: -len(_hi[h]))[:5]
    _pairs = []
    for i, a in enumerate(_top):
        for b in _top[i + 1:]:
            _u = _hi[a] | _hi[b]
            if _u: _pairs.append((len(_hi[a] & _hi[b]) / len(_u), a, b, len(_hi[a] & _hi[b]), len(_u)))
    _pairs.sort(reverse=True)
    md += ["## 注記の混入・二重計上・選択の重なり（4-82）",
           f"- 注記の記号（※・注・＊）の直後に数字がある引用のある品目：{len(_fn)}件（注記の番号が値にくっついて桁が変わる疑い。原表で値と注記を分けてから使う）", *(f"  - {n}" for n in _fn[:10]),
           f"- 延べ・二重計上・重複に触れた引用のある品目：{len(_dup)}件（延べの数を実数・頭数の欄に入れない。合計と内訳を同じ欄で足さない）", *(f"  - {n}" for n in _dup[:10]),
           f"- 独立の出どころ2か所以上が数字を持つ品目（突き合わせできる品目）：{len(_multi)}件／数字のある品目 {len(_ih)}件"
           + (f"（{len(_multi) / len(_ih):.0%}）" if _ih else "") + "。残りは判定の一致を測る前に、別の出どころが同じ問いを扱っていない（選択の不一致）",
           *(f"  - {a} と {b}：品目の重なり {k}/{u}（{s:.0%}）" for s, a, b, k, u in _pairs[:5]), ""]
    md += kougyo_section(topic)
    md += kougyo_size_section(topic)
    md += kougyo_contract_section(topic)
    md += ["## 出どころが1か所だけの欄（裏が薄い。穴埋めの検索の対象・4-23）", *(f"- {t}" for t in thin or ["（なし）"]), ""]
    md += ["## 数字が1つも取れなかった品目（範囲の明示）", *(f"- {n}" for n in empty or ["（なし）"]), ""]
    md += ["## 仕様と実行の照合（inputs と結果・4-59）",
           f"- 仕様にあるがまだ一度も回っていない品目：{len(notrun)}件" + ("（数字0件とは別。次の回を待つ）" if notrun else ""), *(f"  - {n}" for n in notrun[:20]),
           f"- 仕様から外れたのに結果が残る品目：{len(orphan)}件（結論に使うときは、外した理由を先に確かめる）", *(f"  - {n}" for n in orphan[:10]),
           f"- 同じ品目・同じ年に年度と暦年の引用が並ぶ欄：{len(fymix)}件（期間をそろえるまで向き・食い違いを読まない）", *(f"  - {n}" for n in fymix[:15]), ""]
    # 2026-11-07 4-69（CASRAI「Outcome switching and selective outcome reporting」：登録した評価項目と公表した結果を、抜き取りでなく全件、
    # 項目ごとに突き合わせる。落とした・足した・主と副を入れ替えた、の3つの型を見る）。inputs の品目のうち数字が取れたのに
    # 前回の scan.md（Claude の解釈）に名前が出ない品目を「報告から落ちた品目」として数える。都合の悪い品目を黙って落とさないための点検
    try:
        import csv as _csv5
        _inp5 = [r.get("品目", "") for r in _csv5.DictReader(open(os.path.join(os.path.dirname(__file__), "inputs", topic + ".csv"), encoding="utf-8-sig"))]
        _sp = os.path.join(OUT, topic, "scan.md")
        if os.path.exists(_sp):
            _st = re.sub(r"\s", "", unicodedata.normalize("NFKC", open(_sp, encoding="utf-8").read()))
            _hasn = {r["品目"] for r in rows}
            _key = lambda n: re.sub(r"\s", "", unicodedata.normalize("NFKC", re.split(r"[（(]", n)[0]))[:12]
            _miss = [n for n in _inp5 if n in _hasn and _key(n) and _key(n) not in _st]
            md += ["## 報告からの落とし（4-69）",
                   f"- 数字が取れたのに scan.md に名前が出ない品目：{len(_miss)}件／数字のある品目 {sum(1 for n in _inp5 if n in _hasn)}件"
                   + ("。次の scan.md で、採らない理由を1行ずつ書くか結論に入れる" if _miss else ""), *(f"  - {n[:60]}" for n in _miss[:60]), ""]  # 2026-11-07 45回目：20件で切ると残りの品目に理由を書けない（4-69 の全件照合）ので60件まで出す
    except Exception: pass
    ds = DIMSPLIT.get(topic)
    if ds:
        md.insert(3, f"- 食い違いの次元の集計（4-60・4-61）：【食い違い】{ds[0] + ds[1]}欄のうち、次元で分かれた {ds[0]}欄・分かれない {ds[1]}欄（{ds[1] / (ds[0] + ds[1]):.0%}）。分かれない欄は同じ対象か・単位の読みを本文で先に確かめる")
    open(os.path.join(OUT, topic, "scan_auto.md"), "w", encoding="utf-8").write("\n".join(md))
    return len(rows)

if __name__ == "__main__":
    topics = sys.argv[1:] or [os.path.basename(p.rstrip("/")) for p in glob.glob(os.path.join(OUT, "*/")) if not p.rstrip("/").endswith("brief")]
    for t in topics:
        n = scan(t)
        if n: print(t, n)
