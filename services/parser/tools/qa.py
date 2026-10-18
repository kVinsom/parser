"""One command: regenerate contracts, lint, run tests, then benchmark if checks pass."""

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.benchmark import (  # noqa: E402
    BenchmarkConfig,
    add_arguments,
    config_from_args,
    new_folder,
    run_benchmark,
)


def junit_counts(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    return {
        "tests": len(cases),
        "failed": sum(c.find("failure") is not None for c in cases),
        "errors": sum(c.find("error") is not None for c in cases),
        "skipped": sum(c.find("skipped") is not None for c in cases),
    }


def run_checks(folder: Path) -> dict:
    commands = [
        ("contracts", [sys.executable, "tools/generate_proto.py"]),
        (
            "lint",
            [sys.executable, "-m", "ruff", "check", "vpars_parser", "tools", "tests", "--no-cache"],
        ),
        (
            "format",
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                "vpars_parser",
                "tools",
                "tests",
                "--no-cache",
            ],
        ),
        (
            "tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={folder / 'tests.xml'}",
            ],
        ),
    ]
    report = {"passed": False, "steps": []}
    for name, command in commands:
        started = time.perf_counter()
        with (folder / f"{name}.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                    check=False,
                )
                code = result.returncode
            except subprocess.TimeoutExpired:
                code = 124
        report["steps"].append(
            {
                "name": name,
                "exit_code": code,
                "elapsed_seconds": time.perf_counter() - started,
                "log": f"{name}.log",
            }
        )
        if code:
            return report
    try:
        counts = junit_counts(folder / "tests.xml")
        report["tests"] = counts
        report["passed"] = counts["tests"] > 0 and not any(
            counts[k] for k in ("failed", "errors", "skipped")
        )
    except (OSError, ET.ParseError):
        report["junit_error"] = "MissingOrInvalidJUnitReport"
    return report


def write_qa_report(folder: Path, report: dict):
    lines = [
        "# Parser QA",
        "",
        f"State: **{report['state']}**",
        "",
        f"Mode: {report['mode']}. Live requests require live/both mode.",
        "",
    ]
    for step in report.get("checks", {}).get("steps", []):
        lines.append(
            f"- {step['name']}: exit {step['exit_code']}; "
            f"{step['elapsed_seconds']:.3f} s; [{step['log']}]({step['log']})"
        )
    if "tests" in report.get("checks", {}):
        lines.extend(["", f"Tests: {report['checks']['tests']}", "", "[JUnit report](tests.xml)"])
    if "benchmark_state" in report:
        lines.extend(
            [
                "",
                f"Benchmark: **{report['benchmark_state']}**. "
                "[Results](benchmark/report.md) · [JSON](benchmark/report.json)",
            ]
        )
    else:
        lines.extend(["", "Benchmark not run; earlier checks did not finish successfully."])
    for name, content in (
        ("report.json", json.dumps(report, indent=2) + "\n"),
        ("report.md", "\n".join(lines) + "\n"),
    ):
        temporary = folder / (name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(folder / name)


def run_qa(config: BenchmarkConfig, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(UTC).isoformat(), "mode": config.mode, "state": "running"}
    write_qa_report(folder, report)
    try:
        report["checks"] = run_checks(folder)
        write_qa_report(folder, report)
        if not report["checks"]["passed"]:
            report["state"] = "failed"
        else:
            benchmark = run_benchmark(config, folder / "benchmark")
            report["benchmark_state"] = benchmark["state"]
            report["state"] = benchmark["state"]
    except KeyboardInterrupt:
        report["state"] = "interrupted"
    except Exception as exc:
        report["state"] = "error"
        report["error_type"] = type(exc).__name__
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_qa_report(folder, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    try:
        config = config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    folder = new_folder("qa")
    report = run_qa(config, folder)
    print(f"QA {report['state']}: {folder / 'report.md'}")
    return 0 if report["state"] == "passed" else 130 if report["state"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
