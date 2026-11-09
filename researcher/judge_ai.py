"""決めきれない会社（judged.csv の「要確認」）だけを、別のAIに読ませて判定を埋める。

本人の指示（2026-11-09）：判断の工程は Claude でなく別のAIでよい。有料APIには課金しない。
→ GitHub Models（GitHub Actions の GITHUB_TOKEN で使える無料枠）を使う。キーも課金も不要。
無料枠の回数制限に当たったら、そこで止めて次の回に続ける（判定済みはキャッシュ ai_judged.json に残す）。
判定の規則は本人の基準そのまま：日本に誰かいる（日本法人・日本拠点・代理店・取扱い商社・国内通販の正規販売店）→除外、
常識的に一人の会社と組まない大手→除外、それ以外→余地あり。根拠の引用とURLを必ず返させる。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time

import requests
import yaml

from researcher.targets import ROOT, load_topic

ENDPOINT = "https://models.github.ai/inference/chat/completions"
MODEL = os.environ.get("JUDGE_MODEL", "openai/gpt-4.1-mini")
MAX_CALLS = int(os.environ.get("JUDGE_MAX_CALLS", "120"))

RULE = (
    "あなたは海外のAM原料メーカーを、日本で一人の会社（エーワンロード、在庫なし・手持ち約20万円）が担いで売れるかで仕分ける調査員です。"
    "与えられた引用だけを根拠に、次の規則で1つ選びます。"
    "除外（日本に取扱いあり）：日本法人・日本拠点・日本の代理店・日本の取扱い商社・日本の通販での正規販売店が、この会社の製品について実在すると引用から読める。"
    "『日本の代理店を募集』『日本へ直送できる』『日本の会社の話題』だけなら日本に誰かいる証拠にはならない。"
    "除外（大手）：上場企業、従業員が数千人規模、大企業グループの子会社など、常識的に一人の会社と組まない。"
    "余地あり：上のどちらにも当たらない。"
    "必ずJSONだけを返す：{\"verdict\":\"除外（日本に取扱いあり）|除外（大手）|余地あり\",\"reason\":\"日本語1文\",\"evidence_url\":\"根拠のURLか空\"}"
)


def _call(token: str, prompt: str) -> dict | None:
    r = requests.post(ENDPOINT, timeout=60, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"model": MODEL, "temperature": 0, "response_format": {"type": "json_object"},
                            "messages": [{"role": "system", "content": RULE}, {"role": "user", "content": prompt}]})
    if r.status_code == 429:
        return {"_stop": True}
    if r.status_code != 200:
        print(f"judge http {r.status_code}: {r.text[:200]}", flush=True)
        return None
    try:
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        return None


def judge_topic(name: str, token: str, budget: list[int]) -> None:
    base = ROOT / "out" / name
    path = base / "judged.csv"
    if not path.exists():
        return
    cache_p = base / "ai_judged.json"
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    recs = {}
    for p in (base / "companies").glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            recs[r.get("name")] = r
        except Exception:
            pass
    cfg = load_topic(name)["judge"]
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        nm = row["社名"]
        if not row["判定"].startswith("要確認"):
            continue
        if nm not in cache and budget[0] > 0:
            ev = (recs.get(nm) or {}).get("evidence", {})
            items = (ev.get(cfg["japan"], [])[:6] + ev.get(cfg["scale"], [])[:3] + ev.get("検索結果の抜粋", [])[:4])
            quotes = "\n".join(f"- 「{i.get('quote', '')[:300]}」（{i.get('url', '')}）" for i in items)
            res = _call(token, f"会社：{nm}（{row['公式URL']}）\n引用：\n{quotes}")
            budget[0] -= 1
            if res and res.get("_stop"):
                budget[0] = 0
            elif res and res.get("verdict"):
                cache[nm] = res
            time.sleep(1)
        if nm in cache:
            c = cache[nm]
            row["判定"] = f"{c['verdict']}（AI判定）"
            row["判定の根拠（引用）"] = c.get("reason", "")
            row["根拠URL"] = c.get("evidence_url", "") or row["根拠URL"]
    cache_p.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)
    print(f"judge {name}: cached {len(cache)}", flush=True)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("no GITHUB_TOKEN; skip judge")
        return 0
    ctl = yaml.safe_load((ROOT / "control.yaml").read_text(encoding="utf-8")) or {}
    budget = [MAX_CALLS]
    for name, c in (ctl.get("topics") or {}).items():
        try:
            if c and load_topic(name).get("judge"):
                judge_topic(name, token, budget)
        except Exception as e:
            print(f"judge {name} failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
