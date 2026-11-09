"""名簿の門番（2026-11-09 本人の怒りから）：名簿を「対象」として出す前に、本人の条件を1つずつ、1社ずつ引用で確かめる。

起きた失敗：本人の条件は「一都三県・資本金1億円以上・製造業・自動車除く」だったのに、プログラムに「製造業」が入っておらず、
不動産・IT・卸が半分以上混ざった58社を「製造業58社」として報告した。条件の写し漏れと、中身を見ない報告の2つが原因。

ここでやること：
 1. 条件ごとに「その会社でその条件を満たす引用」があるかを確かめる。引用の無い条件が1つでもあれば、その会社は名簿から外す（理由つき）。
 2. 外した会社と理由、条件ごとの欠け件数を verify.md に書く。
 3. 通った会社から無作為に10社、条件ごとの引用を並べて verify.md に書く。Claude は報告の前にこの10社を必ず読む（METHOD 0-1）。
 4. 1つの条件で欠けが2割を超えたら VERIFY_ALERT を置く（条件の写し漏れや読み取りの壊れの疑い。報告の前に直す）。
"""
from __future__ import annotations

import random
from pathlib import Path


def verify(rows: list[dict], conditions: dict, out_dir: Path, name_key: str = "name") -> list[dict]:
    """conditions: {条件名: (判定関数 row->bool, 引用を返す関数 row->str)}。全条件を満たす行だけを返す。"""
    passed, failed = [], []
    miss = {c: 0 for c in conditions}
    for r in rows:
        bad = [c for c, (ok, _) in conditions.items() if not ok(r)]
        for c in bad:
            miss[c] += 1
        (failed if bad else passed).append((r, bad))
    n = len(rows) or 1
    md = ["# 名簿の門番（verify_list.py）", "", f"- 候補 {len(rows)}社 → 通過 {len(passed)}社・外した {len(failed)}社", "",
          "## 条件ごとの欠け（引用で確かめられなかった件数）"]
    md += [f"- {c}：{k}社（{k * 100 // n}%）" for c, k in miss.items()]
    alert = [c for c, k in miss.items() if k / n > 0.2]
    if alert:
        md += ["", f"**警告：{'・'.join(alert)} の欠けが2割を超えた。条件の写し漏れか読み取りの壊れを疑い、報告の前に直す。**"]
    md += ["", "## 無作為10社（報告の前にClaudeが必ず読む）"]
    for r, _ in random.Random(len(rows)).sample(passed, min(10, len(passed))):
        md.append(f"### {r.get(name_key, '')}")
        md += [f"- {c}：{q(r)[:160]}" for c, (_, q) in conditions.items()]
    md += ["", "## 外した会社と理由"]
    md += [f"- {r.get(name_key, '')}：{'・'.join(b)} が確かめられない" for r, b in failed]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "verify.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    flag = out_dir / "VERIFY_ALERT"
    if alert:
        flag.write_text("・".join(alert) + "\n", encoding="utf-8")
    elif flag.exists():
        flag.unlink()
    return [r for r, _ in passed]
