"""mfg_capacity の元の統計：経産省 工業統計表「産業別統計表」（確報）を年ごとに直接読み、
産業細分類（4けたの番号）の行から 事業所数・従業者数・製造品出荷額等・加工賃収入額・付加価値額 を
欄の見出しつきで抜き出す。

2026-11-06 本人の指示「修正かませ」：走査19回目まで、工業統計の細分類・加工賃収入額・金型とめっきの元の表が
3回とも空だった（検索で表を探し、本文の言葉で拾う作りでは、Excel の行と欄が品目に入らない）。
検索をやめ、所管の Excel を年ごとに取り、番号の行を表のまま読む。AIは使わない。

出力（out/mfg_capacity/）
- kougyo_rows.csv：年・シート・番号・行の名前・欄の見出し・値（表のまま。単位は欄の見出しに入る）
- kougyo_series.csv：産業細分類別（シート1100番台）を番号×年で横に並べた表と、
  同じ表・同じ年の組だけで割った 1事業所あたり従業者数・従業者1人あたり加工賃収入額・1人あたり製造品出荷額等
- kougyo_log.md：取れた年・取れなかった年と理由（URL・status・シート数）
年の並びが切れる所（2016年は経済センサスで工業統計なし、2021年からは経済構造実態調査）はそのまま空けておき、
別の統計で埋めない（定義が違う・4-26）。
"""
from __future__ import annotations

import csv
import io
import re
import time

import requests

from researcher.targets import ROOT

OUT = ROOT / "out" / "mfg_capacity"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
BASE = "https://www.meti.go.jp/statistics/tyo/kougyo/result-2/{slug}/kakuho/sangyo/"
# 調査の年（ファイル名の年）→ 所管ページの年の名前。2019-k3-data.xls が r01 にあるのは確認済み（2026-11-06）
YEARS = {2020: "r02", 2019: "r01", 2018: "h30", 2017: "h29", 2015: "h27", 2014: "h26",
         2013: "h25", 2012: "h24", 2011: "h23", 2010: "h22"}
NUM = re.compile(r"^[\d,.\-△▲]+$")
CODE = re.compile(r"^\s*(\d{2,4})(?:\s+|　|$)(.*)$")
FIELDS = {  # 欄の見出しに含まれる言葉 → 出力の欄の名前。内訳の言葉（加工賃収入額）を先に見る（見出しが「製造品出荷額等 加工賃収入額」とつながるため）
    "加工賃収入額": ["加工賃収入額", "加工賃収入"],
    "付加価値額": ["付加価値額"],
    "現金給与総額": ["現金給与総額"],
    "事業所数": ["事業所数"],
    "従業者数": ["従業者数", "従業者 計", "従業者計"],
    "製造品出荷額等": ["製造品出荷額等"],
}


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


def _get(url: str, **kw) -> requests.Response | None:
    for i in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=90, **kw)
            return r
        except Exception as e:  # noqa: BLE001
            print(f"kougyo get {url} try{i + 1} {e}", flush=True)
            time.sleep(5 * (i + 1))
    return None


def _links(year: int, slug: str, log: list[str]) -> list[str]:
    """その年の産業別統計表の Excel。決まった名前を先に試し、無ければ所管ページの中のリンクから探す"""
    page = BASE.format(slug=slug)
    guess = [page + f"xls/{year}-k3-data.xls", page + f"xls/{year}-k3-data.xlsx"]
    out = []
    for u in guess:
        r = _get(u, stream=True)
        if r is not None and r.status_code == 200 and r.content[:4] in (b"\xd0\xcf\x11\xe0", b"PK\x03\x04"):
            out.append(u)
            break
    if out:
        return out
    r = _get(page + "index.html")
    if r is None or r.status_code != 200:
        log.append(f"- {year}（{slug}）：所管ページ {page}index.html が取れない（status={getattr(r, 'status_code', '接続失敗')}）")
        return []
    r.encoding = r.apparent_encoding
    hrefs = re.findall(r"""href=["']([^"']+\.xlsx?)["']""", r.text)
    hrefs = [requests.compat.urljoin(page + "index.html", h) for h in hrefs]
    # 概況文の数値表（gaikyo）より、産業別統計表（data）を先に
    hrefs.sort(key=lambda h: (0 if "data" in h and "gaikyo" not in h else 1, h))
    if not hrefs:
        log.append(f"- {year}（{slug}）：所管ページに Excel のリンクが無い")
    return hrefs[:4]


def _header(rows: list[list[str]], first: int) -> list[str]:
    """最初の数字の行より上（最大6行）を欄ごとにつなぐ。結合セルは左の文字で右を埋める"""
    width = max(len(r) for r in rows[: first + 1])
    stack = []
    for r in rows[max(0, first - 6):first]:
        if sum(1 for x in r if x) <= 1:
            continue  # 表題だけの行
        filled, last = [], ""
        for i in range(width):
            x = r[i] if i < len(r) else ""
            if x:
                last = x
            filled.append(x or (last if i > 0 and not NUM.match(last or "0") else ""))
        stack.append(filled)
    return [" ".join(dict.fromkeys(h[i] for h in stack if i < len(h) and h[i])) for i in range(width)]


def parse_sheet(year: int, sheet: str, rows: list[list[str]]) -> list[dict]:
    """1シートから、番号のある行の値を欄の見出しつきで返す。従業者規模の行は直前の番号に付ける"""
    first = next((i for i, r in enumerate(rows) if sum(1 for x in r if x and NUM.match(x)) >= 3), None)
    if first is None:
        return []
    head = _header(rows, first)
    out, code, cname = [], "", ""
    for r in rows[first:]:
        cells = [x for x in r if x]
        if not cells:
            continue
        texts = [(i, x) for i, x in enumerate(r) if x and not NUM.match(x)]
        size, skip = "", -1
        # 左端の欄が番号だけの数字（"2464" や "2464.0"）で、名前が隣のセルにある表
        lead = next((i for i, x in enumerate(r) if x), -1)
        if lead >= 0 and lead <= 1 and re.fullmatch(r"\d{4}(\.0)?", r[lead]):
            code, cname, skip = r[lead][:4], (texts[0][1] if texts else ""), lead
            size = " ".join(x for _, x in texts[1:])
        elif texts:
            m = CODE.match(texts[0][1])
            if m and len(m.group(1)) >= 2:
                code, cname = m.group(1), (m.group(2) or "").strip()
                # 番号と名前が別のセルに分かれている表
                if not cname and len(texts) > 1:
                    cname = texts[1][1]
                rest = [x for _, x in texts[1:] if x != cname]
                size = " ".join(rest)
            else:
                size = " ".join(x for _, x in texts)  # 「4～9人」など規模の行、または合計の行
        if not code:
            continue
        for i, x in enumerate(r):
            if not x or not NUM.match(x) or i >= len(head) or not head[i]:
                continue
            if i == skip:
                continue
            out.append({"year": year, "sheet": sheet, "code": code, "name": cname, "row": size,
                        "column": head[i][:80], "value": x})
    return out


def _read(url: str, year: int, log: list[str]) -> list[dict]:
    import pandas as pd

    r = _get(url)
    if r is None or r.status_code != 200:
        log.append(f"- {year}：{url} が取れない（status={getattr(r, 'status_code', '接続失敗')}）")
        return []
    raw = r.content
    engine = "xlrd" if raw[:4] == b"\xd0\xcf\x11\xe0" else "openpyxl"
    try:
        sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, header=None, dtype=str, engine=engine)
    except Exception as e:  # noqa: BLE001
        log.append(f"- {year}：{url} を表として読めない（{e}）")
        return []
    got = []
    for name, df in sheets.items():
        rows = [[("" if str(x) in ("nan", "None") else str(x).strip()) for x in row] for row in df.fillna("").values.tolist()]
        got += parse_sheet(year, str(name), rows)
    log.append(f"- {year}：{url}（{len(raw):,} バイト・シート {len(sheets)}・番号つきの値 {len(got):,}件）")
    return got


def _pick(col: str) -> str:
    for f, keys in FIELDS.items():
        if any(k in col for k in keys):
            return f
    return ""


def _num(x: str) -> float | None:
    x = x.replace(",", "")
    if x in ("-", "", "△", "▲") or not re.fullmatch(r"[△▲-]?\d+(\.\d+)?", x):
        return None
    neg = x[0] in "△▲-"
    v = float(x.lstrip("△▲-"))
    return -v if neg else v


def series(rows: list[dict]) -> list[dict]:
    """産業細分類別（シート名が 1 で始まる表・規模の行でない行）を番号×年に並べ、同じ表・同じ年の組だけで割る"""
    acc: dict[tuple, dict] = {}
    for r in rows:
        if not str(r["sheet"]).startswith("1") or r["row"] or len(r["code"]) != 4:
            continue
        f = _pick(r["column"])
        if not f:
            continue
        k = (r["code"], r["year"], r["sheet"])
        d = acc.setdefault(k, {"code": r["code"], "name": r["name"], "year": r["year"], "sheet": r["sheet"]})
        if f not in d:  # 最初に当たった欄（内訳でなく合計）
            d[f] = r["value"]
            d[f + "_欄"] = r["column"]
    out = []
    for d in acc.values():
        e, p, s, k = (_num(d.get(x, "")) for x in ("事業所数", "従業者数", "製造品出荷額等", "加工賃収入額"))
        d["1事業所あたり従業者数"] = round(p / e, 1) if e and p else ""
        d["従業者1人あたり加工賃収入額"] = round(k / p, 1) if p and k is not None else ""
        d["従業者1人あたり製造品出荷額等"] = round(s / p, 1) if p and s else ""
        out.append(d)
    out.sort(key=lambda d: (d["code"], d["year"]))
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    log, rows = ["# 工業統計表 産業別統計表（確報）の直接読み", ""], []
    for year, slug in YEARS.items():
        for url in _links(year, slug, log):
            got = _read(url, year, log)
            rows += got
            if got:
                break
    cols = ["year", "sheet", "code", "name", "row", "column", "value"]
    with open(OUT / "kougyo_rows.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    ser = series(rows)
    scol = ["code", "name", "year", "sheet"] + [x for f in FIELDS for x in (f, f + "_欄")] + \
           ["1事業所あたり従業者数", "従業者1人あたり加工賃収入額", "従業者1人あたり製造品出荷額等"]
    with open(OUT / "kougyo_series.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=scol, extrasaction="ignore")
        w.writeheader()
        w.writerows(ser)
    years = sorted({d["year"] for d in ser})
    log += ["", f"産業細分類×年の行：{len(ser):,}（年：{', '.join(map(str, years)) or 'なし'}）",
            "割り算は同じシート・同じ年の組だけ。単位は各欄の見出し（_欄）のまま（工業統計の金額は万円の表が多い）。"]
    (OUT / "kougyo_log.md").write_text("\n".join(log) + "\n", encoding="utf-8")
    print("\n".join(log), flush=True)
    # 1年も取れなかったときは印を置かず、次の回でもう一度試す
    if years:
        (OUT / "BUILD_DONE").write_text(f"{len(rows)} rows, years {years}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
