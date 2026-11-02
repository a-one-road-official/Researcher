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
FIELDS = ["code", "name", "market", "industry", "period", "sales_mil", "op_mil", "margin", "status"]


def listed() -> list[dict]:
    import pandas as pd

    url = JPX_XLS
    try:  # the file name can change; take the .xls linked from the page if so
        html = requests.get(JPX_PAGE, headers={"User-Agent": UA}, timeout=30).text
        m = re.search(r'href="([^"]+data_j\.xls)"', html)
        if m:
            url = requests.compat.urljoin(JPX_PAGE, m.group(1))
    except Exception:
        pass
    raw = requests.get(url, headers={"User-Agent": UA}, timeout=60).content
    df = pd.read_excel(io.BytesIO(raw), dtype=str)
    out = []
    for _, r in df.iterrows():
        market = str(r.get("市場・商品区分", ""))
        if "内国株式" not in market:  # drop ETF, REIT, foreign stocks, PRO Market
            continue
        out.append({"code": str(r["コード"]).strip(), "name": str(r["銘柄名"]).strip(), "market": market,
                    "industry": str(r.get("33業種区分", "")).strip()})
    return out


def _num(s) -> float | None:
    s = re.sub(r"[,\s＊*]", "", str(s))
    s = s.replace("−", "-").replace("－", "-").replace("▲", "-").replace("△", "-")
    try:
        return float(s)
    except ValueError:
        return None


def latest_full_year(code: str) -> tuple[str, float | None, float | None, str]:
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
            if c["code"] in done and done[c["code"]]["status"] == "ok":
                continue
            if time.monotonic() - t0 > deadline_s:
                break
            period, s, o, status = latest_full_year(c["code"])
            margin = round(o / s, 4) if (s and o is not None) else ""
            done[c["code"]] = {**c, "period": period, "sales_mil": s or "", "op_mil": "" if o is None else o,
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
    tried = {c["code"] for c in companies} <= set(done)
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
