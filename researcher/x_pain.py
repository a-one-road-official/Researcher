"""X（旧Twitter）の公式APIで、ものづくりの現場の「困りごと」の投稿を集め、痛みの深さを5つの印で仕分ける。

2026-11-06 本人の指示：「深さが重要」「APIで抜き出す」。
数字や統計ではなく、白書に出ない本音（人がいなくて仕事を断った、求人に払っても来ない、設備が止まっている…）を拾い、
スペイシー事件（浅い1件の頼まれごとに引っ張られる）を避けるため、深い声だけを上に並べる。AIは使わない（言葉の一致だけ）。

深さの5つの印（1つ1点、5点満点）
 1 お金・件数：損が金額・件数・日数で語られている（数字＋万/億/円/件/日/人 と、断る・失う・止まる などが同じ投稿にある）
 2 繰り返し：毎年・毎月・毎回・いつも・年々 など
 3 手を打って失敗：求人・広告・紹介会社などに手を打ったのに、来ない・辞めた・効果がない
 4 決める人：プロフィールに 社長・代表・取締役・工場長・専務・常務・〇代目・経営 など
 5 期限：納期・受注残・今期・来月・間に合わない など
業種は「候補（プロフィール・本文の語）」として付けるだけで確定しない（キーワードだけでタグを確定させない決まり）。
本人が読むため、投稿の全文・プロフィール・URL をそのまま残す。

使うもの
- 環境変数 X_BEARER_TOKEN（GitHub の Secrets）。無ければ何もせずに終わる。
- GET https://api.x.com/2/tweets/search/recent（直近7日。1投稿ごとに課金。同じ投稿は24時間以内なら1回分）
- control.yaml の x_pain: {run, max_posts_per_run, min_score}
- inputs/x_pain.csv（名前, 検索式, ねらい）

出力（out/x_pain/）
- posts.csv：取れた投稿すべて（印・点数・業種候補・全文・プロフィール・URL）。重複は投稿IDで1行
- deep.md：点数が min_score 以上の投稿を点数の高い順に。本人が読む用
- state.json：検索式ごとの since_id（次の回は新しい投稿だけ取る＝同じ投稿に二度払わない）
- log.md：回ごとの取れた件数・使った読み取り数・エラー
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

from researcher.targets import ROOT

OUT = ROOT / "out" / "x_pain"
API = "https://api.x.com/2/tweets/search/recent"
USAGE = OUT / "usage.json"
JST = timezone(timedelta(hours=9))
# 2026-11-17 課金ガード：読み取り件数の上限。control.yaml の値がこれを超えても、ここで頭打ちにする。
# 1日・1か月の上限は control.yaml の x_pain.max_posts_per_day / max_posts_per_month。書いていなければ 0＝読まない。
HARD_MAX_PER_RUN = 100
API_MIN_RESULTS = 10   # X の API は1回に最低10件を要求する。残りが10件未満なら読まずに止める（上限を超えないため）
MAX_429_RETRIES = 2

NUMBER = r"[0-9０-９一二三四五六七八九十百千]+(?:[,，.][0-9]+)?\s*(?:万円|億円|千万|万|億|円|件|日|週間|ヶ月|か月|カ月|人|社|台|トン)"
LOSS = r"(断|失|損|逃|止ま|止め|見送|飛ん|赤字|流れ|キャンセル|取られ|潰れ|廃業)"
MARKS = {
    "お金・件数": lambda t, b: bool(re.search(NUMBER, t)) and bool(re.search(LOSS, t)),
    "繰り返し": lambda t, b: bool(re.search(r"毎年|毎月|毎回|毎週|いつも|年々|ずっと|何年も|また今年|今年も|今回も|恒例", t)),
    "手を打って失敗": lambda t, b: bool(re.search(r"求人|広告|紹介会社|人材紹介|ハローワーク|ハロワ|媒体|採用費|紹介料|indeed|インディード|マイナビ|リクナビ", t, re.I))
    and bool(re.search(r"来ない|こない|応募ゼロ|応募がない|応募なし|採れない|とれない|辞め|やめ|定着しない|効果なし|効果がない|無駄|高すぎ|続かない", t)),
    "決める人": lambda t, b: bool(re.search(r"社長|代表|取締役|工場長|専務|常務|会長|経営者|経営|[二三四五六七2-7]代目|後継|CEO|COO|owner|president", b, re.I)),
    "期限": lambda t, b: bool(re.search(r"納期|受注残|今期|来期|来月|今月中|年内|期限|間に合わ|待ってもら|延期", t)),
}
INDUSTRY = {  # 候補として付けるだけ（確定しない）
    "金型": r"金型", "めっき": r"めっき|メッキ|鍍金", "熱処理": r"熱処理|焼入", "鋳造": r"鋳造|鋳物|ダイカスト",
    "鍛造": r"鍛造", "板金": r"板金", "プレス": r"プレス", "切削": r"切削|旋盤|マシニング|NC|フライス",
    "溶接": r"溶接", "造船": r"造船|ドック|船台", "町工場・製造（全般）": r"町工場|製作所|工場|製造業|ものづくり|モノづくり|加工",
    "建築・設計": r"建築|設計事務所|工務店", "デザイン": r"デザイン事務所|プロダクトデザイン",
}
ROLE = {  # 2026-11-16：投稿者の立場の候補（プロフィールの言葉の一致だけ。確定ではない）
    "社長・経営": r"社長|代表|取締役|経営|専務|常務|代目|CEO", "工場長・製造": r"工場長|製造部|現場|職人|オペレーター",
    "設計": r"設計|CAD|機械設計|メカ設計", "品質": r"品質|検査|QC|QA|品証", "生産技術": r"生産技術|生技|治具",
    "営業・見積": r"営業|見積", "3Dプリント": r"3D|積層|造形|AM",
}
FIELDS = ["id", "url", "created_at", "query", "score", "marks", "industry_candidates", "role_candidates", "username", "name", "bio",
          "followers", "likes", "retweets", "replies", "text", "first_seen"]


def load_cfg() -> dict:
    ctl = yaml.safe_load((ROOT / "control.yaml").read_text(encoding="utf-8")) or {}
    return ctl.get("x_pain") or {}


def load_usage() -> dict:
    """これまでの読み取り件数（JSTの日・月ごと）。初回は log.md の記録から作る。"""
    if USAGE.exists():
        return json.loads(USAGE.read_text(encoding="utf-8"))
    u = {"days": {}, "months": {}}
    log = OUT / "log.md"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            m = re.match(r"- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC：この回の読み取り (\d+) 件", line)
            if m:
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).astimezone(JST)
                n = int(m.group(2))
                u["days"][at.strftime("%Y-%m-%d")] = u["days"].get(at.strftime("%Y-%m-%d"), 0) + n
                u["months"][at.strftime("%Y-%m")] = u["months"].get(at.strftime("%Y-%m"), 0) + n
    return u


def save_usage(u: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps(u, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


class Allowance:
    """この回・今日・今月の残り件数。読むたびに台帳へ足し、すぐ保存する。"""

    def __init__(self, cfg: dict):
        self.usage = load_usage()
        now = datetime.now(JST)
        self.day, self.month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
        self.run_cap = min(HARD_MAX_PER_RUN, int(cfg.get("max_posts_per_run", 0) or 0))
        self.day_cap = int(cfg.get("max_posts_per_day", 0) or 0)
        self.month_cap = int(cfg.get("max_posts_per_month", 0) or 0)
        self.run_used = 0

    def left(self) -> int:
        return max(0, min(self.run_cap - self.run_used,
                          self.day_cap - self.usage["days"].get(self.day, 0),
                          self.month_cap - self.usage["months"].get(self.month, 0)))

    def add(self, n: int) -> None:
        self.run_used += n
        self.usage["days"][self.day] = self.usage["days"].get(self.day, 0) + n
        self.usage["months"][self.month] = self.usage["months"].get(self.month, 0) + n
        save_usage(self.usage)

    def describe(self) -> str:
        return (f"この回 {self.run_used}/{self.run_cap}・今日 {self.usage['days'].get(self.day, 0)}/{self.day_cap}"
                f"・今月 {self.usage['months'].get(self.month, 0)}/{self.month_cap}")


def load_queries() -> list[dict]:
    with open(ROOT / "inputs" / "x_pain.csv", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if (r.get("検索式") or "").strip() and not (r.get("回す") or "").startswith("保留")]


def judge(text: str, bio: str) -> tuple[int, list[str], list[str]]:
    marks = [k for k, fn in MARKS.items() if fn(text, bio)]
    inds = [k for k, rx in INDUSTRY.items() if re.search(rx, text + " " + bio)]
    return len(marks), marks, inds


def fetch(query: str, token: str, since_id: str | None, budget: int, allow: "Allowance") -> tuple[list[dict], dict, str | None, str]:
    """1つの検索式で、budget 件まで新しい投稿を取る。返り値：(投稿, ユーザー辞書, 最新ID, エラー)
    2026-11-17 課金ガード：毎回のリクエスト前に この回・今日・今月 の残りを確かめ、10件未満なら読まない。"""
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "a1-researcher"}
    params = {
        "query": query, "max_results": 100,
        "tweet.fields": "created_at,author_id,public_metrics,lang,conversation_id",
        "expansions": "author_id", "user.fields": "name,username,description,public_metrics,location",
    }
    if since_id:
        params["since_id"] = since_id
    tweets, users, newest, err = [], {}, None, ""
    retries = 0
    while budget > 0:
        n = min(100, budget, allow.left())
        if n < API_MIN_RESULTS:
            err = err or f"上限のため読まずに止めた（{allow.describe()}）"
            break
        params["max_results"] = n
        r = requests.get(API, headers=headers, params=params, timeout=30)
        if r.status_code == 429:
            retries += 1
            if retries > MAX_429_RETRIES:
                err = "status=429 を繰り返したので止めた"
                break
            reset = int(r.headers.get("x-rate-limit-reset", time.time() + 60))
            time.sleep(max(5, min(900, reset - time.time() + 2)))
            continue
        if r.status_code != 200:
            err = f"status={r.status_code} {r.text[:300]}"
            break
        j = r.json()
        data = j.get("data") or []
        for u in (j.get("includes") or {}).get("users") or []:
            users[u["id"]] = u
        tweets.extend(data)
        allow.add(len(data))
        budget -= len(data)
        meta = j.get("meta") or {}
        newest = newest or meta.get("newest_id")
        if not meta.get("next_token") or not data:
            break
        params["next_token"] = meta["next_token"]
    return tweets, users, newest, err


def main() -> int:
    cfg = load_cfg()
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / "log.md"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not cfg.get("run"):
        print("x_pain: run=false")
        return 0
    if not token:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"- {now}：X_BEARER_TOKEN が GitHub の Secrets に無いので取らなかった\n")
        print("x_pain: no token")
        return 0

    allow = Allowance(cfg)
    total_budget = allow.run_cap
    if allow.left() < API_MIN_RESULTS:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"- {now}：上限のため読まなかった（{allow.describe()}）\n")
        print(f"x_pain: cap reached, no read ({allow.describe()})")
        return 0
    min_score = int(cfg.get("min_score", 2))
    queries = load_queries()
    per_q = max(10, total_budget // max(1, len(queries)))
    state_p = OUT / "state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    posts_p = OUT / "posts.csv"
    rows: dict[str, dict] = {}
    if posts_p.exists():
        with open(posts_p, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows[r["id"]] = r

    used, lines = 0, []
    for q in queries:
        if used >= total_budget:
            lines.append(f"  - {q['名前']}：この回の上限 {total_budget} 件に達したので次の回へ")
            continue
        tw, users, newest, err = fetch(q["検索式"], token, state.get(q["名前"]), min(per_q, total_budget - used), allow)
        used += len(tw)
        new = 0
        for t in tw:
            u = users.get(t.get("author_id"), {})
            text = (t.get("text") or "").replace("\r", " ")
            bio = (u.get("description") or "").replace("\r", " ")
            score, marks, inds = judge(text, bio)
            m = t.get("public_metrics") or {}
            if t["id"] in rows:
                continue
            new += 1
            rows[t["id"]] = {
                "id": t["id"], "url": f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
                "created_at": t.get("created_at", ""), "query": q["名前"], "score": score, "marks": "・".join(marks),
                "industry_candidates": "・".join(inds), "role_candidates": "・".join(k for k, rx in ROLE.items() if re.search(rx, bio)), "username": u.get("username", ""), "name": u.get("name", ""),
                "bio": bio, "followers": (u.get("public_metrics") or {}).get("followers_count", ""),
                "likes": m.get("like_count", ""), "retweets": m.get("retweet_count", ""), "replies": m.get("reply_count", ""),
                "text": text, "first_seen": now,
            }
        if newest:
            state[q["名前"]] = newest
        lines.append(f"  - {q['名前']}：取れた {len(tw)} 件（新しい {new} 件）" + (f"／エラー {err}" if err else ""))

    state_p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    allrows = sorted(rows.values(), key=lambda r: (int(r["score"] or 0), r["created_at"]), reverse=True)
    with open(posts_p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(allrows)

    deep = [r for r in allrows if int(r["score"] or 0) >= min_score]
    from collections import Counter
    tally = Counter((r["query"], r.get("role_candidates") or "立場不明", r["industry_candidates"] or "業種不明") for r in allrows)
    md = ["# X の困りごと — 深い声（点数の高い順）", "",
          f"更新：{now}。全 {len(allrows)} 件のうち、深さの印が {min_score} 個以上の {len(deep)} 件。",
          "印：お金・件数／繰り返し／手を打って失敗／決める人／期限（1つ1点）。業種・立場は候補（言葉の一致だけで確定ではない）。", "",
          "## 仮説×立場×業種の件数（交差は求人票・ipros_allと会社の型で突き合わせる。X単独では「未交差」）", ""]
    md += [f"- {q}｜{role}｜{ind}：{n}件" for (q, role, ind), n in tally.most_common(60)] + [""]
    for r in deep[:300]:
        md += [f"## {r['score']}点｜{r['marks']}｜{r['industry_candidates'] or '業種の手がかりなし'}",
               f"- {r['name']}（@{r['username']}・フォロワー {r['followers']}・立場候補 {r.get('role_candidates') or 'なし'}）：{r['bio'][:160]}",
               f"- {r['created_at'][:10]}｜検索：{r['query']}｜{r['url']}",
               f"> {r['text']}", ""]
    (OUT / "deep.md").write_text("\n".join(md), encoding="utf-8")
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"- {now}：この回の読み取り {used} 件（上限 {total_budget}）［{allow.describe()}］\n" + "\n".join(lines) + "\n")
    print(f"x_pain: read {used}, total {len(allrows)}, deep {len(deep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
