"""Read researcher/control.yaml and print the GitHub Actions matrix of work still to do.
A topic runs only while `run: true` and companies remain. Prints JSON:
{"include": [{"topic": ..., "shard": i, "shards": n, "limit": k}, ...], "remaining": N}
"""
from __future__ import annotations

import json
import sys

import yaml

from researcher.targets import ROOT, done as _done, load_targets as load_companies, load_topic


def remaining(topic_name: str) -> int:
    done = ROOT / "out" / topic_name / "companies"
    t = load_topic(topic_name)
    return sum(1 for c in load_companies(t) if not _done(done, c, t.get("refresh_hours")))


def main() -> int:
    ctl = yaml.safe_load((ROOT / "control.yaml").read_text(encoding="utf-8")) or {}
    include, total = [], 0
    for name, cfg in (ctl.get("topics") or {}).items():
        if not cfg or not cfg.get("run"):
            continue
        b = load_topic(name).get("build")
        if b and not (ROOT / "out" / name / "BUILD_DONE").exists():
            total += 1  # list still being built: keep the loop alive
            # 一覧づくりが途中でも、照合済みの分があれば先に会社の調査を回す（全件待ちで止めない）
            inp = ROOT / (load_topic(name).get("input") or "").replace("researcher/", "", 1)
            try:
                has_rows = inp.exists() and sum(1 for _ in open(inp, encoding="utf-8-sig")) > 1
            except Exception:
                has_rows = False
            if not has_rows:
                continue
        # 2026-11-24 入力のCSVが無いテーマ（am_soft_peers_jp）で plan ごと落ち、11/12以降は調査が毎回0件だった。無いテーマは飛ばして名前を出す
        try:
            left = remaining(name)
        except FileNotFoundError as e:
            print(f"skip {name}: input missing ({e.filename})", file=sys.stderr)
            continue
        total += left
        if left == 0:
            continue
        shards = max(1, min(int(cfg.get("shards", 4)), 20, left))
        for i in range(shards):
            include.append({"topic": name, "shard": i, "shards": shards, "limit": int(cfg.get("per_run", 10))})
    # 2026-11-24 課金の止め：1回の実行で立てる調査の本数に上限を置く（無いと82本×60分が一度に立つ）。既定4本、PLAN_MAX_JOBS で下げられる（上げられるのは本人の指示のときだけ）
    import os
    cap = max(0, min(int(os.environ.get("PLAN_MAX_JOBS", "4") or 4), 4))
    if len(include) > cap:
        print(f"plan: {len(include)} jobs -> capped at {cap}", file=sys.stderr)
        include = include[:cap]
    print(json.dumps({"include": include, "remaining": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
