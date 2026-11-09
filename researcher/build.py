"""Run the list-building step of every switched-on topic that has one (topic key
`build: <module>`), e.g. IR1000本ノック's screening of all listed companies by margin.
Resumable and time-boxed; researcher.plan skips a topic until its build is done."""
from __future__ import annotations

import importlib
import sys

import yaml

from researcher.targets import ROOT, load_topic


def pending() -> list[str]:
    ctl = yaml.safe_load((ROOT / "control.yaml").read_text(encoding="utf-8")) or {}
    out = []
    for name, cfg in (ctl.get("topics") or {}).items():
        if cfg and cfg.get("run"):
            b = load_topic(name).get("build")
            if b and not importlib.import_module(b).is_done():
                out.append(b)
    return out


def main() -> int:
    # 2026-11-09 本人の最優先（表彰リスト起点の芋づる）を先に回す。他の名簿づくりは止めずに後ろで続ける
    for b in sorted(pending(), key=lambda b: b != "researcher.build_award_chain"):
        print(f"build {b}", flush=True)
        importlib.import_module(b).main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
