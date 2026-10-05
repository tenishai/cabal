"""Compare an A2A compatibility kit run with cabal's baseline (#261).

Reads the kit's report (the path to its `reports/compatibility.json` is the one
argument) and `baseline.json`, which sits next to this script: a map from
requirement id to the reason cabal is expected to fail it.
Exits 1 when a requirement fails that the baseline does not list (a
regression, or a new gap to fix or record), or when a baseline entry has no
reason. A listed requirement that now
passes is reported, so the baseline can shrink, but does not fail the job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASELINE = Path(__file__).with_name("baseline.json")


def unexplained(baseline: dict[str, str]) -> list[str]:
    """Baseline entries without a reason: each one must say why."""
    return sorted(rid for rid, reason in baseline.items() if not str(reason).strip())


def compare(report: dict, baseline: dict[str, str]) -> tuple[list[str], list[str]]:
    """(failures the baseline does not list, listed failures that now pass)."""
    per = report.get("per_requirement", {})
    failing = {rid for rid, r in per.items() if r.get("status") == "FAIL"}
    unexpected = sorted(failing - baseline.keys())
    fixed = sorted(rid for rid in baseline if per.get(rid, {}).get("status") == "PASS")
    return unexpected, fixed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_baseline.py <compatibility.json>", file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text())
    baseline = json.loads(BASELINE.read_text())
    if missing := unexplained(baseline):
        print(f"baseline entries without a reason: {', '.join(missing)}")
        return 1
    unexpected, fixed = compare(report, baseline)
    per = report.get("per_requirement", {})
    summary = report.get("summary", {})
    print(f"MUST {summary.get('must_compatibility')}, overall {summary.get('overall_compatibility')}")
    for rid in fixed:
        print(f"now passing, remove from the baseline: {rid}")
    for rid in unexpected:
        errors = "; ".join(per[rid].get("errors", []))[:300]
        print(f"FAIL not in the baseline: {rid} ({per[rid].get('level')}): {errors}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
