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
COPYHOST = re.compile(r"(irbank|buffett-code|kabutan|minkabu|ullet|houjin\.info|houjin-info|corporate-number|alarmbox|catr\.jp|kanpoo\.jp|kanpo-kensaku|b2bportal|houjinbangou\.|stockclip|kabuyoho|yahoo|zukaikeizai)", re.I)
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
# 2026-11-08 69回目の走査の失敗：mfg_capacity の「金型の事業所数の減少と合併の区別（日本金型工業会の自身の公表）」の品目に、
# 大学（tcue.ac.jp）の論文の中の1989・1999年の数字61件が入り【一次で確認】・届出と出た。68回目の imes.boj.or.jp（日本銀行 金融研究所の論文）と同じ型。
# 論文・紀要は統計・届出の写しを使った研究で、記録者本人ではない（4-24・4-93）。大学のサイトと研究所の論文の置き場を一次から外す
PAPERHOST = re.compile(r"(\.ac\.jp$|^www\.imes\.boj\.or\.jp$|^imes\.boj\.or\.jp$|^www\.rieti\.go\.jp$|jstage\.jst\.go\.jp$|^cir\.nii\.ac\.jp$|repository)", re.I)
def is_primary(url):
    host = urlparse(url).netloc
    if PAPERHOST.search(host): return False
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
# 2026-11-08 走査の失敗から（63回目）：「和暦の年が書かれているのに行の年が空」が mfg_hiring_seg 67件・recruit_pr_verify 61件。
# 平成・令和・昭和（元年を含む）と H・R の略記を西暦に直して、西暦の年と同じ並び（文中の位置の順）で返す。引用の文そのものは書き換えない
WAREKI_Y = re.compile(r"(昭和|平成|令和)\s*(\d{1,2}|元)\s*年|(?<![A-Za-z])([HR])\s?(\d{1,2})[\.．年]")
_ERA = {"昭和": 1925, "平成": 1988, "令和": 2018, "H": 1988, "R": 2018}
def years_in(t):
    out = [(m.start(), m.group(1)) for m in YEAR.finditer(t)]
    for m in WAREKI_Y.finditer(t):
        era, n = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        y = _ERA[era] + (1 if n == "元" else int(n))
        if 1980 <= y <= 2039: out.append((m.start(), str(y)))
    return [y for _p, y in sorted(out)]
TO_MAN = {"兆円": 1e8, "億円": 1e4, "千万円": 1e3, "百万円": 100, "万円": 1, "千円": 0.1, "円": 1e-4}
TO_PERSON = {"万人": 1e4, "千人": 1e3, "人": 1, "名": 1}
NOW_YEAR = __import__("datetime").date.today().year
# 2026-11-05 METHOD.md 4-5：数字にしてはいけない引用（見出し・記事題名・関連記事リンク・パンくず・検索結果の抜粋）
# 2026-11-05 4-35：関連記事の紹介文（「〜を解説します」「完全ガイド」「〜まで網羅」）の数字も事実にしない（電子決算公告ドットコムの「1行3,589円」の混入から）
HEADLINE = re.compile(r"(完全ガイド|を?(わかりやすく)?(解説|説明)します|を網羅|を徹底比較|徹底解説|とは[？?]|まとめ|一覧|比較\d*選|\d+選|【20\d\d年[^】]*】|[｜|] ?[^｜|]{0,40}$| > | › |https?://|価格帯別|おすすめ|ランキング|など注目|トピックを整理|ニュースまとめ)")  # 4-88：ニュースの見出しの並び（「純利益71%増…など注目トピックを整理」が社員数の欄に入った）
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

# 2026-11-08 4-90：欄の種類ごとの基準の出どころ（指標ごとに1つ決め、他をそれと比べる）。人数は日本年金機構の被保険者数（4-85）、
# 事業所数・統計の金額・1人あたりは所管の統計（4-41）、売上は決算公告・開示（4-85）
BENCH = [("社員数", ("日本年金機構の適用事業所検索・開示の従業員数", re.compile(r"(nenkin\.go\.jp|disclosure|edinet|/ir/)", re.I))),
         ("事業所数", ("所管の統計表（経産省・総務省・e-Stat）", re.compile(r"(meti\.go\.jp|stat\.go\.jp|e-stat\.go\.jp|soumu\.go\.jp)", re.I))),
         ("1人あたり", ("所管の統計表（経産省・総務省・e-Stat）", re.compile(r"(meti\.go\.jp|stat\.go\.jp|e-stat\.go\.jp|soumu\.go\.jp|chusho\.meti)", re.I))),
         ("売上", ("決算公告・開示（官報・電子公告・EDINET）", re.compile(r"(kanpo|kessan|koukoku|edinet|disclosure)", re.I)))]

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
# 4-109：research_method の結果に生のIPアドレスのホスト・成人向けや偽ブランド品のページが混じった。出どころとして数えない
JUNKPATH = re.compile(r"(bokep|ngentot|memek|/watch/[a-z0-9-]*sex|porn|xxx|-handbags-|-purses-|replica|casino|slot-gacor|viagra)", re.I)
def is_junk_url(url):
    host = urlparse(url).netloc.split(":")[0]
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host)) or bool(JUNKPATH.search(url or ""))

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
PUBFEE = re.compile(r"(掲載料|掲載費|枠掲載|万円\s*/\s*回|円\s*/\s*回|公告(の)?(費用|料金)|官報(の)?(費用|料金))")
BAREFIG = re.compile(r"^\s*[△▲\-−()（）]?\s*[\d,.]+\s*(兆|億|百万|千万|万|千)?\s*円?\s*[)）]?\s*$")
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
                if is_junk_url(url): drop(topic, "生のIPアドレス・スパムのURL（4-109）"); continue
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
                # ④2026-11-08 走査の失敗から（61回目・4-86）：「厚生年金の被保険者数（日本年金機構 適用事業所検索）」の品目に、jsite.mhlw.go.jp の
                # 失業給付・退職金・高齢者人口のページの値（45〜80%、100万円など）が32件入った。適用事業所検索は入力欄で引く仕組みで検索に出ない。
                # 品目が年金機構・被保険者数を名指すのに、引用に被保険者・適用事業所の語が無ければ事実にしない
                if re.search(r"(日本年金機構|被保険者数|適用事業所)", name) and not re.search(r"(被保険者|適用事業所)", q):
                    if NUM.search(q): drop(topic, "名指した出どころの語が無い（年金機構の品目に別の制度のページ・4-86）")
                    continue
                # ⑤2026-11-08 走査の失敗から（63回目・4-88）：品目「官報決算データベース（catr.jp）で引く受託4業種の決算公告」に、
                # 公告を出す側の掲載料（「約7万円/回」「2枠掲載で約8万円」「20万円を超える」）が受注金額・加工賃収入として3件、
                # 主語も科目も無い「▲88200万円」だけの切れ端が5件入り、中央値が −731万円と出た。公告の料金の文と、記号と数字だけの切れ端は事実にしない
                if PUBFEE.search(q):
                    if NUM.search(q): drop(topic, "公告・掲載の料金（受注金額ではない・4-88）")
                    continue
                if BAREFIG.match(q):
                    if NUM.search(q): drop(topic, "主語も科目も無い数字だけの切れ端（4-88）")
                    continue
                ys = years_in(q)
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
                    # 2026-11-09 94回目の走査の失敗から（4-119）：mfg_capacity の金型の品目で、金型新聞の「10年に1兆873億円まで落ち込んだ」
                    # 「17年には1兆5257億円」が 873億円・5257億円として入った（兆の部分を落とし、12分の1以下の桁違い）。
                    # 数字の直前が「N兆」なら兆の分を足し、「N億」の直後の万円（3億5000万円）も億の分を足す。原文の数字にも前の部分を残す
                    _pre_big = ""
                    if u == "万円" and m.group(2) in ("億円", "万円", "千万円"):
                        _pm = re.search(r"(\d+(?:\.\d+)?)\s*兆\s*$", q[max(0, m.start() - 12):m.start()]) if m.group(2) == "億円" else re.search(r"(\d{1,3}(?:,\d{3})*|\d+)\s*億\s*$", q[max(0, m.start() - 12):m.start()])
                        if _pm:
                            v += float(_pm.group(1).replace(",", "")) * (1e8 if m.group(2) == "億円" else 1e4)
                            _pre_big = _pm.group(0).strip()
                    # 2026-11-09 95回目の走査の失敗から（4-120）：兆の位を直した回で、中小企業実態基本調査の「資産合計 訂正前 406兆9,919億円、
                    # 訂正後 407兆32億円」が「受注金額・加工賃収入」の採る値（4.07e+10万円・【一次で確認】）になり、工業統計の「製造品年末在庫額」
                    # 「有形固定資産投資総額」、経済構造実態調査の全産業計・卸売業・医療福祉の売上高が金型の品目に入った。
                    # ①前40字に資産・負債・在庫・投資・予算の科目がある金額は受注・加工賃・1人あたりの欄に入れない
                    # ②「訂正前」の値は捨て、訂正後を採る。「訂正率」の%も値にしない
                    # ③品目が受託の細分類（金型・めっき・熱処理・プレス）なのに、前20字が全産業・他の産業大分類の総額なら事実にしない
                    _bf40, _bf20 = q[max(0, m.start() - 40):m.start()], q[max(0, m.start() - 20):m.start()]
                    if re.search(r"訂正前\s*$|訂正率\s*[+＋]?\s*$", q[max(0, m.start() - 6):m.start()]):
                        drop(topic, "訂正前の値・訂正率（訂正後を採る・4-120）"); continue
                    if u == "万円" and re.search(r"受注|加工賃|1人あたり|1社あたり", cat) and re.search(r"資産合計|総資産|負債|純資産|在庫|仕掛品|有形固定資産|投資総額|予算|補助金|補助事業", _bf40):
                        drop(topic, "主語が別の科目（資産・在庫・投資・予算は受注金額でない・4-120）"); continue
                    if u == "万円" and re.search(r"金型|めっき|鍍金|熱処理|プレス|板金", name) and re.search(r"全産業|産業計|卸売業|小売業|医療|福祉|サービス業", _bf20):
                        drop(topic, "主語が別の産業（全産業・他の大分類の総額・4-120）"); continue
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
                    # 2026-11-09 94回目の走査の失敗から（4-119）：gdfreak の「前年比13.4%減の1,092億円」が増減率 +13.4 として入り、
                    # 減った系列が増えた向きに読めた。増減率の % で直後が「減・低下・下落・縮小」、または前に「減少率」があれば負にする
                    neg_pct = False
                    if u == "%" and ctx_n == "増減率" and v > 0:
                        if re.match(r"\s*\)?\s*(の)?(減|低下|下落|縮小|マイナス|down|decrease|decline)", q[m.end():m.end() + 8], re.I) or re.search(r"減少率\s*(は|が)?\s*$", q[max(0, m.start() - 8):m.start()]):
                            v, neg_pct = -v, True
                    pre = q[max(0, m.start() - 2):m.start()]
                    neg = bool(NEGMARK.search(pre)) or (u == "万円" and pre.endswith("(") and q[m.end():m.end() + 1] == ")" and bool(FINTABLE.search(q)))  # 4-35：括弧は決算の表の文脈だけ
                    if u == "万円" and v <= 0: drop(topic, "0以下の金額"); continue
                    if neg and u == "万円": v = -v  # 4-34 △・▲・括弧は負の値（損失・減少）
                    if u == "人" and v > MAXPERSON: OUTLIERS[topic] = OUTLIERS.get(topic, 0) + 1; continue  # 4-31 桁の感覚の上限
                    # 数字の近く（前40字）の年を優先
                    near = years_in(q[max(0, m.start() - 40):m.start() + 5])
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
                    # 2026-11-09 93回目の走査の失敗から（4-118）：mfg_capacity の金型（HS8480）の輸入のミラーの品目で、gdfreak.com の
                    # 「2023年の輸入額は前年比6.6%増の21.0億円」が「受注金額・加工賃収入」の欄に入った。輸出入の額は受託の受注・加工賃ではない。
                    # 数字の前30字に輸入・輸出の額の語があれば、金額・増減率の文脈を「貿易額（輸出入）」に分けて同じ欄で突き合わせない
                    if re.search(r"(輸入|輸出)(額|高|金額)|(import|export)s?\s*(value)?", q[max(0, m.start() - 30):m.start()], re.I) and u in ("万円", "%") and ctx_n != "シェア・構成比":  # 94回目：相手国の構成比（米国向け31.2%）を増減率にしない
                        ctx_n = "貿易額（輸出入）" if u == "万円" else "貿易額（輸出入）の増減率"
                    yield {"品目": name, "区分": cat, "文脈": ctx_n, "年": y, "値": round(v, 4), "単位": u,
                           "一次": kind, "立場": standing(url, kind),
                           "元へ戻る": "要" if kind != "一次" and CITES_PRIMARY.search(q) else "",
                           "定義": defn_of(q), "版": version_of(q), "規模": size_class_of(q, m.start()),
                           # 2026-11-05 4-19：出どころの記録（URL・取得日・取り方）。欠けた引用は後から確かめられない
                           # 2026-11-05：mfg_capacity の引用は first_seen が無く「記録が欠けた数字 1488/1488」になった。ファイルの researched_at を取得日に使う
                           "取得日": it.get("first_seen", "") or (d.get("researched_at", "") or "")[:10],
                           "予測": "予測" if y and int(y) > NOW_YEAR else "見通し" if OUTLOOK.search(q) else "試算" if TRIAL.search(q) else "仮置き" if ASSUME.search(q) else "",
                           "年の補正": yfix, "符号": "負" if (neg and u == "万円") or neg_pct else "",
                           "出典": urlparse(url).netloc, "URL": url,
                           "原文の数字": (_pre_big + m.group(0).strip())[:40],
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

_TRACK = re.compile(r"^(gs|utm_[a-z]+|fbclid|gclid|ref|ref_src|from|via|s|share|amp)$", re.I)
def canon_url(u):
    """2026-11-08 4-96：同じ頁の別URLを1つに束ねる形。追跡の問い合わせ・断片・末尾の /・符号化の違い（%20 と + など）を落とす。"""
    from urllib.parse import unquote, parse_qsl, urlencode
    if not u: return ""
    p = urlparse(u.strip())
    if not p.netloc: return ""
    q = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACK.match(k)))
    path = unquote(p.path.replace("+", " ")).replace(" ", "%20").rstrip("/")
    return f"{p.netloc.lower().removeprefix('www.')}{path}" + (f"?{q}" if q else "")

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
# 2026-11-08 4-93（ABS 非回答バイアス分析：重み付けの推計と重み無しの集計は使い道が違う）
WEIGHTED = re.compile(r"(ウエイト|ウェイト|重み付け|重みづけ|抽出率の逆数|復元倍率|推計乗率|無回答補正|weighted)", re.I)
# 2026-11-08 4-93（日本銀行 金融研究所 22-E-07：合併は参入・退出と別の型。合併の効きは年を追って大きくなる）
MERGEREXIT = re.compile(r"(合併|統合|吸収|経営統合|事業譲渡)")
# 2026-11-08 4-94（樋口 note「発電シミュレーションの検証」：月の途中から始まった実績を通しの月の試算と比べれば少なく見えるのは当然。
# 同じ名の値でも対象期間が違えば比べない）。年の値を求める品目に、上期・四半期・何か月間などの部分の期間の値が混ざるのを数える
PARTIAL = re.compile(r"(上期|下期|上半期|下半期|中間期|第[1-4１-４一二三四]四半期|[1-4１-４]Q|[0-9０-９]{1,2}\s*[〜～~\-－ー]\s*[0-9０-９]{1,2}\s*月(期|期間|累計)?|[0-9０-９]{1,2}\s*か月(間|累計)|月次|月間|半期)")
# 2026-11-08 4-94（wattson496 note「情報源の評価」：コリンズ＆エヴァンズの専門知の類型。現場でやった人（貢献型）と、語れるが自分ではやらない人（対話型）を分ける）。
# 証言の話し手が、現場の当事者（社長・工場長・職人・現場の担当）か、外から語る人（コンサル・アナリスト・評論家・記者・研究員）かを数える
SPK_DO = re.compile(r"(社長|代表取締役|工場長|職人|現場(の|で)(担当|責任者)|製造部長|生産技術|技能士|オーナー)")
SPK_TALK = re.compile(r"(コンサルタント|アナリスト|評論家|ジャーナリスト|記者|研究員|エコノミスト|専門家|識者|解説)")
SELFREPORT = re.compile(r"(自己評価|自己申告|自己採点|自社調べ|自社調査)")
CONTRACTOR = re.compile(r"(委託先(の|が)?(変更|変わ)|調査(の)?委託先|入札(により|で)[^。]{0,10}委託)")
INTERSEG = re.compile(r"(セグメント間|内部売上|内部取引|社内売上|グループ内(の)?(売上|取引))")
# 2026-11-08 4-88（東京弁護士会 LIBRA 2026年7・8月号の生成AIの注意：誤った出典は「実在しない・文言違い・関連なし・時代遅れ」の4型）。
# 改正前・廃止・旧基準など、後の版で置き換わった出典の語を数える。新しい版・現行の統計で取り直す
OUTDATED = re.compile(r"(改正前|旧法|旧制度|旧基準|旧分類|旧様式|廃止され|廃止済|調査(は|を)?(終了|廃止)|現在は(廃止|終了|行われていない))")
# 2026-11-08 4-88（なかのアセットマネジメント note：会社の出す「それらしい理由」をうのみにせず多方面で整合を見る）。増減・未達の理由を述べる文
REASONWORD = re.compile(r"(主な(要因|理由)|要因(は|として)|が(寄与|響いた|奏功)|の影響(により|で|を受け)|によるものです|が主因)")
# 2026-11-08 4-88（osint.uk の 3x5x2 の格付け：取り扱いの印で共有の範囲を決める）。個人のSNSの発信は公開リポジトリの本文に写さない「内部のみ」
PERSONALHOST = re.compile(r"(^|\.)(x\.com|twitter\.com|facebook\.com|instagram\.com|threads\.net|tiktok\.com)$", re.I)
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
# 2026-11-09 4-110（千葉県・山梨県の鉱工業指数の利用上の注意・解説）：月報の値は年報で年間補正・季節指数の改定を受けて直る。
# 暦年の前年比は原指数、四半期の前期比は季節調整済で出す。在庫は平均でなく期末の値。指数は数量のラスパイレス（基準時固定の重み）で金額ではない。
# 業種の重みには採用していない品目の分を按分で足す「ふくらまし」が入る。同じ品目でこれらが混ざれば印す
IIPMONTHLY = re.compile(r"(月報|速報値?)")
IIPANNUAL = re.compile(r"(年報|年間補正|季節指数(の)?改定)")
IIPWORD = re.compile(r"(鉱工業指数|生産指数|出荷指数|在庫指数|在庫率指数|IIP)")
SARAW = re.compile(r"(原指数|季節調整(を)?(しない|前|なし))")
SAADJ = re.compile(r"(季節調整済|季調済|季節調整値)")
STOCKIDX = re.compile(r"(在庫指数|在庫率指数)")
FUKURAMASHI = re.compile(r"(ふくらまし|膨らまし|非採用(業種|品目)(の|を)[^。]{0,10}(按分|配分))")
# 2026-11-09 4-110（CREX「中小企業の二次情報調査 事例12選」：冒頭にプロモーションの表示があり、本文の数字は説明用の仮の例）。
# 広告・成果報酬の表示があるページ、例示の語で置いた数字は事実の裏付けに数えない
PROMO = re.compile(r"(プロモーションを含み|成果報酬を受け取る|アフィリエイト(広告|プログラム)|広告を含みます|PR表記|\[PR\]|【PR】|sponsored)", re.I)
EXAMPLEFIG = re.compile(r"(例えば|たとえば|例として|一例として|仮の数字|イメージとして)[^。]{0,30}\d")
# 2026-11-09 4-110（Reynolds Center「What first dates teach about investigating a business」：監査人の辞任・訴訟の提起・当局の調査開始は外から見える危機の印。
# 在庫が売上より速く増えるのは負担の兆し）。継続企業の注記と同じく品目に印す
AUDITCHG = re.compile(r"(監査人の(異動|交代|辞任)|会計監査人(の)?(異動|交代|辞任)|監査法人(の)?(異動|交代|辞任)|auditor (resign|change))", re.I)
# 2026-11-09 4-112（中小企業白書2024 凡例）：企業活動基本調査は従業者50人以上かつ資本金3,000万円以上だけ、法人企業統計の小さい階級は参考値、
# 四半期別は資本金1,000万円未満を含まない。「再編加工」は所管が個票を独自に集計し直した値で、公表の表からは再現できない
KIGYOKATSU = re.compile(r"企業活動基本調査")
SMALLSCOPE = re.compile(r"(小規模|零細|従業者?(数)?\s*(1|4|5|10|20)\s*[〜～~\-－]\s*(9|19|29|49)\s*人|(9|19|29|49)\s*人以下|中小)")
HOJINQ = re.compile(r"法人企業統計[^。]{0,15}(四半期|季報)")
SAIHEN = re.compile(r"再編加工")
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

# 2026-11-09 4-112（Mauboussin の基準率・縮み推定）：1年だけ高い・低い業種の値は翌年に平均へ寄る。寄る強さは「同じ業種の値の
# 年をまたいだ相関 r」で測る（技能で決まる値は r が1に近く、運で決まる値は0に近い）。段差をまたがない実績2016〜2019年の隣どうしの年で、
# 注目の細分類の1人あたりの相関 r を出し、最新の年の値を「平均＋r×（値−平均）」に縮めた見込みと並べる。縮めた値は【推定】で事実ではない
def kougyo_shrink(by, fld="従業者1人あたり付加価値額_万円", unit="万円"):
    pts = {}
    for (code, name), rs in sorted(by.items()):
        if not _kfocus(name) or len(str(code)) != 4 or str(code)[-1:] == "0": continue
        for y, r in rs:
            v = _kf(r.get(fld))
            if v is not None and v > 0 and 2016 <= y <= 2019: pts.setdefault((code, name), {})[y] = v
    out, rr = [], []
    for y in (2016, 2017, 2018):
        ks = [k for k in pts if y in pts[k] and y + 1 in pts[k]]
        if len(ks) < 5: continue
        try: r = statistics.correlation([pts[k][y] for k in ks], [pts[k][y + 1] for k in ks])
        except statistics.StatisticsError: continue
        rr.append(r); out.append(f"{y}→{y + 1}年 r={r:.2f}（{len(ks)}業種）")
    if not rr: return []
    r = max(0.0, min(1.0, statistics.median(rr)))
    ly = max(y for k in pts for y in pts[k]); last = {k: v[ly] for k, v in pts.items() if ly in v}
    if len(last) < 5: return []
    m = statistics.mean(last.values()); med = statistics.median(last.values())
    ex = sorted(last.items(), key=lambda kv: -abs(kv[1] - m))[:6]
    md = [f"- 年をまたいだ持続の相関（4-112・{fld}・注目の細分類・実績2016〜2019年・段差をまたがない）：" + "、".join(out) + f"。中央値 r={r:.2f}",
          f"  - 最新の{ly}年の平均 {m:,.1f}{unit}・中央値 {med:,.1f}{unit}（平均÷中央値 {m / med:.2f}。1.2を超えれば上の裾が重い・4-108）。"
          "縮めた見込み＝平均＋r×（値−平均）は【推定】で、翌年に平均へどれだけ寄りそうかの目安。r が0.8未満なら1年の高い・低いを業種の力と書かない",
          "  - 平均から遠い業種（値→縮めた見込み）：" + "、".join(f"{c} {nm} {v:,.1f}→{m + r * (v - m):,.1f}" for (c, nm), v in ex)]
    # 2026-11-09 4-112・4-114：r が0.8を下回った区間は、どの業種の順位が動いたかを国の同じ表から出す（自治体の表の写しで埋めない・4-83・4-104）。
    # 順位の動きと、値の増減率（分子＝付加価値と分母＝従業者のどちらが動いたか・4-70）を並べる
    for y, rv in zip([yy for yy in (2016, 2017, 2018)][:len(rr)], rr):
        if rv >= 0.8: continue
        ks = [k for k in pts if y in pts[k] and y + 1 in pts[k]]
        r0 = {k: i + 1 for i, k in enumerate(sorted(ks, key=lambda k: -pts[k][y]))}
        r1 = {k: i + 1 for i, k in enumerate(sorted(ks, key=lambda k: -pts[k][y + 1]))}
        mv = sorted(ks, key=lambda k: -abs(r1[k] - r0[k]))[:6]
        def _parts(k):
            a = dict(by.get(k, [])); s0, s1 = a.get(y, {}), a.get(y + 1, {})
            va0, va1 = _kf(s0.get("付加価値額_万円") or s0.get("付加価値額")), _kf(s1.get("付加価値額_万円") or s1.get("付加価値額"))
            e0, e1 = _kf(s0.get("従業者数")), _kf(s1.get("従業者数"))
            p = []
            if va0 and va1: p.append(f"付加価値 {va1 / va0 - 1:+.1%}")
            if e0 and e1: p.append(f"従業者 {e1 / e0 - 1:+.1%}")
            return ("・" + "・".join(p)) if p else ""
        md.append(f"  - r={rv:.2f} の {y}→{y + 1}年に順位が大きく動いた業種（{fld}・{len(ks)}業種中の順位・国の工業統計の同じ表）：" +
                  "、".join(f"{c} {nm} {r0[(c, nm)]}位→{r1[(c, nm)]}位（{pts[(c, nm)][y]:,.1f}→{pts[(c, nm)][y + 1]:,.1f}{unit}・{pts[(c, nm)][y + 1] / pts[(c, nm)][y] - 1:+.1%}{_parts((c, nm))}）" for c, nm in mv))
    return md

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
    md += kougyo_jump_exit(by)
    md += kougyo_shrink(by)
    md += kougyo_shrink(by, "従業者1人あたり製造品出荷額等", "百万円")
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

# 2026-11-08 4-89（DataJournalism.com「Data journalism: a guide for editors」：意外な跳び（1年で5割増など）はまず誤りを疑って確かめる・
# 数える対象の範囲（ユニバース）を途中で変えない。CASRAI「Survivorship bias」：退出した先が記録から消えると平均が上振れする。
# 退出した側について最良・中間・最悪の仮定を置き、結論がその幅で残るかを確かめる）。
# ①注目の細分類と製造業計で、隣り合う年に事業所数・従業者数・製造品出荷額等・加工賃収入額が1.5倍以上か2/3以下に動いた点を「意外な跳び」として出す
#   （既知の段差の年 2015・2016 は別に印す）。②段差をまたがない最長の区間で、事業所数が1割以上減り1人あたり出荷額が上がった細分類を
#   「生き残りの疑い」として、退出した割合と並べる。退出した先の1人あたりを仮定する値は作らない（scan.md で3つの仮定を言葉で書く）
def kougyo_jump_exit(by):
    sheet = "1100"
    jumps, surv = [], []
    for (code, name), rs in sorted(by.items()):
        if code != "0000" and (not _kfocus(name) or code[-1:] == "0"): continue
        for fld in ("事業所数", "従業者数", "製造品出荷額等", "加工賃収入額"):
            pts = _kseries(rs, fld, sheet)
            for (y0, v0), (y1, v1) in zip(pts, pts[1:]):
                if not v0 or v0 <= 0 or y1 != y0 + 1: continue
                r = v1 / v0
                if r >= 1.5 or r <= 2 / 3:
                    jumps.append(f"  - {code} {name} {fld} {y0}年 {v0:,.0f}→{y1}年 {v1:,.0f}（{r:.2f}倍" + ("・既知の段差の年" if y1 in (2015, 2016) else "") + "）")
        n = dict(_kseries(rs, "事業所数", sheet)); num = dict(_kseries(rs, "製造品出荷額等", sheet)); den = dict(_kseries(rs, "従業者数", sheet))
        ys = [y for y in sorted(set(n) & set(num) & set(den)) if y not in (2015, 2016)]
        seg, cur = [], []
        for y in ys:
            if cur and y != cur[-1] + 1: seg.append(cur); cur = []
            cur.append(y)
        if cur: seg.append(cur)
        seg = [x for x in seg if len(x) >= 2]
        if not seg: continue
        s = max(seg, key=len); a, b = s[0], s[-1]
        if not (n[a] and den[a] and den[b]): continue
        ex = 1 - n[b] / n[a]
        pa, pb = num[a] / den[a], num[b] / den[b]
        if ex >= 0.10 and pb > pa:
            surv.append(f"  - {code} {name} {a}→{b}年：事業所数 {n[a]:,.0f}→{n[b]:,.0f}（{ex:.0%} 減）、1人あたり出荷額 {pa:,.2f}→{pb:,.2f}百万円（{pb / pa - 1:+.1%}）")
    if not jumps and not surv: return []
    return ["", "### 意外な跳びと生き残りの疑い（4-89・同じ表の組・名目・機械）",
            f"- 隣り合う年で1.5倍以上か2/3以下に動いた点：{len(jumps)}件。向きに使う前に、表の取り違え・単位・秘匿・調査範囲の変更を先に疑って本文で確かめる",
            *jumps[:30],
            f"- 事業所数が1割以上減ったのに1人あたり出荷額が上がった細分類：{len(surv)}件。退出した事業所が記録から消えた上振れの疑い。"
            "scan.md では、退出した先の1人あたりを「残った先と同じ（最良）／最初の年の自分の値（中間）／最初の年の下位（最悪）」の3つで言葉で置き、向きがその幅で残るかを書く。数値の仮置きはしない",
            *surv[:30],
            # 2026-11-08 4-93（日本銀行 金融研究所 22-E-07：退出の効きは多くの研究で正＝退出する先は残る先より生産性が低い）
            "- 既定の読み方（4-93）：退出の効きは多くの研究で正（退出する先は残る先より低い）。反証が無い限り、事業所数が減った区間の1人あたりの上昇は「一部は退出の効き」と書く。合併・統合で減った分は退出に数えない"]

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
    # 2026-11-08 4-90（SCB・米国センサス局「Response rates in business surveys」：件数の回答率は、少数の大きな先が総額を占める調査では質を映さない。
    # 量で見た率（総額のうち回答・同等の出どころで埋まった割合）を並べる）。ここでは引用の件数で見た一次の割合と、品目で見た一次の割合を並べ、
    # 一次の引用が少数の品目に偏っていないかを見る
    _it_all = {r["品目"] for r in rows}; _it_p = {r["品目"] for r in rows if r["一次"] == "一次"}
    _np = sum(1 for r in rows if r["一次"] == "一次")
    md += [f"- 一次の割合（4-90）：引用の件数で {_np}/{len(rows)}（{_np / len(rows):.0%}）／品目で {len(_it_p)}/{len(_it_all)}（{len(_it_p) / len(_it_all):.0%}）"
           + ("。⚠ 品目で見た割合が件数の半分未満：一次は少数の品目に偏る。一次の無い品目の結論は【未確認】のまま" if _np and len(_it_p) / len(_it_all) < 0.5 * _np / len(rows) else "")]
    # 2026-11-08 4-91（PRISMA-S：英語だけ・1言語だけで探すと言語の片寄りが入る）。品目ごとに引用の言語（かなを含むか）を数え、
    # 3件以上あるのに1つの言語だけの品目を並べる。穴埋めの検索で、もう一方の言語の別名を inputs に足す対象
    _lang = {}
    for r in rows:
        _j = bool(re.search(r"[぀-ヿ]", r["引用"]))
        _lang.setdefault(r["品目"], [0, 0])[0 if _j else 1] += 1
    _one = sorted(k for k, (a, b) in _lang.items() if a + b >= 3 and (a == 0 or b == 0))
    md += [f"- 1つの言語だけの品目（4-91・3件以上）：{len(_one)}／{len(_lang)}品目" + ("：" + "、".join(_one[:8]) + ("ほか" if len(_one) > 8 else "") + "。別の言語の別名で取り直すまで「見つからない」を言い切らない" if _one else "")]
    # 2026-11-08 4-91（インティメート・マージャー「デスクリサーチの方法」：グラフ・図版は出典を書いても転載にならないことがある）。
    # 公開リポジトリに図表の写しを載せないため、図・グラフの見出し・注記に見える引用を数える。数字は元の表で取り直す
    _fig = sum(1 for r in rows if re.match(r"\s*(図表?|グラフ|Figure|Fig\.|Chart)\s*[0-9０-９\-－]", r["引用"]))
    md += [f"- 図・グラフの見出しに見える引用（4-91）：{_fig}件" + ("。図の写しは載せず、元の表・本文の数字だけを使う" if _fig else "")]
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
    # 2026-11-08 4-92（aruaruteitei note：AIで作った業界分析で、指摘・訂正を受け付けないと明記）。同じページのどこかに
    # 「AIの分析・生成AIで作成・NotebookLM」や「ご指摘・訂正は受け付けない」があるURLを、ページ単位で印にし、そのURLの数字を数える。
    # 生成物で訂正の窓口も無い出どころは、数字を事実の欄に入れず、一次で取り直す手がかりにだけ使う
    _AIGEN = re.compile(r"(AIの分析|AIによる分析|生成AIで作成|AIが作成|AIで作成|NotebookLM|ChatGPTで作成|(ご意見|ご指摘|訂正)[^。]{0,20}(受け付けて|お受けして)(いません|おりません))")
    aiurls = {r["URL"] for r in rows if r.get("URL") and _AIGEN.search(r["引用"])}
    nai = [r for r in rows if r.get("URL") in aiurls and str(r.get("値", "")).strip()]
    if nai: md.append(f"  - ⚠ 生成物・訂正を受け付けないと明記したページの数字 {len(nai)}件・{len(aiurls)}URL（例：{nai[0]['品目']}）：事実の欄に入れず、一次で取り直す手がかりにだけ使う（4-92）")
    # 同（billye note：売上は1年分でなく10年ほどの推移で見る）。会社・品目ごとに売上の値の年が1つしかないものを数える。
    # 1年だけの売上で「伸びている・大きい」を書かない
    _SALES = re.compile(r"(売上高|売上|年商|営業収益)")
    syears = {}
    for r in rows:
        if _SALES.search(r["品目"] + r["引用"]) and str(r.get("値", "")).strip() and str(r.get("年", "")).strip():
            syears.setdefault(r["品目"], set()).add(str(r["年"]))
    s1 = sorted(k for k, v in syears.items() if len(v) == 1)
    # 2026-11-08 67回目の走査の失敗：「職業別の有効求人倍率」の品目に賃金の万円が、「採用1人あたり費用」の品目に有効求人倍率の倍が入った。
    # 品目の名が求める単位の種類（倍率か金額か）と、取れた値の単位の種類が違うものを数え、その品目の採る値・結論に使わない
    _MONEY = ("円", "万円", "千円", "百万円", "億円")
    def _unit_mismatch(r):
        n = r["品目"]
        if "倍率" in n and not re.search(r"(費用|単価|賃金|給与|額)", n) and r["単位"] in _MONEY: return True
        if re.search(r"(費用|単価|採用費|金額|売上)", n) and "倍率" not in n and r["単位"] == "倍": return True
        return False
    num = [r for r in rows if str(r.get("値", "")).strip() and _unit_mismatch(r)]
    if num: md.append(f"  - ⚠ 品目の名が求める単位と違う単位の値 {len(num)}件（例：{num[0]['品目']}｜{num[0]['単位']}）：別の指標を拾った指紋。その品目の採る値・結論に使わない（4-92）")
    if s1: md.append(f"  - ⚠ 売上の値が1年分しかない品目 {len(s1)}件（例：{s1[0]}）：1年で伸び・規模を書かず、決算公告・有価証券報告書で複数年を取る（4-92）")
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
    # 2026-11-09 4-111（税理士法人チェスター「会社規模判定にかかる従業員数」：税の従業員数は週30時間以上を1人、ほかは年間労働時間÷1800で換算し
    # 役員を除くので 69.4人 のような端数が出る）。①小数の人数は頭数でなく換算値として数える ②同じ品目の人数の引用に
    # 「役員を除く/含まない」と「役員を含む・被保険者・従業者（個人業主・役員を含む統計の定義）」が混ざれば、数える範囲の違う人数の混在として数える
    ndec = [r for r in rows if r["単位"] in ("人", "名") and (_num(r.get("値")) or 0) > 0 and float(_num(r.get("値")) or 0) != int(float(_num(r.get("値")) or 0))]
    if ndec: md.append(f"  - ⚠ 小数の人数 {len(ndec)}件（例：{ndec[0]['品目']}）：頭数でなく換算値（労働時間÷1800時間・常勤換算）か平均の疑い。頭数の従業者数と同じ分母にしない（4-111）")
    _YEX = re.compile(r"役員(を|は)?(除|含まない|含まず)"); _YIN = re.compile(r"(役員を含|役員含む|被保険者|従業者)")
    _yq = {}
    for r in rows:
        if r["単位"] not in ("人", "名"): continue
        t = r["引用"]
        k = "除く" if _YEX.search(t) else ("含む" if _YIN.search(t) else None)
        if k: _yq.setdefault(r["品目"], set()).add(k)
    nyk = sorted(q for q, s in _yq.items() if len(s) > 1)
    if nyk: md.append(f"  - ⚠ 役員を除く人数と含む人数（被保険者・統計の従業者）が同じ品目に混在 {len(nyk)}品目（例：{nyk[0]}）：数える範囲が違う。差を食い違いや増減として読まない（4-111）")
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
    # 2026-11-08 4-85（開示ワイヤー「決算公告の読み方」：貸借対照表は内訳の合計が資産合計と一致した行だけを載せる。株式会社以外の法人は
    # 科目が違い資本金が無い（出資金・基金）。プロ向け市場の上場会社は有報を出さず公告の義務が残る）。
    # ①同じ引用に資産合計・負債合計・純資産合計の3つがあれば、資産合計＝負債合計＋純資産合計かを機械で確かめ、合わない行を出す
    def _bsnum(s):
        s = s.replace(",", "").replace("，", "")
        neg = s.startswith(("△", "▲", "-", "－"))
        try: v = float(re.sub(r"^[△▲\-－]", "", s))
        except ValueError: return None
        return -v if neg else v
    _BSA = re.compile(r"資産(?:の部)?合計[^\d△▲\-－]{0,6}([△▲\-－]?[\d,，]+(?:\.\d+)?)")
    _BSL = re.compile(r"負債(?:の部)?合計[^\d△▲\-－]{0,6}([△▲\-－]?[\d,，]+(?:\.\d+)?)")
    _BSN = re.compile(r"純資産(?:の部)?合計[^\d△▲\-－]{0,6}([△▲\-－]?[\d,，]+(?:\.\d+)?)")
    bs_ok, bs_ng = 0, []
    for r in rows:
        q = r["引用"]
        a = [m for m in _BSA.finditer(q) if not q[max(0, m.start() - 1):m.start()].endswith("純")]
        l, n = _BSL.search(q), _BSN.search(q)
        if not (a and l and n): continue
        va, vl, vn = _bsnum(a[0].group(1)), _bsnum(l.group(1)), _bsnum(n.group(1))
        if None in (va, vl, vn) or va == 0: continue
        if abs(va - (vl + vn)) / abs(va) <= 0.01: bs_ok += 1
        else: bs_ng.append(r)
    if bs_ok or bs_ng:
        md.append(f"  - 貸借対照表の整合（資産合計＝負債合計＋純資産合計、1%以内）：合う {bs_ok}件／合わない {len(bs_ng)}件" + (f"（例：{bs_ng[0]['品目']}）：合わない行は単位・符号（△）・行の取り違えを原本で確かめるまで採らない（4-85）" if bs_ng else "（4-85）"))
    # ②株式会社以外の法人の決算（科目が違い資本金が無い）・プロ向け市場の上場会社（有報が無く公告の義務が残る）の引用を数える
    _NONKK = re.compile(r"(医療法人|社会福祉法人|学校法人|一般社団法人|一般財団法人|公益社団法人|公益財団法人|協同組合|特定非営利活動法人|NPO法人)")
    nnk = [r for r in rows if _NONKK.search(r["引用"]) and re.search(r"(決算|貸借対照表|資本金|出資金|基金|純資産)", r["引用"])]
    if nnk: md.append(f"  - ⚠ 株式会社以外の法人の決算の引用 {len(nnk)}件（例：{nnk[0]['品目']}）：科目の名と様式が会社法の計算書類と違い、資本金の欄が無い（出資金・基金）。株式会社の欄に当てはめて比べない（4-85）")
    _TPM = re.compile(r"(TOKYO\s*PRO\s*Market|プロ向け市場|TPM上場)", re.I)
    ntpm = [r for r in rows if _TPM.search(r["引用"])]
    if ntpm: md.append(f"  - TOKYO PRO Market など、プロ向け市場の引用 {len(ntpm)}件（例：{ntpm[0]['品目']}）：有報が無いので EDINET で0件でも「無い」とせず、決算公告を探す（4-85）")
    # ③日本年金機構の適用事業所検索・厚生年金の被保険者数の引用（人数の一次に近い出どころ。パート・短時間の一部は入らない）を数える
    _NENKIN = re.compile(r"(適用事業所|厚生年金(保険)?(の)?被保険者数|被保険者数|日本年金機構)")
    nnen = [r for r in rows if _NENKIN.search(r["引用"])]
    if nnen: md.append(f"  - 日本年金機構の適用事業所・被保険者数の引用 {len(nnen)}件（例：{nnen[0]['品目']}）：社会保険に入る人の数で、求人・SNSの人数より一次に近い。加入の要件に届かない短時間の人は入らないので、常用の人数と同じ欄にしない（4-85）")
    # 2026-11-08 4-85（stats47「公務員のための Claude Code × データ前処理入門」：統計表は4行目に単位、千人を人と取り違える・億と万の取り違えが典型。
    # シート名・年の表記が「2025_04」「H25.4」と揺れる）。④引用に「単位：千人／千円／百万円」の宣言があるのに、行の単位が宣言と違うものを数える
    _UDECL = re.compile(r"単位\s*[:：]?\s*(千人|千円|百万円|億円|万円|千事業所|人|円)")
    _UMAP = {"千人": ("千人",), "千円": ("千円",), "百万円": ("百万円",), "億円": ("億円",), "万円": ("万円",), "千事業所": ("千事業所",), "人": ("人",), "円": ("円",)}
    nud = [r for r in rows if (m := _UDECL.search(r["引用"])) and r["単位"] and r["単位"] not in _UMAP[m.group(1)]]
    if nud: md.append(f"  - ⚠ 引用の単位の宣言と行の単位が違う {len(nud)}件（例：{nud[0]['品目']}・宣言「{_UDECL.search(nud[0]['引用']).group(1)}」・行「{nud[0]['単位']}」）：千人を人、億を万と読み違えた疑い。原表の単位の行で確かめるまで採らない（4-85）")
    # ⑤和暦（平成・令和・H25 など）の年がある引用で、行の年が空のものを数える（年の換算が抜けて系列に並ばない）
    _WAREKI = re.compile(r"(昭和|平成|令和)\s*(\d{1,2}|元)\s*年|(?<![A-Za-z])[HR]\s?\d{1,2}[\.．年]")
    nwk = [r for r in rows if _WAREKI.search(r["引用"]) and not str(r.get("年") or "").strip()]
    if nwk: md.append(f"  - ⚠ 和暦の年が書かれているのに行の年が空 {len(nwk)}件（例：{nwk[0]['品目']}）：西暦への換算が抜けて系列に入っていない。換算してから年ごとに並べる（4-85）")
    # 2026-11-08 4-86（電子決算公告ドットコム「貸借対照表 要旨の書き方」：要旨の公告でも当期純利益（純損失）は付記する。同じ表で千円と円が混ざる、
    # 当期未処分利益と利益剰余金・資本準備金と資本剰余金の取り違えが典型）。⑥要旨の公告で当期純損益の付記がある引用を数える（売上が無くても利益の手がかり）
    _NETP = re.compile(r"当期純(利益|損失|損益)")
    _BSCTX = re.compile(r"(貸借対照表|要旨|決算公告|官報)")
    nnp = [r for r in rows if _NETP.search(r["引用"]) and _BSCTX.search(r["引用"])]
    if nnp: md.append(f"  - 決算公告・貸借対照表の引用で当期純損益の付記があるもの {len(nnp)}件（例：{nnp[0]['品目']}）：売上は無くても利益の手がかりになる。利益の値は売上の欄に入れない（4-86）")
    # ⑦旧様式・取り違えの疑いの科目（当期未処分利益は2006年の会社法の前の様式。利益剰余金・資本剰余金と同じ欄で比べない）
    _OLDACC = re.compile(r"(当期未処分利益|当期未処理損失|未処分利益)")
    nold = [r for r in rows if _OLDACC.search(r["引用"])]
    if nold: md.append(f"  - ⚠ 旧様式の科目（当期未処分利益など）の引用 {len(nold)}件（例：{nold[0]['品目']}）：2006年より前の様式か科目の取り違え。利益剰余金と同じ欄で比べない（4-86）")
    # ⑧同じ引用に千円の数字と円の数字が混ざる（同じ表の中の単位の混在）
    _KYEN = re.compile(r"[\d,，]+\s*千円"); _YEN = re.compile(r"[\d,，]+\s*(?<![千百万億])円")
    nmix = [r for r in rows if _KYEN.search(r["引用"]) and _YEN.search(r["引用"].replace("千円", "＿＿"))]
    if nmix: md.append(f"  - ⚠ 同じ引用に千円と円の数字が混ざる {len(nmix)}件（例：{nmix[0]['品目']}）：表の中で単位が混在した疑い。行ごとの単位を原表で確かめるまで足し合わせない（4-86）")
    # 2026-11-08 4-86（comix「社長の仕事をAIに任せる」：PDFの表やグラフの数値はページ番号と単位を残し、転記後の合計を検算する）。
    # ⑨PDF から取った値で、引用にページの手がかり（p.・ページ・頁）が無いものを数える（後で原本の位置に戻れない）
    _PG = re.compile(r"(p\.\s*\d|P\.\s*\d|\d+\s*ページ|\d+\s*頁|page\s*\d)", re.I)
    npdf = [r for r in rows if (r["URL"] or "").lower().split("?")[0].endswith(".pdf") and str(r.get("値", "")).strip()]
    npg = [r for r in npdf if not _PG.search(r["引用"])]
    if npdf: md.append(f"  - PDF から取った値 {len(npdf)}件のうち、引用にページの手がかりが無いもの {len(npg)}件：結論に使う値は原本のページと単位の行を scan.md に書き添える（4-86）")
    # 2026-11-08 4-86（RAND RR1408「Assessing the Value of Structured Analytic Techniques」：最初に合う説明で止める「満足化」が典型の落とし穴）。
    # ⑩値のある行が1つのホスト・1つの取得日だけの品目を数える（最初の1か所で止まった疑い。独立の2か所目を探すまで向き・結論に使わない）
    _byit = {}
    for r in rows:
        if str(r.get("値", "")).strip(): _byit.setdefault(r["品目"], set()).add((urlparse(r["URL"]).netloc if r["URL"] else "", r["取得日"] or ""))
    nsat = [k for k, v in _byit.items() if len({h for h, _ in v}) == 1 and len({d for _, d in v}) == 1]
    if nsat: md.append(f"  - ⚠ 値が1か所・1回の取得だけの品目 {len(nsat)}件／{len(_byit)}品目（例：{nsat[0][:40]}）：最初に見つけた出どころで止まった疑い（満足化）。2か所目を探す品目として inputs に戻す（4-86）")
    # 2026-11-08 4-87（アスマーク「社会調査の回収率」：答えた人と答えなかった人で属性・意識が違う非回答バイアス。n は1回の調査の人数で、回収率と両方そろって読める）。
    # ⑪アンケート・調査の割合（%）の引用で、n・回答数・回収率のどれも書かれていないものを数える（母数と偏りが分からない割合）
    _SURV = re.compile(r"(アンケート|調査|回答)"); _PCT = re.compile(r"\d+(\.\d+)?\s*[%％]")
    _NBASE = re.compile(r"([nｎＮN]\s*[=＝]\s*[\d,，]+|回答数|有効回答|回収率|回答率|[\d,，]+\s*(社|人|件|事業所)が回答|[\d,，]+\s*(社|人|件)から回答)")
    nsv = [r for r in rows if _SURV.search(r["引用"]) and _PCT.search(r["引用"])]
    nsv_nb = [r for r in nsv if not _NBASE.search(r["引用"])]
    if nsv: md.append(f"  - 調査の割合（%）の引用 {len(nsv)}件のうち、n・回答数・回収率が書かれていないもの {len(nsv_nb)}件" + (f"（例：{nsv_nb[0]['品目']}）" if nsv_nb else "") + "：答えた会社の中の割合としか書かない。主題に関心のある先ほど答える偏り（人手不足の調査なら困っている先が多めに答える）の向きを添える（4-87）")
    # 2026-11-08 4-87（電子決算公告ドットコム「官報の決算公告記載例」：合計の行は明細の切り捨て後の和でなく合計額を改めて単位変換する。0円の科目は省く）。
    # ⑫決算公告・貸借対照表の引用で「未満切り捨て」の宣言があるものを数える（明細の和と合計が単位1つ分×明細の数までずれてよい。0円科目の省略を欠けに数えない）
    _TRUNC = re.compile(r"(未満(を)?切(り)?捨て|未満四捨五入|単位未満)")
    ntr = [r for r in rows if _TRUNC.search(r["引用"]) and _BSCTX.search(r["引用"])]
    if ntr: md.append(f"  - 決算公告の引用で端数処理（未満切り捨てなど）の宣言があるもの {len(ntr)}件（例：{ntr[0]['品目']}）：明細の和と合計は単位の端数だけずれてよい。載っていない科目は0円で省いた可能性があり、「記載なし」に数えない（4-87）")
    # 2026-11-08 4-87（Project OSINT「OSINT Chinese Companies」：持分の割合を出どころどうしで照らすと、食い違いが隠れた層を示す。集約サイトの名の照合は誤るので原本の届出で確かめる）。
    # ⑬同じ品目で出資比率・持株比率の引用が2つ以上の違う割合を示すものを数える
    _OWN = re.compile(r"(出資比率|持株比率|議決権(比率|の)?|株式の?\s*\d+(\.\d+)?\s*[%％]\s*を?(保有|取得))[^\d]{0,12}(\d+(\.\d+)?)\s*[%％]")
    _own = {}
    for r in rows:
        for m in _OWN.finditer(r["引用"]): _own.setdefault(r["品目"], set()).add(m.group(5))
    nown = [k for k, v in _own.items() if len(v) >= 2]
    if _own: md.append(f"  - 出資・持株の割合の引用がある品目 {len(_own)}件のうち、違う割合が並ぶもの {len(nown)}件" + (f"（例：{nown[0][:40]}）" if nown else "") + "：時点の違いか、間に別の会社（中間の持株会社）が挟まる疑い。登記・有報・本国の届出で持分の連なりを確かめるまで支配の向きを書かない（4-87）")
    # ⑭同じ品目に企業データの集約サイトと届出の原本（EDINET・TDnet・法人番号・官報）が並ぶ件数（食い違えば届出を採り、時点と範囲を書く）
    _AGG = re.compile(r"(baseconnect|salesnow|ullet|buffett-code|nikkei-compass|tsr-net|tdb\.co\.jp|musubu|alarmbox|kaisharesearch|houjin\.info|crunchbase|zoominfo|rocketreach)", re.I)
    _FIL = re.compile(r"(disclosure2?\.edinet-fsa|edinet-fsa\.go\.jp|release\.tdnet|tdnet|houjin-bangou\.nta\.go\.jp|kanpo\.go\.jp|sec\.gov|companieshouse|find-and-update\.company-information)", re.I)
    _af = {}
    for r in rows:
        h = urlparse(r["URL"]).netloc if r["URL"] else ""
        if _AGG.search(h): _af.setdefault(r["品目"], [0, 0])[0] += 1
        elif _FIL.search(h): _af.setdefault(r["品目"], [0, 0])[1] += 1
    nboth = [k for k, (a, f) in _af.items() if a and f]; nagg_only = [k for k, (a, f) in _af.items() if a and not f]
    if _af: md.append(f"  - 集約サイトと届出の原本が並ぶ品目 {len(nboth)}件／集約サイトだけの品目 {len(nagg_only)}件" + (f"（例：{nagg_only[0][:40]}）" if nagg_only else "") + "：集約サイトだけの値は名の照合違いがありうる。届出の原本で取り直すまで【一次で確認】にしない（4-87）")
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
        # 2026-11-09 4-110：鉱工業指数の版・季節調整・在庫の期末値・ふくらまし、宣伝の表示と例示の数字、監査人の異動
        _iip = [r for r in rs if IIPWORD.search(r["引用"])]
        if _iip:
            _m = sum(1 for r in _iip if IIPMONTHLY.search(r["引用"])); _a = sum(1 for r in _iip if IIPANNUAL.search(r["引用"]))
            if _m and _a: md.append(f"- ⚠ 鉱工業指数の月報・速報{_m}件と年報・年間補正後{_a}件が同じ品目に混在：月報の値は年報で直る。年報（年間補正後）の値だけで向きを見る（4-110）")
            _r = sum(1 for r in _iip if SARAW.search(r["引用"])); _s = sum(1 for r in _iip if SAADJ.search(r["引用"]))
            if _r and _s: md.append(f"- ⚠ 原指数{_r}件と季節調整済{_s}件が同じ品目に混在：暦年の前年比は原指数、四半期・月の前期比は季節調整済で出る。同じ欄で比べない（4-110）")
            _st = sum(1 for r in _iip if STOCKIDX.search(r["引用"]))
            if _st: md.append(f"- 在庫指数の引用{_st}件：暦年・四半期の在庫は平均でなく期末の値。生産・出荷の平均と同じ欄で並べない（4-110）")
            _fk = sum(1 for r in _iip if FUKURAMASHI.search(r["引用"]))
            if _fk: md.append(f"- 重みのふくらまし（非採用の業種・品目の按分）の語{_fk}件：業種の指数は採用品目の数量の動きで非採用分も代表させた値。細かい業種（めっき・金型）の数量の事実にしない（4-110）")
            md.append(f"- 鉱工業指数の引用{len(_iip)}件：指数は基準時の重みで固定した数量（ラスパイレス）で、金額・1人あたりの能力ではない。加工賃・出荷額の名目の向きと同じ欄で裏付けにしない（4-110）")
        _pro = [r for r in rs if PROMO.search(r["引用"])]
        if _pro: md.append(f"- ⚠ 広告・成果報酬の表示の語が{len(_pro)}件・{len({r['出典'] for r in _pro})}か所：そのページの数字は宣伝の言明。事実の裏付けに数えない（4-110）")
        _exf = sum(1 for r in rs if EXAMPLEFIG.search(r["引用"]))
        if _exf: md.append(f"- ⚠ 「例えば・例として」で置いた数字が{_exf}件：説明用の仮の例の疑い。出典の付いた値か本文で確かめるまで採らない（4-110・4-56）")
        # 2026-11-09 4-112：統計の対象の裾切りと小さい規模の品目のずれ、再編加工の値
        _kk = [r for r in rs if KIGYOKATSU.search(r["引用"])]
        if _kk and SMALLSCOPE.search(item + " " + " ".join(r["引用"] for r in _kk)):
            md.append(f"- ⚠ 企業活動基本調査の引用{len(_kk)}件が小規模・中小の文脈に出た：この調査は従業者50人以上かつ資本金3,000万円以上だけが対象。49人以下の受託の事実にしない（4-112）")
        _hq = sum(1 for r in rs if HOJINQ.search(r["引用"]))
        if _hq: md.append(f"- 法人企業統計の四半期の引用{_hq}件：資本金1,000万円未満を含まず、小さい階級は標本と回答率から参考値。年次の全規模と同じ欄で並べない（4-112）")
        _sh = sum(1 for r in rs if SAIHEN.search(r["引用"]))
        if _sh: md.append(f"- 「再編加工」の値{_sh}件：所管が個票を集計し直した値で公表の表では再現できない。表の値と食い違っても誤りと決めず、別の品目として並べる（4-112）")
        _au = [r for r in rs if AUDITCHG.search(r["引用"])]
        if _au: md.append(f"- ⚠ 監査人の異動・辞任の語が{len(_au)}件（{'・'.join(sorted({r['出典'] for r in _au})[:3])}）：外から見える危機の印。継続企業の注記・訴訟・当局の処分と並べて見る（4-110）")
        if nreb: md.append(f"- ⚠ 指数の基準年・遡及の作り直しの語が{nreb}件：基準年の違う指数、遡って作り直した系列と当時の公表値を同じ欄でつながない。同じ版の系列どうしで向きを見る（4-51）")
        ntes = sum(1 for r in rs if TESTIMONY.search(r["引用"])); htes = {urlparse(r["URL"]).netloc for r in rs if TESTIMONY.search(r["引用"]) and r["URL"]}
        if ntes: md.append(f"- ⚠ 証言（関係者・取材・インタビュー）からの数字が{ntes}件・{len(htes)}か所：1人の証言は1件の手がかりで裏付けにならない。別の証言かデータと突き合わせるまで【未確認】のまま読む（4-46）")
        # 2026-11-06 4-44：誤差の幅・回収率・自己評価・委託先の変更・内部売上の印
        nse = sum(1 for r in rs if SAMPLEERR.search(r["引用"]))
        if nse: md.append(f"- 標準誤差・標本誤差の語が{nse}件：変化がその誤差の幅より小さければ向きにしない（4-44）")
        lowr = sorted({float(m.group(4)) for r in rs for m in LOWRESP.finditer(r["引用"]) if float(m.group(4)) < 50})
        if lowr: md.append("- ⚠ 回収率・回答率が5割未満（" + "・".join(f"{v:g}%" for v in lowr[:5]) + "）：答えた先と答えない先が違えば偏る（無回答の偏り）。全体の値として言い切らない（4-44）")
        # 2026-11-08 4-93（米国センサス局 ABS 非回答バイアス分析：回収率の高低は偏りの大小と同じでない。OMB の基準は単位の回収率80%未満で
        # 非回答バイアスの分析を求める）。50〜80%の帯を「偏りの分析が要る帯」として数え、名簿の変数（業種・規模の構成）での比べ方を添える
        midr = sorted({float(m.group(4)) for r in rs for m in LOWRESP.finditer(r["引用"]) if 50 <= float(m.group(4)) < 80})
        if midr: md.append("- 回収率・回答率が5〜8割（" + "・".join(f"{v:g}%" for v in midr[:5]) + "）：偏りの分析が要る帯（OMB の80%）。回収率の高低で偏りの大小を書かず、回答の業種・規模の構成を母集団（経済センサスの同じ区分）と並べてから全体の値として読む（4-93）")
        nwt = sum(1 for r in rs if WEIGHTED.search(r["引用"]))
        if nwt: md.append(f"- 重み付け・補正の推計の語が{nwt}件：重みを付けた推計か、答えた先の単純な集計かを注記で確かめ、両者を同じ欄で比べない（4-93）")
        nmx = sum(1 for r in rs if MERGEREXIT.search(r["引用"]) and re.search(r"(廃業|退出|閉鎖|減少|消滅)", r["引用"]))
        if nmx: md.append(f"- ⚠ 合併・統合と廃業・減少の語が同じ引用に{nmx}件：合併・統合で減った分を退出に数えない。合併の効きは年を追って出るので、直後1年の変化で良し悪しを書かない（4-93）")
        # 2026-11-08 4-94（樋口 note：対象期間の違う値は比べない）。年の値の品目に部分の期間の値が混ざれば数える
        npart = sum(1 for r in rs if PARTIAL.search(r["引用"]) and str(r.get("値", "")).strip())
        if npart: md.append(f"- ⚠ 通年でない期間（上期・四半期・何か月間）の値が{npart}件：通年の値と同じ欄で比べず、年の値の採る値・向きに使わない（4-94）")
        # 2026-11-08 4-94（wattson496 note：出版バイアス＝驚きのある結果ほど採録されやすい）。一次と記事の両方がある品目で、
        # 記事の値が一次の値の最小〜最大の外にある件数を数える。記事は目立つ値に寄って載る向きがあるので、外れた記事の値は採らない
        _pv = [r["値"] for r in rs if r["一次"] == "一次" and isinstance(r.get("値"), (int, float)) and r["値"] > 0]
        _av = [r for r in rs if r["一次"] == "記事" and isinstance(r.get("値"), (int, float)) and r["値"] > 0]
        if len(_pv) >= 2 and _av:
            _lo, _hi = min(_pv), max(_pv)
            _out = [r for r in _av if r["値"] < _lo or r["値"] > _hi]
            if _out: md.append(f"- ⚠ 記事の値が一次の範囲（{_lo:g}〜{_hi:g}）の外に{len(_out)}件／記事{len(_av)}件：記事は目立つ値に寄って載る（採録の偏り）。外れた記事の値は採らず、一次の範囲で読む（4-94）")
        # 同（専門知の類型）：証言の話し手を、現場の当事者と外から語る人に分けて数える。外から語る人だけの証言は、現場の事実の裏付けに使わない
        if ntes:
            _do = sum(1 for r in rs if TESTIMONY.search(r["引用"]) and SPK_DO.search(r["引用"]))
            _tk = sum(1 for r in rs if TESTIMONY.search(r["引用"]) and SPK_TALK.search(r["引用"]) and not SPK_DO.search(r["引用"]))
            md.append(f"- 証言の話し手（4-94）：現場の当事者{_do}件・外から語る人{_tk}件・不明{ntes - _do - _tk}件" + ("。⚠ 当事者0件：現場の事実（工程・単価・人数）の裏付けに使わない" if _do == 0 else ""))
        nsr = sum(1 for r in rs if SELFREPORT.search(r["引用"]))
        if nsr: md.append(f"- ⚠ 自己評価・自己申告・自社調べの語が{nsr}件：答える側が良く見せる向きに偏りやすい。記録（統計・開示）と並べる（4-44）")
        nct = sum(1 for r in rs if CONTRACTOR.search(r["引用"]))
        if nct: md.append(f"- ⚠ 調査の委託先の変更の語が{nct}件：集め方が変わり系列の継続性が落ちる。その年を切れ目として扱う（4-44）")
        nis = sum(1 for r in rs if INTERSEG.search(r["引用"]))
        if nis: md.append(f"- ⚠ セグメント間・内部売上の語が{nis}件：外部の客への売上と内部の売上を分け、合計に内部を足さない（4-44）")
        # 2026-11-08 4-88：時代遅れの出典・会社の述べる理由・取り扱いの印・1人あたりのばらつき
        nod = sum(1 for r in rs if OUTDATED.search(r["引用"]))
        if nod: md.append(f"- ⚠ 改正前・廃止・旧基準の語が{nod}件：後の版で置き換わった出典の疑い（誤った出典の4型の④）。現行の法令・統計の版で取り直してから使う（4-88）")
        nrs = [r for r in rs if REASONWORD.search(r["引用"])]
        if nrs: md.append(f"- 増減・未達の理由を述べる文が{len(nrs)}件・{len({r['出典'] for r in nrs})}か所：述べた主体の言い分【言明】。別の出どころ（統計・取引先・競合）の数字と向きが合うかを見てから理由として書く（4-88）")
        npr = sum(1 for r in rs if r["URL"] and PERSONALHOST.search(urlparse(r["URL"]).netloc))
        if npr: md.append(f"- 取り扱い：個人のSNSの発信が{npr}件。公開の scan.md には件数と向きだけを書き、本文・個人名を写さない（内部のみ・4-88）")
        pp = {}
        for r in rs:
            if r["区分"].startswith("1人あたり") and r["年"] and r["値"] > 0: pp.setdefault((r["単位"], str(r["年"])), []).append(r["値"])
        disp = []
        for (u, y), v in sorted(pp.items()):
            if len(v) >= 4:
                qs = statistics.quantiles(v, n=4)
                if qs[0] > 0: disp.append(f"{y}年 {qs[2] / qs[0]:.2f}倍（{u}・{len(v)}件）")
        if len(disp) >= 2: md.append("- 【推定】1人あたりのばらつき（第3四分位÷第1四分位）の年ごとの並び：" + "・".join(disp[:8]) + "。平均の向きと別に、ばらつきが開いているか（資源が生産性の高い先へ動いていない兆し）を見る。出典の混ざり方が年で違えば比べない（4-88）")
        # 2026-11-06 4-50（BMJ Open 2023 の登録と論文の食い違いのメタ分析：主要な結果の差し替えが3割前後の研究で起きていた）。
        # 品目が名指しした指標（加工賃・出荷額・付加価値・従業者・事業所数・受注・給与など）が、取れた引用のどれにも出てこなければ、
        # 別の指標で答えた（結果の差し替え）疑い。その数字で品目の問いに答えたと書かず、計画どおりの指標を取り直す
        named = [k for k in INDICATOR if k in subj]
        if named and rs and not any(any(k in r["引用"] for k in named) for r in rs):
            md.append(f"- ⚠ 品目が名指しした指標（{'・'.join(named)}）が引用に1件も無い：{len(rs)}件は別の指標の数字。品目の問いの答えとして使わず、名指しした指標を取り直す（結果の差し替えの疑い・4-50）")
        # 2026-11-08 4-91（走査の失敗：「全国」を名指しした mfg_capacity の品目に、小樽市・県の統計だけが入り、全国の値として採る値が出た）。
        # 全国を名指しした品目で、引用がすべて自治体（lg.jp・pref.・city.）のホストなら、その品目の答えにしない
        if "全国" in item and rs and all(re.search(r"\.lg\.jp$|^www\.pref\.|^www\.city\.", urlparse(r["URL"]).netloc or "") for r in rs):
            md.append(f"- ⚠ 全国の品目なのに引用{len(rs)}件がすべて自治体の統計（4-91）：地域の値で全国の問いに答えない。所管（meti・e-Stat）で取り直す")
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
            # 2026-11-08 4-90（Grokipedia「Marketing data reconciliation」：指標ごとに基準の出どころを1つ決めて他をそれと比べる。
            # 許容の幅は指標の種類で変え、金額は小さい差、件数・成果は大きめの差が普通）。欄の種類ごとの基準の出どころから値が1件も無ければ印す
            _bk = next((b for p, b in BENCH if cat.startswith(p)), None)
            if _bk and not any(_bk[1].search(r["URL"]) for r in g):
                md.append(f"  - 基準の出どころ0件（4-90）：この欄の基準は{_bk[0]}。基準で取り直すまで、他の出どころの一致を水準の確定にしない")
            _ph = {}
            for r in prim:
                if r["値"] > 0: _ph.setdefault(src_key(r), []).append(r["値"])
            if ctx in ("売上・決算", "統計の総額") and len(_ph) >= 2:
                _pv = sorted(statistics.median(v) for v in _ph.values())
                if _pv[0] > 0 and 1.1 < _pv[-1] / _pv[0] <= 3:
                    md.append(f"  - ⚠ 一次どうしの金額が{_pv[-1] / _pv[0]:.2f}倍ずれる（4-90）：届出・統計の金額は普通は小さい差しか出ない。1割を超えるずれは定義（単体・連結、税込み・抜き、事業所・企業）か時点の違いを先に疑う")
            # 2026-11-08 4-90（ONS「GDP revisions in Blue Book」：直しの大きさは最初の値の大きさと並べて読む＝相対の平均絶対改訂）。
            # 同じ年に速報と確報・改定が両方ある欄で、直しの割合と、隣の年との変化の割合を並べる。直しが変化以上なら速報の向きは使えない
            _rv = []
            for y in sorted(fin_years):
                if not y: continue
                _s = [r["値"] for r in g if r["年"] == y and r.get("版") == "速報" and r["値"] > 0]
                _f = [r["値"] for r in g if r["年"] == y and r.get("版") in ("確報", "改定") and r["値"] > 0]
                if _s and _f: _rv.append((int(y), statistics.median(_f) / statistics.median(_s) - 1))
            for y, rv in _rv[:3]:
                _prev = med.get(y - 1)
                _chg = (med[y] / _prev - 1) if _prev and med.get(y) else None
                md.append(f"  - 速報→確報の直し（4-90）：{y}年 {rv:+.1%}" + (f"／前年からの変化 {_chg:+.1%}" + ("。⚠ 直しが変化以上：速報の値で向きを書かない" if abs(rv) >= abs(_chg) else "") if _chg is not None else ""))
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
    # 2026-11-08 4-95（看護研究のトライアンギュレーション：同じ現象を「異なる時・場所・人」から取って比べる＝データの三角測量）。
    # 同じ品目・同じ年・同じ単位・同じ文脈の値が、別の取得日に取り直されて1割超ずれた欄を数える（ページの改定か別のページの混入）。
    # あわせて、すべての引用が1つの取得日に固まった品目（時間の三角測量が無い）を数える。値は scan.py だけが出す
    try:
        from collections import defaultdict as _dd95
        _g95 = _dd95(list); _days95 = _dd95(set)
        for r in rows:
            _days95[r["品目"]].add(r["取得日"])
            try: _v = float(r["値"])
            except Exception: continue
            if r["予測"] or not r["年"] or not r["取得日"]: continue
            _g95[(r["品目"], r["年"], r["単位"], r["文脈"])].append((r["取得日"], _v))
        _drift = []
        for (it, y, u, c), vs in _g95.items():
            _bd = {}
            for d, v in vs: _bd.setdefault(d, []).append(v)
            if len(_bd) < 2: continue
            _med = sorted(statistics.median(x) for x in _bd.values())
            if _med[0] > 0 and _med[-1] / _med[0] > 1.1:
                _drift.append(f"  - {it[:40]}｜{y}年｜{c}｜取得日 {len(_bd)}日で中央値 {_med[0]:,.4g}〜{_med[-1]:,.4g}{u}")
        _one = sum(1 for d in _days95.values() if len(d) == 1)
        md += ["## 時間の三角測量（4-95・機械）",
               f"- 同じ品目・年・単位・文脈で、別の取得日の値が1割超ずれた欄：{len(_drift)}件（取り直しで変わった。版・ページの差し替え・別の表の混入を本文で確かめるまで、どちらも採らない）",
               *_drift[:20],
               f"- すべての引用が1つの取得日に固まった品目：{_one}件／{len(_days95)}件（時を変えた取り直しが無い。結論に効く品目から次の回で取り直す）", ""]
    except Exception: pass
    # 2026-11-08 4-95（MetricGate「Survivorship Bias in Data Analysis」：入る段と残る段の二重の選別は最悪の型。
    # 数字が全部正しくても、標本が「答えた先かつ残った先」なら平均は上に偏る。幅が狭くても中心がずれていれば誤り）。
    # 調査の回答・アンケートの値で、平均・1人あたり・中央値を名指しする品目を数え、二重の選別の疑いとして印す
    try:
        _ds95 = sorted({r["品目"] for r in rows
                        if re.search(r"回答|アンケート|調査対象|回収", r["引用"] or "")
                        and re.search(r"平均|1人あたり|1人当たり|中央値|一人当たり", (r["品目"] or "") + (r["引用"] or ""))})
        md += ["## 二重の選別の疑い（4-95・機械）",
               f"- 調査の回答の値で平均・1人あたりを語る品目：{len(_ds95)}件。標本は「答えた先かつ残った先」。四分位の幅が狭くても中心が上にずれうるので、"
               "全数の統計（経済センサス・工業統計）の同じ欄と並べるまで向き・水準の根拠にしない", *(f"  - {n[:60]}" for n in _ds95[:20]), ""]
    except Exception: pass
    # 2026-11-08 4-96（71回目の走査の失敗：method_read.txt に読んだURLが、?gs= の付いた形・%20 と + の違いで「未読」に見え、
    # 同じ記事を別の頁として数えかけた）。追跡用の問い合わせ（gs・utm_・fbclid・ref など）と末尾の / と符号化の違いを落とした形で URL を束ね、
    # 1つの頁が何通りのURLで入っているかを数える。独立の出どころの数は束ねた形で数える
    try:
        _canon = {}
        for r in rows:
            _canon.setdefault(canon_url(r["URL"]), set()).add(r["URL"])
        _multi = {k: v for k, v in _canon.items() if k and len(v) >= 2}
        md += ["## 同じ頁の別URL（4-96・機械）",
               f"- 追跡の問い合わせ・符号化・末尾の / を落とすと1つになるURL：{len(_multi)}頁（元のURL {sum(len(v) for v in _multi.values())}通り）。"
               "出典の数・読んだ記事の数は束ねた形で数える", *(f"  - {k[:90]}（{len(v)}通り）" for k, v in list(_multi.items())[:10]), ""]
    except Exception: pass
    # 2026-11-08 4-96（登記申請ラボ「法人登記番号の調べ方」：法務局の会社法人番号は12桁、国税庁の法人番号は13桁で別の番号。
    # 同じ商号の別法人・グループの似た商号があるので、商号だけで決めず本店所在地・法人種別まで合わせる）。
    # 「法人番号」と書かれた12桁の数字（会社法人番号の取り違え）と、1つの品目に別々の13桁が2つ以上ある品目（同名の別法人の疑い）を数える
    try:
        _b12 = sorted({r["品目"] for r in rows if re.search(r"法人番号[^0-9]{0,6}(?<!\d)\d{12}(?!\d)", unicodedata.normalize("NFKC", r["引用"] or ""))})
        _h13 = {}
        for r in rows:
            for m in re.findall(r"(?<!\d)\d{13}(?!\d)", unicodedata.normalize("NFKC", r["引用"] or "")):
                if re.search("法人番号", r["引用"] or ""): _h13.setdefault(r["品目"], set()).add(m)
        _same = sorted(k for k, v in _h13.items() if len(v) >= 2)
        md += ["## 法人番号の取り違えと同名の別法人（4-96・機械）",
               f"- 「法人番号」と書かれた12桁の数字（法務局の会社法人番号の取り違え）がある品目：{len(_b12)}件。国税庁の13桁で取り直すまで名寄せに使わない",
               *(f"  - {n[:60]}" for n in _b12[:10]),
               f"- 1つの品目に別々の13桁の法人番号が2つ以上ある品目：{len(_same)}件（同名の別法人・グループ会社の疑い。本店所在地・法人種別で1つに決めるまで数字を足さない）",
               *(f"  - {n[:60]}" for n in _same[:10]), ""]
    except Exception: pass
    # 2026-11-08 4-96（PRISMA 2020 の解説 BMJ 2021・PMC8005925：1つの研究に結果が複数あるとき、どれを選んだかの規則を先に書く。
    # 選び方を書かないと、都合のよい値を選んだ偏りが見えない）。同じ出どころ（ホスト）・同じ品目・年・単位で別の値が2つ以上ある欄を数え、
    # 採る規則（4-21：本文の表の値・通年・確報・低い側）を scan.md に書く対象にする
    try:
        _sel = {}
        for r in rows:
            if r["予測"]: continue
            _sel.setdefault((r["品目"], r["年"], r["単位"], r["出典"]), set()).add(r["値"])
        _ms = sorted({k[0] for k, v in _sel.items() if len(v) >= 2})
        md += ["## 同じ出どころの中の複数の値（4-96・機械）",
               f"- 同じホスト・品目・年・単位で別の値が2つ以上ある品目：{len(_ms)}件。scan.md で、どの値を採ったかの規則（表の値・通年・確報・低い側・4-21）を1行で書く",
               *(f"  - {n[:60]}" for n in _ms[:15]), ""]
    except Exception: pass
    # 2026-11-08 4-96（71回目の走査の失敗：research_method の証拠に、下着の通販・温室の設計・CRAN の新着一覧・用語集の一覧・小説の頁が
    # 50〜75件ずつ入り、手法の記事の候補の上位を占めた）。手法の言葉（調査・推定・検証・出典・統計・突き合わせ など）を含む引用が2割未満の頁を
    # 主題外として数える。主題外の頁は手法の記事の候補・引用の件数に入れない
    if topic == "research_method":
        try:
            _mk = re.compile(r"調査|調べ|推定|推計|検証|確か|出典|統計|一次|突き合わせ|裏取り|照合|偏り|バイアス|分析|情報源|research|source|estimat|verif|bias|data", re.I)
            _pq = {}
            for f in glob.glob(os.path.join(OUT, topic, "companies", "*.json")):
                try: d = json.load(open(f, encoding="utf-8"))
                except Exception: continue
                for items in (d.get("evidence") or {}).values():
                    for it in items:
                        u = canon_url(it.get("url", ""))
                        if u: _pq.setdefault(u, set()).add(it.get("quote", ""))
            _off = sorted(((u, len(q), sum(1 for x in q if _mk.search(x))) for u, q in _pq.items()
                           if len(q) >= 10 and sum(1 for x in q if _mk.search(x)) < 0.2 * len(q)), key=lambda x: -x[1])
            md += ["## 主題外の頁（4-96・機械）",
                   f"- 引用10件以上で手法の言葉を含む引用が2割未満の頁：{len(_off)}頁。手法の記事の候補・件数に入れない（検索語か取得の層を直す）",
                   *(f"  - {u[:80]}（引用 {n}件・手法の言葉 {k}件）" for u, n, k in _off[:15]), ""]
            # 次に読む候補：method_read.txt に無い（束ねた形で照合）・主題外でない頁を、手法の言葉を含む引用の数の順に
            _mr = os.path.join(os.path.dirname(__file__), "method_read.txt")
            _rd = {canon_url(l.strip()) for l in open(_mr, encoding="utf-8")} if os.path.exists(_mr) else set()
            # 2026-11-08 73回目の失敗から：抜き書きで読んで METHOD.md に出典として載せた頁（method_read.txt に足さない決まり・4-95）が
            # 未読の候補に戻っていた。METHOD.md に出てくるURLも既読に数える
            _mf = os.path.join(os.path.dirname(__file__), "METHOD.md")
            if os.path.exists(_mf):
                _rd |= {canon_url(x) for x in re.findall(r"https?://[^\s）)／]+", open(_mf, encoding="utf-8").read())}
            _offs = {u for u, _, _ in _off}
            _cand = sorted(((u, sum(1 for x in q if _mk.search(x))) for u, q in _pq.items() if u not in _rd and u not in _offs),
                           key=lambda x: -x[1])
            md += ["## 次に読む手法の記事の候補（4-96・機械）",
                   f"- 未読（束ねた形で照合）で主題外でない頁：{len(_cand)}頁。手法の言葉を含む引用の多い順に", *(f"  - {u}（{k}件）" for u, k in _cand[:15]), ""]
        except Exception: pass
    # 2026-11-08 4-97（マイビジョン「フェルミ推定」：問題はストック型（ある時点の数）とフロー型（期間の量）に分かれ、
    # 公表値と比べるときは対象の範囲と期間をそろえる。FCSM の事業所調査の非回答の報告：事業所の調査は少数の大きな先が総額の
    # 多くを占めるので、件数の回収率（重みなし）だけでは偏りが読めず、総額のうち答えた先が占める割合（重み付きの回収率）を見る）。
    # ①売上・統計の総額・市場規模の金額で、期間（年間・月間・四半期・年度・決算期）も年も書かれていないもの＝ストックかフローか、
    #   何か月分か分からない金額。②事業所・企業の調査の回収率で、重み付き（金額・出荷額・従業者のベース、カバー率）が添えられていないもの
    try:
        _FLOWCTX = ("売上・決算", "統計の総額", "市場規模")
        _PER2 = re.compile(r"(年間|月間|四半期|年度|通期|通年|決算|期[のは]|FY|上期|下期|半期|\d{4}年|累計)")
        _nop = {}
        for r in rows:
            if r["予測"] or "円" not in r["単位"] or r["文脈"] not in _FLOWCTX: continue
            if r["年"] or _PER2.search(r["引用"]): continue
            _nop.setdefault(r["品目"], 0); _nop[r["品目"]] += 1
        _RR = re.compile(r"(回収率|回答率)")
        _WRR = re.compile(r"(加重|重み|ウェイト|金額ベース|出荷額ベース|売上高ベース|従業者ベース|カバー率|捕捉率)")
        _est = re.compile(r"(事業所|企業|会社|社)")
        _rr = [r for r in rows if _RR.search(r["引用"]) and _est.search(r["引用"])]
        _rrw = [r for r in _rr if not _WRR.search(r["引用"])]
        md += ["## ストックとフロー・重み付きの回収率（4-97・機械）",
               f"- 売上・総額・市場規模の金額で、期間も年も書かれていないもの：{sum(_nop.values())}件（{len(_nop)}品目）。何か月分の量か（フロー）、ある時点の残高か（ストック）が分からないので、年間の値と同じ欄で比べない（4-94 を補う）",
               *(f"  - {n[:60]}（{c}件）" for n, c in sorted(_nop.items(), key=lambda x: -x[1])[:10]),
               f"- 事業所・企業の調査の回収率の引用 {len(_rr)}件のうち、重み付き（金額・出荷額・従業者のベース、カバー率）が書かれていないもの {len(_rrw)}件。件数の回収率が高くても大きな先が抜ければ総額は大きく欠ける。総額に占める答えた先の割合を探してから全体の値として読む",
               ""]
    except Exception: pass
    # 2026-11-08 4-98（NIRA「統計改革」0803：物価指数は指数ごとに作り方が違い、基準年の違う系列はリンクしてつなぐ。
    # 米国センサス局 2010 の事業所調査の非回答バイアス分析：欠けは行政記録の直接の値で埋めるのが先で、モデルの推計は後。
    # 大きい先の欠けから当たる）。①同じ品目の引用に「○年基準・○年=100」が2つ以上＝基準年の違う指数・実質値の混在。
    # ②同じ品目に、推計・推定と書かれていない一次の値と、推計・推定・試算と書かれた値が並ぶ＝直接の記録があるのに推計を採る恐れ。
    try:
        _BASEY = re.compile(r"((?:19|20)\d{2})\s*年?\s*(?:=\s*100|基準|価格)")
        _EST = re.compile(r"(推計|推定|試算|見込み|補完|imput)")
        _bym, _dir, _estd = {}, {}, {}
        for r in rows:
            for m in _BASEY.finditer(r["引用"]): _bym.setdefault(r["品目"], set()).add(m.group(1))
            if _EST.search(r["引用"]): _estd[r["品目"]] = _estd.get(r["品目"], 0) + 1
            elif r["一次"] == "一次" and not r["予測"]: _dir[r["品目"]] = _dir.get(r["品目"], 0) + 1
        _mix = sorted(((n, s) for n, s in _bym.items() if len(s) >= 2), key=lambda x: -len(x[1]))
        _both = sorted(((n, _dir[n], _estd[n]) for n in _dir if n in _estd), key=lambda x: -x[2])
        md += ["## 基準年の混在・直接の記録と推計の並存（4-98・機械）",
               f"- 同じ品目の引用に基準年（○年基準・○年=100）が2つ以上：{len(_mix)}品目。指数ごとに作り方も基準も違うので、どの指数のどの基準年かをそろえるまで実質の向きを書かない",
               *(f"  - {n[:60]}（{'・'.join(sorted(s))}）" for n, s in _mix[:10]),
               f"- 直接の一次の値と、推計・推定・試算と書かれた値が同じ品目に並ぶ：{len(_both)}品目。直接の記録（統計の表・開示・公告）を先に採り、推計は記録が無い先の穴埋めに限る。穴埋めは総額への寄与の大きい先から",
               *(f"  - {n[:60]}（直接 {a}件・推計 {b}件）" for n, a, b in _both[:10]),
               ""]
    except Exception: pass
    # 2026-11-08 4-99（経産省「鉱工業指数の見方」riyou-3：基準時を固定した指数は基準から遠いほど偏りが大きく、伸びる品目を
    # 小さく見せる。FCSM 2020「連邦の調査の非回答バイアス研究の系統的レビュー」：補正で偏りが減ったと述べても数字で示さない
    # 報告が多い。コンコード「フェルミ推定」：推定は実績値と同じ桁に入るかで分解を確かめ、桁が外れたら仮定に戻る）。
    # ①同じ引用の基準年と対象年の差が5年以上の指数 ②「補正・調整で偏りが減った」と書くが数字の無い引用
    # ③同じ品目・同じ単位で、推計と書かれた値の中央値と直接の値の中央値が10倍以上離れる（桁違い）
    try:
        _BY2 = re.compile(r"((?:19|20)\d{2})\s*年?\s*(?:=\s*100|基準)")
        _far = {}
        for r in rows:
            m = _BY2.search(r["引用"])
            if m and r["年"]:
                try:
                    gap = int(str(r["年"])[:4]) - int(m.group(1))
                    if gap >= 5: _far.setdefault(r["品目"], set()).add(f"{m.group(1)}基準→{str(r['年'])[:4]}")
                except Exception: pass
        _ADJ = re.compile(r"(補正|調整|ウェイト|重み付け|weight|adjust)", re.I)
        _BIAS = re.compile(r"(偏り|バイアス|bias)", re.I)
        _RED = re.compile(r"(減|改善|小さくな|解消|reduc|lower)", re.I)
        _decl = [r for r in rows if _ADJ.search(r["引用"]) and _BIAS.search(r["引用"]) and _RED.search(r["引用"]) and not re.search(r"\d", r["引用"])]
        _EST2 = re.compile(r"(推計|推定|試算|見込み)")
        _ge, _gd = {}, {}
        for r in rows:
            if r["予測"] or not r["単位"]: continue
            k = (r["品目"], r["単位"])
            (_ge if _EST2.search(r["引用"]) else _gd).setdefault(k, []).append(r["値"])
        _ord = []
        for k in _ge:
            if k in _gd:
                a, b = statistics.median(_ge[k]), statistics.median(_gd[k])
                if a > 0 and b > 0 and max(a, b) / min(a, b) >= 10:
                    _ord.append((k, a, b, max(a, b) / min(a, b)))
        _ord.sort(key=lambda x: -x[3])
        md += ["## 基準からの距離・数字の無い補正の宣言・推計と実績の桁（4-99・機械）",
               f"- 基準年から5年以上離れた年の指数を引く品目：{len(_far)}品目。固定基準の指数は基準から遠いほど伸びる品目を小さく見せる偏りが大きい。基準改定後の系列か、連鎖の系列で取り直す",
               *(f"  - {n[:60]}（{'・'.join(sorted(s)[:4])}）" for n, s in sorted(_far.items(), key=lambda x: -len(x[1]))[:10]),
               f"- 補正・調整で偏りが減ったと書くが数字の無い引用：{len(_decl)}件。減ったことの宣言は確かめられていないものとして扱い、補正前後の値が並ぶ原表を探す",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _decl[:5]),
               f"- 同じ品目・同じ単位で、推計と書かれた値と直接の値の中央値が10倍以上離れる：{len(_ord)}組。桁が外れたら分解の仮定（単位・分母・範囲）に戻る",
               *(f"  - {k[0][:50]}（{k[1]}）：推計の中央値 {a:,.4g}・直接の中央値 {b:,.4g}・{x:,.1f}倍" for k, a, b, x in _ord[:10]),
               ""]
    except Exception: pass
    # 2026-11-08 4-100（小川真由「淘汰されていく中小製造業」MONOist：本文の「約1割」が表の値では1割超、指数は基準年=100、
    # 従業者4人以上の本表と3人以下の推計表は別系列）。丸め語だけの量・指数の値・裾切りの違う系列の混在を数える
    try:
        _ROUND = re.compile(r"(約|およそ|ほぼ|概ね)?([一二三四五六七八九十数][割倍]|倍増|半減|激減|急増|大幅に?[増減])")
        _UNITNUM = re.compile(r"\d[\d,\.]*\s*(%|％|円|人|件|社|事業所|倍|割)")
        _rnd = [r for r in rows if _ROUND.search(r["引用"]) and not _UNITNUM.search(r["引用"])]
        _IDX = re.compile(r"(=|＝)\s*100|100\s*と(し|する)|指数")
        _idx = {}
        for r in rows:
            if _IDX.search(r["引用"]): _idx[r["品目"]] = _idx.get(r["品目"], 0) + 1
        _cut = {}
        for r in rows:
            q = r["引用"]
            if re.search(r"(3|３|三)人以下", q): _cut.setdefault(r["品目"], set()).add("3人以下(推計表)")
            if re.search(r"(4|４|四)人以上", q): _cut.setdefault(r["品目"], set()).add("4人以上(本表)")
            if re.search(r"(10|１０)人以上", q): _cut.setdefault(r["品目"], set()).add("10人以上")
        _mix = {k: v for k, v in _cut.items() if len(v) >= 2}
        md += ["## 丸め語・指数・裾切りの混在（4-100・機械）",
               f"- 量を丸め語（約1割・倍増・半減など）だけで言い、数字の無い引用：{len(_rnd)}件。丸め語は向きの手がかりにとどめ、大きさは表の値から出す",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _rnd[:5]),
               f"- 指数（基準=100）を含む引用のある品目：{len(_idx)}品目。指数の値は実額・人数の欄に入れない。基準年を値に添える",
               *(f"  - {k[:50]}（{n}件）" for k, n in sorted(_idx.items(), key=lambda x: -x[1])[:8]),
               f"- 裾切り（従業者4人以上の本表・3人以下の推計表など）の違う系列が同じ品目に混ざる：{len(_mix)}品目。範囲の違う系列は同じ欄で比べない",
               *(f"  - {k[:50]}（{'・'.join(sorted(v))}）" for k, v in list(_mix.items())[:8]),
               ""]
    except Exception: pass
    # 2026-11-08 4-101（宮野 note「景気動向指数の読み方」：先行・一致・遅行に分け、先行は1本で決めず複数の分野で同じ向きが
    # 重なるかを見る、一致は水準でなく勢い（増加・加速・下げ止まり）で読む。あゆさわ「与信管理」：公告の純資産は簿価で、
    # 実態修正で下がる側。Quartz「Bad Data Guide」：知りえない量は誰かの推定）。
    # ①引用を先行・一致・遅行に振り分け、先行だけの品目を出す ②向きの語を持つ引用の分野の数（信号の重なり）
    # ③年ごとの中央値の最後の3点の勢い ④公告・官報の純資産を「簿価・上限側」として数える ⑤知りえない量の言い切り
    try:
        _LEAD = re.compile(r"(新規求人|求人数|求人倍率|受注|引き合い|設備投資計画|増産計画|新工場|採用計画|景況|見通し|DI)")
        _COIN = re.compile(r"(出荷|生産額|生産量|生産指数|売上|販売額|稼働率|加工賃)")
        _LAG = re.compile(r"(従業者数|雇用者|常用労働者|賃金|給与|在庫|倒産|廃業)")
        _FIELD = {"受注・需要": re.compile(r"(受注|引き合い|需要|販売)"), "在庫": re.compile(r"在庫"),
                  "雇用の入口": re.compile(r"(求人|採用|入職)"), "設備・投資": re.compile(r"(設備|投資|新工場|増産)"),
                  "価格・単価": re.compile(r"(価格|単価|値上げ|値下げ|加工賃)"), "出荷・生産": re.compile(r"(出荷|生産)")}
        _UP = re.compile(r"(増|伸び|上昇|拡大|改善|回復)")
        _DN = re.compile(r"(減|低下|縮小|悪化|下落|落ち込)")
        _cls, _fld = {}, {}
        for r in rows:
            q, n = r["引用"], r["品目"]
            c = _cls.setdefault(n, {"先行": 0, "一致": 0, "遅行": 0})
            if _LEAD.search(q): c["先行"] += 1
            if _COIN.search(q): c["一致"] += 1
            if _LAG.search(q): c["遅行"] += 1
            sgn = "増" if (_UP.search(q) and not _DN.search(q)) else ("減" if (_DN.search(q) and not _UP.search(q)) else None)
            if sgn:
                for f, p in _FIELD.items():
                    if p.search(q): _fld.setdefault((n, sgn), set()).add(f)
        _leadonly = sorted((n for n, c in _cls.items() if c["先行"] and not c["一致"]), key=lambda n: -_cls[n]["先行"])
        _one = sorted(((n, s, list(v)[0]) for (n, s), v in _fld.items() if len(v) == 1))
        _multi = sorted(((n, s, v) for (n, s), v in _fld.items() if len(v) >= 3), key=lambda x: -len(x[2]))
        _ser = {}
        for r in rows:
            if r["予測"] or not r["年"] or not r["単位"]: continue
            if r["一次"] != "一次": continue  # 2026-11-08 77回目：一次でない値・別の会社の値が混ざり +9442700% のような勢いが出た。一次だけで系列を作る
            try: y = int(str(r["年"])[:4])
            except Exception: continue
            _ser.setdefault((r["品目"], r["単位"], r["文脈"]), {}).setdefault(y, []).append(r["値"])
        _mom, _momx = [], []
        for k, by in _ser.items():
            ys = sorted(by)
            if len(ys) < 3: continue
            a, b, c3 = (statistics.median(by[y]) for y in ys[-3:])
            if a <= 0 or b <= 0: continue
            g1, g2 = b / a - 1, c3 / b - 1
            if abs(g1) > 0.5 or abs(g2) > 0.5 or len(set(len(by[y]) for y in ys[-3:])) > 1:
                _momx.append(k); continue  # 区間で ±50% 超か、年で値の数が違う＝同じ系列でない疑い
            if g2 > 0 and g1 > 0: lab = "増加・加速" if g2 > g1 else "増加・減速"
            elif g2 > 0 >= g1: lab = "減少→下げ止まり・反転"
            elif g2 <= 0 < g1: lab = "増加→頭打ち・反転"
            else: lab = "減少・加速" if g2 < g1 else "減少・減速"
            _mom.append((k, ys[-3:], lab, g1, g2))
        _KOUKOKU = re.compile(r"(官報|決算公告|貸借対照表の要旨|電子公告)")
        _nav = [r for r in rows if _KOUKOKU.search(r["引用"]) and re.search(r"(純資産|資本合計)", r["引用"])]
        _UNK = re.compile(r"(すべての|全ての|全国の全|世界中の|あらゆる).{0,12}(企業|会社|工場|事業所|人).{0,20}\d")
        _unk = [r for r in rows if _UNK.search(r["引用"]) and r["一次"] != "一次"]
        md += ["## 先行・一致・遅行と信号の重なり・勢い・公告の簿価（4-101・機械）",
               f"- 先行の信号（求人・受注・設備計画・景況など）だけで、一致の信号（出荷・生産・売上・加工賃）が無い品目：{len(_leadonly)}品目。先行は外れることがあるので、一致の系列に波及したかを見るまで向きの結論にしない",
               *(f"  - {n[:60]}（先行 {_cls[n]['先行']}件・遅行 {_cls[n]['遅行']}件）" for n in _leadonly[:10]),
               f"- 同じ向きの語が1つの分野（受注・在庫・雇用の入口・設備・価格・出荷）にしか出ない品目×向き：{len(_one)}組。1分野の信号は重なりが無いとして、別の分野の同じ向きを探す",
               *(f"  - {n[:50]}（{s}・{f}だけ）" for n, s, f in _one[:10]),
               f"- 同じ向きが3分野以上で重なる品目×向き：{len(_multi)}組（重なりの強い信号。それでも一致の系列で確かめる）",
               *(f"  - {n[:50]}（{s}・{'・'.join(sorted(v))}）" for n, s, v in _multi[:8]),
               f"- 年ごとの中央値の最後の3点の勢い（水準でなく勢いで読む。隣の年の跳び・段差の点検 4-5・4-89 を通るまで向きにしない）：{len(_mom)}系列",
               *(f"  - {k[0][:45]}（{k[1]}・{k[2]}・{ys[0]}→{ys[2]}）：{lab}（前の区間 {g1:+.1%}・直近の区間 {g2:+.1%}）" for k, ys, lab, g1, g2 in _mom[:12]),
               f"- 勢いから外した系列（区間で ±50% 超の跳び、または年で値の数が違う＝別の会社・別の表の値が混ざった疑い・4-89）：{len(_momx)}系列",
               f"- 官報・決算公告の純資産を含む引用：{len(_nav)}件。公告の純資産は簿価で、回収できない売掛金・在庫・資産の含み損で実態は下がる側。上限側の値として読み、与信の判断に使うときは数期の並びと当期純損益と組で見る",
               f"- 「すべての企業」のように数えようのない範囲の数字を一次でない出どころが言い切る引用：{len(_unk)}件。知りえない量は誰かの推定として【推定】で扱い、作り方を探す",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _unk[:5]),
               ""]
    except Exception: pass
    # 2026-11-08 4-102（Dhami ほか 2019「情報分析の競合仮説分析」：合わない証拠の数だけで決めると支持する証拠と基準率を捨て、
    # 同順位が増え、判定の規則がぶれる。チェスター「非上場株式の評価の従業員数」：人数は継続勤務の人＋それ以外の労働時間÷1,800、
    # 役員の一部は除く＝人数は定義で変わる。Warner「CTIの構造化分析」：情報の質の点検を結論の前と後に）。
    # ①品目ごとに人数の定義の語が何種類混ざるか ②「1人あたり」の引用で分母の定義が書かれていないもの
    # ③品目ごとに上向き・下向きの引用の数と一次の数を両方出す（合わない側だけを数えない）・同数は「判定できない」
    try:
        _HEAD = {"従業者": re.compile(r"従業者"), "従業員": re.compile(r"従業員|社員数"), "常用労働者": re.compile(r"常用(労働|雇用)者"),
                 "被保険者": re.compile(r"被保険者"), "正社員": re.compile(r"正社員|正規"), "パート・時間換算": re.compile(r"パート|非正規|時間換算|1,?800時間"),
                 "役員含む": re.compile(r"役員|個人業主|家族従業者"), "就業者": re.compile(r"就業者|労働者")}
        _hd = {}
        for r in rows:
            if "人" not in str(r["単位"]) and not re.search(r"(従業|社員|人員|雇用)", r["引用"]): continue
            for k, p in _HEAD.items():
                if p.search(r["引用"]): _hd.setdefault(r["品目"], set()).add(k)
        _mixhd = sorted(((n, v) for n, v in _hd.items() if len(v) >= 2), key=lambda x: -len(x[1]))
        _PER = re.compile(r"(1人|一人|１人)(あたり|当たり)")
        # 2026-11-08 78回目の走査の失敗から：経済センサスの表の注記（「注:「事業所数」、「年間入場者1人当たり売上高」…」）は
        # 他の産業の項目名の並びで、受託の1人あたりの値ではない。注記・項目名の並びから始まる引用は1人あたりの点検から外して数だけ出す
        _NOTE = re.compile(r"^\s*(注|※)\s*\d*\s*[:：]")
        _pernote = [r for r in rows if _PER.search(r["引用"]) and _NOTE.search(r["引用"])]
        _per = [r for r in rows if _PER.search(r["引用"]) and not _NOTE.search(r["引用"])]
        _perno = [r for r in _per if not any(p.search(r["引用"]) for p in _HEAD.values())]
        _UP2 = re.compile(r"(増|伸び|上昇|拡大|改善|回復)")
        _DN2 = re.compile(r"(減|低下|縮小|悪化|下落|落ち込)")
        _ev = {}
        for r in rows:
            q = r["引用"]; u, d = bool(_UP2.search(q)), bool(_DN2.search(q))
            if u == d: continue
            e = _ev.setdefault(r["品目"], {"上": 0, "下": 0, "上一次": 0, "下一次": 0})
            k = "上" if u else "下"; e[k] += 1
            if r["一次"] == "一次": e[k + "一次"] += 1
        _tie = sorted(n for n, e in _ev.items() if e["上"] == e["下"])
        _both = sorted(((n, e) for n, e in _ev.items() if e["上"] and e["下"] and e["上"] != e["下"]), key=lambda x: -(x[1]["上"] + x[1]["下"]))
        md += ["## 人数の定義・1人あたりの分母・支持と反証を両方数える（4-102・機械）",
               f"- 人数の引用に2種類以上の定義（従業者・従業員・常用労働者・被保険者・正社員・パートの時間換算・役員含む）が混ざる品目：{len(_mixhd)}品目。定義の違う人数で割らず、同じ定義どうしで1人あたりを出す",
               *(f"  - {n[:55]}（{'・'.join(sorted(v))}）" for n, v in _mixhd[:10]),
               f"- 「1人あたり」の引用 {len(_per)}件のうち、分母の人数の定義が本文に無いもの：{len(_perno)}件。定義が分かるまで他の出典の1人あたりと並べない",
               f"- 表の注記・項目名の並び（「注:」から始まる）で1人あたりの語を含むため点検から外した引用：{len(_pernote)}件（値にしない・4-5）",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _perno[:5]),
               f"- 上向きと下向きの引用が同数の品目（判定できない・同順位）：{len(_tie)}品目",
               *(f"  - {n[:55]}" for n in _tie[:8]),
               f"- 上向き・下向きの両方がある品目（合わない側だけでなく両方と一次の数を見る）：{len(_both)}品目",
               *(f"  - {n[:45]}：上向き {e['上']}件（一次 {e['上一次']}）・下向き {e['下']}件（一次 {e['下一次']}）" for n, e in _both[:10]),
               ""]
    except Exception: pass
    # 2026-11-08 4-103（長崎県統計年鑑「第7章 鉱工業・建設」の用語の説明：製造品出荷額等は従業者30人以上の事業所では
    # 在庫・半製品の増減を含み、29人以下では出荷額＋加工賃収入額だけ。付加価値額は30人以上だけで、29人以下は粗付加価値。
    # 合計の行は1〜3人を含むことがある。instagit「private-company-research の確度」：同じ確度の出どころが割れたら両方を並べ採否の列を付ける。
    # Foodservice IP「市場を調べているのか、仮定を確かめているのか」：熱の数字（市場規模・成長率）でなく払う側の採算で見る）。
    # ①同じ品目に29人以下と30人以上の規模が混ざる出荷額・付加価値の行 ②4人以上と裾切りの書かれない合計が同じ品目に混ざる行
    # ③品目ごとに「熱の数字」だけで「払う側の採算」（人件費・利益率・保管・在庫・粗利）の引用が無いもの
    try:
        def _szlo(s):
            m = re.search(r"(\d+)\s*[〜~～\-－]\s*(\d+)\s*人", s or "")
            if m: return int(m.group(1)), int(m.group(2))
            m = re.search(r"(\d+)\s*人以上", s or "")
            if m: return int(m.group(1)), 10**6
            m = re.search(r"(\d+)\s*人以下", s or "")
            if m: return 1, int(m.group(1))
            return None
        _sz = {}
        for r in rows:
            if not re.search(r"(出荷|付加価値)", r["区分"] + r["引用"]): continue
            b = _szlo(r["規模"] + " " + r["引用"])
            if not b: continue
            e = _sz.setdefault(r["品目"], set())
            e.add("29人以下" if b[1] <= 29 else ("30人以上" if b[0] >= 30 else "またぐ"))
        _szmix = sorted(n for n, v in _sz.items() if "29人以下" in v and "30人以上" in v)
        _thr = {}
        for r in rows:
            if r["一次"] != "一次": continue
            k = "4人以上" if re.search(r"4\s*人以上", r["規模"] + r["引用"]) else ("裾切り不明" if not r["規模"] else None)
            if k: _thr.setdefault(r["品目"], set()).add(k)
        _thrmix = sorted(n for n, v in _thr.items() if len(v) == 2)
        _HYPE = re.compile(r"(市場規模|CAGR|年平均成長率|成長率|兆円|億ドル)")
        _ECON = re.compile(r"(人件費|利益率|粗利|採算|保管|在庫|原価|単価|回収)")
        _hy = {}
        for r in rows:
            e = _hy.setdefault(r["品目"], [0, 0])
            if _HYPE.search(r["引用"]): e[0] += 1
            if _ECON.search(r["引用"]): e[1] += 1
        _hyonly = sorted(((n, e[0]) for n, e in _hy.items() if e[0] >= 2 and e[1] == 0), key=lambda x: -x[1])
        md += ["## 規模で変わる出荷額の定義・裾切りの混在・熱の数字だけの品目（4-103・機械）",
               f"- 出荷額・付加価値の行に従業者29人以下と30人以上が混ざる品目：{len(_szmix)}品目。29人以下は在庫増減を含まず付加価値も粗付加価値なので、規模をまたいで1人あたり・付加価値率を並べない",
               *(f"  - {n[:60]}" for n in _szmix[:8]),
               f"- 一次の行に「4人以上」と裾切りの書かれない値が混ざる品目：{len(_thrmix)}品目。合計に1〜3人が入っていないかを表の注記で確かめてからつなぐ",
               *(f"  - {n[:60]}" for n in _thrmix[:8]),
               f"- 市場規模・成長率の引用が2件以上あり、払う側の採算（人件費・利益率・在庫・単価・回収）の引用が0件の品目：{len(_hyonly)}品目。熱の数字だけで A1 の候補を決めない",
               *(f"  - {n[:55]}（熱の数字 {c}件）" for n, c in _hyonly[:8]),
               ""]
    except Exception: pass
    # 2026-11-08 4-104（中小企業白書2023 第1部第3章第3節「生産性の現況」：労働生産性は従業員1人あたり付加価値だが、
    # 厳密な分母は従業員数×労働時間で、同じ節の中でも人数あたりと時間あたり（2015年価格）が混ざる。規模は期初の規模で分ける。
    # TFP は寄与分解の値で労働生産性の水準とは別物。小川真由 MONOist「ファクトから考える中小製造業の生きる道(4)」：
    # 規模を資本金で区切り、法人企業統計の1人あたりに OECD の平均労働時間を掛け合わせた合成の値・ドル建ては為替で動く。
    # ICFJ「Working with Data」：作り方・標本・誤差・欠け・用語集のメタデータを先に見る。Reynolds Center
    # 「How to check a source's statistics」：原典を示せない調査・自社の客だけの調査は母集団の数字にしない）。
    # ①品目ごとに人数あたりと時間あたりが混ざる ②規模の区切りが資本金と従業者で混ざる ③ドル建て・為替換算の値
    # ④TFP・全要素生産性の引用 ⑤一次でない「調査」の割合で、回答数・n・方法が本文に無いもの
    try:
        _PH = re.compile(r"(1時間|一時間|時間)(あたり|当たり)|人時|マンアワー|per hour", re.I)
        _PP = re.compile(r"(1人|一人|１人|従業員|従業者)(あたり|当たり)|per (employee|worker|head)", re.I)
        _dn = {}
        for r in rows:
            q = r["引用"]
            if _PH.search(q): _dn.setdefault(r["品目"], set()).add("時間あたり")
            if _PP.search(q): _dn.setdefault(r["品目"], set()).add("人数あたり")
        _dnmix = sorted(n for n, v in _dn.items() if len(v) == 2)
        _CAPB = re.compile(r"資本金\s*[\d,０-９]+\s*(千|百)?万?円|資本金\s*\d+\s*億円")
        _EMPB = re.compile(r"\d+\s*[〜~～\-－]\s*\d+\s*人|\d+\s*人(以上|以下|未満)")
        _sb = {}
        for r in rows:
            s = r["規模"] + " " + r["引用"]
            if _CAPB.search(s): _sb.setdefault(r["品目"], set()).add("資本金")
            if _EMPB.search(s): _sb.setdefault(r["品目"], set()).add("従業者")
        _sbmix = sorted(n for n, v in _sb.items() if len(v) == 2)
        _USD = re.compile(r"(米?ドル|USD|US\$|\$\s*\d)")
        _usd = [r for r in rows if _USD.search(r["引用"] + " " + str(r["単位"])) and re.search(r"(生産性|付加価値|1人|一人|賃金|売上)", r["引用"])]
        _TFP = re.compile(r"(TFP|全要素生産性)")
        _tfp = [r for r in rows if _TFP.search(r["引用"])]
        _SURV = re.compile(r"(調査|アンケート|回答)")
        _NMETH = re.compile(r"(n\s*[=＝]|回答(数|者数|社数)|有効回答|サンプル|標本|対象[:：]|調査対象|調査方法|調査期間)", re.I)
        _nosamp = [r for r in rows if r["一次"] != "一次" and "%" in (r["引用"] + str(r["単位"])) and _SURV.search(r["引用"]) and not _NMETH.search(r["引用"])]
        md += ["## 人数あたりと時間あたり・規模の区切り・為替・TFP・標本の書かれない調査（4-104・機械）",
               f"- 同じ品目に「人数あたり」と「時間あたり（人時）」の引用が混ざる品目：{len(_dnmix)}品目。分母が違うので同じ欄で並べず、時間あたりは労働時間の出どころ（実労働か平均か）を添える",
               *(f"  - {n[:60]}" for n in _dnmix[:8]),
               f"- 規模の区切りが資本金（法人企業統計・白書）と従業者数（工業統計・経済構造実態調査）で混ざる品目：{len(_sbmix)}品目。区切りの基準が違う「中小」を同じ規模として並べない",
               *(f"  - {n[:60]}" for n in _sbmix[:8]),
               f"- ドル建て・為替換算の生産性・売上・賃金の引用：{len(_usd)}件。円との比べは換算の年と率（市場レートか購買力平価か）を添え、向きは円の系列で見る",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _usd[:5]),
               f"- TFP（全要素生産性）の引用：{len(_tfp)}件。寄与分解の変化率で、1人あたりの水準や向きの値として使わない",
               f"- 一次でない「調査」の割合（%）で、回答数・対象・方法が本文に無い引用：{len(_nosamp)}件。原典の調査票と回答者の範囲（自社の客だけか）を確かめるまで母集団の数字にしない",
               *(f"  - {r['品目'][:40]}：{r['引用'][:60]}" for r in _nosamp[:5]),
               ""]
    except Exception: pass
    # 2026-11-08 4-105（総務省 諮問第149号の概要 2021-03-24：工業統計は2022年から経済構造実態調査に包摂され、
    # 従業者30人以上は全数・4〜29人は抽出（約24.5万事業所）、母集団は事業所母集団データベースに変わり、公表は推計込みの確報1回。
    # 2021年は経済センサス‐活動調査の年で経済構造実態調査は行わない。Reynolds Center（Erik Sherman 2017）「How to vet story sources」：
    # 「上位」「最大手」のような自称の地位は、名指しされた一覧（ランキングそのもの）で照合する。OpenRegistry（dev.to）
    # 「Tracing a Cypriot holding company across 4 registries」：住所の共有だけでは関係の根拠にならず、赤信号は次に調べる手がかり）。
    # ①一次でない引用の自称の順位・地位 ②2022年以降の4〜29人の値（抽出推計）③2021年の値を経済構造実態調査とする引用
    # ④住所・所在地の一致だけで同じ・関係ありとする引用
    try:
        _RANK = re.compile(r"(No\.?\s*1|ナンバーワン|トップシェア|シェア\s*(No|ナンバー|1位|首位|トップ)|業界(最大|トップ|首位|初)|最大手|国内(最大|首位|トップ)|世界(最大|トップ|初)|リーディング|leading|largest|Fortune\s*500)", re.I)
        _rank = [r for r in rows if r["一次"] != "一次" and _RANK.search(r["引用"])]
        _rk = {}
        for r in _rank: _rk[r["品目"]] = _rk.get(r["品目"], 0) + 1
        def _yr(r):
            try: return int(str(r["年"])[:4])
            except Exception: return None
        _smp = [r for r in rows if (_yr(r) or 0) >= 2022 and re.search(r"(経済構造実態|工業統計)", r["引用"] + r["出典"])
                and re.search(r"(4\s*[〜~～\-－]\s*29\s*人|29\s*人以下|30\s*人未満)", r["規模"] + " " + r["引用"])]
        _c21 = [r for r in rows if _yr(r) == 2021 and "経済構造実態" in (r["引用"] + r["出典"]) and "センサス" not in r["引用"]]
        _ADDR = re.compile(r"(同じ(住所|所在地)|所在地が(同じ|一致)|住所が(同じ|一致)|同一(住所|所在地))")
        _addr = [r for r in rows if _ADDR.search(r["引用"])]
        md += ["## 自称の地位・統計の包摂後の抽出推計・住所だけの一致（4-105・機械）",
               f"- 一次でない引用の自称の順位・地位（No.1・最大手・トップシェア・業界初・leading 等）：{len(_rank)}件・{len(_rk)}品目。名指しされた一覧・ランキングの原本で照合するまで事実にせず、会社の言い分として読む",
               *(f"  - {n[:50]}：{c}件" for n, c in sorted(_rk.items(), key=lambda x: -x[1])[:8]),
               f"- 2022年以降の工業統計・経済構造実態調査で従業者4〜29人（29人以下）の値：{len(_smp)}件。包摂後は抽出の推計値（30人以上は全数）なので【推定】として読み、2021年以前の全数の値と同じ系列でつながない",
               *(f"  - {r['品目'][:40]}（{r['年']}）：{r['引用'][:50]}" for r in _smp[:5]),
               f"- 2021年の値を経済構造実態調査とする引用：{len(_c21)}件。2021年は経済センサス‐活動調査の年で同調査は行われないので、調査の年と実績の年の取り違え（4-34）を先に疑う",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _c21[:5]),
               f"- 住所・所在地の一致を根拠にした同一・関係の引用：{len(_addr)}件。住所の共有（バーチャルオフィス・同じビル・グループの本店）だけでは同じ会社・関係ありと決めない（4-8）",
               ""]
    except Exception: pass
    # 2026-11-08 4-106（In Practise「Primary Research Methods: Working with Expert Networks」2024-12-21：
    # 意見より「何が起きたか」の事実の説明を採る・辞めた経緯で見方が偏る・初めの数件の話を繰り返すだけの声に寄らない。
    # EBU Spotlight「Tracing beneficial ownership with OSINT」：名義だけの役員（ノミニー）・数百社を兼ねる役員・秘匿性の高い法域は赤信号、
    # 規制の緩い法域の登記は古い・誤りがあり暫定と印す。OSINT Industries「Cracking the Shells」2025-07-22：
    # 「休眠」と称する会社が大きな資産を持つような食い違い（incongruity）を手がかりにする）。
    # ①一次でない引用で数字を持つ見立て・意見の文 ②元社員・退職者・OB の証言 ③名義・登記代行・秘匿法域の語 ④休眠・活動なしと金額が同じ引用
    # ⑤1人の役員が多数の会社を兼ねる引用。どれも事実の確定に使わず、件数を出して次に調べる手がかりにする。
    try:
        _OPN = re.compile(r"(と思う|と思います|と考え(る|ます|られる)|だろう|でしょう|はずだ|はずです|とみられ|と見られ|と予想|と推測|気がする|感覚では|印象では|肌感)")
        _opn = [r for r in rows if r["一次"] != "一次" and _OPN.search(r["引用"])]
        _ok = {}
        for r in _opn: _ok[r["品目"]] = _ok.get(r["品目"], 0) + 1
        _EX = re.compile(r"(元社員|元従業員|元幹部|元役員|元営業|元工場長|元担当|退職者|退職した|OB|OG|former\s+(employee|executive|manager))", re.I)
        _ex = [r for r in rows if _EX.search(r["引用"])]
        _NOM = re.compile(r"(名義(上|貸し|だけ)|ノミニー|nominee|登記代行|バーチャルオフィス|私書箱|ペーパーカンパニー|タックスヘイブン|租税回避地|ケイマン|英領ヴァージン|BVI|パナマ|セーシェル|shell\s+compan)", re.I)
        _nom = [r for r in rows if _NOM.search(r["引用"])]
        _DOR = re.compile(r"(休眠|活動(実態)?(が)?な(い|し)|実態のない|dormant)", re.I)
        _dor = [r for r in rows if _DOR.search(r["引用"]) and re.search(r"(億|万|千)\s*円|百万円", r["引用"])]
        _MULTI = re.compile(r"(\d{2,4})\s*社(の|で)?(役員|取締役|代表)(を)?(兼|務)")
        _multi = [r for r in rows if _MULTI.search(r["引用"])]
        md += ["## 意見と事実・辞めた人の声・名義と秘匿法域・休眠の食い違い・役員の多数兼任（4-106・機械）",
               f"- 一次でない引用で、見立て・意見の文（〜と思う・だろう・とみられる・予想・肌感）に乗った数字：{len(_opn)}件・{len(_ok)}品目。何が起きたかの事実の文でないので、採る値・裏付けにせず、事実の出どころを探す手がかりにする",
               *(f"  - {n[:50]}：{c}件" for n, c in sorted(_ok.items(), key=lambda x: -x[1])[:8]),
               f"- 元社員・退職者・OB の証言の引用：{len(_ex)}件。辞めた経緯（良い別れか悪い別れか）で見方が偏り、今の中の事実とは時点も違うので【言明】にとどめ、時点を添える（4-46・4-94）",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _ex[:5]),
               f"- 名義・登記代行・バーチャルオフィス・秘匿性の高い法域（ケイマン・BVI・パナマ等）の語がある引用：{len(_nom)}件。その法域の登記の値は古い・誤りの恐れがあり暫定と印し、支配・同一の根拠にしない（4-8・4-87）",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _nom[:5]),
               f"- 「休眠・活動なし」と金額が同じ引用に並ぶもの：{len(_dor)}件。休眠と言いながら大きな資産・取引がある食い違いは、別会社の取り違えか実態の隠れの手がかりとして本文で確かめる",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _dor[:5]),
               f"- 1人の役員が多数（10社以上）の会社を兼ねる引用：{len(_multi)}件。名義だけの役員の疑いとして、その人の名で同じ会社と束ねない（4-8・4-105）",
               ""]
    except Exception: pass
    # 2026-11-08 4-107（Pew Research Center「How we check numbers and facts」2019：書いた人と別の点検者が、出す文の数字を1つずつ元の表と照らし、
    # 直しを入れた後にもう一度照らす。GIJN「10 Simple Data Errors」：国ごとの小数点・桁区切りの違い、範囲から外れた値は桁の打ち間違いを疑う。
    # Visualize Value「Nobody Checks the Math」：大きい数字ほど誰も検算しない。最大の値から順に確かめる）。
    # ①前回の scan.md（Claude が書いた解釈）の数字のうち、今回の scan_auto.md・cross.csv のどこにも無いもの＝写し違いか古い値の疑い
    # ②日本語でない引用の欧州式の書き方（1.234.567／12,5 %）＝桁・小数点の読み違いの疑い
    # ③単位ごとの最大の値が中央値の100倍以上＝大きい数字から本文で確かめる
    try:
        _sp = os.path.join(OUT, topic, "scan.md")
        _NUMU = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(億円|万円|百万円|千円|円|万人|人|社|事業所|件|%|％|倍)")
        _miss, _tot = [], 0
        if os.path.exists(_sp):
            _have = set()
            _auto = "\n".join(md)
            # 83回目：単位が数字の直後に無い書き方（「734.2→792.5（年率」）を拾えず、scan_auto.md にある値を「見当たらない」と誤って並べた。単位なしの数字も全部持つ
            for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", _auto): _have.add(m.group(0).replace(",", "")); _have.add(m.group(0).replace(",", "").rstrip("0").rstrip("."))
            for r in rows:
                _have.add(str(r["原文の数字"]).replace(",", ""))
                for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", str(r["原文の数字"])): _have.add(m.group(0).replace(",", ""))
                try:
                    _v = float(r["値"]); _have.add(f"{_v:g}"); _have.add(str(int(_v)) if _v == int(_v) else f"{_v}")
                except Exception: pass
            _seen = set()
            for line in open(_sp, encoding="utf-8").read().splitlines():
                if line.startswith("#") or "機械" in line[:20]: continue
                for m in _NUMU.finditer(line):
                    k = m.group(1).replace(",", "")
                    if len(k.replace(".", "")) < 2 or (k, m.group(2)) in _seen: continue
                    _seen.add((k, m.group(2))); _tot += 1
                    if k not in _have and k.rstrip("0").rstrip(".") not in _have: _miss.append(f"{m.group(1)}{m.group(2)}：{line.strip()[:50]}")
        _EU = re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3}){2,}(?![\d.])|(?<![\d.,])\d+,\d{1,2}\s*(?:%|€|EUR|eur)")
        _eu = [r for r in rows if not re.search(r"[぀-ヿ]", r["引用"]) and _EU.search(r["引用"])]
        _big = []
        _byu = {}
        for r in rows:
            try: _byu.setdefault(r["単位"], []).append((float(r["値"]), r))
            except Exception: pass
        for u, vs in _byu.items():
            if len(vs) < 4: continue
            vs.sort(key=lambda x: x[0]); med = vs[len(vs) // 2][0]
            if med > 0 and vs[-1][0] >= 100 * med: _big.append((u, vs[-1][1], round(vs[-1][0] / med)))
        md += ["## 書いた結論の数字の照らし合わせ・欧州式の数字・大きい数字から検算（4-107・機械）",
               f"- 前回の scan.md の数字（単位つき）{_tot}個のうち、今回の scan_auto.md・cross.csv に見当たらないもの：{len(_miss)}個。写し違い・古い回の値・頭で計算した値の疑いとして、scan.md を書き直すときに scan_auto.md の値に置き換えるか消す",
               *(f"  - {x}" for x in _miss[:10]),
               f"- 日本語でない引用で欧州式の数字（1.234.567 の点の桁区切り・12,5 % のカンマの小数点）：{len(_eu)}件。千倍・千分の一の読み違いを疑い、値の読みを本文で確かめるまで採る値にしない",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _eu[:5]),
               f"- 単位ごとの最大の値が中央値の100倍以上：{len(_big)}単位。大きい数字ほど検算されずに残るので、この値から本文・原表で桁を確かめる",
               *(f"  - {u}：{r['品目'][:40]}（中央値の約{x}倍）：{r['引用'][:50]}" for u, r, x in _big[:8]),
               ""]
    except Exception: pass
    # 2026-11-08 4-108（Miguel Paz「Checklist to work with data and avoid mistakes」2017：既定の日付（1900・1904・1970年1月1日）は空欄・0の読み違い、
    # 先頭のゼロが落ちたコード、表の合計を公表の総数と照らす。OpenNews「SRCCON: How Not to Skew Data with Statistics」2014：最小・最大で外れを見て、
    # 平均と中央値が近いかで片寄りを見る。立命館大学図書館「企業情報の調べ方」2026-03：帝国データバンク会社年鑑・全国企業あれこれランキングは2025年で刊行終了。
    # CIA「A Tradecraft Primer」2009 の情報の質の点検：前に捨てた情報を新しい事実に照らして見直す）。
    # ①既定の日付の疑い ②品目×単位で平均が中央値の2倍以上・半分以下（片寄り） ③刊行終了の名簿・年鑑を2026年以降の値の出典にした引用 ④捨てた理由の内訳（見直しの入口）
    try:
        _DEF = re.compile(r"(1900|1904|1970)\s*[年/.\-]\s*0?1\s*[月/.\-]\s*0?1(?!\d)")
        _def = [r for r in rows if _DEF.search(r["引用"])]
        _skew = []
        _bi = {}
        for r in rows:
            if r.get("予測"): continue
            try: _bi.setdefault((r["品目"], r["単位"]), []).append(float(r["値"]))
            except Exception: pass
        for (n, u), vs in _bi.items():
            if len(vs) < 4: continue
            vs.sort(); med = vs[len(vs) // 2]; mean = sum(vs) / len(vs)
            if med > 0 and (mean >= 2 * med or mean <= med / 2): _skew.append((n, u, len(vs), round(mean / med, 1), vs[0], vs[-1]))
        _DISC = re.compile(r"(帝国データバンク会社年鑑|全国企業あれこれランキング|日本スーパー名鑑|世界自動車統計年報)")
        _disc = [r for r in rows if _DISC.search(r["引用"])]
        _disc_new = [r for r in _disc if str(r["年"]).isdigit() and int(r["年"]) >= 2026]
        _dr = DROPS.get(topic, {})
        md += ["## 既定の日付・平均と中央値の片寄り・刊行終了の出典・捨てた情報の見直し（4-108・機械）",
               f"- 既定の日付（1900/1904/1970年1月1日）が出る引用：{len(_def)}件。空欄・0が日付として読まれた疑いとして、その日付を創業・設立・時点にしない",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _def[:5]),
               f"- 品目×単位で値が4つ以上あり、平均が中央値の2倍以上か半分以下の組：{len(_skew)}組。片寄りの組は中央値を採り、最小・最大の引用から本文で桁と対象を確かめる（4-21・4-107）",
               *(f"  - {n[:40]}（{u}・{k}件・平均÷中央値 {x}・最小 {lo:g}・最大 {hi:g}）" for n, u, k, x, lo, hi in sorted(_skew, key=lambda t: -abs(t[3] - 1))[:8]),
               f"- 刊行終了の年鑑・名簿（帝国データバンク会社年鑑・全国企業あれこれランキングは2025年で終了ほか）を出典にした引用：{len(_disc)}件、うち2026年以降の値：{len(_disc_new)}件。最新版として扱わず、2026年以降の値はどこからの写しかを先に疑う",
               *(f"  - {r['品目'][:40]}（{r['年']}）：{r['引用'][:50]}" for r in _disc_new[:5]),
               f"- 今回捨てた引用の理由の内訳：{sum(_dr.values())}件・{len(_dr)}種。捨てた情報は新しい事実が出たら見直す（多い理由から、正しい数字まで捨てていないかを正解例で点検・4-38・4-47）",
               *(f"  - {w[:40]}：{c}件" for w, c in sorted(_dr.items(), key=lambda x: -x[1])[:6]),
               ""]
    except Exception: pass
    # 2026-11-09 4-113（米国センサス局「2002 Economic Census Manufacturing 付録C 方法論」：出荷額は事業所間の移送で上位の集計ほど重複し、
    # 産業や地域の比較は付加価値で行う。産業統計の出荷額（その業種の事業所の全出荷）と品目統計の出荷額（どの業種の事業所が作ってもその品目）は一致しない。
    # 小さい事業所は調査票を送らず行政記録と業界平均で推計し、20人未満の事業所数は目安。相対標準誤差15%超は大きい集計に束ねて読む。
    # journalism.co.uk「Getstats 12 number hygiene rules」2012：平均が平均値か中央値かを確かめる・1回の変化を傾向にしない・頻度は一定の人数あたりで。
    # National Press Club「Panel of statistics experts offers four tips」2014：方法・標本数・誤差・回答率を数字に添え、調査の資金の出所をたどる）。
    # ①出荷額を足し上げて市場規模とした引用 ②同じ品目に産業別と品目別の出荷額が混ざる ③同じ品目に平均値と中央値が混ざる
    # ④標準誤差率・相対標準誤差が15%を超える値 ⑤20人未満の事業所数（目安）
    try:
        _ship = [r for r in rows if re.search(r"出荷額", r["引用"]) and re.search(r"(市場規模|市場は|マーケット)", r["引用"])]
        _ind, _prd = {}, {}
        for r in rows:
            q = r["引用"] + " " + r["出典"]
            if "出荷" not in q: continue
            if re.search(r"(産業別|産業編|産業細分類|業種別)", q): _ind[r["品目"]] = _ind.get(r["品目"], 0) + 1
            if re.search(r"(品目別|品目編|品目6桁|品目番号)", q): _prd[r["品目"]] = _prd.get(r["品目"], 0) + 1
        _ip = sorted(set(_ind) & set(_prd))
        _mn, _md = {}, {}
        for r in rows:
            if re.search(r"平均", r["引用"]) and "中央値" not in r["引用"]: _mn[r["品目"]] = _mn.get(r["品目"], 0) + 1
            if "中央値" in r["引用"] and "平均" not in r["引用"]: _md[r["品目"]] = _md.get(r["品目"], 0) + 1
        _mm = sorted(set(_mn) & set(_md))
        _rse = []
        for r in rows:
            for m in re.finditer(r"(標準誤差率|相対標準誤差|標準誤差|relative standard error|RSE)\D{0,8}(\d+(?:\.\d+)?)\s*[%％]", r["引用"], re.I):
                try:
                    if float(m.group(2)) > 15: _rse.append((r, m.group(2)))
                except Exception: pass
        _sm = [r for r in rows if re.search(r"事業所", r["単位"] + r["引用"]) and re.search(r"(1\s*[〜~～\-－]\s*(19|9)\s*人|20\s*人未満|19\s*人以下|9\s*人以下|3\s*人以下)", r["規模"] + " " + r["引用"])]
        md += ["## 出荷額の重複・産業と品目の出荷額・平均の種類・標準誤差・小さい事業所の数（4-113・機械）",
               f"- 出荷額を市場規模として書いた引用：{len(_ship)}件。出荷額は事業所間の移送（部品が別の事業所の材料になる）で上位の集計ほど重複するので、業種を足し上げた出荷額を市場規模にしない。業種どうし・地域どうしの比較は付加価値で行う",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _ship[:5]),
               f"- 同じ品目に産業別（その業種の事業所の全出荷）と品目別（どの業種の事業所が作ってもその品目）の出荷額が混ざる品目：{len(_ip)}品目。2つは定義が違い一致しないので同じ欄で突き合わせない（【食い違い】にしない）",
               *(f"  - {n[:50]}（産業別 {_ind[n]}件・品目別 {_prd[n]}件）" for n in _ip[:6]),
               f"- 同じ品目に平均と中央値の引用が混ざる品目：{len(_mm)}品目。平均値と中央値を同じ欄で比べない（片寄りの大きい給与・売上は特に開く・4-108）",
               *(f"  - {n[:50]}（平均 {_mn[n]}件・中央値 {_md[n]}件）" for n in _mm[:6]),
               f"- 標準誤差率・相対標準誤差が15%を超える値：{len(_rse)}件。そのままの細かい区分では向きにせず、上の区分（業種の中分類・規模の大きい区切り）に束ねて読む",
               *(f"  - {r['品目'][:40]}（{x}%）：{r['引用'][:50]}" for r, x in _rse[:5]),
               f"- 20人未満（1〜9人・19人以下・3人以下など）の事業所数の引用：{len(_sm)}件。小さい事業所は調査票でなく行政記録・推計で数えられることが多く、事業所数は目安として読む（20人以上の数のほうが確か）",
               ""]
    except Exception: pass
    # 2026-11-09 4-114（Crustdata「How to Build a Private-Company Research Agent」2026：出どころどうしが食い違うときは平均せず、
    # 差そのもの（人数で4割の差など）を赤信号として出す。期待される記録の欠け（あるはずの数字が無い）も点検の対象。古い記録は新しい記録と同じ重みにしない。
    # Veridion「5 Firmographic Data Accuracy Problems」：取得日の無い人数は今の事実か分からない・合併で出どころが同じでも人数が変わる。
    # Affinity「Understanding employee headcount data accuracy」：名でなく確かな鍵で照合し、親と子を分ける）。
    # ①同じ品目・同じ年・同じ規模の人数で、一次どうしが1.4倍を超えて割れる組 ②取得の年から2年以上前の年の人数（古い人数）
    # ③売上・受注の値はあるが人数が0件（またはその逆）の品目＝1人あたりを出すのに片方が欠ける「期待される欠け」
    try:
        _hc = {}
        for r in rows:
            if r["単位"] == "人" and r["区分"].startswith("社員数") and r["一次"] == "一次" and r["年"]:
                try:
                    v = float(r["値"])
                    if v > 0: _hc.setdefault((r["品目"], r["年"], r.get("規模", ""), r["文脈"]), []).append(v)
                except Exception: pass
        _hcx = sorted(((k, min(v), max(v)) for k, v in _hc.items() if len(v) >= 2 and max(v) / min(v) > 1.4), key=lambda t: -(t[2] / t[1]))
        _old = {}
        for r in rows:
            if r["単位"] == "人" and r["区分"].startswith("社員数") and r["年"] and re.match(r"\d{4}", r["取得日"] or ""):
                try:
                    if int(r["取得日"][:4]) - int(r["年"]) >= 2: _old[r["品目"]] = _old.get(r["品目"], 0) + 1
                except Exception: pass
        _has_s, _has_h = set(), set()
        for r in rows:
            if re.search(r"(売上|受注|出荷|加工賃)", r["区分"]) and r["単位"] not in ("%", "人"): _has_s.add(r["品目"])
            if r["単位"] == "人" and r["区分"].startswith("社員数"): _has_h.add(r["品目"])
        _miss_h = sorted(_has_s - _has_h); _miss_s = sorted(_has_h - _has_s)
        md += ["## 人数の割れ・古い人数・期待される欠け（4-114・機械）",
               f"- 同じ品目・同じ年・同じ規模の人数で、一次どうしが1.4倍を超えて割れる組：{len(_hcx)}組。平均・中央値で1つにまとめず、割れたこと自体を赤信号として両端の出典を並べ、数える範囲（役員・パート・連結・事業所か会社か）を先に確かめる",
               *(f"  - {k[0][:40]}（{k[1]}年・{k[2] or '規模不明'}）：{lo:g}〜{hi:g}人・{hi / lo:.1f}倍" for k, lo, hi in _hcx[:6]),
               f"- 取得した年から2年以上前の年の人数がある品目：{len(_old)}品目。今の人数として使わず、年を添えて書く（合併・閉鎖で出どころが同じでも変わる・4-74）",
               *(f"  - {n[:50]}（{c}件）" for n, c in sorted(_old.items(), key=lambda x: -x[1])[:6]),
               f"- 売上・受注・出荷の値はあるが人数が0件の品目：{len(_miss_h)}品目／人数はあるが売上・受注・出荷が0件の品目：{len(_miss_s)}品目。1人あたりを出すには両方が要る。欠けた側を同じ表・同じ年で探す品目として inputs に戻す",
               *(f"  - 人数が欠け：{n[:50]}" for n in _miss_h[:4]),
               *(f"  - 売上が欠け：{n[:50]}" for n in _miss_s[:4]),
               ""]
    except Exception: pass
    # 2026-11-09 4-115（Inkwood Research「How Accurate Are Market Size Estimates?」2026：調査会社どうしの市場規模の食い違いは誤りより範囲の定義の違いが多い。
    # 範囲（地域・区分・金額か数量か）を先にそろえ、供給側（生産・出荷）と需要側（消費・貿易統計）を突き合わせる。
    # CREX「市場規模の算出方法を3つのステップで解説」2025：需要側と供給側の2つの式を比べ、楽観・標準・悲観の3つで幅を出す。
    # IRE「Piercing the secrecy of private companies」2011：届出が無いことは事実が無いことの証明にならない）。
    # ①市場規模の引用で地域の語が無いもの（範囲不明）②同じ品目に世界と国内の市場規模が混ざる品目（食い違いにしない）
    # ③同じ品目・同じ年に生産・出荷と輸入と輸出の金額がそろう組＝見かけの国内需要（生産＋輸入−輸出・各中央値）【推定】
    # ④同じ品目・同じ年の市場規模が3件以上あれば第1四分位・中央値・第3四分位を悲観・標準・楽観として並べる【推定】
    try:
        _GEO = r"(世界|グローバル|global|world|国内|日本|全国|米国|北米|欧州|アジア|中国|韓国|台湾|県|地域)"
        _ms = [r for r in rows if r["文脈"] == "市場規模" and r["単位"] in ("万円", "円", "億円", "ドル", "万ドル", "億ドル")]
        _nogeo = [r for r in _ms if not re.search(_GEO, r["引用"] + r["品目"], re.I)]
        _gw, _gd = set(), set()
        for r in _ms:
            if re.search(r"(世界|グローバル|global|world)", r["引用"], re.I): _gw.add(r["品目"])
            if re.search(r"(国内|日本)", r["引用"]): _gd.add(r["品目"])
        _wd = sorted(_gw & _gd)
        _sd = {}
        for r in rows:
            if r["単位"] != "万円" or not r["年"] or r.get("予測"): continue
            q = r["区分"] + r["引用"]
            k = "輸入" if "輸入" in q else "輸出" if "輸出" in q else "生産" if re.search(r"(生産額|出荷額|生産高)", q) else None
            if not k: continue
            try: _sd.setdefault((r["品目"], r["年"]), {}).setdefault(k, []).append(float(r["値"]))
            except Exception: pass
        _app = []
        for (n, y), d in _sd.items():
            if all(k in d for k in ("生産", "輸入", "輸出")):
                p, i, e = (statistics.median(d[k]) for k in ("生産", "輸入", "輸出"))
                _app.append((n, y, p, i, e, p + i - e))
        _sc = {}
        for r in _ms:
            if r["単位"] == "万円" and r["年"] and not r.get("予測"):
                try: _sc.setdefault((r["品目"], r["年"]), []).append(float(r["値"]))
                except Exception: pass
        _sc3 = []
        for (n, y), v in _sc.items():
            if len(v) >= 3:
                q = statistics.quantiles(sorted(v), n=4)
                _sc3.append((n, y, q[0], statistics.median(v), q[2], len(v)))
        _sc3.sort(key=lambda t: -t[5])
        md += ["## 市場規模の範囲・供給側と需要側・3つの幅（4-115・機械）",
               f"- 市場規模の金額の引用：{len(_ms)}件のうち、地域（世界・国内・地域）の語が無いもの：{len(_nogeo)}件。範囲が分からない市場規模は他と突き合わせず、本文で範囲（地域・区分・金額か数量か）を確かめてから使う",
               *(f"  - {r['品目'][:40]}：{r['引用'][:50]}" for r in _nogeo[:4]),
               f"- 同じ品目に世界と国内の市場規模が混ざる品目：{len(_wd)}品目。範囲が違うので【食い違い】にせず別の欄に分ける",
               *(f"  - {n[:50]}" for n in _wd[:6]),
               f"- 同じ品目・同じ年に生産（出荷）・輸入・輸出の金額がそろう組：{len(_app)}組。見かけの国内需要＝生産＋輸入−輸出（各中央値・万円）【推定】。需要側の市場規模の引用と並べ、桁が合わなければ範囲か単位の違いを疑う",
               *(f"  - {n[:36]}（{y}年）：生産 {p:,.0f}＋輸入 {i:,.0f}−輸出 {e:,.0f}＝{a:,.0f}万円" for n, y, p, i, e, a in _app[:6]),
               f"- 同じ品目・同じ年の市場規模が3件以上ある組：{len(_sc3)}組。悲観（第1四分位）・標準（中央値）・楽観（第3四分位）として幅で書く【推定】（点の1つの値を約束として書かない）",
               *(f"  - {n[:36]}（{y}年・{c}件）：{lo:,.0f}／{md_:,.0f}／{hi:,.0f}万円" for n, y, lo, md_, hi, c in _sc3[:6]),
               ""]
    except Exception: pass
    # 2026-11-09 4-116（Quartz「The Quartz guide to bad data」：手で測れない細かさの数字は計測でなく誰かの推計を疑う。
    # nhimg「Structured Analytic Techniques for CTI」：結論が1つの証拠に寄るなら、それが覆ったら見直す合図を置く）。
    # ①一次でない出典の数字で、小数点以下3桁以上か、有効数字7桁以上のもの＝見せかけの精度（記事が推計を計測のように書いた疑い）
    # ②品目×区分で、値の件数が3件以上あり、1つの出典（ドメイン）が半分超を占める欄＝1つの出典への寄り（要の証拠）。
    #   寄った出典を抜いた残りの中央値と、全部の中央値を並べる（抜いて変わる欄は、その出典が覆ったら見直す）【推定】
    try:
        def _sig(s):
            s = re.sub(r"[^\d.]", "", s or "")
            if not s or s.count(".") > 1: return 0, 0
            ip, _, dp = s.partition(".")
            return len(dp), len((ip + dp).lstrip("0").rstrip("0") if not dp else (ip + dp).lstrip("0"))
        _fp = []
        for r in rows:
            if r.get("一次") == "一次": continue
            d, n = _sig(r.get("原文の数字", ""))
            if d >= 3 or n >= 7: _fp.append(r)
        _cc = {}
        for r in rows:
            try: v = float(r["値"])
            except Exception: continue
            _cc.setdefault((r["品目"], r["区分"], r["単位"]), []).append((r["出典"], v))
        _lp = []
        for (n, k, u), lst in _cc.items():
            if len(lst) < 3: continue
            cnt = {}
            for s, _ in lst: cnt[s] = cnt.get(s, 0) + 1
            s0, c0 = max(cnt.items(), key=lambda x: x[1])
            if c0 * 2 <= len(lst) or len(cnt) < 2: continue
            rest = [v for s, v in lst if s != s0]
            _lp.append((n, k, u, s0, c0, len(lst), statistics.median([v for _, v in lst]), statistics.median(rest)))
        _lp.sort(key=lambda t: -t[5])
        md += ["## 見せかけの精度と1つの出典への寄り（4-116・機械）",
               f"- 一次でない出典の数字で、小数点以下3桁以上か有効数字7桁以上のもの：{len(_fp)}件。計測でなく推計を細かく書いた疑いとして確度を1段下げ、一次で取り直すまで向きに使わない",
               *(f"  - {r['品目'][:30]}／{r['原文の数字'][:24]}（{r['出典']}）" for r in _fp[:5]),
               f"- 値が3件以上の欄で、1つの出典が半分超を占める欄：{len(_lp)}欄。その出典を抜いた中央値と並べる。大きく変わる欄は、その出典が覆ったら見直す（4-116）【推定】",
               *(f"  - {n[:30]}／{k}：{s0} が {c0}／{t}件・全部の中央値 {m1:,.4g}{u}／抜いた残り {m2:,.4g}{u}" for n, k, u, s0, c0, t, m1, m2 in _lp[:6]),
               ""]
    except Exception: pass
    # 2026-11-09 4-117（GIJN／Segnini「Investigating With Databases: Verifying Data Quality」：受け取った直後に
    # 完全さ（極端な値・0・負の値の件数）と整合（年や状態の矛盾）を数えてから使う。
    # UN IMTS 2010 Compilation Guide 付録9.C・Grigoriou「Mirror analysis」WCJ 2019：輸入はCIF・輸出はFOBで記録され、
    # 計上時点・相手国の帰属・再輸出・秘匿・報告の裾切りで食い違う。月の値は時点の差が大きい）。
    # ①金額・人数の区分で値が0以下の行（増減率・符号の付く区分を除く）＝欠けを0で埋めた・符号の取り違えの疑い
    # ②品目×区分で値の半分以上が0の欄＝欠けの指紋（4-73）
    # ③年が取得日の年より後なのに「予測」の印が無い行＝見込みを実績と読んだ疑い。1950年より前の年＝番号や金額を年と読んだ疑い
    # ④貿易統計の行：CIF/FOB の印の有無、月の値か、再輸出・仲介貿易の語の有無を数える（輸入と輸出の値を突き合わせる前の確かめ）
    try:
        _neg, _zc, _fut, _old = [], {}, [], []
        for r in rows:
            try: v = float(r["値"])
            except Exception: continue
            k = r.get("区分", "") + r.get("文脈", "")
            if v <= 0 and not re.search(r"増減|率|伸び|差|損益|利益|符号", k) and not r.get("符号"):
                _neg.append(r)
            _zc.setdefault((r["品目"], r["区分"]), [0, 0])
            _zc[(r["品目"], r["区分"])][1] += 1
            if v == 0: _zc[(r["品目"], r["区分"])][0] += 1
            try:
                y = int(float(r.get("年") or 0)); ty = int((r.get("取得日") or "0")[:4] or 0)
            except Exception: y, ty = 0, 0
            if y and ty and y > ty and not r.get("予測"): _fut.append(r)
            if y and y < 1950: _old.append(r)
        _zh = [(n, k, z, t) for (n, k), (z, t) in _zc.items() if t >= 2 and z * 2 >= t]
        _tr = [r for r in rows if re.search(r"輸入|輸出|貿易統計|通関|import|export", (r.get("品目", "") + r.get("引用", "")), re.I)]
        _cf = [r for r in _tr if re.search(r"CIF|FOB|c\.i\.f|f\.o\.b|運賃|保険料込", r.get("引用", ""), re.I)]
        _mo = [r for r in _tr if re.search(r"\d{1,2}月|月次|月分|単月", r.get("引用", ""))]
        _rx = [r for r in _tr if re.search(r"再輸出|再輸入|仲介貿易|三国間|re-?export|transit", r.get("引用", ""), re.I)]
        md += ["## 受け取った直後の点検と、貿易統計の食い違いの元（4-117・機械）",
               f"- 金額・人数の区分で値が0以下の行：{len(_neg)}件。欠けを0で埋めたか符号の取り違えを疑い、元の表で確かめるまで割り出しに使わない",
               *(f"  - {r['品目'][:30]}／{r['区分']}：{r['値']}{r['単位']}（{r['出典']}）" for r in _neg[:4]),
               f"- 値の半分以上が0の欄：{len(_zh)}欄（欠けの指紋・4-73）",
               *(f"  - {n[:30]}／{k}：0が{z}／{t}件" for n, k, z, t in _zh[:4]),
               f"- 年が取得日の年より後で「予測」の印が無い行：{len(_fut)}件（見込みを実績と読んだ疑い）／1950年より前の年の行：{len(_old)}件（番号・金額を年と読んだ疑い）",
               f"- 貿易統計の行：{len(_tr)}件。うち CIF/FOB の印がある {len(_cf)}件・月の値 {len(_mo)}件・再輸出や仲介の語がある {len(_rx)}件。輸入（CIF）と相手国の輸出（FOB）を突き合わせるときは、印の無い行を同じ欄に並べず、月の値は時点の差が大きいので年の値で比べる（4-117）",
               ""]
    except Exception: pass
    # 2026-11-09 4-118（GIJN／Journalist's Resource「4 Tips for Avoiding Math Errors When Reporting」：割合どうしの差は
    # 「ポイント」で、割合の伸びは「%」で書く。1%→5%は4ポイント増で、4%増ではない（相対では400%増）。
    # iMediaEthics「Why Journalists Should Learn Math」：数字の誤りの元は 聞き違い／出どころの無自覚な誤り／意図的な誇張／
    # 計算の誤り／他媒体の誤りの写し の5つで、多くは計算力でなく確かめ直しの省略から出る）。
    # ①引用に「A%から（→）B%」と「C%増・減」が並び、C が B−A に近い＝ポイント差を%と書いた疑い
    # ②同じ品目に「ポイント」の値と「%」の増減率が混ざる＝割合の差と割合の伸びを同じ欄で比べている疑い
    # ③転載・配信の印（「より転載」「originally published」「republished」「出典：」）がある引用＝元の記事を探して1件に束ねる
    try:
        _pp, _mix, _syn = [], {}, []
        _seen = set()
        for r in rows:
            q = unicodedata.normalize("NFKC", r.get("引用", "") or "")
            if q in _seen: continue
            _seen.add(q)
            m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:から|→|より)\s*(\d+(?:\.\d+)?)\s*%", q)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                for c in re.findall(r"(\d+(?:\.\d+)?)\s*%\s*(?:の)?(?:増|減|上昇|低下|伸び|拡大|縮小)", q[m.end():]):
                    c = float(c)
                    if abs(b - a) > 0 and abs(c - abs(b - a)) <= 0.05 * abs(b - a) + 0.01:
                        _pp.append((r, a, b, c)); break
            k = r.get("品目", "")
            if re.search(r"ポイント|pt\b|percentage point", q, re.I): _mix.setdefault(k, [0, 0])[0] += 1
            if re.search(r"\d\s*%\s*(?:増|減|上昇|低下|伸び)", q): _mix.setdefault(k, [0, 0])[1] += 1
            if re.search(r"より転載|転載元|配信元|originally (?:published|appeared)|republished|reprinted", q, re.I): _syn.append(r)
        _mx = [(k, a, b) for k, (a, b) in _mix.items() if a and b]
        md += ["## 割合の差（ポイント）と割合の伸び（%）、転載の印（4-118・機械）",
               f"- 「A%からB%」と並ぶ「C%増・減」で、C が B−A と同じ＝ポイント差を%と書いた疑い：{len(_pp)}件。増減率に使わず、元の表の2つの割合から scan.py の差（ポイント）と伸び（%）を別の欄で出し直す（4-118）",
               *(f"  - {r['品目'][:30]}：{a:g}%→{b:g}% と {c:g}%（{r['出典']}）" for r, a, b, c in _pp[:4]),
               f"- 同じ品目に「ポイント」と「%の増減」が混ざる品目：{len(_mx)}件。割合の差と割合の伸びを同じ欄で比べない",
               *(f"  - {k[:30]}：ポイント {a}件・%増減 {b}件" for k, a, b in _mx[:4]),
               f"- 転載・配信の印がある引用：{len(_syn)}件。元の記事を探して出どころ1件に束ねる（4-1・4-118）",
               ""]
    except Exception: pass
    # 2026-11-09 4-119（EBU Spotlight「Financial OSINT: Guide to tracing corporate assets & networks」：登記の住所は地図で
    # 実在を確かめ、住宅・私書箱の住所は実態の証拠にしない。取った資料は時刻と一緒に保ち、後で中身が変わっても元が分かるようにする。
    # Social Links「Due Diligence: Closing the Verification Gap」2026：否定的な記述をすぐ証拠にせず重みを付ける。出どころの不一致は深い確認に上げる）。
    # ①引用に バーチャルオフィス・レンタルオフィス・私書箱 などの語＝その住所を工場・生産の実態の証拠にしない
    # ②同じURL・同じ品目・同じ区分で、取得日によって値が違う＝ページが書き換わった。新しい日の値を採り、古い値も時点の印付きで残す
    # ③倒産・破産・行政処分・違反・訴訟・リコールの語が、品目の中で出どころ1か所だけ＝手がかりどまり（独立2か所か一次の公表で確かめる）
    try:
        _vo = [r for r in rows if re.search(r"バーチャルオフィス|レンタルオフィス|シェアオフィス|コワーキング|私書箱|住所貸し|登記専用|virtual office|P\.?O\.? Box|registered agent", r.get("引用", "") or "", re.I)]
        _bu = {}
        for r in rows:
            u = canon_url(r.get("URL", "") or "")
            if not u or not r.get("取得日") or r.get("値") in (None, ""): continue
            _bu.setdefault((u, r["品目"], r["区分"]), {}).setdefault(r["取得日"][:10], set()).add(str(r["値"]))
        _chg = [(k, d) for k, d in _bu.items() if len(d) >= 2 and len({frozenset(v) for v in d.values()}) >= 2]
        _ng = {}
        for r in rows:
            if re.search(r"倒産|破産|民事再生|廃業|行政処分|指名停止|業務停止|違反|訴訟|提訴|リコール|bankrupt|lawsuit|recall|sanction", r.get("引用", "") or "", re.I):
                _ng.setdefault(r["品目"], set()).add(src_key(r))
        _ng1 = [k for k, s in _ng.items() if len(s) == 1]
        md += ["## 登記の住所の型、書き換わったページ、否定的な記述の重み（4-119・機械）",
               f"- バーチャルオフィス・レンタルオフィス・私書箱などの語がある引用：{len(_vo)}件。その住所を工場・人員・生産能力の証拠にせず、事業所の所在は許認可・届出（PRTR・水濁法の特定事業場など）か年金の適用事業所で確かめる（4-119）",
               *(f"  - {r['品目'][:30]}（{r['出典']}）" for r in _vo[:4]),
               f"- 同じURL・品目・区分で取得日により値が違う組：{len(_chg)}組（ページが書き換わった）。新しい取得日の値を採り、古い値は時点の印を付けて残す（4-61・4-119）",
               *(f"  - {k[1][:25]}／{k[2]}：" + "、".join(f"{d}={'/'.join(sorted(v))}" for d, v in sorted(dd.items())[-3:]) for k, dd in _chg[:4]),
               f"- 倒産・処分・違反・訴訟・リコールの語が出どころ1か所だけの品目：{len(_ng1)}件。手がかりどまりにし、独立の2か所か一次の公表（官報・所管の処分の公表）で確かめるまで結論に使わない（4-119）",
               *(f"  - {k[:40]}" for k in _ng1[:4]),
               ""]
    except Exception: pass
    # 2026-11-09 4-120（van Ark & Pilat「Productivity Levels in Germany, Japan, and the United States」Brookings 1993：
    # 生産性の水準は統計の裾切り（独は20人未満を除く）・数える人（補助部門を含むか）・時間か人数かで変わる。
    # 厚労省「毎月勤労統計調査の調査手法の検討等に関するWGの目的及び進め方」：1月の標本交替・ウェイト更新の断層は遡及改定しない。
    # CJR「Audit Notes: Reporting numbers」2013：推計は推計と書き、誰の推計かを名指す。「独自」データは来歴を確かめられない）。
    # ①同じ品目で「N人以上」の裾切りが2種以上混ざる＝裾切りの違う値を同じ欄で並べない
    # ②推計・推定・見込みの語があるのに、誰の推計か（によると・調べ・調査・試算）の主語が無い数字＝【記事】のまま
    # ③「独自調査」「独自データ」「proprietary」＝来歴を確かめられない。一次に数えない
    # ④毎月勤労統計の1月の前年比＝標本交替・ウェイト更新の断層を含む値として読む
    try:
        _cut = {}
        for r in rows:
            for m in re.finditer(r"(?:従業者|従業員|常用)?(\d{1,4})\s*人以上", r.get("引用", "") or ""):
                _cut.setdefault(r["品目"], set()).add(int(m.group(1)))
        _cutx = [(k, sorted(v)) for k, v in _cut.items() if len(v) >= 2]
        _est = [r for r in rows if r.get("値") not in (None, "")
                and re.search(r"推計|推定|見込み|試算|estimate", r.get("引用", "") or "", re.I)
                and not re.search(r"によると|によれば|の調べ|調査では|が推計|が試算|が推定|according to|estimated by|発表", r.get("引用", "") or "", re.I)]
        _prop = [r for r in rows if re.search(r"独自調査|独自データ|独自の調査|独自集計|proprietary", r.get("引用", "") or "", re.I)]
        _mk1 = [r for r in rows if re.search(r"毎月勤労統計|毎勤", r.get("引用", "") + r.get("品目", ""))
                and re.search(r"1月", r.get("引用", "") or "") and re.search(r"前年(同月)?比", r.get("引用", "") or "")]
        md += ["## 裾切り・推計の主語・来歴・1月の断層（4-120・機械）",
               f"- 同じ品目で「N人以上」の裾切りが2種以上混ざる品目：{len(_cutx)}件。裾切りの違う値（4人以上・20人以上・30人以上など）を同じ欄で並べず、裾切りごとに分けて読む（4-103・4-120）",
               *(f"  - {k[:35]}：" + "・".join(f"{c}人以上" for c in v[:5]) for k, v in _cutx[:4]),
               f"- 推計の語があるのに誰の推計か書かれていない数字：{len(_est)}件。【記事】のまま結論に使わず、推計した主体と式を探す（4-116・4-120）",
               *(f"  - {r['品目'][:30]}：{r.get('値')}（{r['出典']}）" for r in _est[:4]),
               f"- 「独自調査・独自データ」の数字：{len(_prop)}件。来歴を確かめられないので一次に数えず、広く出回っていても裏付けにしない（4-1・4-120）",
               *(f"  - {r['品目'][:30]}（{r['出典']}）" for r in _prop[:4]),
               f"- 毎月勤労統計の1月の前年比：{len(_mk1)}件。標本交替・ウェイト更新の断層を含み遡及改定されない値として読み、向きに使わない（4-26・4-120）",
               ""]
    except Exception: pass
    # 2026-11-09 4-121（MetricGate「MAD基準化zスコア」「ロバスト統計」：中央値とMAD×1.4826で標準化し|z|≥3.5を強い外れ値、
    # MAD=0（半分以上が同じ値）ならIQRの柵に切り替える。SD/MADが1.5超は裾の重さ・外れ値の兆し、平均と20%トリム平均の差がMADの半分超は汚染の兆し。
    # 経産省「2023年経済構造実態調査 製造業事業所調査 利用上の注意」：対象は分類ごとに出荷額の9割に達するまでの事業所で残りは推計、
    # 個人経営を除く全事業所の集計で、4人以上の工業統計と単純に比べられない。雇用区分は「正社員」から「無期・有期」に変わった。
    # SCU「Tricky Questions Buried in the Number」：期待される件数・全単位がそろうかを先に数える）。
    # ①品目×区分×単位で値5件以上の欄ごとに、ロバストな外れ値（|z|≥3.5、MAD=0ならIQRの柵）と SD/MAD・トリム平均の兆しを出す（外れ値は消さず印すだけ）
    # ②同じ品目×区分に工業統計（〜2020年）と経済構造実態調査（2021年〜）の値が並ぶ組＝範囲・推計の違う値をつないでいる
    # ③雇用区分の旧い語（正社員・正職員以外）と新しい語（無期雇用・有期雇用）が同じ品目に混ざる
    # ④都道府県別を名指す品目で、引用に出る都道府県の数（47に足りない＝全単位がそろっていない）
    try:
        import statistics as _st
        _grp = {}
        for r in rows:
            try: v = float(r.get("値"))
            except (TypeError, ValueError): continue
            if r.get("予測"): continue
            _grp.setdefault((r["品目"], r.get("区分", ""), r.get("単位", "")), []).append((v, r))
        _rob = []; _heavy = []; _iqr0 = 0
        for k, lst in _grp.items():
            xs = sorted(v for v, _ in lst)
            if len(xs) < 5: continue
            med = _st.median(xs); mad = _st.median([abs(x - med) for x in xs])
            if mad > 0:
                s = 1.4826 * mad
                out = [(v, r) for v, r in lst if abs(v - med) / s >= 3.5]
                sd = _st.pstdev(xs); kk = int(len(xs) * 0.2)
                tm = _st.mean(xs[kk:len(xs) - kk]) if len(xs) - 2 * kk > 0 else med
                if sd / s > 1.5 or abs(_st.mean(xs) - tm) > mad / 2:
                    _heavy.append((k, round(sd / s, 2), len(xs)))
            else:
                _iqr0 += 1
                q = _st.quantiles(xs, n=4); iq = q[2] - q[0]
                out = [(v, r) for v, r in lst if v < q[0] - 1.5 * iq or v > q[2] + 1.5 * iq] if iq > 0 else []
            if out: _rob.append((k, len(out), len(xs), out))
        _br = {}
        for r in rows:
            t = (r.get("引用", "") or "") + (r.get("出典", "") or "")
            y = str(r.get("年", ""))[:4]
            if not y.isdigit(): continue
            kk2 = (r["品目"], r.get("区分", ""))
            if re.search(r"工業統計", t) and int(y) <= 2020: _br.setdefault(kk2, set()).add("工業統計")
            if re.search(r"経済構造実態調査", t) and int(y) >= 2021: _br.setdefault(kk2, set()).add("経済構造実態調査")
        _brx = [k for k, v in _br.items() if len(v) == 2]
        _emp = {}
        for r in rows:
            q2 = r.get("引用", "") or ""
            if re.search(r"正社員|正職員", q2): _emp.setdefault(r["品目"], set()).add("旧")
            if re.search(r"無期雇用|有期雇用", q2): _emp.setdefault(r["品目"], set()).add("新")
        _empx = [k for k, v in _emp.items() if len(v) == 2]
        _PREF = "北海道 青森 岩手 宮城 秋田 山形 福島 茨城 栃木 群馬 埼玉 千葉 東京 神奈川 新潟 富山 石川 福井 山梨 長野 岐阜 静岡 愛知 三重 滋賀 京都 大阪 兵庫 奈良 和歌山 鳥取 島根 岡山 広島 山口 徳島 香川 愛媛 高知 福岡 佐賀 長崎 熊本 大分 宮崎 鹿児島 沖縄".split()
        _pc = {}
        for r in rows:
            if not re.search(r"都道府県別|都道府県ごと", r["品目"]): continue
            q3 = r.get("引用", "") or ""
            for p in _PREF:
                if p in q3: _pc.setdefault(r["品目"], set()).add(p)
        md += ["## ロバストな外れ値・統計のつなぎ目・全単位の点検（4-121・機械）",
               f"- 値5件以上の欄で、中央値とMAD×1.4826で|z|≥3.5（MAD=0の欄はIQRの柵）の外れ値を持つ欄：{len(_rob)}欄（うちMAD=0でIQRに切り替えた欄の総数 {_iqr0}）。外れ値は消さず、元の表で単位・対象を確かめるまで採る値の根拠にしない（4-89・4-121）",
               *(f"  - {k[0][:30]}／{k[1]}（{k[2]}）：{n}/{m}件　例 {out[0][0]:g}（{out[0][1].get('出典', '')}）" for k, n, m, out in sorted(_rob, key=lambda x: -x[1])[:5]),
               f"- SD/MAD が1.5超か、平均と20%トリム平均の差がMADの半分超の欄（裾が重い・混入の兆し）：{len(_heavy)}欄。平均でなく中央値で読み、混ざった文脈を本文で分ける（4-5・4-121）",
               *(f"  - {k[0][:30]}／{k[1]}：SD/MAD={x}（n={n}）" for k, x, n in sorted(_heavy, key=lambda x: -x[1])[:4]),
               f"- 同じ品目×区分に工業統計（2020年以前）と経済構造実態調査（2021年以降）の値が並ぶ組：{len(_brx)}組。後者は出荷額の9割までの事業所＋推計・個人経営を除く全規模で、4人以上の工業統計と範囲が違うので、つないで向きを書かない（4-105・4-121）",
               *(f"  - {k[0][:35]}／{k[1]}" for k in _brx[:4]),
               f"- 雇用区分の旧い語（正社員・正職員）と新しい語（無期・有期雇用）が混ざる品目：{len(_empx)}件。区分が連続しないので同じ欄で比べない（4-121）",
               *(f"  - {k[:40]}" for k in _empx[:3]),
               f"- 都道府県別を名指す品目で、引用に出る都道府県の数が47に足りない品目：{sum(1 for v in _pc.values() if len(v) < 47)}件。全単位がそろわない表で全国の向きを書かない（4-91・4-121）",
               *(f"  - {k[:35]}：{len(v)}都道府県" for k, v in list(_pc.items())[:3] if len(v) < 47),
               ""]
    except Exception: pass
    # 2026-11-09 4-122（統計センター 野呂・和田「統計実務におけるレンジチェックのための外れ値検出方法」統計研究彙報72号：
    # 調査データは正規分布に従わず平均±2〜3SDのレンジは検出漏れを起こす。右に裾の長い値は対称化（対数）してから中央値と四分位範囲でレンジを置く。外れ値は誤りとは限らない。
    # sgbms1/ja-news-skills「data-hodo」：分子と分母の年をそろえる・最新が取れるのに古い年を使うのは重大な失敗・載せる数字は独立に計算し直して突き合わせる。
    # UN「International Recommendations for the Index of Industrial Production」：受託・手数料での加工は生産の扱いが特別で、名目から数量へは対応する物価で割る）
    # ①正の値5件以上の欄で、対数にしてから中央値・IQR（IQR/1.349を尺度）で|z|≥3.5の外れ値。生の値のMADでは出ず対数でだけ出る欄＝裾の歪みで隠れた外れ値
    # ②「あたり」の品目で、引用に2つ以上の違う年が出る行＝分子と分母の年ずれの疑い
    # ③品目ごとの最新の年が NOW_YEAR-5 以前＝古いデータのまま（新しい版を探す）
    # ④受託・賃加工・加工賃を名指す品目で、引用が製造品出荷額だけで加工賃に触れない行＝範囲違い（受託は加工賃で測る）
    # ⑤金額の10年の比較を名指す品目で、実質・物価・デフレーターの語が引用に無い行＝名目のまま（4-37）
    try:
        import math as _m, statistics as _st2
        _logo = []
        for k, lst in (_grp.items() if '_grp' in dir() else []):
            xs = [v for v, _ in lst if v > 0]
            if len(xs) < 5 or len(xs) != len(lst): continue
            lx = sorted(_m.log(x) for x in xs); q = _st2.quantiles(lx, n=4); s = (q[2] - q[0]) / 1.349
            if s <= 0: continue
            med = _st2.median(lx)
            lo = [(v, r) for v, r in lst if abs(_m.log(v) - med) / s >= 3.5]
            raw = {id(r) for _k, _n, _mm, out in _rob if _k == k for _v, r in out} if '_rob' in dir() else set()
            only = [(v, r) for v, r in lo if id(r) not in raw]
            if only: _logo.append((k, len(only), len(xs), only))
        _ymix = []
        for r in rows:
            if not re.search(r"あたり|当たり|1人|一人", r["品目"]): continue
            ys = sorted(set(years_in(r.get("引用", "") or "")))
            if len(ys) >= 2: _ymix.append((r, ys))
        _ly = {}
        for r in rows:
            y = str(r.get("年", ""))[:4]
            if y.isdigit(): _ly[r["品目"]] = max(_ly.get(r["品目"], 0), int(y))
        _stale = sorted(((k, y) for k, y in _ly.items() if y <= NOW_YEAR - 5), key=lambda x: x[1])
        _fee = [r for r in rows if re.search(r"受託|賃加工|加工賃", r["品目"])
                and re.search(r"製造品出荷額|出荷額", r.get("引用", "") or "") and not re.search(r"加工賃", r.get("引用", "") or "")]
        _nom = [r for r in rows if re.search(r"10年|推移|増減", r["品目"]) and re.search(r"円", r.get("単位", "") or "")
                and not re.search(r"実質|物価|デフレーター|価格指数", (r.get("引用", "") or "") + r["品目"])]
        md += ["## 対数でのレンジ・分子分母の年・古さ・受託の範囲（4-122・機械）",
               f"- 正の値5件以上の欄で、対数にして中央値とIQR/1.349で|z|≥3.5、かつ生の値のMADでは出なかった外れ値を持つ欄：{len(_logo)}欄。右に裾の長い値で隠れた外れ値なので、元の表で単位・対象を確かめるまで採る値の根拠にしない（4-121・4-122）",
               *(f"  - {k[0][:30]}／{k[1]}（{k[2]}）：{n}/{mm}件　例 {o[0][0]:g}（{o[0][1].get('出典', '')}）" for k, n, mm, o in sorted(_logo, key=lambda x: -x[1])[:4]),
               f"- 「あたり」の品目で、引用に違う年が2つ以上出る行：{len(_ymix)}件。分子と分母が同じ年の値かを本文で確かめるまで1人あたり・1社あたりに使わない（4-122）",
               *(f"  - {r['品目'][:30]}：{'・'.join(ys[:4])}年（{r['出典']}）" for r, ys in _ymix[:4]),
               f"- 品目の最新の年が{NOW_YEAR - 5}年以前の品目：{len(_stale)}件。新しい版が出ていないかを先に探し、古い年のまま結論にしない（4-48・4-122）",
               *(f"  - {k[:35]}：最新 {y}年" for k, y in _stale[:4]),
               f"- 受託・賃加工の品目で、引用が出荷額だけで加工賃に触れない行：{len(_fee)}件。受託は材料の持ち主が客なので出荷額でなく加工賃収入で測り、同じ欄で比べない（4-113・4-122）",
               *(f"  - {r['品目'][:30]}（{r['出典']}）" for r in _fee[:3]),
               f"- 金額の推移・10年の品目で、実質・物価指数の語が無い行：{len(_nom)}件。名目のまま向きを書くときは「名目」と添え、物価で割った値は国内企業物価指数の品目を足して次の実行で出す（4-37・4-122）",
               ""]
    except Exception: pass
    # 2026-11-09 4-123（総務省「統計法令に基づく統計調査の承認及び届出の状況」月報：各調査の【調査方法】に選定（全数／無作為抽出）と
    # 客体数／母集団数が並ぶ＝抽出率は承認の記録から出せる。jgiajfid「調査計画」note：発言・確認済み事実・仮説・未取得を分け、
    # 「担当者によれば」は検証できるまで証言（B）として扱う。betaitohuman「インテリジェンス」note：仮説に確率・期限・判定基準を書き、
    # 期限が来たら Brier Score で採点する）
    # ①引用に「客体数／母集団数」の2つの数が並ぶ行から抽出率（客体÷母集団）を出し、「全数」の行と分けて数える
    # ②数字の品目で、引用が話し手の言明（によれば・とのこと・と話す・と語る・と認識・という声）だけの行＝証言どまり
    # ③HYPOTHESES.md の H 行で、確率（%）・期限（年月・まで）の両方がそろわない行＝採点できない仮説
    try:
        import unicodedata as _ud
        def _nn(s):
            s = _ud.normalize("NFKC", s or "").replace(",", "")
            m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)?", s)
            if not m: return None
            v = float(m.group(1)); return v * (10000 if m.group(2) == "万" else 1000 if m.group(2) == "千" else 1)
        _samp, _full = [], []
        for r in rows:
            q = _ud.normalize("NFKC", r.get("引用", "") or "")
            if re.search(r"選定\)?\s*全数", q): _full.append(r)
            m = re.search(r"客体数\s*/\s*母集団数\)?[^0-9約]*約?\s*([\d,\.]+\s*[万千]?)[^/0-9]*/\s*約?\s*([\d,\.]+\s*[万千]?)", q)
            if m:
                a, b = _nn(m.group(1)), _nn(m.group(2))
                if a and b and b > 0: _samp.append((r, a, b, a / b))
        _TESTI = re.compile(r"によれば|によると|とのこと|と話す|と話した|と語る|と語った|と認識|という声|と証言|と明かす|と明かした|said|told")
        _MEAS = re.compile(r"統計|調査|決算|有価証券|公告|ログ|記録|集計|実績|表\d|e-Stat", re.I)
        _testi = [r for r in rows if r.get("値") not in (None, "") and _TESTI.search(r.get("引用", "") or "")
                  and not _MEAS.search(r.get("引用", "") or "")]
        _hyp_bad = []
        try:
            for ln in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "HYPOTHESES.md"), encoding="utf-8"):
                mh = re.match(r"\|\s*(H\d+)\s*\|", ln)
                if not mh: continue
                has_p = bool(re.search(r"\d+\s*%|確率", ln)); has_d = bool(re.search(r"20\d\d年\d{1,2}月|期限|まで", ln))
                if not (has_p and has_d): _hyp_bad.append((mh.group(1), has_p, has_d))
        except OSError: pass
        # ④（98回目の走査の失敗）費用・単価の区分に、人数・社数の単位の値や、1件1億円（10000万円）以上の値が入った行＝欄の取り違えの疑い
        _COSTC = re.compile(r"採用単価|いくら払う|単価|費用|料金")
        _mis = [r for r in rows if _COSTC.search(r.get("区分", "") or "") and (
                (r.get("単位") in ("人", "名", "社", "件", "事業所")) or
                (r.get("単位") == "万円" and isinstance(r.get("値"), (int, float)) and r["値"] >= 10000 and re.search(r"採用|1人", r.get("区分", "") or "")))]
        md += ["## 抽出率・証言どまり・採点できる仮説・費用の欄の取り違え（4-123・機械）",
               f"- 費用・単価の区分に、人数・社数の単位の値か、採用の1件・1人で1億円以上の値が入った行：{len(_mis)}件。企業数・出荷額・求人数を費用と取り違えた疑いなので、元の引用で何の数字かを確かめるまで採る値・中央値の根拠にしない（4-5・4-123）",
               *(f"  - {r['品目'][:30]}／{r.get('区分', '')}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')}）" for r in _mis[:5]),
               f"- 引用に「客体数／母集団数」が並ぶ行：{len(_samp)}件（抽出率は下の値。全数と書かれた行 {len(_full)}件）。抽出の統計の細かい区分（業種×規模）は客体が少ないので、抽出率の低い調査の細分類を向きに使わない（4-93・4-121・4-123）",
               *(f"  - {r['品目'][:30]}：客体 {a:,.0f}／母集団 {b:,.0f}＝抽出率 {p:.2%}（{r.get('出典', '')}）" for r, a, b, p in sorted(_samp, key=lambda x: x[3])[:5]),
               f"- 数字の品目で、引用が話し手の言明だけ（によれば・とのこと・と話す・と認識など、統計・決算・記録の語が無い）の行：{len(_testi)}件。証言（B）として扱い、記録・統計で測るまで採る値にしない。言明と記録が割れたら両方を残す（4-106・4-123）",
               *(f"  - {r['品目'][:30]}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')}）" for r in _testi[:4]),
               f"- HYPOTHESES.md で確率（%）と期限がそろわない仮説：{len(_hyp_bad)}件（{'・'.join(h for h, _p, _d in _hyp_bad) or 'なし'}）。確率と期限と判定基準が無い仮説は答え合わせ（Brier Score）ができないので、走査の「仮説への効き」では向き（進めた／崩した）だけを書き、確率を作らない（4-53・4-123）",
               ""]
    except Exception: pass
    # 2026-11-09 4-124（reading_monkey「理系にも分かる史料批判」note：出どころの依存は「写し」「共通の元」「共通の制度的な偏り（同じ報告様式・宣伝方針）」の
    # 3つの型で、別々のホストでも同じ元の発表を名指しする引用は1系統に束ねる。発生の有無と因果は別の問い。m_y_edo（デフレ論）note：
    # 掲載の年率は算出方法（端点の複利か平均か）を確定できなければ自分で出した年率と同じ欄にしない。総務省「公的統計の整備に関する基本的な計画」：
    # 調査設計・推計方法を公表せずに変えた不適切事案がある＝訂正・遡及・不適切の発表がある統計は影響の期間をまたいで向きを書かない）
    # ①同じ品目で、引用が名指す元（〜研究所・総研・工業会・協会・省庁など＋によると／の調査／の推計）が2ホスト以上に出る＝共通の元のこだま
    # ②引用に掲載の年率（年平均・年率・CAGR）がある行＝scan.py の複利の年率と別の欄
    # ③数字の引用で因果の言明（原因・要因・影響で・起因・が響き）がある行＝発生の事実と因果の主張を分ける
    # ④引用に統計の訂正・不適切・二重計上・遡及改定・再集計の語がある行
    try:
        _ORIG = re.compile(r"([^\s、。「」『』（）()・,]{2,20}?(?:経済研究所|研究所|総研|総合研究所|リサーチ|工業会|協会|連合会|省|庁|機構|委員会))(?:の調査|の調べ|によると|によれば|の推計|の試算|の発表|が発表|の予測)")
        _org = {}
        for r in rows:
            for o in set(_ORIG.findall(r.get("引用", "") or "")):
                _org.setdefault((r.get("品目", ""), o), set()).add(src_key(r))
        _common = sorted(((k, len(h)) for k, h in _org.items() if len(h) >= 2), key=lambda x: -x[1])
        _RATE = re.compile(r"年平均|年率|CAGR|年平均成長率", re.I)
        _rate = [r for r in rows if r.get("値") not in (None, "") and _RATE.search(r.get("引用", "") or "")]
        _CAUSE = re.compile(r"原因|要因|影響で|に起因|が響き|が響いた|のせいで|によって減|によって増")
        _cause = [r for r in rows if r.get("値") not in (None, "") and _CAUSE.search(r.get("引用", "") or "")]
        _FIX = re.compile(r"不適切|二重計上|訂正|遡及改定|遡及して|再集計|誤りがあ|過大に計上|過少に計上")
        _fix = [r for r in rows if _FIX.search(r.get("引用", "") or "")]
        md += ["## 共通の元・掲載の年率・因果の言明・統計の訂正（4-124・機械）",
               f"- 同じ品目で、引用が同じ元の発表（研究所・工業会・省庁など＋によると／の調査）を名指しし、2ホスト以上に出る組：{len(_common)}組。別のホストでも元が1つなら独立の裏付けに数えず1系統に束ねる。同じ報告様式・宣伝方針に従う出どころどうしの一致も重みを下げる（4-23・4-57・4-124）",
               *(f"  - {k[0][:30]}／{k[1]}：{n}ホスト" for k, n in _common[:5]),
               f"- 引用に掲載の年率（年平均・年率・CAGR）がある行：{len(_rate)}件。算出方法（端点の複利か、各年の平均か、期間）を本文で確かめるまで、scan.py が端点から出した複利の年率と同じ欄に並べない。食い違っても誤記と決めつけず両方を区別して残す（4-37・4-124）",
               f"- 数字の引用で因果の言明（原因・要因・影響で・起因など）がある行：{len(_cause)}件。「起きたか」（発生）と「それが原因か」（因果）を別の問いにし、因果は話し手の主張として書く。原因か結果かを一度で決めず、時期で区切って経路を書く（4-106・4-124）",
               f"- 引用に統計の訂正・不適切・二重計上・遡及改定・再集計の語がある行：{len(_fix)}件。その統計は所管の訂正の発表で影響の期間を確かめるまで、その期間をまたいで向きを書かない（4-26・4-51・4-124）",
               *(f"  - {r['品目'][:30]}（{r.get('出典', '')}）" for r in _fix[:4]),
               ""]
    except Exception: pass
    # 2026-11-09 4-125（GIJN「Data Don'ts」NICAR 2025：率の平均は元の大きさで重みを付ける・「N倍多い」は曖昧・丸い件数は表示上限の指紋・
    # 「8,000,000.00」型の小数つきの金額は桁の読み違いを生む。Satter「Busting Ghost Firms」：自称の創業年数・業種は登記と食い違いを照らす。
    # 坂本「統計所得」報道の批判（ITmedia）：手法が変わった系列に「過大・急増」などの評価語を付けない）
    try:
        _AVGR = re.compile(r"(増加率|伸び率|増減率|成長率|上昇率)の(単純)?平均|平均(増加|伸び|増減|成長|上昇)率")
        _avgr = [r for r in rows if r.get("値") not in (None, "") and _AVGR.search(r.get("引用", "") or "")]
        _TIMES = re.compile(r"\d+(?:\.\d+)?倍(?:も|以上)?多")
        _times = [r for r in rows if _TIMES.search(r.get("引用", "") or "")]
        _CAPV = {100, 200, 500, 1000, 2000, 3000, 5000, 10000}
        _CAPQ = re.compile(r"検索|件中|表示|ヒット|該当")
        _cap = [r for r in rows if r.get("単位") in ("件", "社", "事業所") and isinstance(r.get("値"), (int, float))
                and r["値"] in _CAPV and _CAPQ.search(r.get("引用", "") or "")]
        _DEC = re.compile(r"\d{1,3}(?:,\d{3}){2,}\.\d{2}(?!\d)")
        _dec = [r for r in rows if _DEC.search(r.get("引用", "") or "")]
        _AGE = re.compile(r"(創業|設立|創立)(から|以来)?\s*\d{1,3}\s*(年|周年)")
        _age = [r for r in rows if _AGE.search(r.get("引用", "") or "")]
        _EVAL = re.compile(r"過大|過小|急増|急減|急上昇|急落")
        _CHG = re.compile(r"手法|基準(の)?改定|基準年|方法(の)?変更|調査方法|定義(の)?変更|標本|抽出替")
        _evalc = [r for r in rows if _EVAL.search(r.get("引用", "") or "") and _CHG.search(r.get("引用", "") or "")]
        md += ["## 率の平均・倍の言い方・表示上限・桁の読み違い・自称の年数・手法変更の評価語（4-125・機械）",
               f"- 引用が増加率・伸び率の平均を出している行：{len(_avgr)}件。大きさの違う対象の率を単純に平均すると、小さい先の大きな伸びと大きい先の小さな伸びが同じ重みになる。元の金額・人数の合計から率を出し直すか、重みを確かめるまで採る値にしない（4-118・4-125）",
               *(f"  - {r['品目'][:30]}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')}）" for r in _avgr[:4]),
               f"- 「N倍多い」の言い方の行：{len(_times)}件。「5倍」と「5倍多い（＝6倍）」が混ざるので、元の2つの値を本文で取り、倍は scan.py が値から出したものだけを使う（4-125）",
               f"- 件数の欄で、丸い値（100・500・1,000・5,000・10,000など）が検索・表示の語と並ぶ行：{len(_cap)}件。サイトの表示上限・取得上限で切れた数の疑い（4-73 の行数上限と同じ指紋）。全件の数としない（4-125）",
               *(f"  - {r['品目'][:30]}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')}）" for r in _cap[:4]),
               f"- 引用に「8,000,000.00」型の小数つきの大きな金額がある行：{len(_dec)}件。円の整数を万・億と読み違えやすい（800万を80億と取る例）。単位を本文の表頭で確かめ、桁の感覚（4-31）と照らす（4-125）",
               f"- 引用に自称の創業・設立からの年数がある行：{len(_age)}件。会社の自称は登記（gBizINFO・登記事項の設立年月日、法人番号の付番日）と照らし、食い違えば自称を採らない。業種の自称も登記の目的・許認可の登録と照らす（4-8・4-125）",
               f"- 引用に「過大・急増」などの評価語と、手法・基準・定義の変更の語が並ぶ行：{len(_evalc)}件。連続性の切れた系列の増減には評価語を付けず、新旧の両方式で出した重なりの年（併記・参考値）があるときだけつなぐ（4-26・4-125）",
               ""]
    except Exception: pass
    # 2026-11-09 4-126（TextQL「Beyond Guesswork」2025：推定の失敗の主な型は、どこにも無い数字を作ること（幻覚）で、精度は桁のずれで測る。
    # Tetlock「Ten Commandments」・CAS Actuarial Review の書評：難しい問いを易しい問いにすり替えない・基準率から出て小刻みに直す。
    # GIJN「Expert Tips for Following Ships, Smugglers and Supply Chains」：輸入と相手国の輸出を突き合わせ、季節に合わない跳びを手がかりにする）
    # ①人の書いた scan.md の数字（3桁以上か小数つき、年・章番号を除く）で、scan_auto.md・cross.csv・calc.md・facts.md・METHOD.md のどこにも無いもの
    #   ＝出どころの無い数字の疑い（Claude が頭で計算した値か写し違い）。次の走査で消すか、scan.py に計算を足して出させる
    # ②2つの値を並べる欄（【食い違い】の印がある行）で、最大÷最小が10倍以上＝桁のずれ（単位の読み違いを先に疑う・4-31）
    try:
        _sm = os.path.join(OUT, topic, "scan.md")
        if os.path.exists(_sm):
            _ref = "\n".join(md)
            for _f in ("cross.csv", "calc.md", "facts.md", "summary.csv"):
                _p = os.path.join(OUT, topic, _f)
                if os.path.exists(_p): _ref += "\n" + open(_p, encoding="utf-8-sig", errors="ignore").read()
            for _f in ("METHOD.md", "HYPOTHESES.md"):
                _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), _f)
                if os.path.exists(_p): _ref += "\n" + open(_p, encoding="utf-8", errors="ignore").read()
            import unicodedata as _ud2
            _refn = _ud2.normalize("NFKC", _ref).replace(",", "")
            _NUM = re.compile(r"(?<![\d\-\.A-Za-z])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d{3,})(?![\d\-])")
            _orph = []
            _orl = {}
            for _no, _ln in enumerate(open(_sm, encoding="utf-8", errors="ignore"), 1):
                _l = _ud2.normalize("NFKC", _ln)
                for _m in _NUM.finditer(_l):
                    _v = _m.group(1).replace(",", "")
                    if re.fullmatch(r"(19|20)\d\d", _v): continue  # 年
                    if re.search(r"\d-\d", _l[max(0, _m.start() - 3):_m.end() + 3]) and "." not in _v: continue  # 章番号 4-12 など
                    if _v not in _refn and _v not in _orph: _orph.append(_v); _orl[_v] = _no
            md += ["## 出どころの無い数字・桁のずれ（4-126・機械）",
                   f"- scan.md の数字（3桁以上か小数つき、年・章番号を除く）で、scan_auto.md・cross.csv・calc.md・facts.md・METHOD.md のどこにも無いもの：{len(_orph)}個。"
                   "頭で計算した値か写し違いの疑い。次の走査で scan.md から外すか、その計算を scan.py に足して出させる（0. 動き方・4-126）",
                   *(f"  - {v}（scan.md {_orl.get(v)}行目）" for v in _orph[:8]), ""]
    except Exception: pass
    try:
        _grp = {}
        for r in rows:
            if isinstance(r.get("値"), (int, float)) and r["値"] > 0 and r.get("単位"):
                _grp.setdefault((r.get("品目", ""), r.get("区分", ""), r.get("単位")), []).append(r["値"])
        _mag = sorted(((k, min(v), max(v)) for k, v in _grp.items() if len(v) >= 2 and max(v) >= 10 * min(v)), key=lambda x: -(x[2] / x[1]))
        md += [f"- 同じ品目・区分・単位で、最大が最小の10倍以上の欄（桁のずれ）：{len(_mag)}欄。平均・中央値で丸めず、先に単位（千円・万円・百万円）と範囲（全社・1事業所・1人）の読み違いを元の表で確かめる（4-31・4-126）",
               *(f"  - {k[0][:30]}／{k[1][:15]}：{lo:g}〜{hi:g}{k[2]}" for k, lo, hi in _mag[:5]), ""]
    except Exception: pass
    # 2026-11-09 4-127（ONS「Asymmetries in trade data: a UK perspective」2017：貿易の値は同じ国でも基準（通関か国際収支か・一般か特別か）で版が複数あり、
    # 評価（CIF/FOB・申告の裾切り・為替）と範囲・秘匿の規則で食い違う。Sun&R.Lab「その数字は、誰が計ったのか」note：手順が公開されない市場規模は主体が名乗っていても検証できない。
    # Woozle Research「Expert calls」：人の声は同じ問いの型で複数人に聞き、要の数字は記録の原文で確かめ、単独の逸話にしない）
    try:
        _MKT = re.compile(r"市場規模|市場は.{0,12}(億|兆)|market size", re.I)
        _HOW = re.compile(r"調査方法|推計方法|算出方法|母数|母集団|回答|対象[はに]|ベース[でに]|集計方法|methodology", re.I)
        _mkt = [r for r in rows if r.get("値") not in (None, "") and _MKT.search(r.get("引用", "") or "")]
        _mkt_nohow = [r for r in _mkt if not _HOW.search(r.get("引用", "") or "")]
        _TRD = re.compile(r"輸出|輸入|貿易|export|import", re.I)
        _BASIS = re.compile(r"通関|国際収支|CIF|FOB|一般貿易|特別貿易|保税|再輸出|原産国|仕向", re.I)
        _trd = [r for r in rows if r.get("値") not in (None, "") and _TRD.search((r.get("品目", "") or "") + (r.get("引用", "") or ""))]
        _trd_nob = [r for r in _trd if not _BASIS.search((r.get("品目", "") or "") + (r.get("引用", "") or ""))]
        _VOICE = re.compile(r"専門家|元社員|元従業員|インタビュー|ヒアリング|担当者は|関係者は|話す|語る")
        _voice = [r for r in rows if r.get("値") not in (None, "") and _VOICE.search(r.get("引用", "") or "")]
        _vh = {}
        for r in _voice: _vh.setdefault(r.get("品目", ""), set()).add(r.get("出典", ""))
        _v1 = [k for k, s in _vh.items() if len(s) < 2]
        md += ["## 手順の無い市場規模・貿易の基準・人の声の単独（4-127・機械）",
               f"- 市場規模の数字の行：{len(_mkt)}件のうち、引用に調査・推計の方法・母数・対象の語が無い行：{len(_mkt_nohow)}件。主体が名乗っていても手順が公開されない推計は【未検証】止まりにし、主体・時期・母数・手順の4点が取れるまで採る値の根拠にしない（4-115・4-127）",
               *(f"  - {r['品目'][:30]}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')}）" for r in _mkt_nohow[:4]),
               f"- 貿易（輸出・輸入）の数字の行：{len(_trd)}件のうち、基準（通関か国際収支か・CIF/FOB・一般か特別か・原産国か仕向国か）の語が無い行：{len(_trd_nob)}件。基準の違う値を同じ欄で比べず、ミラーの食い違いはゼロにならない前提で型（評価・範囲・時期・相手国の付け方・秘匿）を書く（4-117・4-127）",
               f"- 人の声（専門家・元社員・関係者の話）の数字の行：{len(_voice)}件、品目で{len(_vh)}品目。うち出典が1か所だけの品目：{len(_v1)}品目。同じ問いの型で2人以上に当てるまで証言どまりにし、要の数字は話の原文で確かめる（4-106・4-127）",
               *(f"  - {k[:40]}" for k in _v1[:4]), ""]
    except Exception: pass
    # 2026-11-09 4-128（ESS guidelines on revision policy：速報の直しに偏りがあれば作り方の問題の印・方法の変更は総改定への影響を見積もって添える。
    # UN Comtrade「Guidelines on Analyzing and Reducing Bilateral Asymmetry」2019：①食い違いがあるかを総額に対する割合で確かめ ②元を CIF/FOB・時間差・相手国の付け方・範囲で調べる。
    # 輸入は原産国、輸出は最終仕向国で付けるので、2国の数字は相手国の付け方が違う。Resixley「OSINTでニュースを検証する方法」：同じ文は最も古い掲載を元の候補にする・
    # 「ほぼ間違いない」と書くなら何が未確認かを添える）
    try:
        # ①速報→確報の直しの向きの偏り（同じ品目・区分・単位・年で速報と確報・改定が両方ある組）
        _pv = {}
        for r in rows:
            if isinstance(r.get("値"), (int, float)) and r["値"] > 0 and r.get("年") and r.get("版") in ("速報", "確報", "改定"):
                _pv.setdefault((r.get("品目", ""), r.get("区分", ""), r.get("単位", ""), r["年"]), {}).setdefault("速報" if r["版"] == "速報" else "確", []).append(r["値"])
        _up = _dn = 0
        for _k, _d in _pv.items():
            if "速報" in _d and "確" in _d:
                _s, _c = statistics.median(_d["速報"]), statistics.median(_d["確"])
                if _c > _s: _up += 1
                elif _c < _s: _dn += 1
        _np = _up + _dn
        _bias = _np >= 3 and max(_up, _dn) >= 2 * min(_up, _dn) + 1
        # ②貿易の相手国の付け方と時間差
        _TRD2 = re.compile(r"輸出|輸入|貿易|export|import", re.I)
        _PART = re.compile(r"原産国|仕出国|仕向国|最終仕向|消費国|積出国|country of origin|consignment|destination", re.I)
        _LAG = re.compile(r"船便|海上輸送|航空|輸送日数|到着|積出|in transit|time lag", re.I)
        _trd2 = [r for r in rows if r.get("値") not in (None, "") and _TRD2.search((r.get("品目", "") or "") + (r.get("引用", "") or ""))]
        _tp = [r for r in _trd2 if _PART.search(r.get("引用", "") or "")]
        _tl = [r for r in _trd2 if _LAG.search(r.get("引用", "") or "")]
        # ③同じ値・同じ文が2ホスト以上に出る組で、取得日のいちばん古いホスト（元の候補）
        _ec = {}
        for r in rows:
            if r.get("値") in (None, "") or not r.get("出典"): continue
            # 「1人あたり・1台あたり」の分母の1は値でない（4-5）。104回目の走査で 1.0人・1.0台 が最古の掲載の組の上位に出た失敗から外す
            if isinstance(r.get("値"), (int, float)) and r["値"] == 1: continue
            _ec.setdefault((r.get("品目", ""), str(r.get("値")), r.get("単位", "")), []).append(r)
        _old = []
        for _k, _g in _ec.items():
            if len({r["出典"] for r in _g}) >= 2:
                _g2 = sorted(_g, key=lambda r: (str(r.get("取得日") or "9999"), r["出典"]))
                _old.append((_k, _g2[0]["出典"], str(_g2[0].get("取得日") or "日付なし"), len({r["出典"] for r in _g})))
        # ④scan.md で強い可能性の語を使う行に、未確認の点が添えてあるか
        _sm2 = os.path.join(OUT, topic, "scan.md")
        _strong = _strong_nou = 0
        if os.path.exists(_sm2):
            for _ln in open(_sm2, encoding="utf-8", errors="ignore"):
                if re.search(r"ほぼ確実|かなり高い|ほぼ間違いない", _ln):
                    _strong += 1
                    if not re.search(r"未確認|分かっていない|確かめていない|未検証|足りない", _ln): _strong_nou += 1
        md += ["## 直しの偏り・相手国の付け方・最古の掲載・強い語の裏（4-128・機械）",
               f"- 同じ欄・同じ年に速報と確報・改定がそろう組：{_np}組（確報で上に直った {_up}組・下に直った {_dn}組）。" + ("⚠ 直しが片側に偏る：その系列の速報は系統的にずれる。速報の年を向きの端に使わない（4-20・4-90・4-128）" if _bias else "偏りの判定には3組以上で片側が他方の2倍＋1以上を要する（4-128）"),
               f"- 貿易の数字の行：{len(_trd2)}件のうち、相手国の付け方（輸入は原産国・輸出は最終仕向国など）の語がある行：{len(_tp)}件、輸送の時間差の語がある行：{len(_tl)}件。2国の数字を突き合わせる前に、①食い違いを総額に対する割合で測り ②CIF/FOB・時間差（年末の船便は翌年に入る）・相手国の付け方・範囲の順で元を調べる（4-117・4-127・4-128）",
               f"- 同じ品目で同じ値が2ホスト以上に出る組：{len(_old)}組。取得日のいちばん古いホストを元の候補として先に本文で確かめる（取得日は掲載日ではないので、元と決めるのは公開日を本文で見てから・4-24・4-128）",
               *(f"  - {k[0][:28]}：{k[1]}{k[2]} → 最古 {h}（{d}）／{n}ホスト" for k, h, d, n in sorted(_old, key=lambda x: -x[3])[:5]),
               f"- scan.md で「ほぼ確実・かなり高い」を使う行：{_strong}行、うち同じ行に未確認の点が書かれていない行：{_strong_nou}行。強い語を使うなら何が未確認かを同じ行に添える（4-39・4-128）",
               ""]
    except Exception: pass
    # 2026-11-09 4-129（kagawatomo「Waymo」note：報道が「独自に検証できていない」と断った数字は未確認のまま・評価額は調達前か後かの定義で食い違う。
    # 日本銀行 spri02c「GDPギャップの推計とバブル期の金融政策」：過去の判断は当時使えた版のデータで評価する・折れ目の後の2〜3年分だけでトレンドの折れを決めない）
    try:
        _NOV = re.compile(r"独自に検証|独自に確認|確認できていない|検証できていない|確認が取れていない|could not (be )?independently verif|not independently verif|unverified", re.I)
        _nov = [r for r in rows if r.get("値") not in (None, "") and _NOV.search(r.get("引用", "") or "")]
        _VAL = re.compile(r"ポストマネー|プレマネー|評価額|バリュエーション|post-money|pre-money|valuation", re.I)
        _SAL = re.compile(r"売上|売上高|収益|revenue|sales", re.I)
        _vh = {}
        for r in rows:
            if r.get("値") in (None, ""): continue
            q = r.get("引用", "") or ""
            if _VAL.search(q): _vh.setdefault(r.get("品目", ""), set()).add("評価額")
            elif _SAL.search(q): _vh.setdefault(r.get("品目", ""), set()).add("売上")
        _vmix = sorted(k for k, v in _vh.items() if len(v) == 2)
        # 2022年（工業統計→経済構造実態調査）の切れ目の後に何年分あるか：3年未満の系列は新方式での向きを言わない
        _ser = {}
        for r in rows:
            try: _y = int(r.get("年") or 0)
            except (TypeError, ValueError): _y = 0
            if isinstance(r.get("値"), (int, float)) and _y and not r.get("予測"):
                _ser.setdefault((r.get("品目", ""), r.get("区分", ""), r.get("単位", "")), set()).add(_y)
        _short = sorted(((k, len([y for y in ys if y >= 2022])) for k, ys in _ser.items() if any(y < 2022 for y in ys) and any(y >= 2022 for y in ys)), key=lambda x: x[1])
        _sh3 = [x for x in _short if x[1] < 3]
        md += ["## 報道の未検証・評価額の定義・切れ目の後の年数（4-129・機械）",
               f"- 引用の中で書き手自身が「独自に検証できていない・確認できていない」と断った数字の行：{len(_nov)}件。これらは出典が大手でも【未確認】にし、採る値の根拠にしない（4-7・4-129）",
               *(f"  - {r.get('品目', '')[:24]}：{r.get('値')}{r.get('単位', '')}（{r.get('出典', '')[:50]}）" for r in _nov[:5]),
               f"- 同じ品目に評価額（調達前・調達後）と売上の両方の数字が入る品目：{len(_vmix)}品目。評価額は売上でも規模でもない。調達前か後かを確かめるまで評価額どうしも比べない（4-63・4-129）",
               *(f"  - {k[:40]}" for k in _vmix[:5]),
               f"- 2022年の切れ目をまたぐ系列：{len(_short)}本、うち切れ目の後が3年未満の系列：{len(_sh3)}本。切れ目の後の2〜3年分だけで「新しい傾き」を決めず、前後は別の系列として向きは前の区間だけで書く（4-26・4-105・4-129）",
               *(f"  - {k[0][:24]}｜{k[1][:16]}｜{k[2]}：2022年以後 {n}年分" for k, n in _sh3[:5]),
               ""]
    except Exception: pass
    # 2026-11-09 4-130（Veridion「How to find a private company's revenue」：届出の値と人数からの推定が割れたら、①グループ全体の人数か
    # ②観測の日付が新しいか ③別業種の基準値か、を先に確かめる・ランキングの値は推定のことがある。itsmattgeorge「Entity resolution」：
    # 1つの値に絞る規則（最新・最頻・最初の空でない値）を欄ごとに決めて書く。ONS「International comparison of GDP revisions」2016：
    # 直しが小さいのは、後の情報を入れない方針の結果のことがあり精度の証拠でない・方法の変更の分を除いてから直しの偏りを測る）
    try:
        _SALE = re.compile(r"売上|売上高|年商|revenue|sales|turnover", re.I)
        _GRP = re.compile(r"グループ|連結|全社|グローバル|group|consolidated|worldwide", re.I)
        _RANK = re.compile(r"ランキング|番付|上位\d|位）|Forbes|フォーブス|ranking|top \d+", re.I)
        _sg = {}
        for r in rows:
            if isinstance(r.get("値"), (int, float)) and r["値"] > 0 and _SALE.search((r.get("区分", "") or "") + (r.get("引用", "") or "")):
                _sg.setdefault((r.get("品目", ""), r.get("単位", "")), []).append(r)
        _split = []
        for _k, _g in _sg.items():
            _vs = [r["値"] for r in _g]
            if len({r.get("出典") for r in _g}) >= 2 and min(_vs) > 0 and max(_vs) / min(_vs) >= 1.5:
                _yrs = {r.get("年") for r in _g if r.get("年")}
                _split.append((_k, round(max(_vs) / min(_vs), 2), any(_GRP.search(r.get("引用", "") or "") for r in _g), len(_yrs) >= 2))
        _split.sort(key=lambda x: -x[1])
        _rank = [r for g in _sg.values() for r in g if _RANK.search(r.get("引用", "") or "")]
        # 欄ごとに「最新の取得日の値」と「最も多く出た値」が割れる欄（1つに絞る規則が要る欄）
        _cell = {}
        for r in rows:
            if r.get("値") in (None, "") or r.get("予測"): continue
            _cell.setdefault((r.get("品目", ""), r.get("区分", ""), r.get("単位", ""), r.get("年")), []).append(r)
        _surv = []
        for _k, _g in _cell.items():
            _vals = [str(r["値"]) for r in _g]
            if len(set(_vals)) < 2: continue
            _late = sorted(_g, key=lambda r: str(r.get("取得日") or ""))[-1]
            _cnt = {}
            for v in _vals: _cnt[v] = _cnt.get(v, 0) + 1
            _mode = max(_cnt.items(), key=lambda x: x[1])
            _prim = [str(r["値"]) for r in _g if r.get("一次") == "一次"]
            if _mode[1] >= 2 and _mode[0] != str(_late["値"]):
                _surv.append((_k, str(_late["値"]), _mode[0], _prim[0] if _prim else "なし"))
        # 速報と確報の組で直しがゼロの組と、方法の変更の語が版・引用にある組
        _zero = _meth = 0
        _METH = re.compile(r"基準改定|基準年|方法の変更|推計方法|分類の改定|methodolog|rebas", re.I)
        _pz = {}
        for r in rows:
            if isinstance(r.get("値"), (int, float)) and r.get("年") and r.get("版") in ("速報", "確報", "改定"):
                _pz.setdefault((r.get("品目", ""), r.get("区分", ""), r.get("単位", ""), r["年"]), []).append(r)
        for _g in _pz.values():
            if {"速報"} <= {r["版"] for r in _g} and ({"確報", "改定"} & {r["版"] for r in _g}):
                if len({r["値"] for r in _g}) == 1: _zero += 1
                if any(_METH.search((r.get("引用", "") or "") + (r.get("定義", "") or "")) for r in _g): _meth += 1
        md += ["## 売上の割れの点検・1つに絞る規則・直しの小ささ（4-130・機械）",
               f"- 同じ品目で2か所以上の売上の値が1.5倍以上に割れる品目：{len(_split)}品目。どちらを採るか決める前に、①グループ・連結の人数や売上か ②観測の年が違うか ③別業種の基準値を当てた推定か、を確かめる（4-80・4-84・4-130）",
               *(f"  - {k[0][:30]}（{k[1]}）：最大÷最小 {x}倍" + ("・引用にグループ/連結の語" if g else "") + ("・年が2つ以上" if y else "") for k, x, g, y in _split[:5]),
               f"- 売上の値のうちランキング・番付から来た行：{len(_rank)}件。ランキングの値は会社の届出でなく推定のことがあるので、方法の注記を確かめるまで【記事】の扱い（4-51・4-130）",
               f"- 同じ欄・同じ年で「最新の取得日の値」と「最も多く出た値」が割れる欄：{len(_surv)}欄。1つに絞る規則（一次を先・同じ一次なら最新の版・最頻は写しの数なので採る理由にしない・4-57）を scan.md に欄ごとに書く（4-130）",
               *(f"  - {k[0][:24]}｜{k[1][:12]}｜{k[3]}：最新 {a}{k[2]}／最頻 {b}{k[2]}／一次 {p}" for k, a, b, p in _surv[:5]),
               f"- 速報と確報・改定がそろう組のうち、直しがゼロの組：{_zero}組、基準改定・方法の変更の語がある組：{_meth}組。直しが小さいのは後の情報を入れない方針の結果のことがあり精度の証拠にしない。方法の変更による直しは、直しの偏りを測る組から外して別に数える（4-46・4-128・4-130）",
               ""]
    except Exception: pass
    # 2026-11-10 4-131（VantaInsights「How to Research an Industry」：業種の範囲は分類コードを先に全部書き出す・1つの事業が複数の
    # コードにまたがる・開示の分類と統計の分類は別・既存の統計で答えられる問いに独自の調査を使わない・上位社のセグメント開示は統計に無い
    # 取り分の手がかり。ichi_twnovel「情報安全保障」note：利害当事者の予測が検証されないまま前提に入る・監視する側にも事業上の動機がある）
    try:
        _CODE = re.compile(r"産業分類|細分類|小分類|中分類|[（(]\d{3,4}[)）]|NAICS|SIC|JSIC", re.I)
        _STAT = re.compile(r"工業統計|経済構造実態|経済センサス|事業所・企業統計|法人企業統計|賃金構造")
        _items = {}
        for r in rows:
            _items.setdefault(r.get("品目", ""), []).append(r)
        _nocode = [k for k, g in _items.items()
                   if any(_STAT.search((r.get("引用", "") or "") + k) for r in g)
                   and not _CODE.search(k) and not any(_CODE.search(r.get("引用", "") or "") for r in g)]
        _STAKE = re.compile(r"社長|CEO|代表取締役|会長|当社|弊社|自社|our company|we expect", re.I)
        _FC = re.compile(r"見通し|見込み|予測|予想|目指す|計画|forecast|expect|project", re.I)
        _stake = [r for r in rows if (r.get("予測") or _FC.search(r.get("引用", "") or "")) and _STAKE.search(r.get("引用", "") or "")]
        _SURV = re.compile(r"アンケート|当社調べ|自社調べ|調査会社|インターネット調査|n=\d|回答者", re.I)
        _dup = [k for k, g in _items.items()
                if any(r.get("一次") == "一次" for r in g) and any(_SURV.search(r.get("引用", "") or "") and r.get("一次") != "一次" for r in g)]
        _SEG = re.compile(r"セグメント|事業別|部門別|事業部門|segment", re.I)
        _SAL2 = re.compile(r"売上|売上高|revenue|sales", re.I)
        _seg = [r for r in rows if _SEG.search(r.get("引用", "") or "") and _SAL2.search(r.get("引用", "") or "")]
        md += ["## 分類コード・利害当事者の予測・統計で足りる問い・セグメント（4-131・機械）",
               f"- 統計（工業統計・経済構造実態調査など）から取る品目のうち、品目名にも引用にも産業分類の番号・階層の記載が無い品目：{len(_nocode)}品目。表を引く前に日本標準産業分類の番号を全部書き出し、1つの事業が2つ以上の番号にまたがるなら全部を並べる（4-79・4-83・4-131）",
               *(f"  - {k[:40]}" for k in _nocode[:5]),
               f"- 社長・自社など利害当事者の言う見通し・予測の行：{len(_stake)}件。証言の中でも【言明（利害）】として分け、検証されないまま推定の前提に入れない（4-51・4-106・4-131）",
               f"- 一次の統計がある品目に、アンケート・自社調べの値も入っている品目：{len(_dup)}品目。統計で答えられる問いはアンケートの値で答えず、アンケートは統計に無い問いにだけ使う（4-104・4-131）",
               *(f"  - {k[:40]}" for k in _dup[:5]),
               f"- セグメント・事業別の売上の行：{len(_seg)}件。上位社のセグメントの売上を統計の総額で割った取り分は【推定】の手がかりで、範囲（取引段階・国内か）をそろえるまで取り分と書かない（4-63・4-115・4-131）",
               ""]
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
