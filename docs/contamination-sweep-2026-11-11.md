# Researcher 全面点検（2026-11-11）— am_owners 以外への欠陥の広がり

点検対象：GitHub リポジトリ a-one-road-official/Researcher の全ビルダー・共通エンジンと、`researcher/out/` の全データセット（トピック66件＝市場型40・会社型26、うち summary.csv が存在する62件を取得して検査）。
方法：コード実読＋出力CSVへの指紋検査（(a) 社名として成立しない行 (b) 正規化社名の重複 (c) 複数社で共有される公式URLホスト (d) 件数列>0なのに実データ列が空の行）＋実URLスポットチェック。
am_owners の欠陥番号は先行報告書（audit-2026-11-11.md）に対応：D1=文字列照合だけの同定／D2=スニペット証拠化／D3=正規表現での社名切り出し／D4=ゲートなし。

## 結論（先に）

- **同じ病気が最も重いのは feedstock 系（build_feedstock_dir → feedstock_jp_check）で、am_owners より大規模**。212社中211社の「公式サイト」が同一の bit.ly 短縮URL（飛び先は VoxelMatters のニュースレター登録ページ）で、それが「given（検証なし）」として208社の調査の土台になった。
- **共通エンジン（run.py の identify）を使う全会社型トピックが D1 を共有**する。ir1000 では京浜急行電鉄・京王電鉄・東京地下鉄の3社とも「公式サイト」が日本民営鉄道協会（mintetsu.or.jp）になっている（実地確認済み）。
- **もう1つの共通欠陥が見つかった：merge.py は out/<topic>/companies/ に残る全JSONを summary.csv に出し続ける**。入力リストから外れた会社が summary に残り続ける（award_chain：summary 65社中56社が現在の入力に無い／setouchi_mfg：528社中164社が残存、その中にブース番号・電話番号・様付きの名が現行の summary に残っている）。
- 会社型サマリ23件・4,064行の合計：社名として成立しない行138行、**「証拠件数>0なのに会社概要の全欄が空」の行が1,495行（件数>0の行の37%）**。創業年度・株主の列はほぼ全データセットで9割以上が空（ir1000：各1,030/1,034行が空）。
- 対策済みは build_setouchi_mfg と build_award_chain の最終成果物（inputs/list.csv）だけで、その防御（ポータルホストのブロックリスト約150件等）は共通エンジン run.py に伝播していない（run.py の AGGREGATORS は約40語で、catr.jp も cybo.com も入っていない）。

## 1. コード側：共有関係とビルダー×欠陥

### 共通エンジン（全トピックが使う第2段）
| 部品 | 役割 | 欠陥 |
|---|---|---|
| run.py `identify()` | 公式URLの特定 | **D1そのもの**。入力にURLがあれば集約サイトでない限り無検証で採用（`given`）。無ければ検索し「題名＋抜粋に社名文字列がある最初のヒット」のホームを、さらに「ホーム本文の先頭4,000字に社名文字列がある」だけで確定。住所・法人形態の照合なし |
| variants.py `name_hits()` | 同一性の判定 | 社名を括弧・スラッシュで割った**2文字以上の断片でも一致扱い**（例：「フジ」「Tech」級の断片）。全工程の同一性判定がこれ |
| run.py pass1/pass2 | 公式サイトからの抽出 | 取得したページに**社名チェックなし**（need_name は pass3 のみ）。間違ったサイトに着地すると、その会社の概要・証拠としてそのまま取り込む |
| run.py pass3 / social_pass | 外部・SNS | ページは name_hits のみで証拠化。検索スニペットは「検索結果の抜粋」「SNS・note（検索結果の抜粋）」として証拠に保存される（ラベル付きだが下記の判定に使われる） |
| extract.py | 抽出 | 証拠＝「キーワードを含む行」の引用。会社概要はラベル照合＋型チェック（valid_value）があり比較的防御されている |
| merge.py | summary.csv 生成 | **D4＋残存欠陥**：companies/ の全JSONを無条件に出力。入力から外れた会社・古い同定の結果が残り続ける。不合格という概念がない（not_found も行として出る）。件数列はカテゴリ別の引用数で、merge_into の上限60で頭打ち |
| merge.py `write_judged()` | feedstock_jp_check / oldlist_hr の判定 | 「日本に取扱いあり」の判定に**検索スニペットの引用も使う**。強い日本の手がかりの正規表現には「株式会社」単体も含まれる（引用に株式会社と出れば強い手がかり扱い） |
| judge_ai.py | 要確認行のAI判定 | 上流の引用をそのまま食う。実例：Polymaker B.V. に「余地あり」と判定しながら理由は「日本の代理店が存在することが明記されています」（理由と判定が矛盾、ai_judged.json 内、40件中） |
| scan.py | cross.csv（数字の突き合わせ） | 証拠引用から数字を抜くため、上流の証拠の質をそのまま引き継ぐ（一次情報タグ等の防御はある） |

### ビルダー×欠陥
| ビルダー | 出力先 | D1 文字列同定 | D2 スニペット証拠 | D3 正規表現切り出し | D4 ゲートなし | 備考 |
|---|---|---|---|---|---|---|
| build_am_owners | inputs/am_owners | ●（第2段） | ● | ● | ● | 先行報告書の通り（基準症例） |
| build_feedstock_dir | inputs/feedstock_jp_check | ●（検証なし） | ― | ▲（題名の分割失敗） | ● | 企業ページの「ディレクトリ外・SNS以外の最初のリンク」を公式サイトと決める。そのリンクが全ページ共通のニュースレター用 bit.ly だった |
| build_ipros | out/ipros_hr | ● | ● | ― | ▲ | 社名・URLは ipros_all.csv の掲載データの写し（無検証）。同定は award_chain.check_one に委譲するが、check_one は渡された home を社名照合せずに読む。有料求人の判定はスニペット。売上は check_one の外部スニペット fallback で足りる場合がある。門番（verify）はあるが3条件中2つまでスニペット依存になりうる |
| build_award_chain | inputs/award_chain | ▲ | ▲ | ▲ | ― | find_official は題名の鍵一致のみ。資本金は検索スニペット fallback が chain 1,329社中280社（21%）で、1億円の門番の入力になっている（引用には「外部：」と印は付く）。最終 list.csv は verify_list の門番（引用必須・4条件）を通る |
| build_setouchi_mfg | inputs/setouchi_mfg | ▲ | ▲ | ●（ただし valid_name で防御） | ― | 最も防御が厚い：valid_name（文の切れ端・求人文言・大手グループを排除）、ポータル約150ホストのブロック、題名条件、共有ホストの公式化禁止、社名鍵＋ホストの両方一致でのみ名寄せ、上場・大手の除外。ただし段階Bの照合（製造業・所在地）と従業員・資本金はスニペット由来。防御はこのファイルの中だけ |
| build_bv100 | inputs/bv100 | ― | ― | ▲ | ▲ | 公式サイトの企業ページ（slug単位）から取るので構造的に重複しにくい。題名からの社名抽出で2社が同じ「Tech」になった（下記）。home_url は「外部リンクの最初」で feedstock_dir と同じ型の弱点（実測では共有ホスト0件） |
| build_ir1000 | inputs/ir1000 | ― | ― | ― | ― | JPX上場会社一覧／EDINETコード表が種、鍵は証券コード。ビルダー側は健全。第2段の identify で汚染される（下記 ir1000 の項） |
| build_kougyo | out/mfg_capacity | ― | ― | ― | ― | 経産省の統計表そのものを読む。会社名は扱わない。対象外として健全 |
| coconala.py | out/coconala | ― | ― | ― | ― | ココナラのサービス出品（ID単位）であって会社名簿ではない。対象外 |
| x_pain.py | （別仕組み） | ― | ― | ― | ― | X投稿の困りごと抽出。会社同定は扱わない。対象外 |

## 2. 出力側：指紋検査の数値

### 会社型 summary.csv（merge.py 生成）
代表的なもの（全23件の生データは fingerprint_results.json）：

| データセット | 行数 | (a) 不成立な社名 | (b) 重複行 | (c) 共有ホスト | (d) 件数>0＆概要全空 | 特定方法 |
|---|---|---|---|---|---|---|
| am_owners | 171 | 11（様7・日付2・切断1・法人格のみ1） | 2 | 14ホスト/28社 | 保有設備は全行空（先行報告） | search 129 / not_found 42 |
| feedstock_jp_check | 266 | 48（全て「— VoxelMatters」付き等の長名） | 0 | **bit.ly を208社が共有** | **244/261（93%）** | given 262 / not_found 4 |
| ir1000 | 1,034 | 0 | 0 | mintetsu.or.jp を3社が共有 | 164/1,030（16%） | **search 971** / not_found 63 |
| oldlist_hr | 1,418 | 68 | 0（ただし変種重複あり・下記） | 9ホスト/18社 | **715/1,399（51%）** | given 1,362 / search 24 / not_found 32 |
| setouchi_mfg | 528 | 1（こっこー様）＋残存行に番号・年・電話番号 | 2 | **32ホスト/110社（catr.jp 15社・cybo.com 12社）** | 123/513（24%） | given 512 / search 15 |
| award_chain | 65 | 0 | 0 | 0 | ― | ―（ただし65社中56社は現在の入力に無い残存行） |
| material_traders | 34 | 0 | 0 | 0 | 20/33（61%） | given 33 |
| mfg_platforms | 34 | 1（MFG.com） | 0 | 0 | 20/34（59%） | given 32 |
| mfg_recruit_cos | 17 | 0 | 0 | 0 | 1/17 | given 16 |
| supplier_chain | 3 | 0 | 0 | 0 | 3/3（概要6欄すべて全行空） | given 3 |
| am_software | 52 | 0 | 0 | ― | ― | 手作り入力。社名は健全 |
| am_bureau | 26 | 0 | 0 | ― | ― | 同上 |
| bv100 | 129 | 0 | 0 | ― | ― | 下記 names.csv 参照 |

注：(b) の正規化は法人格・空白・中点を落とすだけなので、「Perfect / Perfectcorp」のような変種重複は数えられていない（oldlist_hr で目視確認：perfectcorp.com を2社、tempress.nl を「Tempress / Tempress Engineering B.V.」、xpanner.com を「Xpanner / xpanner.com」が共有）。

### ビルダー出力（中間・最終成果物）
| ファイル | 行数 | 結果 |
|---|---|---|
| feedstock_dir/companies.csv | 212 | **home_url：211社が同一の https://bit.ly/3zUkyBr（残り1社は空）**。社名43件に「— VoxelMatters」の接尾辞（題名の区切りが全角ダッシュ「—」で分割正規表現が半角系のみのため残存）。重複0 |
| feedstock_jp_check/judged.csv | 266 | 判定：余地あり197・除外（日本に取扱いあり）51・要確認13・除外（大手）5。うち208社は bit.ly 土台の調査結果からの判定 |
| oldlist_hr/judged.csv | 8,229 | 未調査6,804。判定済み1,425（余地あり519・除外410・要確認383・大手113）。判定は引用＋スニペットへの正規表現 |
| bv100/names.csv | 98 | slug単位で健全。ただし name='Tech' が2社（slug: naturaltech / tobetech）— 題名抽出で別会社が同じ表示名に潰れた。第2段は社名キーなので衝突する |
| bv100/_v1_wrong/ | ― | 旧版の誤り出力を退避した跡がある（names.csv / pages.csv）。失敗を隔離する運用自体は他ビルダーに無い |
| setouchi_mfg/names.csv（段階A・生） | 2,459 | 重複436行/177組（県違いの重複を含む設計上のものもある）。ただし「志望動機・自己PR不要」「説明会参加企業一覧表」「本日締切」という求人・一覧の文言が社名として残存（現行 valid_name では弾かれるはずのものが checked.csv にも残っている＝古い結果の残存） |
| setouchi_mfg/checked.csv（段階B） | 1,500 | status: ok 661 / pref_mismatch 342 / not_mfg 255 / not_found 242。ok の中にも公式URLが catr.jp 28社・cybo.com 22社・マイナビエージェント12社（いずれも自社サイトではない）。最終 inputs では除去済み（現行 inputs 364社にポータルホスト0件を確認） |
| award_chain/seeds.csv | 9,753 | 不成立1件のみ（'2024年5月14日UNTRACKED'＝Excelの変更履歴の断片）。種は公式の表彰・認定一覧の行単位で、行に県名/「製造」がある社だけ取る作りで健全 |
| award_chain/chain.csv | 1,329 | 不成立0・重複0。共有ホスト12件/33社：best100.v-tsushin.jp を6社（自社の公式でなくベストベンチャー100の掲載ページが公式URLに）、kensetsu.profile-partner.info を3社（建設業のDB）。資本金が検索スニペット由来の社280社（21%） |
| award_chain/list.csv（最終・門番通過） | 9 | 全指紋クリーン |
| inputs/setouchi_mfg.csv（最終） | 364 | 全指紋クリーン（ポータルホスト0） |
| ipros_hr/seeds.csv | 45,108 | ipros_all.csv の写し。不成立542件（1.2%：本社・支店の列挙が社名に付着322件の文断片ほか）。**重複3,604行/1,470組**（イプロス上の複数掲載・支店が別行のまま）。公式URLはイプロスの掲載データ由来で、このビルダーは検証しない。checked.csv / list.csv は git 上では0行（判定工程の成果物はまだコミットされていない） |
| ir1000/screen.csv | 3,702 | status ok 3,623。社名はJPX由来で健全（「株式会社ホテル、ニューグランド」は実在の上場社名で誤検出） |
| inputs/oldlist_hr.csv | 8,229 | ドメインそのものが社名の行がある（'moneyforward.com'、'WhiteLabelCRM.com by AllClients' 等）。長い正式社名（トルコの法人等）を含め要精査の行が多い |
| inputs/feedstock_jp_check.csv | 266 | bit.ly 208行（feedstock_dir からの流入）。社名48件に接尾辞・括弧注記 |
| inputs/am_trade.csv（am_flow用） | 197 | 社名に注記が埋め込まれた行（'Shimoda Iron Works Co Ltd (日本語社名は未確認)'、'rightmfg.co.jp 運営会社(社名ページ未取得)'＝ドメイン＋メモが「社名」）。手作り入力の清書漏れ |

### 市場型トピック（40件）について
会社同定の病気は対象外（「品目」が行）。ただし2点は引き継がれている：
- 証拠は phrase_hits（別名の全単語がページのどこかにあれば一致）で集めた引用行で、extract.figures と scan.py がその引用から数字（市場規模・CAGR等）を抜くため、引用の質がそのまま数字の質になる。
- 件数列は上限60で飽和する（例：whitelabel の summary では複数カテゴリが全行60）。「引用数」を量として読めない。
（注：本点検の初期検出で whitelabel / wl_* / feedstock_makers_world / feedstock_startups の「社名が全行空」と出たのは、列名「自社名で出せる条件」に含まれる「社名」を検出器が拾った誤検出。これらは市場型で品目行は正常に入っている。）

## 3. スポットチェック（実際に開いて確認）

1. **ir1000 / mintetsu.or.jp**：京浜急行電鉄・京王電鉄・東京地下鉄の公式URLが全て https://www.mintetsu.or.jp/ 。実際に開くとタイトルは「日本民営鉄道協会」— 3社の業界団体のトップページ。identify が団体の会員一覧ページで社名文字列に当たり、ホームでも文字列が見つかったため確定したもの。
2. **feedstock / bit.ly/3zUkyBr**：飛び先は https://voxelmatters.us12.list-manage.com/subscribe?...（VoxelMatters のメールマガジン登録ページ）。個別JSON（3DBioFibR — VoxelMatters）では pass1 がこの登録ページを「公式サイト」として取得（status ok）し、そこから「連絡先」の証拠1件が取られ、SNS証拠は VoxelMatters 自身の Facebook や無関係な個人の LinkedIn だった。現在の VoxelMatters の企業ページでは当該 bit.ly は確認できなかった（サイト側が変わった可能性）ため、これはリポジトリ内の記録に基づく確定事実として扱う。
3. **setouchi_mfg / catr.jp**：残存行「タダノインフラエンジニアリング」の公式URLは https://catr.jp/ 。開くと「官報決算データベース」（企業DBのトップページ）で、同社のサイトではない。現在の summary.csv に残っている。
4. **oldlist_hr / formnext.mesago.com**：Hiperbaric SA と Quintus Technologies AB の公式URLが、Formnext の出展社検索ページ（…/exhibitor-search.html）。旧リスト由来のURLが 'given' で無検証に通ったもの。

## 4. データセットごとの判定

### 同じ病気あり
- **feedstock_dir / feedstock_jp_check**（最重症）：公式URLの土台が全社同一の短縮URL→無関係な登録ページ。208社の調査・判定（judged.csv、ai_judged 40件）がその上に乗っている。プロフィール欄は266社中 従業員数266/266・株主265/266・売上高265/266 が空。
- **setouchi_mfg の summary.csv（第2段の公表物）**：ビルダーの最終成果物（inputs 364社）は健全なのに、summary は残存JSONで膨らみ、164/528社が現在の入力に無い会社。ブース番号（106、114、122…）、'1943年'、'39社'、電話番号 '03-5693-2775'、'こっこー様'、除外対象のはずの大手（JFEスチール、JR東海）が行として残り、27社の公式URLが catr.jp / cybo.com。
- **ir1000 の第2段同定**：971/1,034社（94%）が文字列検索による同定。協会・DBへの吸着が実例つきで確認できた（mintetsu 3社）。63社は not_found（空欄のまま公表）。
- **am_owners**：先行報告の通り（171社中26%が他社サイト等）。

### 一部あり
- **共通エンジンを使う全会社型トピック**：'given' の無検証採用と identify の文字列照合が既定動作。入力が手作りで小さいトピック（am_software 52社、am_bureau 26社、am_bigplayer_why 24社 等）は社名・URLの実害は見当たらないが、(d) の「件数>0＆概要全空」は中規模トピックでも出る（material_traders 61%、mfg_platforms 59%、wl_profit 45%、oldlist_hr 51%）。
- **oldlist_hr**：入力リスト自体にドメイン=社名や変種重複が混在し、summary の51%が概要全空。判定（judged.csv）はスニペットを含む引用への正規表現。
- **award_chain**：最終 list.csv（9社）は門番を通り健全。ただし chain.csv の資本金の21%がスニペット由来で門番の入力になっており、公式URLに掲載ページ・DBが混入（6社＋3社）。summary.csv は65社中56社が残存行。
- **ipros_hr**：種（45,108行）に重複1,470組と不成立542件。URL・社名はイプロスの掲載データの無検証の写しで、同定と判定は award_chain.check_one（home を照合しない）とスニペット判定に依存。最終リストはまだ生成されていない（checked/list が0行）ため、被害は未確定だが、土台は未検証。
- **bv100**：構造は健全。'Tech' 2社の表示名衝突のみ。
- **am_flow（am_trade入力）**：注記埋め込み社名が数件。手作り入力の清書の問題で、エンジンの病気ではない。

### なし（健全と判断）
- build_ir1000（ビルダー側）／ir1000/screen.csv：公式一覧＋証券コードが鍵。
- build_kougyo / mfg_capacity の統計表読み：会社を扱わない。
- coconala：サービス出品の名簿で、会社同定の対象外。
- award_chain/list.csv、inputs/setouchi_mfg.csv：門番・防御を通った最終成果物そのものは指紋検査クリーン。
- 市場型40件：会社同定の観点では対象外（ただし数字の出所が引用依存・件数飽和の弱点は上記の通り）。

## 5. 最悪の実例（3つ）

1. **feedstock_jp_check の208社**：公式サイトが全社同じ bit.ly → メールマガジン登録ページ。その登録ページから「連絡先」の証拠が取られ、VoxelMatters 自身のSNSが各社の証拠に混ざり、その結果で「日本の取扱いなし（余地あり）197社」等の判定が出ている。
2. **ir1000 の大手私鉄3社**：京浜急行電鉄・京王電鉄・東京地下鉄の公式サイトが業界団体「日本民営鉄道協会」のトップページ。上場企業の名簿で、最も目立つ位置の会社に起きている。
3. **setouchi_mfg summary の残存行**：現在の入力に無い164社が公表物に残り続け、その中にブース番号「106」、電話番号「03-5693-2775」、年「1943年」が「社名」として並び、タダノインフラエンジニアリングの公式URLが企業DBのトップページになっている。ビルダー側で直したはずの失敗が、公表物では直っていない。

## 6. 検出器の限界（過大評価しないための注記）
- (a) の「長すぎる社名（>34字）」は、海外の長い正式法人名（トルコ・ドイツ等）では正常な場合がある。oldlist_hr と ipros_hr の件数にはその分が含まれる。
- (b) の重複は表記変種（Perfect/Perfectcorp 型）を数えられないため過小評価。
- (d) の「概要全空」は、そのトピックで会社概要の取得自体が目的でない場合にも出る。ただし創業年度・株主は多くのトピックで明示の調査項目でありながら9割以上空である点は、項目設計と実装の乖離として記録する。
- HEALTH.md（リポジトリ自身の点検）は「空・エラー」の検出に限られ、同定の正しさは点検していない。本報告書の指摘はいずれも HEALTH.md に現れない種類の欠陥。
