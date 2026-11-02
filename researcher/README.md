# Researcher（全テーマ共通のリサーチャー）

金属AM・戦略17分野・商社・ソフトウェアなど、どのテーマも同じコードで回す。テーマごとに変えるのは `topics/<テーマ>.yaml` と入力CSVだけ。

- **オン・オフ**：`control.yaml` の `run: true / false` を書き換えて push。オンにすると GitHub Actions が回り、終わるまで自分で次の回を呼ぶ。
- **AI・有料APIは使わない**：HTTP、ヘッドレスChromium、pypdf、tesseract OCR、無料の複数検索エンジン（Yahoo! JAPAN＝Google系、Google/Bing/DuckDuckGo/Brave/Yahoo）。
- **1社ごとの三重確認**：①公式サイト本文 ②PDF・画像のOCR ③外部情報源（表記ゆれ：全角/半角、株式会社の有無、別名、Japan/日本/K.K.）。3回とも空なら「記載なし（3回確認済み：見た件数）」。
- **出力は事実だけ**：`out/<テーマ>/companies/*.json`（引用＋URL＋何回目で見つけたか）、`out/<テーマ>/facts.md`、`out/<テーマ>/summary.csv`。要約・解釈・順位づけはしない。

## 2種類の調べ方（topics/*.yaml の `kind`）
- `kind: company`（既定）：会社ごと。公式サイト→PDF・画像→外部の三重確認。例：`am_trade`
- `kind: market`：市場・品目ごと。日本語名と英語名で複数エンジンを検索し、品目名が出てくるページから、市場規模・成長率・上位シェア・主要企業・投資の行をそのまま引用。数字の入っていない行は捨てる。CAGRとシェアの数字は出典URLつきで `summary.csv` に並べる。例：`jgs17_market`（戦略17分野の75小区分）

## 新しいテーマを足す
1. `inputs/<テーマ>.csv`（会社：社名, 公式URL, 別名, 文脈／市場：品目, 英語名, 別名, 文脈。別名は ; 区切り、空でよい）
2. `topics/<テーマ>.yaml`（会社なら `am_trade.yaml`、市場なら `jgs17_market.yaml` を写して、拾う項目・キーワード・外部検索の型を変える）
3. `control.yaml` に `<テーマ>: {run: true, shards: 6, per_run: 12}` を足して push

## 結果の場所
`researcher/out/<テーマ>/summary.csv`（一覧）と `facts.md`（引用全部）。1件ごとの生データは `out/<テーマ>/companies/*.json`。済んだ件は次の回で飛ばすので、途中で止めても続きから回る。やり直すときはその件のJSONを消す。
