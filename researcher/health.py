"""空・エラーの結果を見逃さない点検。GitHub の毎回の実行の最後に回る（トークン不使用）。
- 1件ごとの結果（out/<テーマ>/companies/*.json）で、引用が1つも無いもの・エラーで落ちたものを見つけたら、その JSON を消して次の実行でやり直させる（同じ件は3回まで。回数は out/<テーマ>/retries.json）。
- 3回やっても空のもの、テーマの半分以上が空のもの、run: true なのに結果が0件のテーマは out/HEALTH.md に「要修正」として書く。1時間おきの Claude はこれを最初に読み、原因のコードや設定を直す。"""
import glob, json, os, re, yaml

ROOT = os.path.dirname(__file__)
OUT = os.path.join(ROOT, "out")
MAX_RETRY = 3

def main():
    ctl = (yaml.safe_load(open(os.path.join(ROOT, "control.yaml"), encoding="utf-8")) or {}).get("topics") or {}
    lines, bad_total = ["# 点検（空・エラー）", ""], 0
    for t, c in ctl.items():
        d = os.path.join(OUT, t)
        js = glob.glob(os.path.join(d, "companies", "*.json"))
        rp = os.path.join(d, "retries.json")
        retries = json.load(open(rp)) if os.path.exists(rp) else {}
        empty, gave_up, errs = [], [], {}
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
            if err:
                errs[name] = str(err)[:80]
            n = int(retries.get(name, 0))
            if n < MAX_RETRY:
                retries[name] = n + 1
                os.remove(f)  # 次の実行でやり直す
                empty.append(name)
            else:
                gave_up.append(name)
        if retries:
            json.dump(retries, open(rp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
        fm = os.path.join(d, "facts.md")
        fact_errs = re.findall(r"エラー：([^\n]{0,80})", open(fm, encoding="utf-8").read()) if os.path.exists(fm) else []
        problems = []
        if c and c.get("run") and not js and not os.path.exists(os.path.join(d, "BUILD_DONE")) and not glob.glob(os.path.join(d, "*.csv")):
            problems.append("run: true なのに結果が0件")
        if empty:
            problems.append(f"空・エラーで自動やり直し {len(empty)}件")
        if gave_up:
            problems.append(f"3回やっても空 {len(gave_up)}件（要修正）：" + "、".join(gave_up[:5]))
        if js and (len(empty) + len(gave_up)) * 2 >= len(js):
            problems.append(f"テーマの半分以上が空（{len(empty)+len(gave_up)}/{len(js)}）— 設定かコードの不具合の疑い（要修正）")
        if fact_errs or errs:
            sample = list(errs.values())[:2] or fact_errs[:2]
            problems.append("エラー：" + " / ".join(sample))
        if problems:
            bad_total += 1
            lines.append(f"- **{t}**：" + "／".join(problems))
    if bad_total == 0:
        lines.append("問題なし")
    open(os.path.join(OUT, "HEALTH.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
