"""Build the knowledge base Claude and Muse read before answering (not for the person to read).

From the refreshing topics (frame_export, frame_import, industry_news, customer_3c) it writes
researcher/out/brief/:
  INDEX.md               reading rules for the AI + what changed in the last 24h + file list
  分野_<field>.md         one file per field: 輸出 / 輸入 / ニュース, each item in 5 boxes + PEST
  顧客_<company>.md       one file per customer: profile + 5 boxes + PEST
Each quote is tagged 一次 (government, industry bodies, universities, IR/PDF, the company's own
site), 記事 or SNS, and dated with the day it was first seen. Newest first. Facts only.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse as _raw_urlparse


def urlparse(u):
    """全角記号入りURLで落ちないようにする（HEALTH.md の am_ceiling エラー対策）。"""
    try:
        return _raw_urlparse(u or "")
    except ValueError:
        return _raw_urlparse("")

from researcher.targets import ROOT, load_targets, load_topic, slug

JST = timezone(timedelta(hours=9))
OUT = ROOT / "out" / "brief"
MARKET_TOPICS = [("frame_export", "日本→海外（輸出）", "輸出"), ("frame_import", "海外→日本（輸入）", "輸入"), ("industry_news", "業界ニュース（毎日）", "ニュース")]
CUSTOMER_TOPIC = "customer_3c"
PER_BOX = 5
SOCIAL_HOSTS = ("note.com", "x.com", "twitter.com", "linkedin.com", "facebook.com")
PRIMARY_HINTS = (".go.jp", ".lg.jp", ".ac.jp", ".or.jp", ".gov", "meti", "mod.go", "jetro", "nedo", "jaxa", "edinet", "tdnet", "kantei")

RULES = """# A1 知識の土台（Claude と Muse が答える前に必ず読む）

## 読み方（AIへの指示）
- 業界・顧客・事業・営業・戦略の質問には、答える前にこの索引と、該当する分野・顧客のファイルを読む。読まずに一般論で答えない。
- 枠は「顧客起点の3C＋PEST」：顧客（金を払う側）／顧客の顧客（発注元が求めること）／顧客の競合（奪いに来る側）／自社（A1）／自社の競合（商社・代理店・仲介）＋政治・経済・社会・技術。順番はまず業界（輸出・輸入の両方向）、その上で顧客1社ごと。
- ここにあるのは引用と出典だけ。箱の問い（何を求めているか、何に困っているか、何を失いたくないか）への答えは、AIが質問のたびにこの事実から組み立てる。本人に書かせない。
- 引用には［一次］［記事］［SNS］の印と、初めて見つけた日付がある。数字は［一次］を優先し、［記事］だけの数字はそう断って使う。数字を作らない。
- 事業の層を混ぜない：①領域＝樹脂以外（決定済み）②やること＝海外の製品を担いで日本で売る、または日本の製品を海外に売る（決定済み）③財布の入り口（消費か投資か）④ビジネスモデル（A1がどうお金を受け取るか）。決まっていないのは③④だけ。
- 答えに使いたい事実が無かった・薄かった箱は、答えの最後に「掘る対象」として挙げる（次の回でリサーチャーの対象に足す）。
"""


def jst_now() -> datetime:
    return datetime.now(JST)


def tag(url: str, own_host: str = "") -> str:
    h = urlparse(url).netloc.lower()
    if any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS):
        return "SNS"
    if (own_host and h.endswith(own_host.removeprefix("www."))) or url.lower().split("?")[0].endswith(".pdf") or any(p in h for p in PRIMARY_HINTS):
        return "一次"
    return "記事"


def load(topic: str) -> dict[str, dict]:
    d = ROOT / "out" / topic / "companies"
    out = {}
    for p in d.glob("*.json") if d.exists() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if "error" not in r:
            out[r.get("name")] = r
    return out


# 2026-11-05 4-39：extract.py の除外（広告枠・メニュー）は新しく取った引用にしか効かず、前の回に入った
# 「BRAND VOICE・Paid Program」やページ題名（「…｜Forbes JAPAN 公式サイト」）、著者名だけの行が brief に残った。
# 書き出すたびに今の判定で外す（4-9 の「前の版で入ったものも読み込むたびに今の判定で外す」を brief にも当てる）
from researcher.extract import _is_menu
TITLE_RE = re.compile(r"(([｜|]|\s[-－–]\s)\s*[^｜|]{0,40}(公式サイト|公式|JAPAN|NEWS|ニュース)[^｜|]{0,20}$|^[^。、]{0,40}[-－]\s*[^。、]{0,20}\([A-Za-z]+\)$)")
def junk(q: str) -> bool:
    q = re.sub(r"\s+", " ", q or "").strip()
    return _is_menu(q) or bool(TITLE_RE.search(q))


# 2026-11-06 4-42 走査の失敗から：forbesjapan.com の1つの記事から、同じページの関連記事の題名（「企業の海外進出を支える…の「多様性」」
# 「サステナグロースを実現する企業の条件…審査員対談」）が人型ロボットの「顧客」の事実として brief に出た。
# 句点も数字も無い60字以下の行で、品目の名前（の3字）を含まないものは、同じページの別記事の題名とみなして外す（4-35 の本文の枠を brief にも当てる）
def off_topic_title(q: str, name: str) -> bool:
    q = re.sub(r"\s+", " ", q or "").strip()
    if "。" in q or re.search(r"\d", q) or len(q) > 60: return False
    keys = {t[i:i + 3] for t in re.split(r"[\s・／/（）()、,]+", name or "") if len(t) >= 3 for i in range(len(t) - 2)}
    return bool(keys) and not any(k in q for k in keys)


# 2026-11-07 4-73 走査の失敗から：36kr.jp の3つの別の記事に同じ「ショートドラマ量産の舞台裏」の題名が出て、人型ロボットの「経済」の事実になった
# （同じサイトの関連記事の枠）。worldinsight.com の日本語の記事からは、英語の別記事（AIエージェントの記憶の費用）の文が「自社の競合」に入った。
# 同じホストの2つ以上のURLに同じ文が出たらサイト共通の枠として外す。英字が6割を超える文で、品目の名前（日本語3字・英語4字以上の語）を含まないものは別記事として外す
def site_common(rec: dict) -> set:
    seen: dict = {}
    for v in (rec.get("evidence") or {}).values():
        for i in v or []:
            q = re.sub(r"\s+", "", i.get("quote", ""))[:60]
            if q: seen.setdefault((urlparse(i.get("url", "")).netloc, q), set()).add(i.get("url", ""))
    return {k for k, u in seen.items() if len(u) >= 2}


def off_topic_latin(q: str, name: str, en: str = "", url: str = "") -> bool:
    q = re.sub(r"\s+", " ", q or "").strip()
    # 日本語のページ（パスに /japanese/・/ja/・/jp/ か、日本語の文字を含む）の英語の文だけを見る。英語の原典（REI の英語版など）は外さない
    if not q or not re.search(r"(/japanese/|/ja/|/jp/|%E3%)", url or "", re.I): return False
    lat = sum(1 for c in q if c.isascii() and c.isalpha())
    if lat / max(1, len(q.replace(" ", ""))) <= 0.6: return False
    words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", (en or "") + " " + (name or ""))]
    return not any(w in q.lower() for w in words)


# 2026-11-08 4-106 走査の失敗から：リクルートエージェントの求人検索ページ（r-agent.com/job_search/kw/…）の求人票の文言
# （「求人票の作成・更新、応募者対応…」「取締役会、株主総会運営…」）が、定置用蓄電池・生成AI半導体の「社会」「顧客の顧客」「自社の競合」
# の事実として INDEX.md に出た。日経の記事からは同じページの関連記事の見出し（「米民主党はAIブームを失速させるのか 5つのポイントで解説」
# 「「特集・インド半導体」「米陸軍で痛感した壁」 注目5本」）が数字入りのため題名の判定をすり抜けた。
# 求人サイトの検索結果・求人一覧のページの引用は brief に出さない（4-9・4-31）。句点の無い「〜で解説」「注目N本」「〜選」の行は見出しとして外す。
JOBHOST = re.compile(r"(r-agent|stanby|indeed|doda|mynavi|rikunabi|en-japan|townwork|baitoru|careerjet|kyujinbox|hatarako|bizreach|green-japan|wantedly|type\.jp|levtech|jac-recruitment)", re.I)
JOBPATH = re.compile(r"(job_search|/jobs?/|/kw/|search|/q-|[?&](q|keyword|kw)=)", re.I)
HEADLINE_RE = re.compile(r"(で解説|を解説|注目\s*\d+\s*本|\d+\s*選|まとめ|ランキング|とは[？?]?)\s*$")
def job_or_headline(q: str, url: str) -> bool:
    p = urlparse(url or "")
    if JOBHOST.search(p.netloc) and JOBPATH.search((p.path or "") + "?" + (p.query or "")): return True
    q = re.sub(r"\s+", " ", q or "").strip()
    return "。" not in q and len(q) <= 80 and bool(HEADLINE_RE.search(q))


def keep(i: dict, rec: dict, common: set) -> bool:
    q, nm = i.get("quote", ""), rec.get("name", "")
    if job_or_headline(q, i.get("url", "")): return False
    if junk(q) or off_topic_title(q, nm) or off_topic_latin(q, nm, rec.get("en", ""), i.get("url", "")): return False
    return (urlparse(i.get("url", "")).netloc, re.sub(r"\s+", "", q)[:60]) not in common


def box_lines(rec: dict, cats: list[str], own_host: str = "") -> list[str]:
    ev, lines = rec.get("evidence", {}), []
    common = site_common(rec)
    ev = {k: [i for i in (v or []) if keep(i, rec, common)] for k, v in ev.items()}
    extra = [k for k in ev if k.startswith("SNS・note") or k == "検索結果の抜粋"]
    for c in cats + extra:
        items = ev.get(c, [])
        if not items:
            lines.append(f"- {c}：未完（まだ取れていない・掘る対象）")
            continue
        items = sorted(items, key=lambda i: ({"一次": 0, "記事": 1, "SNS": 2}[tag(i["url"], own_host)], -int((i.get("first_seen") or "0000-00-00").replace("-", ""))))
        lines.append(f"- {c}（{len(items)}件）：")
        for i in items[:PER_BOX]:
            q = re.sub(r"\s+", " ", i["quote"])[:220]
            lines.append(f"  - ［{tag(i['url'], own_host)}］{i.get('first_seen', '')}「{q}」{i['url']}")
    return lines


def field_of(context: str) -> str:
    m = re.match(r"\s*\d+\s+(\S+)", context or "")
    return m.group(1) if m else "その他"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    since = (jst_now() - timedelta(hours=24)).strftime("%Y-%m-%d")
    changes: list[str] = []
    files: list[str] = []
    # fields
    by_field: dict[str, list[str]] = {}
    for topic, label, short in MARKET_TOPICS:
        if not (ROOT / "topics" / f"{topic}.yaml").exists():
            continue
        t = load_topic(topic)
        cats = list(t["evidence"])
        recs = load(topic)
        for c in load_targets(t):
            f = field_of(c["context"])
            sec = by_field.setdefault((f, short, label), [])
            r = recs.get(c["name"])
            sec.append(f"### {label}｜{c['name']}（{c.get('en', '')}）")
            if not r:
                sec.append("- 未調査（次の回で調べる）")
                continue
            sec.append(f"- 取得：{r.get('researched_at', '')[:16]}・調査{r.get('runs', 1)}回目・読んだ頁{len(r.get('checked', {}).get('pass3', []))}")
            sec += box_lines(r, cats)
            for cat, items in (r.get("new_this_run") or {}).items():
                for i in [i for i in items if keep(i, r, site_common(r))][:3]:
                    if i.get("first_seen", "") >= since:
                        changes.append(f"- {f}｜{label}｜{c['name']}｜{cat}：「{re.sub(r'\s+', ' ', i['quote'])[:120]}」{i['url']}")
    for (f, short, label), sec in by_field.items():
        title = f"分野_{f}_{short}"
        name = f"{slug(title)}.md"
        (OUT / name).write_text(f"# {title}（{label}・顧客起点の3C＋PEST・事実のみ）\n\n" + "\n".join(sec) + "\n", encoding="utf-8")
        files.append(f"- {name}：{title}")
    # customers
    if (ROOT / "topics" / f"{CUSTOMER_TOPIC}.yaml").exists():
        t = load_topic(CUSTOMER_TOPIC)
        cats, keys = list(t["evidence"]), list(t["profile_keys"])
        recs = load(CUSTOMER_TOPIC)
        for c in load_targets(t):
            r = recs.get(c["name"])
            name = f"{slug('顧客_' + c['name'])}.md"
            lines = [f"# 顧客：{c['name']}（顧客起点の3C＋PEST・事実のみ）", f"- 文脈：{c['context']}"]
            if not r:
                lines.append("- 未調査（次の回で調べる）")
            else:
                own = urlparse(r.get("official_url", "")).netloc
                lines.append(f"- 公式：{r.get('official_url') or '特定できず'}・取得{r.get('researched_at', '')[:16]}・調査{r.get('runs', 1)}回目")
                for k in keys:
                    v = r.get("profile", {}).get(k) or []
                    lines.append(f"- {k}：" + (" ／ ".join(f"{i['value'][:120]}（{i['url']}）" for i in v[:2]) if v else "未完"))
                lines += box_lines(r, cats, own)
                for cat, items in (r.get("new_this_run") or {}).items():
                    for i in [i for i in items if keep(i, r, site_common(r))][:3]:
                        if i.get("first_seen", "") >= since:
                            changes.append(f"- 顧客｜{c['name']}｜{cat}：「{re.sub(r'\s+', ' ', i['quote'])[:120]}」{i['url']}")
            (OUT / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
            files.append(f"- {name}：{c['name']}")
    idx = [RULES, f"## 更新：{jst_now().strftime('%Y-%m-%d %H:%M')} JST", "", "## この24時間で増えた事実（一部）"]
    idx += changes[:80] or ["- なし"]
    idx += ["", "## ファイル", *files, ""]
    (OUT / "INDEX.md").write_text("\n".join(idx), encoding="utf-8")
    print(f"brief: {len(files)} files, {len(changes)} changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
