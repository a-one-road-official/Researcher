"""空・エラーの結果を見逃さない点検。GitHub の毎回の実行の最後に回る（トークン不使用）。
- 1件ごとの結果（out/<テーマ>/companies/*.json）で、引用が1つも無いもの・エラーで落ちたものを見つけたら、その JSON を消して次の実行でやり直させる（同じ件は3回まで。回数は out/<テーマ>/retries.json）。
- 3回やっても空のもの、テーマの半分以上が空のもの、run: true なのに結果が0件のテーマは out/HEALTH.md に「要修正」として書く。1時間おきの Claude はこれを最初に読み、原因のコードや設定を直す。"""
import glob, json, os, re, yaml

ROOT = os.path.dirname(__file__)
OUT = os.path.join(ROOT, "out")
MAX_RETRY = 3
# 2026-11-06 METHOD 4-46（電子決算公告ドットコム「非上場企業に決算公告は義務？」：官報を公告方法とする株式会社で実際に公告したのは約1.8%（東京商工リサーチ 2022年））。
# 中小の決算公告は「無いのが普通」なので、3回空でも設定・コードの不具合（要修正）に数えず、別の行に「無いのが普通」として残す
NORMAL_ABSENT = re.compile(r"(決算公告|官報)")

def layer_of(x):
    """2026-11-07 METHOD 4-71（AIよろず室「RAG導入でよくある失敗7選」：失敗を層に分けて切り分け、層を見ずに直さない）。
    3回空の品目を、取得の不具合／検索0件（検索語の層）／検索は当たったが本文から取れない（取得・抜き出しの層）に分ける。"""
    if x.get("error"):
        return "取得の不具合"
    log = x.get("search_log") or []
    hits = sum(len(e.get("results") or []) for e in log)
    if not log:
        return "検索を回していない（仕様の層）"
    if hits == 0:
        return "検索0件（検索語の層：別名・英語名・統計名を足す）"
    return "検索は当たったが本文から取れない（取得・抜き出しの層：表・PDFを直接読む処理を足す）"


def main():
    ctl = (yaml.safe_load(open(os.path.join(ROOT, "control.yaml"), encoding="utf-8")) or {}).get("topics") or {}
    lines, bad_total = ["# 点検（空・エラー）", ""], 0
    for t, c in ctl.items():
        d = os.path.join(OUT, t)
        js = glob.glob(os.path.join(d, "companies", "*.json"))
        rp = os.path.join(d, "retries.json")
        retries = json.load(open(rp)) if os.path.exists(rp) else {}
        empty, gave_up, errs, normal_absent, layers = [], [], {}, [], {}
        stale = []
        for f in js:
            try:
                x = json.load(open(f, encoding="utf-8"))
            except Exception:
                x = {}
            ev = x.get("evidence") or {}
            err = x.get("error") or ""
            if any(ev.values()) and not err:
                continue
            name = x.get("name") or os.path.basename(f)
            # 2026-11-07 57回目：瀬戸内の一覧から外したブース番号（「106」など数字だけの名・4-80）の古い結果が、
            # 「3回やっても空（要修正）」に残り続けていた。数字だけの名は社名でも品目でもないので、要修正に数えず別の行に出す
            if re.fullmatch(r"[0-9０-９]+", str(name).strip()):
                stale.append(name)
                continue
            if err:
                errs[name] = str(err)[:80]
            n = int(retries.get(name, 0))
            if n < MAX_RETRY:
                retries[name] = n + 1
                os.remove(f)  # 次の実行でやり直す
                empty.append(name)
            elif NORMAL_ABSENT.search(name):
                normal_absent.append(name)
            else:
                gave_up.append(name)
                layers[layer_of(x)] = layers.get(layer_of(x), 0) + 1
        if retries:
            json.dump(retries, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        fm = os.path.join(d, "facts.md")
        fact_errs = re.findall(r"エラー：([^\n]{0,80})", open(fm, encoding="utf-8").read()) if os.path.exists(fm) else []
        problems = []
        if c and c.get("run") and not js and not os.path.exists(os.path.join(d, "BUILD_DONE")) and not glob.glob(os.path.join(d, "*.csv")):
            problems.append("run: true なのに結果が0件")
        if stale:
            problems.append(f"仕様から外れた古い結果（数字だけの名・要修正に数えない・4-80）{len(stale)}件：" + "、".join(stale[:5]))
        if empty:
            problems.append(f"空・エラーで自動やり直し {len(empty)}件")
        if gave_up:
            problems.append(f"3回やっても空 {len(gave_up)}件（要修正）：" + "、".join(gave_up[:5]))
            problems.append("空の層（4-71）：" + "、".join(f"{k} {v}件" for k, v in sorted(layers.items(), key=lambda kv: -kv[1])))
        if normal_absent:
            problems.append(f"決算公告の品目で3回空 {len(normal_absent)}件（公告しない会社が大半なので要修正に数えない・4-46）")
        if js and (len(empty) + len(gave_up)) * 2 >= len(js):
            problems.append(f"テーマの半分以上が空（{len(empty)+len(gave_up)}/{len(js)}）— 設定かコードの不具合の疑い（要修正）")
        if fact_errs or errs:
            sample = list(errs.values())[:2] or fact_errs[:2]
            problems.append("エラー：" + " / ".join(sample))
        if problems:
            bad_total += 1
            lines.append(f"- **{t}**：" + "／".join(problems))
    # 2026-11-06 走査の失敗から（mfg_capacity：品目名の「74,587人」「5,626〜31,487人」のカンマで CSV の列がずれ、品目名が途中で切れ、
    # 英語名・別名・直接URLの列が別の文字になった）。inputs の行の列の数が見出しと違えば要修正として出す
    import csv
    for fp in sorted(glob.glob(os.path.join(ROOT, "inputs", "*.csv"))):
        try:
            rr = list(csv.reader(open(fp, encoding="utf-8-sig")))
        except Exception:
            continue
        badr = [i + 1 for i, r in enumerate(rr[1:], 1) if r and rr and len(r) != len(rr[0])]
        if badr:
            bad_total += 1
            lines.append(f"- **inputs/{os.path.basename(fp)}**：列の数が見出しと違う行 {len(badr)}件（要修正・品目名のカンマを疑う）：{badr[:5]}")
    if bad_total == 0:
        lines.append("問題なし")
    open(os.path.join(OUT, "HEALTH.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
