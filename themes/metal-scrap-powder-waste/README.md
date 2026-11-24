# 金属端材・粉・廃棄物回収プラットフォーム（metal-scrap-powder-waste）

2026-11-24 本人指示で追加。既存の共通コード（researcher/）で回す。別のリポジトリ・別のエンジンは作っていない。

## 集める問い
1. 日本で、どの会社の、どの部品工程から、どの合金（チタン・ニッケル・コバルト・タングステン・アルミ・マグネシウム優先）の端材・削りくず・使用済み粉が、どれだけ出ているか（公式の事業ページ・工場紹介・環境報告書・統合報告書、自治体の産廃データ、業界団体の統計）
2. その端材をいま誰が、いくらで引き取っているか（スクラップ業者・非鉄問屋の相場、業者名、創業年、規模）
3. 粉を作る会社が外から端材を買っているか（6K Additive、Green Titanium 3D／Jorcar Titanium、AMAZEMET（rePOWDER）ほか国内外）
4. 端材マッチング・端材データベースの前例（素材を問わず。メーカー（例：マツダ）に持ち込んだ会社。日・英・独・蘭・仏・伊・中・韓の業界紙・PR配信・展示会出展者リスト）
5. 前提条件（消防法の金属粉、産廃の収集運搬・処分の許可、古物商、輸出入の規制）

## ファイル
| 置き場所 | 中身 |
|---|---|
| researcher/topics/metal-scrap-powder-waste.yaml ＋ researcher/inputs/metal-scrap-powder-waste.csv | 第1段：問い1〜5を30品目に割ったもの（kind: market） |
| researcher/topics/metal-scrap-powder-waste-cos.yaml ＋ researcher/inputs/metal-scrap-powder-waste-cos.csv | 第2段：会社40社の種（kind: company・三重確認）。会社は inputs の CSV に行を足せば次の回で調べる |
| researcher/build_metal_scrap_sheet.py | 下書きシート・交差・円換算・引用・ドライブ用の文をつくる |
| .github/workflows/metal_scrap.yml | 手動実行だけのワークフロー |
| themes/metal-scrap-powder-waste/draft_sheet.csv | 会社一覧の粗い下書き（事実の列だけ・「本人記入」の空欄つき） |
| themes/metal-scrap-powder-waste/cross.csv | 会社×項目の交差数・状態（交差／未交差）・各線の出典 |
| themes/metal-scrap-powder-waste/questions.csv | 問い×項目の交差数・状態・各線の出典 |
| themes/metal-scrap-powder-waste/facts.md | 引用そのまま（系統・出典URL・円換算つき） |
| themes/metal-scrap-powder-waste/drive_append.md | ドライブの記録ドキュメントの末尾に足す日付つきの文 |

会社1社ごとの列：社名／公式URL／創業年／株主構成／売上・従業員数／何でどう稼いでいるか（誰が誰にいくら払うか）／端材・粉との関わり／出典URL（＋交差した項目・未交差の項目・円換算・記載なし・調べた日・本人記入の3列）。

## 判定の決まり（METHOD.md 末尾の追記と同じ）
- キーワード一致だけでタグを付けない。判定・地図の位置は本人がシートに書く。プログラムは事実の列だけ。
- 「記載なし」は ①本文 ②画像・PDF（OCR） ③別言語・別の情報源 の三重確認の後だけ。
- 別系統の出どころ2つ以上が同じ会社・同じ項目で交わったものだけ「交差」。交差数と各線の出典を残し、1本だけは「未交差」。
- 外国通貨の金額は円換算を併記（ECB参照レート・基準日つき）。

## 手動実行のやり方（本人が指示したときだけ）
1. この枝を main に入れる（workflow_dispatch のワークフローは既定の枝にあるときだけ Actions の画面に出る）。main に入れても、ほかのワークフローは11/17から止めたまま（disabled_manually）で、このファイルに push の起動は無い。
2. GitHub → Actions → 「metal-scrap-powder-waste」→ Run workflow。
   - stage：questions（問い1〜5）／companies（会社ごと）／both
   - shards：同時に回す本数 1〜4　／　per_run：1本が1回で調べる件数 1〜10
3. 終わると結果が実行した枝に commit される。次の回は自分で呼ばない（続きが要るときはもう一度押す。済んだ件は飛ばす）。
4. ドライブへの追記：GitHub Actions からは Google に書かない（鍵を置かない）。回った後に Claude に「ドライブに追記」と言えば、drive_append.md と下書きシートの要点を記録ドキュメントの末尾に日付つきで足す。

## 使用量の上限（metal_scrap.yml の中で固定）
- 同時に回す本数 最大4（max-parallel: 4）、1本の制限時間 60分（中の調査は55分で打ち切り）、1本の件数 最大10、まとめ 20分、準備 5分。
- 1回の手動実行の最悪値：約265ランナー分。schedule・push・自分で次の回を呼ぶ仕組みは無い。
- GitHub 側の使用量の上限（Billing の Budget と「上限で止める」）には触っていない。外さない。
