"""ココナラのカテゴリ一覧 → 各サービスのページ（価格・ランク・評価・販売実績・納期・オプション・レビュー本文）を取って保存する。
本人の指示（2026-11-07）：「3Dプリンター用データ作成（671/330）は死ぬほど重要。傾向と単価と記録をコメントからも分析しろ」。
入力：researcher/inputs/coconala.csv（名前,一覧URL）。出力：researcher/out/coconala/<名前>/services/<id>.json, summary.csv, reviews.csv。
済んだサービスは飛ばす（続きから回る）。AI・有料APIは使わない。ヘッドレスChromiumで描画した本文をそのまま保存し、数字は正規表現で抜く（原文も残す）。
"""
import csv, json, os, re, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out" / "coconala"
PACE = 2.0
MAX_PAGES = int(os.environ.get("COCONALA_MAX_PAGES", "40"))
DEADLINE = time.time() + float(os.environ.get("COCONALA_SECONDS", "16800"))

def rx(p, t, g=1, f=None):
    m = re.search(p, t)
    if not m: return ""
    v = m.group(g)
    return f(v) if f else v

def num(v):
    return v.replace(",", "")

def page_text(pg, url, scroll=6):
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    try: pg.wait_for_load_state("networkidle", timeout=8000)
    except Exception: pass
    for _ in range(scroll):
        pg.mouse.wheel(0, 4000); time.sleep(0.4)
    return pg.inner_text("body"), pg.content()

def expand_reviews(pg, rounds=30):
    for _ in range(rounds):
        hit = False
        for label in ["もっと見る", "さらに表示", "評価をもっと見る", "すべての評価を見る", "続きを読む"]:
            try:
                loc = pg.get_by_text(label, exact=False)
                n = loc.count()
                for i in range(min(n, 5)):
                    try:
                        loc.nth(i).click(timeout=2000); hit = True; time.sleep(0.6)
                    except Exception: pass
            except Exception: pass
        if not hit: break

def parse(text, html):
    ld = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try: ld.append(json.loads(m.group(1)))
        except Exception: pass
    return {
        "タイトル": text.strip().split("\n")[0][:200],
        "価格_円": rx(r"([\d,]{3,})\s*円", text, f=num),
        "ランク": rx(r"(プラチナ|ゴールド|シルバー|ブロンズ|レギュラー)", text),
        "評価": rx(r"(\d\.\d)\s*[（(]\s*([\d,]+)\s*[）)]", text),
        "評価件数": rx(r"(\d\.\d)\s*[（(]\s*([\d,]+)\s*[）)]", text, 2, num),
        "販売実績": rx(r"販売実績\s*[:：]?\s*([\d,]+)", text, f=num),
        "総販売実績": rx(r"総販売実績\s*[:：]?\s*([\d,]+)", text, f=num),
        "お願い中_人": rx(r"お願い中\s*([\d,]+)\s*人", text, f=num),
        "残り枠": rx(r"残り\s*([\d,]+)\s*枠", text, f=num),
        "納期_日": rx(r"約\s*([\d,]+)\s*日", text, f=num),
        "星5": rx(r"5\s*[★☆]?\s*\(?\s*([\d,]+)\s*件", text, f=num),
        "ld_json": ld,
    }

SAVED = 0

def main():
    global SAVED
    rows = list(csv.DictReader(open(ROOT / "inputs" / "coconala.csv", encoding="utf-8")))
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(locale="ja-JP", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")
        for r in rows:
            name, base = r["名前"], r["一覧URL"]
            d = OUT / name; (d / "services").mkdir(parents=True, exist_ok=True); (d / "lists").mkdir(exist_ok=True)
            ids, order = [], {}
            for p in range(1, MAX_PAGES + 1):
                url = base + ("&" if "?" in base else "?") + f"page={p}"
                try: t, h = page_text(pg, url)
                except Exception as e:
                    print("list fail", url, e); break
                (d / "lists" / f"page{p}.txt").write_text(t, encoding="utf-8")
                found = list(dict.fromkeys(re.findall(r'/services/(\d+)', h)))
                new = [i for i in found if i not in order]
                print("list", name, "page", p, "links", len(found), "new", len(new), "textlen", len(t), "head:", t[:150].replace("\n"," "), flush=True)
                if os.environ.get("COCONALA_PROBE"): return
                if len(new) < 3: break
                for i in new: order[i] = len(order) + 1
                ids += new; time.sleep(PACE)
            json.dump(order, open(d / "order.json", "w"), ensure_ascii=False)
            print(name, "services:", len(ids))
            for sid in ids:
                f = d / "services" / f"{sid}.json"
                if f.exists() or time.time() > DEADLINE: continue
                url = f"https://coconala.com/services/{sid}"
                try:
                    t, h = page_text(pg, url); expand_reviews(pg)
                    t2 = pg.inner_text("body"); h = pg.content()
                    t = t2 if len(t2) > len(t) else t
                except Exception as e:
                    print("svc fail", sid, e); continue
                rec = {"id": sid, "url": url, "一覧の順位": order.get(sid), **parse(t, h), "本文": t[:120000]}
                rv = []
                for suf in ["/ratings", "/reviews"]:
                    try:
                        tr, hr = page_text(pg, url + suf, scroll=3); expand_reviews(pg)
                        tr = pg.inner_text("body")
                        if len(tr) > 800 and "評価" in tr: rv.append({"url": url + suf, "本文": tr[:200000]})
                    except Exception: pass
                    time.sleep(PACE)
                rec["レビューのページ"] = rv
                json.dump(rec, open(f, "w"), ensure_ascii=False, indent=1); SAVED += 1
                time.sleep(PACE)
            keys = ["id", "url", "一覧の順位", "タイトル", "価格_円", "ランク", "評価", "評価件数", "販売実績", "総販売実績", "お願い中_人", "残り枠", "納期_日"]
            with open(d / "summary.csv", "w", encoding="utf-8", newline="") as w:
                cw = csv.DictWriter(w, fieldnames=keys, extrasaction="ignore"); cw.writeheader()
                for f in sorted((d / "services").glob("*.json"), key=lambda x: order.get(x.stem, 9999)):
                    cw.writerow(json.load(open(f)))
        b.close()
    (OUT / "saved_last_run.txt").write_text(str(SAVED))

if __name__ == "__main__":
    main()
