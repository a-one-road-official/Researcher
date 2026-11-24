"""metal-scrap-powder-waste（金属端材・粉・廃棄物回収）の読みやすい出力をつくる。2026-11-24 本人指示。

読むもの：researcher/out/metal-scrap-powder-waste/companies/*.json（第1段・問いごと）
          researcher/out/metal-scrap-powder-waste-cos/companies/*.json（第2段・会社ごと）
書くもの：themes/metal-scrap-powder-waste/
  draft_sheet.csv … 会社一覧の粗い下書き（事実の列だけ。地図の位置・判定は本人が埋める空欄）
  cross.csv       … 会社×項目ごとの交差（別系統の出どころ2つ以上）と各線の出典。1本だけは「未交差」
  questions.csv   … 問い×項目ごとの交差（第1段）
  facts.md        … 引用そのまま（出典URL・系統・円換算つき）
  fx.json         … 円換算に使ったレート（ECBの参照レート・無料・キー不要）。取れなければ換算しない
  drive_append.md … Googleドライブの記録ドキュメントの末尾に足す日付つきの文

決まり（METHOD.md 末尾「metal-scrap-powder-waste の決まり」）：
- タグ・判定は付けない。キーワードは行を拾う目印にすぎない。列は事実（引用）だけ。
- 交差は「別系統」の出どころが2つ以上。同じ系統（例：PR配信の転載どうし）は何本あっても1本。
  検索結果の抜粋・SNSの抜粋は数えない（本文を読んでいないので）。
- 創業年だけは値（西暦）ごとに交差を数える。ほかの項目は「項目交差（値の一致は本人が照合）」と書く。
- 外国通貨の金額は円換算を併記する（レートと基準日を必ず添える）。
- 計算はこのプログラムだけがする。AI・有料APIは使わない。
"""
from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from researcher.targets import ROOT, load_targets, load_topic, slug

THEME = "metal-scrap-powder-waste"
COS = THEME + "-cos"
OUT = ROOT.parent / "themes" / THEME
JST = timezone(timedelta(hours=9))

# ---- 出どころの系統 -------------------------------------------------------------
SYS_RULES = [
    ("官公庁・自治体", re.compile(r"(\.go\.jp|\.lg\.jp|^pref\.|\.pref\.|^www\.city\.|\.city\.|\.gov(\.|$)|europa\.eu|\.gov\.cn|\.go\.kr|e-stat)")),
    ("PR配信", re.compile(r"(prtimes|businesswire|prnewswire|globenewswire|newswire|atpress|value-press|dreamnews|presseportal|pressebox|openpr|einpresswire|accesswire|cision)")),
    ("企業データベース", re.compile(r"(crunchbase|pitchbook|dealroom|tracxn|zoominfo|baseconnect|salesnow|houjin|gbiz|northdata|opencorporates|kaisharesearch|bizdb)")),
    ("展示会", re.compile(r"(formnext|ifat|bir\.org|rematters|isri|tct|rapid3d|messe|exhibitor|expo)")),
    ("SNS", re.compile(r"(note\.com|x\.com|twitter\.com|linkedin\.com|facebook\.com|youtube\.com|instagram\.com)")),
    ("業界紙・報道", re.compile(r"(nikkan|nikkei|japanmetal|tekko|sanpo|metal-am|3dnatives|voxelmatters|3dprintingindustry|3dprint\.com|tctmagazine|3dadept|recyclinginternational|euwid|fastmarkets|argusmedia|reuters|bloomberg|techcrunch|monoist|fabcross|kitco|letsrecycle|recyclingtoday|wardsauto|automotiveworld|smm\.cn|mysteel|36kr|yonhap|etnews|handelsblatt|usinenouvelle|ilsole24ore|fd\.nl|news)")),
    ("業界団体", re.compile(r"(\.or\.jp|association|kyokai|kyoukai|\.asso\.|verband)")),
]
EXCERPT = ("抜粋",)


def host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
    except ValueError:
        return ""


def base_domain(h: str) -> str:
    p = h.split(".")
    if len(p) >= 3 and p[-2] in ("co", "or", "ne", "ac", "go", "com", "org", "net") and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def system_of(url: str, official: str = "") -> str:
    h = host(url)
    if official and h and base_domain(h) == base_domain(host(official)):
        return "公式"
    for name, rx in SYS_RULES:
        if rx.search(h):
            return name
    return "その他のウェブ"


# ---- 円換算 -----------------------------------------------------------------------
CUR = {"USD": "USD", "US$": "USD", "$": "USD", "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP", "CNY": "CNY",
       "RMB": "CNY", "元": "CNY", "KRW": "KRW", "₩": "KRW", "원": "KRW", "CHF": "CHF", "PLN": "PLN", "zł": "PLN",
       "SEK": "SEK", "AUD": "AUD", "A$": "AUD", "CAD": "CAD", "C$": "CAD"}
MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "mio": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
        "万": 1e4, "亿": 1e8, "億": 1e8, "억": 1e8, "만": 1e4, "milliarden": 1e9, "millionen": 1e6, "milliards": 1e9, "millions": 1e6}
PRE = re.compile(r"(US\$|A\$|C\$|USD|EUR|GBP|CNY|RMB|KRW|CHF|PLN|SEK|AUD|CAD|\$|€|£|₩)\s?(\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(thousand|million|billion|millionen|milliarden|millions|milliards|mio|mn|bn|[kmb]\b|万|亿|億|억|만)?", re.I)
POST = re.compile(r"(\d{1,3}(?:[,.]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(thousand|million|billion|millionen|milliarden|millions|milliards|mio|mn|bn|万|亿|億|억|만)?\.?\s*(USD|EUR|GBP|CNY|RMB|KRW|CHF|PLN|SEK|AUD|CAD|euros?|dollars?|元|원|zł|€)", re.I)


def load_fx() -> dict:
    """ECB の参照レート（1ユーロあたり）から 1通貨あたりの円を出す。取れなければ空（換算しない）。"""
    try:
        req = urllib.request.Request("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", headers={"User-Agent": "researcher"})
        xml = urllib.request.urlopen(req, timeout=20).read().decode()
        day = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})", xml).group(1)
        per_eur = {m.group(1): float(m.group(2)) for m in re.finditer(r"currency=['\"](\w{3})['\"]\s+rate=['\"]([\d.]+)", xml)}
        jpy = per_eur["JPY"]
        rates = {"EUR": jpy} | {c: jpy / r for c, r in per_eur.items() if c != "JPY"}
        return {"source": "ECB euro foreign exchange reference rates", "date": day, "jpy_per_unit": rates}
    except Exception as e:
        return {"source": "取得失敗", "error": f"{type(e).__name__}: {e}"[:200], "jpy_per_unit": {}}


def _num(s: str) -> float | None:
    """1,234,567 / 1.234.567 は桁区切り、3,2 / 4.50 は小数（最後の区切りの後が3桁なら桁区切り）。"""
    s = s.replace(" ", "")
    seps = [i for i, ch in enumerate(s) if ch in ",."]
    if seps:
        last = seps[-1]
        if len(s) - last - 1 == 3:
            s = re.sub(r"[.,]", "", s)
        else:
            s = re.sub(r"[.,]", "", s[:last]) + "." + s[last + 1:]
    try:
        return float(s)
    except ValueError:
        return None


def yen_notes(text: str, fx: dict) -> str:
    rates = fx.get("jpy_per_unit") or {}
    out = []
    for m in PRE.finditer(text):
        out.append((m.group(0), CUR.get(m.group(1).upper(), CUR.get(m.group(1))), m.group(2), m.group(3)))
    for m in POST.finditer(text):
        c = m.group(3)
        cur = "EUR" if c.lower().startswith("euro") or c == "€" else "USD" if c.lower().startswith("dollar") else CUR.get(c.upper(), CUR.get(c))
        out.append((m.group(0), cur, m.group(1), m.group(2)))
    notes = []
    for raw, cur, n, mul in out:
        v = _num(n)
        if v is None or not cur:
            continue
        if not rates:
            notes.append(f"{raw.strip()}→円換算できず（レート未取得）")
            continue
        if cur not in rates:
            continue
        v *= MULT.get((mul or "").lower(), MULT.get(mul or "", 1))
        y = v * rates[cur]
        ys = f"約{y/1e8:,.1f}億円" if y >= 1e8 else f"約{y/1e4:,.1f}万円" if y >= 1e4 else f"約{y:,.0f}円"
        tag = "（$はUSDとみなした）" if raw.strip().startswith("$") else ""
        notes.append(f"{raw.strip()}→{ys}（1{cur}={rates[cur]:.2f}円・ECB {fx['date']}）{tag}")
    return "／".join(dict.fromkeys(notes))


# ---- 読み込み ---------------------------------------------------------------------
def recs(topic: str) -> dict[str, dict]:
    d = ROOT / "out" / topic / "companies"
    out = {}
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out[r.get("name", p.stem)] = r
    return out


def lines_of(rec: dict, field: str) -> list[dict]:
    """その項目の引用（profile の値＋evidence の行）。抜粋は除く。"""
    items = []
    for it in (rec.get("profile", {}).get(field) or []):
        items.append({"quote": f"{it.get('label', field)}：{it.get('value', '')}", "url": it.get("url", ""), "pass": it.get("pass")})
    for it in (rec.get("evidence", {}).get(field) or []):
        items.append(it)
    return items


YEAR = re.compile(r"((?:18|19|20)\d{2})")


def crossing(items: list[dict], official: str) -> dict[str, list[str]]:
    """系統 → URL（重複なし）"""
    by: dict[str, list[str]] = {}
    for it in items:
        u = it.get("url", "")
        if not u:
            continue
        s = system_of(u, official)
        if u not in by.setdefault(s, []):
            by[s].append(u)
    return by


def fmt_lines(by: dict[str, list[str]]) -> str:
    return " | ".join(f"{s}:{' '.join(us[:3])}" for s, us in by.items())


# 会社シートの列 ← 第2段の項目
SHEET = [
    ("創業年", ["創業年"]),
    ("株主構成", ["株主構成"]),
    ("売上・従業員数", ["売上", "従業員数", "売上・従業員数"]),
    ("何でどう稼いでいるか（誰が誰にいくら払うか）", ["稼ぎ方（誰が誰にいくら払うか）", "事業内容"]),
    ("端材・粉との関わり", ["端材・粉との関わり"]),
]


def main(argv=None) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fx = load_fx()
    (OUT / "fx.json").write_text(json.dumps(fx, ensure_ascii=False, indent=1), encoding="utf-8")
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")

    # ---- 第2段：会社 ----
    cos_topic = load_topic(COS)
    targets = load_targets(cos_topic)
    cr = recs(COS)
    sheet_rows, cross_rows, md = [], [], [f"# 金属端材・粉・廃棄物回収 — 引用そのまま（{now}）", "",
                                          "列は事実（引用）だけ。判定・タグは付けていない。系統＝出どころの種類。抜粋は交差に数えない。", ""]
    md += ["## 会社ごと（第2段）", ""]
    for t in targets:
        r = cr.get(t["name"])
        row = {"社名": t["name"], "問い・文脈": t["context"], "公式URL": (r or {}).get("official_url") or t.get("url", "")}
        if not r or "error" in r:
            for col, _ in SHEET:
                row[col] = "未取得（手動実行待ち）" if not r else f"エラー：{r.get('error', '')[:80]}"
            row.update({"出典URL": "", "交差した項目": "", "未交差の項目": "", "円換算": "", "記載なし（3回確認）": "", "調べた日": ""})
        else:
            official = r.get("official_url", "")
            md += [f"### {t['name']}", f"- 公式URL：{official or '特定できず'}（{r.get('identified_by', '')}）", f"- 調べた日：{r.get('researched_at', '')[:10]}"]
            srcs, crossed, single, yen = [], [], [], []
            for col, fields in SHEET:
                items = [i for f in fields for i in lines_of(r, f)]
                if not items:
                    miss = next((r.get("missing", {}).get(f) for f in fields if r.get("missing", {}).get(f)), "")
                    row[col] = miss or "記載なし"
                    continue
                row[col] = items[0]["quote"][:160]
                srcs += [i["url"] for i in items[:2]]
                md.append(f"- **{col}**")
                for i in items[:8]:
                    yn = yen_notes(i["quote"], fx)
                    if yn:
                        yen.append(yn)
                    md.append(f"  - 「{i['quote'][:300]}」 — {system_of(i['url'], official)} {i['url']}" + (f" 〔円換算：{yn}〕" if yn else ""))
                if col == "創業年":
                    byyear: dict[str, list[dict]] = {}
                    for i in items:
                        for y in set(YEAR.findall(i["quote"])):
                            byyear.setdefault(y, []).append(i)
                    groups = [(f"創業年={y}", v) for y, v in byyear.items()] or [("創業年", items)]
                else:
                    groups = [(col + "（項目交差・値は本人が照合）", items)]
                for label, its in groups:
                    by = crossing(its, official)
                    n = len(by)
                    st = "交差" if n >= 2 else "未交差"
                    (crossed if n >= 2 else single).append(f"{label}×{n}")
                    cross_rows.append([t["name"], label, n, st, fmt_lines(by)])
            row["出典URL"] = " ".join(dict.fromkeys(srcs))
            row["交差した項目"] = "／".join(crossed)
            row["未交差の項目"] = "／".join(single)
            row["円換算"] = "／".join(dict.fromkeys(yen))[:400]
            row["記載なし（3回確認）"] = "／".join(r.get("missing", {}))
            row["調べた日"] = r.get("researched_at", "")[:10]
            md.append("")
        row.update({"業界の地図の位置（本人記入）": "", "判定（本人記入）": "", "メモ（本人記入）": ""})
        sheet_rows.append(row)
    cols = ["社名", "問い・文脈", "公式URL"] + [c for c, _ in SHEET] + ["出典URL", "交差した項目", "未交差の項目", "円換算",
            "記載なし（3回確認）", "調べた日", "業界の地図の位置（本人記入）", "判定（本人記入）", "メモ（本人記入）"]
    with open(OUT / "draft_sheet.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sheet_rows)
    with open(OUT / "cross.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["社名", "項目", "交差数（別系統の数）", "状態", "各線の出典（系統:URL）"])
        w.writerows(cross_rows)

    # ---- 第1段：問い ----
    q_topic = load_topic(THEME)
    qr = recs(THEME)
    qrows = []
    md += ["## 問いごと（第1段）", ""]
    for t in load_targets(q_topic):
        r = qr.get(t["name"])
        md.append(f"### {t['name']}（{t['context']}）")
        if not r or "error" in r:
            md += ["- 未取得（手動実行待ち）" if not r else f"- エラー：{r.get('error', '')}", ""]
            continue
        for cat in q_topic["evidence"]:
            items = r.get("evidence", {}).get(cat) or []
            if not items:
                qrows.append([t["name"], cat, 0, "記載なし", r.get("missing", {}).get(cat, "")])
                continue
            by = crossing(items, "")
            qrows.append([t["name"], cat, len(by), "交差" if len(by) >= 2 else "未交差", fmt_lines(by)])
            md.append(f"- **{cat}**（系統{len(by)}）")
            for i in items[:10]:
                yn = yen_notes(i["quote"], fx)
                md.append(f"  - 「{i['quote'][:300]}」 — {system_of(i['url'])} {i['url']}" + (f" 〔円換算：{yn}〕" if yn else ""))
        for cat, items in r.get("evidence", {}).items():
            if any(x in cat for x in EXCERPT):
                md.append(f"- {cat}（交差に数えない）：{len(items)}件")
        md.append("")
    with open(OUT / "questions.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["問い・品目", "項目", "交差数（別系統の数）", "状態", "各線の出典（系統:URL）／記載なし"])
        w.writerows(qrows)
    (OUT / "facts.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ---- ドライブに足す文 ----
    done_c = sum(1 for r in cr.values() if "error" not in r)
    done_q = sum(1 for r in qr.values() if "error" not in r)
    n_cross = sum(1 for x in cross_rows if x[3] == "交差")
    n_single = sum(1 for x in cross_rows if x[3] == "未交差")
    da = [f"■ {now}　metal-scrap-powder-waste（金属端材・粉・廃棄物回収プラットフォーム）",
          f"- 第1段（問い）：{done_q}/{len(load_targets(q_topic))} 品目　第2段（会社）：{done_c}/{len(targets)} 社",
          f"- 会社×項目：交差 {n_cross} ／ 未交差 {n_single}（交差＝別系統の出どころ2つ以上）",
          f"- 円換算レート：{fx.get('source')} {fx.get('date', '')}",
          "- 置き場所：themes/metal-scrap-powder-waste/（draft_sheet.csv・cross.csv・questions.csv・facts.md）",
          "- 事実の列だけ。判定・地図の位置は本人記入の空欄。", ""]
    (OUT / "drive_append.md").write_text("\n".join(da), encoding="utf-8")
    print("\n".join(da))
    return 0


if __name__ == "__main__":
    sys.exit(main())
