"""Writes n8n/workflows/*.json from the definitions; --check fails when the committed files are out of date."""

from __future__ import annotations

import argparse
import json
import sys

from lab.paths import N8N_DIR
from lab.workflows.defs import all_workflows

OUT_DIR = N8N_DIR / "workflows"


def render() -> dict[str, str]:
    return {name: json.dumps(wf.to_json(), indent=2, ensure_ascii=False) + "\n" for name, wf in all_workflows().items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if n8n/workflows is not what the definitions build")
    args = parser.parse_args(argv)
    rendered = render()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale = [n for n, t in rendered.items() if not (OUT_DIR / n).exists() or (OUT_DIR / n).read_text(encoding="utf-8") != t]
    if args.check:
        if stale:
            print("out of date: " + ", ".join(stale) + "  (run: uv run python scripts/build_workflows.py)")
        return 1 if stale else 0
    for name, text in rendered.items():
        (OUT_DIR / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(rendered)} workflows to n8n/workflows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
