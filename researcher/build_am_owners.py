"""日本で金属AM（粉末床溶融結合＝PBF：L-PBF/SLM/SLS金属/DMLS/EB-PBF）の装置を持っている会社・研究機関の名簿を作る。

本人の指示（2026-11-10）：
- 目的：日本ですでに金属粉を買っている先（＝金属AM装置を持っている先）を洗い出す。まずは洗い出しだけ（予算の割り出しはしない）。
- 対象：3Dプリンティングの中でも金属AMを持っている会社・研究機関だけ。PBF（SLMなど）の装置を持っているところが望ましい。
- 装置の製造元は問わない（EOS・ニコンSLM・3D Systems・Renishaw・TRUMPF・GE/Concept Laser/Arcam・松浦機械・ソディック・
  DMG森精機・日本電子・Farsoon・BLT・Velo3D・Xact Metal など全部）。
- 市場はまだ小さい。数を水増ししない。

やり方（AIなし・検索と本文の機械的な抜き出しだけ）
  A. 「装置の型番・製造元 × 導入/保有/設置」「PBF・SLMの言い方 × 導入/保有/共用設備」の検索語で、無料の複数検索エンジンを回す。
  B. 出てきたページを1枚ずつ開き、PBF装置の言い方（型番・方式名）が出てくる文だけを抜く。
  C. その文と同じ文、なければページ題名から、組織名（株式会社・大学・高専・公設試・研究所）を抜く。
  D. 組織名で束ね、証拠（引用・URL・見つけた装置の言い方）を並べる。PBFの証拠がある組織は pbf=yes、
     金属3Dプリンタの記述だけ（方式不明・DED等）は pbf=unknown として残す（勝手に捨てない）。
  出力：out/am_owners/owners.csv（組織ごと）、out/am_owners/evidence.csv（引用ごと）、
       inputs/am_owners.csv（第2段：会社ごとに公式サイトで設備・資本金・創業年度・株主構成を三重確認）。
時間切れでも途中から再開する（済んだ検索語・URLは evidence.csv と done_queries.txt で飛ばす）。
"""
from __future__ import annotations

import csv
import re
import sys
import time

from researcher.fetch import fetch
from researcher.search import search
from researcher.targets import ROOT

OUT = ROOT / "out" / "am_owners"
EVID = OUT / "evidence.csv"
OWNERS = OUT / "owners.csv"
DONEQ = OUT / "done_queries.txt"
DONEU = OUT / "done_urls.txt"
INPUT = ROOT / "inputs" / "am_owners.csv"
EV_FIELDS = ["org", "org_kind", "pbf", "machine", "quote", "url", "query"]

# 装置の言い方（型番・シリーズ）。PBF方式のものだけ。製造元は問わない
PBF_MODELS = [
    "EOS M 290", "EOS M290", "EOS M 400", "EOS M400", "EOS M 300", "EOS M300", "EOS M 100", "EOS M100", "EOSINT M",
    "SLM 125", "SLM125", "SLM 280", "SLM280", "SLM 500", "SLM500", "SLM 800", "NXG XII", "ニコンSLM", "Nikon SLM",
    "DMP Flex", "DMP Factory", "ProX DMP", "3D Systems DMP",
    "RenAM", "AM400", "AM 400", "AM250", "AM 250", "レニショー",
    "TruPrint", "トゥループリント",
    "Concept Laser", "M2 cusing", "Mlab", "Xline", "M Line",
    "Arcam", "Spectra H", "Q20plus", "Q10plus", "A2X",
    "LUMEX", "ルーメックス",
    "OPM250", "OPM350", "OPM 250", "OPM 350",
    "LASERTEC 30 SLM", "LASERTEC 12 SLM", "LASERTEC SLM",
    "JAM-5200EBM", "JAM-5200", "電子ビーム金属3D", "電子ビーム積層",
    "Farsoon", "FS271M", "FS121M", "FS301M", "FS421M", "FS721M",
    "BLT-S", "Bright Laser",
    "Sapphire", "Velo3D",
    "Xact Metal", "XM200",
    "AddUp", "FormUp",
    "Additive Industries", "MetalFAB",
    "One Click Metal", "ORLAS", "Sisma", "MYSINT",
    "Prima Additive", "Print Sharp",
    "HBD", "Eplus3D", "EP-M",
    "Aconity", "Freemelt", "Wayland",
    "Raplas", "マツウラ", "松浦機械",
    "ソディック", "DMG森精機", "日本電子",
]
PBF_TERMS = [
    "粉末床溶融結合", "パウダーベッドフュージョン", "パウダーベッド", "PBF", "L-PBF", "LPBF", "PBF-LB", "PBF-EB", "EB-PBF",
    "SLM方式", "選択的レーザー溶融", "レーザー溶融", "SLS方式（金属）", "DMLS", "直接金属レーザー焼結",
    "電子ビーム溶融", "EBM", "粉末積層", "レーザ積層造形", "レーザー積層造形", "金属粉末積層",
]
OWN_VERBS = ["導入", "保有", "設置", "所有", "稼働", "共用設備", "利用可能", "貸出", "開放機器", "設備紹介", "保有設備"]
GENERIC = ["金属3Dプリンタ", "金属3Dプリンター", "金属積層造形", "金属AM", "金属アディティブ"]

# 公設試・大学の共用設備は予算で買っているので必ず当てる
INST_QUERIES = [
    "金属3Dプリンタ 共用設備 大学", "金属3Dプリンター 開放機器 工業技術センター", "金属積層造形装置 産業技術センター 設備",
    "金属3Dプリンタ 機器利用 公設試験研究機関", "金属3Dプリンター 依頼試験 技術センター", "粉末床溶融結合 装置 大学 研究室",
    "金属3Dプリンタ 設備 高等専門学校", "金属積層造形 共同利用 研究所", "金属3Dプリンター 導入 産業技術総合研究所",
    "電子ビーム金属3Dプリンタ 大学", "金属AM 拠点 大学 装置", "技術研究組合 次世代3D積層造形 装置",
]
COMPANY_QUERIES = [
    "金属3Dプリンター 導入 株式会社 PBF", "金属3Dプリンタ 導入しました", "金属3Dプリンター 新規導入 お知らせ",
    "金属3Dプリンタ 保有設備 一覧", "金属3Dプリンター 受託造形 保有", "金属積層造形 受託 設備一覧",
    "金属3Dプリンター 導入事例 製作所", "ものづくり補助金 金属3Dプリンター 導入", "金属3Dプリンタ 設備投資 プレスリリース",
    "金属3Dプリンター 量産 導入 工場", "SLM 金属3Dプリンター 導入 町工場", "金属粉末 積層造形 導入 中小企業",
]

ORG_PATTERNS = [
    ("company", re.compile(r"(?:株式会社|有限会社|合同会社)\s?[一-龥ァ-ヶーA-Za-z0-9＆&・\.]{1,20}")),
    ("company", re.compile(r"[一-龥ァ-ヶーA-Za-z0-9＆&・\.]{1,20}(?:株式会社|有限会社|合同会社|\(株\)|（株）)")),
    ("university", re.compile(r"[一-龥ァ-ヶーA-Za-z]{1,15}(?:大学|高等専門学校|工業高等専門学校)(?:大学院)?")),
    ("public_lab", re.compile(r"[一-龥ァ-ヶー]{1,15}(?:産業技術センター|工業技術センター|技術センター|産業技術研究所|工業技術研究所|産業技術総合研究所|試験研究所|研究センター|研究所|試験場|技術研究組合)")),
]
NOT_ORG = re.compile(r"^(?:当社|弊社|本学|同社|貴社|御社|各社|全社|大手|国内|海外|日本|世界|この|その|当|本|新|旧)")
SENT_SPLIT = re.compile(r"(?<=[。！？\n])")


def _queries() -> list[str]:
    qs = []
    for m in PBF_MODELS:
        qs += [f"{m} 導入", f"{m} 保有"]
    for t in PBF_TERMS:
        qs += [f"{t} 装置 導入", f"{t} 保有設備"]
    return COMPANY_QUERIES + INST_QUERIES + qs


def _read_set(p) -> set[str]:
    return set(p.read_text(encoding="utf-8").split("\n")) - {""} if p.exists() else set()


def _add_line(p, s: str) -> None:
    with open(p, "a", encoding="utf-8") as f:
        f.write(s.replace("\n", " ") + "\n")


def _machine_in(s: str) -> str:
    hit = [m for m in PBF_MODELS if m.lower() in s.lower()] + [t for t in PBF_TERMS if t in s]
    return ";".join(dict.fromkeys(hit))


def _orgs(s: str) -> list[tuple[str, str]]:
    out = []
    for kind, pat in ORG_PATTERNS:
        for m in pat.findall(s):
            n = re.sub(r"\s+", "", m).strip("・.")
            if len(n) >= 3 and not NOT_ORG.match(n):
                out.append((n, kind))
    return list(dict.fromkeys(out))


def _scan(url: str, title: str, text: str, query: str) -> list[dict]:
    rows = []
    for s in SENT_SPLIT.split(text or ""):
        s = s.strip()
        if not (8 <= len(s) <= 400):
            continue
        machine = _machine_in(s)
        generic = any(g in s for g in GENERIC)
        if not (machine or generic) or not any(v in s for v in OWN_VERBS + GENERIC):
            continue
        orgs = _orgs(s) or _orgs(title)
        for org, kind in orgs:
            rows.append({"org": org, "org_kind": kind, "pbf": "yes" if machine else "unknown", "machine": machine,
                         "quote": s[:300], "url": url, "query": query})
    return rows


def _append(rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    new = not EVID.exists()
    with open(EVID, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def _summarize() -> int:
    if not EVID.exists():
        return 0
    orgs: dict[str, dict] = {}
    with open(EVID, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            o = orgs.setdefault(r["org"], {"org": r["org"], "org_kind": r["org_kind"], "pbf": "unknown",
                                           "machines": set(), "urls": set(), "n_quotes": 0, "first_quote": r["quote"]})
            if r["pbf"] == "yes":
                o["pbf"] = "yes"
            o["machines"] |= set(filter(None, r["machine"].split(";")))
            o["urls"].add(r["url"])
            o["n_quotes"] += 1
    with open(OWNERS, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["組織名", "種別", "PBFの証拠", "見つけた装置の言い方", "出典ページ数", "引用数", "最初の引用", "出典URL"])
        for o in sorted(orgs.values(), key=lambda o: (o["pbf"] != "yes", -len(o["urls"]), o["org"])):
            w.writerow([o["org"], o["org_kind"], o["pbf"], ";".join(sorted(o["machines"])), len(o["urls"]), o["n_quotes"],
                        o["first_quote"], " ".join(sorted(o["urls"])[:5])])
    # 第2段の入力：会社と研究機関すべて（PBFの証拠ありを先頭に）。公式URLは第2段が検索で探す
    have = set()
    if INPUT.exists():
        with open(INPUT, encoding="utf-8-sig") as f:
            have = {r["社名"] for r in csv.DictReader(f)}
    new_rows = [o for o in sorted(orgs.values(), key=lambda o: o["pbf"] != "yes") if o["org"] not in have]
    write_header = not INPUT.exists()
    with open(INPUT, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["社名", "公式URL", "別名", "文脈"])
        for o in new_rows:
            w.writerow([o["org"], "", "", f"PBF={o['pbf']} 装置={';'.join(sorted(o['machines']))[:120]} 出典={sorted(o['urls'])[0]}"])
    return len(orgs)


def main(deadline_s: float = 3 * 3600) -> int:
    deadline = time.time() + deadline_s
    OUT.mkdir(parents=True, exist_ok=True)
    done_q, done_u = _read_set(DONEQ), _read_set(DONEU)
    for q in _queries():
        if time.time() > deadline:
            break
        if q in done_q:
            continue
        try:
            results = search(q, 10)
        except Exception as e:  # 検索が落ちても次へ
            print(f"search fail {q}: {e}", flush=True)
            continue
        for r in results:
            u = r["url"].split("#")[0]
            # 検索結果の題名と抜粋も証拠の候補（本文が開けないページもある）
            _append(_scan(u, r.get("title", ""), f"{r.get('title', '')}。{r.get('snippet', '')}", q))
            if u in done_u or time.time() > deadline:
                continue
            try:
                p = fetch(u)
            except Exception:
                p = {}
            _append(_scan(u, p.get("title", "") or r.get("title", ""), p.get("text", ""), q))
            _add_line(DONEU, u)
            done_u.add(u)
        _add_line(DONEQ, q)
        done_q.add(q)
        print(f"q done: {q} ({len(done_q)}/{len(_queries())})", flush=True)
        if len(done_q) % 10 == 0:
            _summarize()
    n = _summarize()
    print(f"orgs: {n}", flush=True)
    if time.time() < deadline and done_q >= set(_queries()):
        (OUT / "BUILD_DONE").write_text(f"{n}\n", encoding="utf-8")
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
