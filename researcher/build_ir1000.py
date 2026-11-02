"""IR1000本ノック step 1: screen every listed Japanese company by operating margin.

Same method as PLEX's 黒崎さん: take all listed companies, keep those with an
operating margin of 10% or more, then read each one's IR (who / what / how / how
the money moves). This module only builds the list; researcher.run reads the IR.

Sources (free, no key): JPX listed-company file (codes, names, market, 33業種) and
each company's latest full-year 売上高・営業利益 from kabutan's finance page.
Resumable: every company screened is cached in out/ir1000/screen.csv, so a run that
hits its time limit continues next run. When every code has been tried, writes the
topic input CSV (highest margin first) and a done marker.
"""
from __future__ import annotations

import csv
import io
import re
import sys
import time
from pathlib import Path

import requests

from researcher.targets import ROOT

JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_XLS = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
FIN = "https://kabutan.jp/stock/finance?code={code}"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
OUT = ROOT / "out" / "ir1000"
CACHE = OUT / "screen.csv"
FIELDS = ["code", "name", "market", "industry", "period", "sales_mil", "op_mil", "margin", "status", "tries"]


EDINET_CODES = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"


def _from_jpx() -> list[dict]:
    import pandas as pd

    html = requests.get(JPX_PAGE, headers={"User-Agent": UA}, timeout=30)
    links = re.findall(r"""href=["']([^"']+\.xlsx?)["']""", html.text)
    print(f"jpx page status={html.status_code} xls_links={links[:5]}", flush=True)
    for link in links:
        url = requests.compat.urljoin(JPX_PAGE, link)
        r = requests.get(url, headers={"User-Agent": UA, "Referer": JPX_PAGE}, timeout=60)
        raw = r.content
        print(f"jpx {url} status={r.status_code} bytes={len(raw)}", flush=True)
        if raw[:4] == b"\xd0\xcf\x11\xe0":
            df = pd.read_excel(io.BytesIO(raw), dtype=str, engine="xlrd")
        elif raw[:2] == b"PK":
            df = pd.read_excel(io.BytesIO(raw), dtype=str, engine="openpyxl")
        else:
            continue
        if "コード" not in df.columns:
            continue
        out = []
        for _, r in df.iterrows():
            market = str(r.get("市場・商品区分", ""))
            if "内国株式" not in market:  # drop ETF, REIT, foreign stocks, PRO Market
                continue
            out.append({"code": str(r["コード"]).strip(), "name": str(r["銘柄名"]).strip(), "market": market,
                        "industry": str(r.get("33業種区分", "")).strip()})
        if out:
            return out
    return []


def _from_edinet() -> list[dict]:
    """Fallback: FSA's EDINET code list (free, no key). Listed domestic companies only."""
    import zipfile

    raw = requests.get(EDINET_CODES, headers={"User-Agent": UA}, timeout=60).content
    z = zipfile.ZipFile(io.BytesIO(raw))
    text = z.read(z.namelist()[0]).decode("cp932", "ignore")
    rows = list(csv.reader(io.StringIO(text)))
    head = rows[1]
    idx = {h: i for i, h in enumerate(head)}
    out = []
    for r in rows[2:]:
        g = lambda k: r[idx[k]].strip() if k in idx and idx[k] < len(r) else ""
        code = g("証券コード")
        if g("上場区分") != "上場" or not code or "内国" not in g("提出者種別"):
            continue
        out.append({"code": code[:4], "name": g("提出者名"), "market": "上場（EDINET）", "industry": g("提出者業種")})
    print(f"edinet listed={len(out)}", flush=True)
    return out


def listed() -> list[dict]:
    try:
        out = _from_jpx()
    except Exception as e:
        print(f"jpx failed: {type(e).__name__}: {e}", flush=True)
        out = []
    return out or _from_edinet()


def _num(s) -> float | None:
    s = re.sub(r"[,\s＊*]", "", str(s))
    s = s.replace("−", "-").replace("－", "-").replace("▲", "-").replace("△", "-")
    try:
        return float(s)
    except ValueError:
        return None


def _kabutan(code: str) -> tuple[str, float | None, float | None, str]:
    """(決算期, 売上高, 営業益) of the latest actual full year, in 百万円."""
    import pandas as pd

    from researcher.fetch import _allowed

    if not _allowed(FIN.format(code=code)):
        return "", None, None, "robots_disallow"
    try:
        r = requests.get(FIN.format(code=code), headers={"User-Agent": UA}, timeout=30)
    except Exception as e:
        return "", None, None, f"fetch_error:{type(e).__name__}"
    if r.status_code != 200:
        return "", None, None, f"http_{r.status_code}"
    try:
        tables = pd.read_html(io.StringIO(r.text))
    except Exception:
        return "", None, None, "no_table"
    for t in tables:
        cols = [str(c[-1] if isinstance(c, tuple) else c) for c in t.columns]
        if not any("売上" in c or "営業収益" in c or "経常収益" in c for c in cols) or not any("営業益" in c for c in cols):
            continue
        ki = next(i for i, c in enumerate(cols) if "決算期" in c) if any("決算期" in c for c in cols) else 0
        si = next(i for i, c in enumerate(cols) if "売上" in c or "営業収益" in c or "経常収益" in c)
        oi = next(i for i, c in enumerate(cols) if "営業益" in c)
        best = None
        for _, row in t.iterrows():
            period = str(row.iloc[ki])
            if not re.search(r"\d{4}\.\d{2}", period) or "予" in period or "前期比" in period:
                continue
            s, o = _num(row.iloc[si]), _num(row.iloc[oi])
            if s:
                best = (period.strip(), s, o)
        if best:  # the first matching table is the 通期 (full-year) one
            return best[0], best[1], best[2], "ok"
    return "", None, None, "no_full_year"


def _yahoo(code: str) -> tuple[str, float | None, float | None, str]:
    """Latest full-year revenue / operating income from Yahoo Finance (via yfinance), in 百万円."""
    try:
        import yfinance as yf

        t = yf.Ticker(f"{code}.T")
        inc = t.income_stmt
        if inc is None or inc.empty:
            return "", None, None, "yahoo_empty"
        col = inc.columns[0]
        rev = next((inc.loc[k, col] for k in ("Total Revenue", "Operating Revenue") if k in inc.index), None)
        op = next((inc.loc[k, col] for k in ("Operating Income", "Total Operating Income As Reported") if k in inc.index), None)
        if rev is None or rev != rev:
            return "", None, None, "yahoo_no_revenue"
        op = None if (op is None or op != op) else float(op) / 1e6
        return col.strftime("%Y.%m"), float(rev) / 1e6, op, "ok"
    except Exception as e:
        return "", None, None, f"yahoo_error:{type(e).__name__}"


def latest_full_year(code: str):
    r = _kabutan(code)
    if r[3] == "ok":
        return r
    y = _yahoo(code)
    return y if y[3] == "ok" else (r[0], r[1], r[2], f"{r[3]}|{y[3]}")


def main(deadline_s: float = 4 * 3600) -> int:
    t0 = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    done: dict[str, dict] = {}
    if CACHE.exists():
        with open(CACHE, encoding="utf-8") as f:
            done = {r["code"]: r for r in csv.DictReader(f)}
    companies = listed()
    print(f"listed={len(companies)} cached={len(done)}", flush=True)
    new = 0
    try:
        for c in companies:
            if c["code"] in done and (done[c["code"]]["status"] == "ok" or done[c["code"]].get("tries") == "2"):
                continue
            if time.monotonic() - t0 > deadline_s:
                break
            period, s, o, status = latest_full_year(c["code"])
            margin = round(o / s, 4) if (s and o is not None) else ""
            tries = str(int(done.get(c["code"], {}).get("tries") or 0) + 1)
            done[c["code"]] = {**c, "tries": tries, "period": period, "sales_mil": s or "", "op_mil": "" if o is None else o,
                               "margin": margin, "status": status}
            new += 1
            if new % 100 == 0:
                print(f"screened {new}", flush=True)
            time.sleep(1.5)
    finally:
        with open(CACHE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for r in done.values():
                w.writerow({k: r.get(k, "") for k in FIELDS})
    ok = sum(1 for r in done.values() if r.get("status") == "ok")
    tried = {c["code"] for c in companies} <= set(done) and ok >= len(companies) // 2
    print(f"new={new} tried_all={tried}", flush=True)
    if tried:
        write_input(done.values())
    return 0


def write_input(rows) -> None:
    keep = [r for r in rows if r["status"] == "ok" and r["margin"] != "" and float(r["margin"]) >= 0.10]
    keep.sort(key=lambda r: -float(r["margin"]))
    path = ROOT / "inputs" / "ir1000.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in keep:
            w.writerow([r["name"], "", "", f"証券コード{r['code']}／{r['industry']}／{r['market']}／{r['period']}期 "
                        f"売上高{float(r['sales_mil']):,.0f}百万円 営業利益率{float(r['margin'])*100:.1f}%"])
    (OUT / "BUILD_DONE").write_text(f"{len(keep)}\n", encoding="utf-8")
    print(f"input written: {len(keep)} companies with margin>=10%", flush=True)


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
