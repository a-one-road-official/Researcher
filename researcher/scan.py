"""走査（機械でできる部分）。トークンを使わずに毎回の結果から突き合わせ表と推定を作る。
out/<テーマ>/companies/*.json の引用から「年・数字・単位・出典」を抜き、品目×区分×単位ごとに並べる。
出典ごとの食い違い（最大÷最小）、一次情報かどうか、年ごとの中央値、10年の向き（上昇/下降/横ばい/判定不可）を出す。
同じ品目・同じ年に「金額」と「人数」の両方があれば、金額÷人数を【推定】1人あたりとして出す（式と出典件数つき）。
出力：out/<テーマ>/cross.csv（全数字）、out/<テーマ>/scan_auto.md（品目ごとの結論の下書き）。解釈の最終判断は scan.md（Claude）で行う。"""
import csv, glob, json, os, re, statistics, sys
from urllib.parse import urlparse

OUT = os.path.join(os.path.dirname(__file__), "out")
PRIMARY = re.compile(r"(\.go\.jp|\.lg\.jp|e-stat|edinet|jpx\.co\.jp|boj\.or\.jp|jil\.go|meti|mhlw|stat\.go|tdb\.co\.jp|tsr-net|\.or\.jp|\.ac\.jp|ir\.|/ir/|disclosure)")
NUM = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(兆円|億円|百万円|千万円|万円|千円|円|万人|千人|人|名|社|事業所|件|倍|％|%|台)")
YEAR = re.compile(r"(20[0-3]\d|19[89]\d)\s*年")
TO_MAN = {"兆円": 1e8, "億円": 1e4, "千万円": 1e3, "百万円": 100, "万円": 1, "千円": 0.1, "円": 1e-4}
TO_PERSON = {"万人": 1e4, "千人": 1e3, "人": 1, "名": 1}

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
                q, url = it.get("quote", ""), it.get("url", "")
                ys = YEAR.findall(q)
                for m in NUM.finditer(q):
                    v, u = norm(*m.groups())
                    if u == "万円" and v <= 0: continue
                    # 数字の近く（前40字）の年を優先
                    near = YEAR.findall(q[max(0, m.start() - 40):m.start() + 5])
                    y = (near or ys or [""])[-1]
                    yield {"品目": name, "区分": cat, "年": y, "値": round(v, 4), "単位": u,
                           "一次": "一次" if PRIMARY.search(url) else "記事",
                           "出典": urlparse(url).netloc, "URL": url, "引用": q[:160]}

def trend(series):
    ys = sorted(series)
    if len(ys) < 3: return "判定不可（年が3点未満）"
    first, last = series[ys[0]], series[ys[-1]]
    if first == 0: return "判定不可"
    ch = (last - first) / abs(first)
    word = "上昇" if ch > 0.05 else "下降" if ch < -0.05 else "横ばい"
    return f"{word}（{ys[0]}→{ys[-1]}で{ch*100:+.0f}%）"

def scan(topic):
    rows = list(rows_for(topic))
    if not rows: return 0
    with open(os.path.join(OUT, topic, "cross.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    md = [f"# 走査（自動）— {topic}", "", "機械で出した突き合わせと【推定】。式と出典件数つき。最終の結論は scan.md。", ""]
    by_item = {}
    for r in rows: by_item.setdefault(r["品目"], []).append(r)
    for item, rs in sorted(by_item.items()):
        md.append(f"## {item}")
        groups = {}
        for r in rs: groups.setdefault((r["区分"], r["単位"]), []).append(r)
        for (cat, u), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            if len(g) < 2 or u in ("件",): continue
            vals = [r["値"] for r in g]; prim = [r for r in g if r["一次"] == "一次"]
            lo, hi = min(vals), max(vals)
            spread = f"{hi/lo:.1f}倍" if lo > 0 else "-"
            peryear = {}
            for r in (prim or g):
                if r["年"]: peryear.setdefault(int(r["年"]), []).append(r["値"])
            med = {y: statistics.median(v) for y, v in peryear.items()}
            md.append(f"- 【事実】{cat}（{u}）：{len(g)}件・一次{len(prim)}件・出典{len({r['出典'] for r in g})}か所、幅 {lo:g}〜{hi:g}（食い違い{spread}）")
            if med: md.append(f"  - 【推定】年ごとの中央値（{'一次のみ' if prim else '記事含む'}）：" + "、".join(f"{y}年 {v:g}" for y, v in sorted(med.items())) + f" → 向き：{trend(med)}")
        # 1人あたり：同じ年に万円と人がある場合
        money = {}; people = {}
        for r in rs:
            if not r["年"]: continue
            if r["単位"] == "万円": money.setdefault(int(r["年"]), []).append(r["値"])
            if r["単位"] == "人": people.setdefault(int(r["年"]), []).append(r["値"])
        per = {y: statistics.median(money[y]) / statistics.median(people[y]) for y in money if y in people and statistics.median(people[y]) > 0}
        if per:
            md.append("  - 【推定】1人あたり金額＝その年の金額の中央値÷人数の中央値（同じ品目の別出典をまたぐので桁違いに注意）：" + "、".join(f"{y}年 {v:,.1f}万円" for y, v in sorted(per.items())) + f" → 向き：{trend(per)}")
        md.append("")
    open(os.path.join(OUT, topic, "scan_auto.md"), "w", encoding="utf-8").write("\n".join(md))
    return len(rows)

if __name__ == "__main__":
    topics = sys.argv[1:] or [os.path.basename(p.rstrip("/")) for p in glob.glob(os.path.join(OUT, "*/")) if not p.rstrip("/").endswith("brief")]
    for t in topics:
        n = scan(t)
        if n: print(t, n)
