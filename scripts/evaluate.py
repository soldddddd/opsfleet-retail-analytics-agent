import argparse
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

GROUPS = {
    "correctness": {"test_numeric_sql", "test_agent"},
    "safety": {"test_foundation", "test_data_security", "test_reports"},
    "resilience": {"test_adapters", "test_agent", "test_cli"},
    "observability": {"test_foundation", "test_adapters", "test_agent"},
    "cli_and_report_workflow": {"test_cli", "test_reports"},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".runtime/evaluation.json"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        xml = Path(directory) / "results.xml"
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--disable-socket",
                "-m",
                "not live",
                f"--junitxml={xml}",
            ],
            check=False,
        )
        cases = []
        if xml.exists():
            for case in ET.parse(xml).iter("testcase"):
                status = "passed"
                for label in ("skipped", "failure", "error"):
                    if case.find(label) is not None:
                        status = label
                cases.append(
                    {
                        "suite": case.attrib.get("classname", ""),
                        "name": case.attrib.get("name", ""),
                        "seconds": float(case.attrib.get("time", 0)),
                        "status": status,
                    }
                )
        groups = {}
        for name, suites in GROUPS.items():
            selected = [c for c in cases if str(c["suite"]).split(".")[-1] in suites]
            groups[name] = {
                "tests": len(selected),
                "passed": sum(c["status"] == "passed" for c in selected),
                "nonpassing": sum(c["status"] != "passed" for c in selected),
                "note": "Overlapping test groups; not a model-quality score.",
            }
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "mode": "offline",
            "status": "passed" if run.returncode == 0 and cases else "failed",
            "test_count": len(cases),
            "dimensions": groups,
            "cases": cases,
            "live_acceptance": "not_run",
            "report_quality": "human rubric required; unscored",
            "model_flexibility": "mock control-flow and SQL oracles only; live review separate",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Evaluation evidence: {args.output}")
        return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
