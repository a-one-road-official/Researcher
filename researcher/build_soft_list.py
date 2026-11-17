"""イプロス掲載企業（ipros_all）から、海外AMソフトを売る営業先の名簿を作る（2026-11-17 本人指示）。

条件の正本：docs/soft_list_ipros-spec-2026-11-17.md（条件書 01_soft-list-spec の実装）。
リストは「ソフト別」ではなく「客の役割（R1〜R11）別」に組む。担ぐ製品が決まったら役割タグで切り出す。

段階
  第1段（種）：Drive の ipros_all.csv（約45,108社）だけを元に、category・info・product の文で役割の候補を拾い、
    employees が読めて 100〜500 の範囲外なら足切りする。employees 空欄・範囲またぎは落とさず第2段へ回す。
    社名は ipros の行そのものから取る（文字列の断片から会社を作らない）。キーは ipros_id。
  第2段（1社ずつ）：公式サイトを読み（build_award_chain.check_one を流用）、
    従業員数（引用つき）・事業内容の引用・AM判定 A/B/C（引用つき）・役割の裏付け・上場/完全子会社の形跡・
    メール／問い合わせフォームURL を取る。引用の無い判定は名簿に入れない（捨てる経路を持つ）。
  門番：verify_list.verify で4条件（従業員100〜500・役割の裏付け・AM判定の引用・上場/完全子会社でない）を
    1社ずつ引用で確かめる。通過分だけ list.csv に出す。
  出力：out/soft_list_ipros/seeds.csv・checked.csv・list.csv・verify.md・stats.md、inputs/soft_list_ipros.csv
    Drive のスプレッドシート「ソフト営業リスト（ipros由来）」への書き出しは GitHub Actions では行わない
    （リポジトリに Google の書き込み資格情報が無い）。出力が着地したら Claude 側で list.csv／checked.csv から作る。
時間で区切り、次の回に続きから回る。control.yaml の soft_list_ipros は run: false で登録し、
回し始め（run: true への切替）は GitHub 側の担当が行う。
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

from researcher.targets import ROOT

OUT = ROOT / "out" / "soft_list_ipros"
SEEDS = OUT / "seeds.csv"
CHECKED = OUT / "checked.csv"
INPUT = ROOT / "inputs" / "soft_list_ipros.csv"
DRIVE_ID = "1cZSz82ZvNBV238tyrtN093Xy91oX1sHd"  # ipros_all.csv（build_ipros と同じ正本）

# ---- 第1段：役割（R1〜R11）の拾い方。条件書2章の表をキーワード集合に写したもの。
# ここで拾うのは「候補」まで。第2段で公式サイトの事業内容の引用が裏付けにならなければ名簿に入らない。
ROLE_PATTERNS: dict[str, list[str]] = {
    "R1": [r"宇宙|航空宇宙|ロケット|衛星", r"半導体製造装置|半導体装置", r"医療機器", r"産業機械|工作機械|専用機"],
    "R2": [r"包装機械|製袋機|充填機", r"マニホールド", r"自動組立機|組立専用機"],
    "R3": [r"治具", r"真空機器|真空ポンプ|真空チャック", r"搬送機器|搬送装置|コンベヤ", r"グリッパー|ハンドリング"],
    "R4": [r"切削加工", r"板金加工|板金", r"試作", r"多品種少量", r"受託加工|加工受託"],
    "R5": [r"熱交換器", r"ヒートシンク", r"導波管", r"冷却部品|冷却器"],
    "R6": [r"締結|ファスナー", r"コネクタ|電気接続", r"義肢|装具", r"ねじ|ボルト|ナット"],
    "R7": [r"鋳造|鋳物", r"木型", r"発泡模型|発泡スチロール模型"],
    "R8": [r"CAE|構造解析|流体解析|受託解析", r"設計受託|受託設計|設計請負"],
    "R9": [r"金属3D|金属積層|金属AM|メタル3D", r"造形受託|受託造形"],
    "R10": [r"3Dプリント|3Dプリンター出力|光造形", r"樹脂造形|樹脂3D"],
    "R11": [r"3Dプリンター.{0,12}(製造|メーカー|開発・製造|自社開発)", r"(製造|メーカー).{0,12}3Dプリンター"],
}
# 役割→当たるソフト（条件書2章の表そのまま。名簿の文脈と集計にだけ使う）
ROLE_SOFT: dict[str, str] = {
    "R1": "Spherene／Cognitive Design", "R2": "nureo", "R3": "trinckle",
    "R4": "AMFG／3D Spark（AM機能だけ）", "R5": "Spherene／VOXSHELL",
    "R6": "Additive Marking／trinckle", "R7": "CIRTES／DeskArtes",
    "R8": "VOXSHELL／nureo", "R9": "amsight／Assembrix／AMFG",
    "R10": "AMIS／AMFG／DeskArtes", "R11": "Create it REAL／Spherene",
}

SEED_FIELDS = ["ipros_id", "name", "employees_ipros", "roles", "category", "homepage", "status", "cut_reason"]
FIELDS = ["ipros_id", "name", "status", "official_url", "employees", "employees_quote", "employees_src",
          "business_quote", "roles_cand", "roles", "role_quote", "am_judge", "am_quote",
          "listed_flag", "listed_quote", "emails", "contact_url", "judge"]


def _read(p) -> list[dict]:
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(p, rows, fields) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_emp(text: str) -> tuple[int | None, bool]:
    """従業員数の文字列→ (人数, 範囲またぎか)。読めなければ (None, False)。
    範囲（100〜300人など）が 100〜500 の境界をまたぐときは第2段へ回すため straddle=True を返す。"""
    t = re.sub(r"[,，\s]", "", text or "")
    m = re.search(r"(\d+)[〜~～-](\d+)\s*(?:人|名|従業員|社員)", t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi < 100 or lo > 500:
            return (lo if hi < 100 else hi), False  # 範囲ごと外（代表値つきで足切り）
        if 100 <= lo and hi <= 500:
            return lo, False
        return None, True  # 境界またぎ→第2段で確定させる
    m = re.search(r"(\d+)\s*(?:人|名)(?:以上|以下|程度|前後)?", t) or re.search(r"(?:従業員|社員)(?:数)?\D{0,4}(\d+)", t)
    if m:
        return int(m.group(1)), False
    if re.fullmatch(r"\d{1,6}", t):
        return int(t), False
    return None, False


def roles_of(text: str) -> list[str]:
    return [r for r, pats in ROLE_PATTERNS.items() if any(re.search(p, text) for p in pats)]


def seeds_from_drive() -> bool:
    """ipros_all.csv をリンク共有から取り、第1段の候補（seeds.csv）を作る。元ファイルはリポジトリに入れない。"""
    import requests
    if SEEDS.exists() and (OUT / "SEEDS_FROM_DRIVE").exists():
        return True
    s = requests.Session()
    url = f"https://drive.usercontent.google.com/download?id={DRIVE_ID}&export=download&confirm=t"
    r = s.get(url, timeout=600)
    if r.status_code != 200 or b"<html" in r.content[:200].lower():
        print(f"soft_list: drive download failed {r.status_code}", flush=True)
        return False
    raw = r.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        return False
    cols = list(rows[0].keys())
    pick = lambda exact, rx: (exact if exact in cols else next((c for c in cols if re.search(rx, c or "", re.I)), None))
    c_name = pick("name", r"社名|会社名|企業名")
    c_ipros = pick("ipros_url", r"ipros")
    c_emp = pick("employees", r"従業員|社員数")
    c_cat = pick("category", r"カテゴリ|業種")
    c_info, c_prod = pick("info", r"紹介|概要"), pick("product", r"製品|商品")
    c_home = pick("normalized_homepage", r"homepage|ホームページ|公式")
    print(f"soft_list: columns name={c_name} emp={c_emp} cat={c_cat} rows={len(rows)}", flush=True)
    have, total, blank = {}, 0, 0
    for i, x in enumerate(rows):
        nm = (x.get(c_name) or "").strip() if c_name else ""
        if not nm:
            continue
        total += 1
        idm = re.search(r"/company/detail/(\d+)", x.get(c_ipros) or "") if c_ipros else None
        key = idm.group(1) if idm else f"row{i}"
        emp_raw = (x.get(c_emp) or "").strip() if c_emp else ""
        if not emp_raw:
            blank += 1
        emp, straddle = parse_emp(emp_raw)
        text = " ".join((x.get(c) or "") for c in (c_cat, c_info, c_prod) if c)
        roles = roles_of(text)
        home = (x.get(c_home) or "").strip() if c_home else ""
        row = {"ipros_id": key, "name": nm, "employees_ipros": emp_raw, "roles": "・".join(roles),
               "category": (x.get(c_cat) or "").strip() if c_cat else "",
               "homepage": home if home.startswith("http") and "ipros" not in home else "",
               "status": "候補", "cut_reason": ""}
        if emp is not None and not straddle and not (100 <= emp <= 500):
            row["status"], row["cut_reason"] = "足切り", f"従業員数（ipros：{emp_raw}）が100〜500の範囲外"
        elif not roles:
            row["status"], row["cut_reason"] = "足切り", "役割（R1〜R11）に当たらない"
        have[key] = row
    _write(SEEDS, list(have.values()), SEED_FIELDS)
    (OUT / "SEEDS_FROM_DRIVE").write_text(
        json.dumps({"total": total, "blank_emp": blank}, ensure_ascii=False), encoding="utf-8")
    print(f"soft_list: seeds {len(have)} total {total} blank_emp {blank}", flush=True)
    return True


AM_WORD = re.compile(r"3Dプリンター|3Dプリンタ|積層造形|アディティブ|金属AM|樹脂AM|3D造形|光造形|粉末床|WAAM|3Dプリント")
AM_BIZ = re.compile(r"受託|サービス|量産|注文|依頼|見積|販売|対応可能|承り")
AM_TRIAL = re.compile(r"導入|試作|検証|研究|開発|検討|保有|設備")


def am_scan(home: str) -> tuple[str, str, str]:
    """公式サイトの本文から AM の形跡を探し、(判定 A/B/C, 引用, 読んだページ) を返す。
    C＝事業として（受託・サービス・量産の言葉と AM が同じ行）、B＝形跡はあるが事業の言葉が無い、A＝読んだ範囲に形跡なし。"""
    from researcher.fetch import fetch
    first = fetch(home)
    host = urlparse(first.get("final_url") or home).netloc
    pages, texts = [home], [first.get("text", "")]
    for label, href in (first.get("links") or [])[:300]:
        if len(pages) >= 7:
            break
        if urlparse(href).netloc == host and href not in pages and re.search(
                r"事業|製品|サービス|設備|事例|business|product|service|equipment|works", (label or "") + href, re.I):
            pages.append(href)
            texts.append(fetch(href).get("text", ""))
    lines = [ln.strip() for t in texts for ln in t.splitlines() if ln.strip()]
    biz_q, trial_q = "", ""
    for ln in lines:
        if AM_WORD.search(ln):
            if AM_BIZ.search(ln) and not biz_q:
                biz_q = ln[:160]
            elif not trial_q:
                trial_q = ln[:160]
    if biz_q:
        return "C", biz_q, " ".join(pages)
    if trial_q:
        return "B", trial_q, " ".join(pages)
    return "A", "", " ".join(pages)


LISTED_RX = re.compile(r"上場|証券コード|東証|プライム市場|スタンダード市場|グロース市場|株式市場")
SUB_RX = re.compile(r"の100%子会社|の完全子会社|完全子会社|グループ会社です|子会社です")


def check_one_soft(seed: dict) -> dict:
    from researcher.build_award_chain import check_one
    from researcher.build_ipros import official_from_ipros
    r: dict = {"ipros_id": seed["ipros_id"], "name": seed["name"], "roles_cand": seed.get("roles", "")}
    home = seed.get("homepage") or (official_from_ipros(seed["ipros_id"]) if seed["ipros_id"].isdigit() else "")
    if not home:
        r.update({"status": "公式サイト見つからず", "judge": "外す（公式サイト無し）"})
        return r
    base = check_one(seed["name"], 0, "イプロス", home=home)
    r["status"] = base.get("status", "")
    r["official_url"] = base.get("official_url", "")
    r["business_quote"] = base.get("business_quote", "")
    if base.get("status") != "読了":
        r["judge"] = f"外す（{r['status'] or '未読'}）"
        return r
    # 従業員数：公式の会社概要の引用を正とし、読めなければ ipros の記載（出典を記録）。どちらも無ければ人数不明で門番に落とす
    emp, _ = parse_emp(base.get("employees_quote", ""))
    if emp is not None:
        r["employees"], r["employees_quote"], r["employees_src"] = str(emp), base.get("employees_quote", ""), "公式サイト"
    else:
        emp2, straddle2 = parse_emp(seed.get("employees_ipros", ""))
        if emp2 is not None and not straddle2:
            r["employees"], r["employees_quote"], r["employees_src"] = str(emp2), f"ipros記載：{seed.get('employees_ipros', '')}", "イプロス"
        else:
            r["employees"], r["employees_quote"], r["employees_src"] = "", "", ""
    # 役割の裏付け：公式サイトの事業内容＋AM走査で読んだ本文に、候補の役割のパターンが当たるか
    am_judge, am_quote, pages = am_scan(r["official_url"])
    site_text = r["business_quote"]
    roles_ok, role_quote = [], ""
    for role in (seed.get("roles") or "").split("・"):
        if role and any(re.search(p, site_text) for p in ROLE_PATTERNS.get(role, [])):
            roles_ok.append(role)
            if not role_quote:
                role_quote = r["business_quote"][:160]
    r["roles"] = "・".join(roles_ok)
    r["role_quote"] = role_quote
    # AM判定：A は「読んだ範囲に形跡なし」＋事業内容の引用を根拠にする
    r["am_judge"] = am_judge
    r["am_quote"] = am_quote if am_quote else (f"形跡なし（事業内容：{r['business_quote'][:120]}）" if am_judge == "A" else "")
    # 上場・大手の完全子会社：会社概要まわりの文から形跡だけを記録する（断定しない）
    prof = " ".join([r["business_quote"], base.get("employees_quote", ""), base.get("capital_quote", ""), base.get("subsidiaries_quote", "")])
    m = LISTED_RX.search(prof)
    if m:
        r["listed_flag"], r["listed_quote"] = "上場の形跡", prof[max(0, m.start() - 30): m.end() + 30]
    else:
        m2 = SUB_RX.search(prof)
        r["listed_flag"], r["listed_quote"] = ("子会社の形跡", prof[max(0, m2.start() - 30): m2.end() + 30]) if m2 else ("", "")
    r["emails"] = (base.get("emails") or "").split(" ")[0] if base.get("emails") else ""
    r["contact_url"] = base.get("contact_url", "")
    r["judge"] = judge_soft(r)
    return r


def judge_soft(r: dict) -> str:
    if r.get("status") != "読了":
        return r.get("judge") or "未判定"
    bad = []
    emp = r.get("employees", "")
    if not (emp.isdigit() and 100 <= int(emp) <= 500 and r.get("employees_quote")):
        bad.append("従業員100〜500を確認できず")
    if not (r.get("roles") and r.get("role_quote")):
        bad.append("役割の裏付けなし")
    if not r.get("am_quote"):
        bad.append("AM判定の引用なし")
    if r.get("listed_flag"):
        bad.append(r["listed_flag"])
    return ("対象（" + f"判定{r.get('am_judge', '')}・" + (r.get("roles") or "") + "）") if not bad else "外す（" + "・".join(bad) + "）"


CONDS = {
    "従業員100〜500（出典つき）": (
        lambda r: (r.get("employees") or "").isdigit() and 100 <= int(r["employees"]) <= 500 and bool(r.get("employees_quote")),
        lambda r: f"{r.get('employees_src', '')}：{r.get('employees_quote', '')}"),
    "役割の裏付け（公式サイト）": (
        lambda r: bool(r.get("roles") and r.get("role_quote")),
        lambda r: r.get("role_quote", "")),
    "AM判定の引用": (
        lambda r: bool(r.get("am_quote")),
        lambda r: f"判定{r.get('am_judge', '')}：{r.get('am_quote', '')}"),
    "上場・大手完全子会社でない": (
        lambda r: not r.get("listed_flag"),
        lambda r: r.get("business_quote", "")),
}


def run_checks(deadline: float) -> bool:
    seeds = [s for s in _read(SEEDS) if s.get("status") == "候補"]
    done = {r["ipros_id"]: r for r in _read(CHECKED)}
    for s in seeds:
        if s["ipros_id"] in done:
            continue
        if time.monotonic() > deadline:
            break
        try:
            r = check_one_soft(s)
        except Exception as e:
            r = {"ipros_id": s["ipros_id"], "name": s["name"], "status": f"読み取りエラー:{type(e).__name__}",
                 "roles_cand": s.get("roles", ""), "judge": "未判定（エラー）"}
        done[s["ipros_id"]] = r
        print(f"soft_list {s['name']}: {r.get('judge', '')}", flush=True)
        if len(done) % 5 == 0:
            _write(CHECKED, list(done.values()), FIELDS)
    _write(CHECKED, list(done.values()), FIELDS)
    return len(done) >= len(seeds) > 0


def write_outputs(finished: bool) -> None:
    from researcher.verify_list import verify
    seeds = _read(SEEDS)
    rows = _read(CHECKED)
    for r in rows:
        if r.get("status") == "読了":
            r["judge"] = judge_soft(r)
    _write(CHECKED, rows, FIELDS)
    cand = [r for r in rows if r.get("status") == "読了"]
    ok = verify(cand, CONDS, OUT)
    _write(OUT / "list.csv", ok, FIELDS)
    INPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(INPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["社名", "公式URL", "別名", "文脈"])
        for r in ok:
            softs = "／".join(ROLE_SOFT.get(x, "") for x in (r.get("roles") or "").split("・") if x)
            w.writerow([r["name"], r.get("official_url", ""), "",
                        f"役割{r.get('roles', '')}・AM判定{r.get('am_judge', '')}・当たるソフト：{softs}"])
    meta = {}
    if (OUT / "SEEDS_FROM_DRIVE").exists():
        meta = json.loads((OUT / "SEEDS_FROM_DRIVE").read_text(encoding="utf-8"))
    cand_seeds = [s for s in seeds if s.get("status") == "候補"]
    cut1 = Counter(s.get("cut_reason", "").split("（")[0] or "その他" for s in seeds if s.get("status") == "足切り")
    # 役割×判定の表（名簿通過分）
    table: dict[str, Counter] = {}
    for r in ok:
        for role in (r.get("roles") or "").split("・"):
            if role:
                table.setdefault(role, Counter())[r.get("am_judge", "")] += 1
    no_contact = sum(1 for r in ok if not r.get("emails") and not r.get("contact_url"))
    cut2 = Counter()
    for r in rows:
        if r.get("judge", "").startswith("外す"):
            for reason in re.findall(r"外す（(.+)）", r["judge"]):
                for part in reason.split("・"):
                    cut2[part] += 1
    total = meta.get("total") or len(seeds)
    blank = meta.get("blank_emp", 0)
    md = [f"# ソフト営業リスト（ipros由来）{'（完了）' if finished else '（途中）'}", "",
          f"- ipros_all：{total}社（従業員空欄 {blank}社＝{blank * 100 // max(total, 1)}%）",
          f"- 第1段の候補：{len(cand_seeds)}社　第2段で調べた会社：{len(rows)}社　名簿通過：{len(ok)}社",
          f"- 連絡先（メール／フォーム）が取れなかった会社：{no_contact}社（名簿通過分のうち）", "",
          "## 役割×判定の社数（名簿通過分）", "",
          "| 役割 | A（未使用） | B（試験的） | C（事業） | 計 |", "|---|---|---|---|---|"]
    for role in sorted(table):
        c = table[role]
        md.append(f"| {role} | {c.get('A', 0)} | {c.get('B', 0)} | {c.get('C', 0)} | {sum(c.values())} |")
    md += ["", "## 第1段で足切りした数"] + [f"- {k}：{v}" for k, v in cut1.most_common()]
    md += ["", "## 第2段で外した数（理由別・重複あり）"] + [f"- {k}：{v}" for k, v in cut2.most_common()]
    (OUT / "stats.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if finished:
        (OUT / "BUILD_DONE").write_text(f"{len(ok)}\n", encoding="utf-8")


def main(deadline_s: float = 90 * 60) -> int:
    deadline = time.monotonic() + deadline_s
    OUT.mkdir(parents=True, exist_ok=True)
    if not seeds_from_drive():
        write_outputs(False)
        return 0
    write_outputs(run_checks(deadline))
    return 0


def is_done() -> bool:
    return (OUT / "BUILD_DONE").exists()


if __name__ == "__main__":
    sys.exit(main())
