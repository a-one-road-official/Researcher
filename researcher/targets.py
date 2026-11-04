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


def done(d: Path, c: dict, max_age_hours: float | None = None) -> bool:
    """Done = researched without error, and (for refreshing topics) not older than max_age_hours."""
    p = d / f"{slug(c['name'])}.json"
    if not p.exists():
        return False
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if "error" in rec:
        return False
    if max_age_hours:
        from datetime import datetime, timezone
        try:
            t = datetime.fromisoformat(rec.get("researched_at", ""))
        except ValueError:
            return False
        return (datetime.now(timezone.utc) - t).total_seconds() < float(max_age_hours) * 3600
    return True


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
