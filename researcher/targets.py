"""Topic and target loading, kept free of network libraries so the planner and the
merger run with pyyaml alone. A target is one row of a topic's input CSV: a company
(kind: company) or a market / product category (kind: market).
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def slug(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", unicodedata.normalize("NFKC", name)).strip("_")[:40]
    return f"{s}_{hashlib.md5(name.encode()).hexdigest()[:6]}"


def done(d: Path, c: dict) -> bool:
    p = d / f"{slug(c['name'])}.json"
    if not p.exists():
        return False
    try:
        return "error" not in json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False


def load_topic(name: str) -> dict:
    t = yaml.safe_load((ROOT / "topics" / f"{name}.yaml").read_text(encoding="utf-8"))
    t.setdefault("kind", "company")
    return t


def load_targets(topic: dict) -> list[dict]:
    cols = topic["columns"]
    rows = []
    with open(ROOT.parent / topic["input"], encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            get = lambda k: (r.get(cols.get(k, ""), "") or "").strip()
            name = get("name")
            if not name:
                continue
            aliases = [a.strip() for a in re.split(r"[;；|]", get("aliases")) if a.strip()]
            rows.append({"name": name, "url": get("url"), "aliases": aliases, "context": get("context"),
                         "en": get("en")})
    return rows


def out_dir(topic_name: str) -> Path:
    return ROOT / "out" / topic_name / "companies"
