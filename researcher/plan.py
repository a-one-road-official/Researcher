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
    return sum(1 for c in load_companies(load_topic(topic_name)) if not _done(done, c))


def main() -> int:
    ctl = yaml.safe_load((ROOT / "control.yaml").read_text(encoding="utf-8")) or {}
    include, total = [], 0
    for name, cfg in (ctl.get("topics") or {}).items():
        if not cfg or not cfg.get("run"):
            continue
        left = remaining(name)
        total += left
        if left == 0:
            continue
        shards = max(1, min(int(cfg.get("shards", 4)), 20, left))
        for i in range(shards):
            include.append({"topic": name, "shard": i, "shards": shards, "limit": int(cfg.get("per_run", 10))})
    print(json.dumps({"include": include, "remaining": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
