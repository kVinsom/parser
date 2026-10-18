"""Repeatable tests and bounded Vinted benchmarks; all artifacts stay here."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import platform
import statistics
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import vinted
from artifacts import read_html, save_html
from vinted_regions import select_regions

ROOT = Path(__file__).resolve().parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def distribution(values: list[float]) -> dict:
    """Nearest-rank p95, with no fabricated zero latency for an empty sample."""
    if not values:
        return {key: None for key in ("min", "mean", "median", "p95", "max")}
    ordered = sorted(values)
    return {"min": min(values), "mean": round(statistics.mean(values), 6),
            "median": round(statistics.median(values), 6),
            "p95": ordered[math.ceil(0.95 * len(ordered)) - 1], "max": max(values)}


def summarize(rows: list[dict], planned: int) -> dict:
    successful = [row for row in rows if row["outcome"] == "page_loaded"]
    return {
        "planned": planned, "attempted": len(rows), "successful": len(successful),
        "success_percent": round(100 * len(successful) / len(rows), 2) if rows else None,
        "outcomes": dict(Counter(row["outcome"] for row in rows)),
        "latency_seconds": distribution([row["elapsed_seconds"] for row in rows]),
        "successful_latency_seconds": distribution([row["elapsed_seconds"] for row in successful]),
        "items_per_success": distribution([row["item_count"] for row in successful]),
    }


class TimedTestResult(unittest.TextTestResult):
    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        if not hasattr(self, "case_times"):
            self.case_times = {}
        self.case_times[test.id()] = time.perf_counter() - self._started
        super().stopTest(test)


def run_tests(folder: Path) -> dict:
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
    with (folder / "tests.log").open("w", encoding="utf-8") as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=TimedTestResult).run(suite)
    elapsed = time.perf_counter() - started
    def by_parent(entries):
        messages = {}
        for case, message in entries:
            identifier = getattr(case, "test_case", case).id()
            messages[identifier] = messages.get(identifier, "") + case.id() + "\n" + message + "\n"
        return messages
    failures = by_parent(result.failures)
    errors = by_parent(result.errors)
    skips = by_parent(result.skipped)
    expected = by_parent(result.expectedFailures)
    unexpected = {case.id() for case in result.unexpectedSuccesses}
    durations = getattr(result, "case_times", {})
    passed = (result.wasSuccessful() and result.testsRun > 0 and not result.skipped
              and not result.expectedFailures)
    xml = ET.Element("testsuite", name="bypass", tests=str(result.testsRun),
                     failures=str(len(failures) + len(unexpected)), errors=str(len(errors)),
                     skipped=str(len(skips) + len(expected)), time=f"{elapsed:.6f}")
    for identifier, duration in durations.items():
        classname, _, name = identifier.rpartition(".")
        case = ET.SubElement(xml, "testcase", classname=classname, name=name, time=f"{duration:.6f}")
        for kind, messages in (("failure", failures), ("error", errors), ("skipped", skips), ("skipped", expected)):
            if identifier in messages:
                ET.SubElement(case, kind).text = messages[identifier]
        if identifier in unexpected:
            ET.SubElement(case, "failure").text = "Unexpected success of a test marked as expected failure"
    ET.indent(xml)
    ET.ElementTree(xml).write(folder / "tests.xml", encoding="utf-8", xml_declaration=True)
    return {"status": "passed" if passed else "failed", "total": result.testsRun,
            "failures": len(failures), "errors": len(errors), "skipped": len(skips),
            "expected_failures": len(expected), "unexpected_successes": len(unexpected),
            "elapsed_seconds": round(elapsed, 3), "log": str(folder / "tests.log"),
            "junit": str(folder / "tests.xml")}


def run_benchmark(regions, *, rounds: int, timeout: int, pause: float, endpoint: str,
                  folder: Path, update, block_media: bool = True, fetch=None, sleep=None) -> dict:
    fetch = fetch or vinted.fetch_catalog
    sleep = sleep or time.sleep
    result = {"status": "running", "attempts": [], "stopped_regions": {}, "rounds": rounds,
              "planned": rounds * len(regions), "elapsed_seconds": 0, "parser": []}
    started = time.perf_counter()

    def checkpoint():
        result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        result["summary"] = summarize(result["attempts"], result["planned"])
        result["regions"] = {
            region.code: summarize([row for row in result["attempts"] if row["region"] == region.code], rounds)
            for region in regions
        }
        result["observed_requests_per_minute"] = (
            round(len(result["attempts"]) * 60 / result["elapsed_seconds"], 3)
            if result["elapsed_seconds"] else None)
        update(result)

    checkpoint()
    try:
        for round_number in range(1, rounds + 1):
            for region in regions:
                if region.code in result["stopped_regions"]:
                    continue
                if result["attempts"]:
                    sleep(pause)
                attempt = fetch(region, endpoint=endpoint, timeout=timeout, block_media=block_media)
                row = {"round": round_number, "recorded_at": now(), **attempt.summary()}
                # Preserve only the first page per region; no cookies enter reports.
                snapshot = folder / (region.code + ("" if attempt.outcome == "page_loaded" else "-failed") + ".html.gz")
                if attempt.body and not snapshot.exists():
                    save_html(snapshot, attempt.body)
                    row["html_path"] = str(snapshot)
                result["attempts"].append(row)
                print(f"BENCH {len(result['attempts'])}/{result['planned']} {region.code} "
                      f"round={round_number}: {attempt.outcome}; {attempt.elapsed_seconds}s; "
                      f"items={attempt.item_count}", flush=True)
                if attempt.outcome in ("blocked", "rate_limited"):
                    result["stopped_regions"][region.code] = attempt.outcome
                checkpoint()
                if attempt.outcome == "service_error":
                    result["status"] = "stopped_service_error"
                    checkpoint()
                    return result
        result["status"] = "completed" if len(result["attempts"]) == result["planned"] else "incomplete"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        checkpoint()
        raise
    checkpoint()
    return result


def benchmark_parser(benchmark: dict, regions, folder: Path, iterations: int, update) -> None:
    for region in regions:
        snapshot = folder / (region.code + ".html.gz")
        first = next((row for row in benchmark["attempts"]
                      if row["region"] == region.code and row["outcome"] == "page_loaded"), None)
        if not first or first["outcome"] != "page_loaded" or not snapshot.is_file():
            continue
        body = read_html(snapshot)
        samples = []
        outcomes = []
        # The file read is excluded; each iteration creates a fresh result/parser.
        for _ in range(iterations):
            started = time.perf_counter()
            parsed = vinted.classify(vinted.CatalogResult(region.code, region.catalog_url()), region,
                                     first["final_url"], first["solution_status"], body)
            samples.append(round((time.perf_counter() - started) * 1000, 6))
            outcomes.append(parsed.outcome == "page_loaded" and parsed.item_count == first["item_count"])
        benchmark["parser"].append({"region": region.code, "iterations": iterations,
                                    "response_bytes": first["response_bytes"],
                                    "consistent": all(outcomes), "duration_ms": distribution(samples)})
        update(benchmark)


def quality_checks(benchmark: dict, min_success: float, max_p95: float | None) -> list[dict]:
    summary = benchmark["summary"]
    checks = [{"name": "all_planned_requests_completed",
               "passed": benchmark["status"] == "completed" and summary["attempted"] == summary["planned"]},
              {"name": "minimum_success_rate", "target_percent": min_success,
               "actual_percent": summary["success_percent"],
               "passed": summary["success_percent"] is not None and summary["success_percent"] >= min_success},
              {"name": "parser_consistency", "passed": bool(benchmark["parser"])
               and all(row["consistent"] for row in benchmark["parser"])}]
    if max_p95 is not None:
        actual = summary["successful_latency_seconds"]["p95"]
        checks.append({"name": "maximum_successful_p95", "target_seconds": max_p95,
                       "actual_seconds": actual, "passed": actual is not None and actual <= max_p95})
    return checks


def write_report(folder: Path, report: dict) -> None:
    temporary = folder / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(folder / "report.json")
    lines = ["# Автоматичні тести та бенчмарк", "", f"Початок (UTC): {report['started_at']}",
             f"Стан: **{report['status']}**", ""]
    tests = report.get("tests")
    if tests:
        lines.extend([f"Тести: {tests['status']}; загалом {tests['total']}, помилок {tests['errors']}, "
                      f"провалів {tests['failures']}, пропущено {tests['skipped']}; {tests['elapsed_seconds']} с.",
                      "[Лог тестів](tests.log) · [JUnit XML](tests.xml)", ""])
    benchmark = report.get("benchmark")
    if benchmark and "summary" in benchmark:
        summary = benchmark["summary"]
        lines.extend([f"Запити: {summary['attempted']}/{summary['planned']}; успішно {summary['successful']}; "
                      f"частка успіху серед виконаних: {summary['success_percent']}%.",
                      f"Час мережевого бенчмарку: {benchmark['elapsed_seconds']} с; "
                      f"спостережувана швидкість: {benchmark['observed_requests_per_minute']} запитів/хв.", "",
                      "| Регіон | Виконано/план | Успішно | Медіана, с | p95, с | Мін., с | Макс., с |",
                      "|---|---:|---:|---:|---:|---:|---:|"])
        def number(value):
            return "—" if value is None else f"{value:.3f}"
        for code, stats in benchmark["regions"].items():
            latency = stats["successful_latency_seconds"]
            lines.append(f"| {code} | {stats['attempted']}/{stats['planned']} | {stats['successful']} "
                         f"| {number(latency['median'])} | {number(latency['p95'])} "
                         f"| {number(latency['min'])} | {number(latency['max'])} |")
        if benchmark["stopped_regions"]:
            lines.extend(["", "Зупинені регіони: " + json.dumps(benchmark["stopped_regions"])])
        if benchmark["parser"]:
            lines.extend(["", "Час локальної обробки отриманого HTML (без читання файлу та мережі):", "",
                          "| Регіон | Повторів | Розмір HTML, МБ | Медіана, мс | p95, мс | Узгоджені результати |",
                          "|---|---:|---:|---:|---:|---|"])
            for row in benchmark["parser"]:
                latency = row["duration_ms"]
                lines.append(f"| {row['region']} | {row['iterations']} | {row['response_bytes'] / 1e6:.2f} "
                             f"| {latency['median']:.3f} | {latency['p95']:.3f} | {row['consistent']} |")
        lines.extend(["", "Запити виконуються послідовно, з паузою між ними. Час запиту включає",
                      "браузерну навігацію, передавання HTML та перевірку вмісту. Швидкість враховує",
                      "паузи й запис звітів, без локального бенчмарку обробки HTML. Це спостереження, а не гранична пропускна здатність.",
                      "Медіана та p95 у таблиці — тільки для успішних запитів; JSON також містить час усіх спроб.",
                      "p95 обчислюється методом найближчого рангу; для малої вибірки дорівнює максимуму.",
                      "Кількість товарів стосується сторінки; CAPTCHA та HTTP 200 без товарів не є успіхом.", ""])
    if report.get("checks"):
        lines.extend(["Перевірки критеріїв:", ""])
        lines.extend(f"- {check['name']}: {'PASS' if check['passed'] else 'FAIL'}" for check in report["checks"])
    if report.get("error"):
        lines.extend(["", report["error"]])
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Automatic tests and Vinted benchmarks (UK, FR, IT, DE)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true", help="Only local tests, without Docker or external requests")
    modes.add_argument("--benchmark-only", action="store_true", help="Skip local tests")
    parser.add_argument("--regions", nargs="+", default=["all"])
    parser.add_argument("--rounds", type=int, default=3, help="1-10 rounds, one request per region per round")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--pause", type=float, default=1, help="0-30 seconds between requests")
    parser.add_argument("--parse-iterations", type=int, default=10)
    parser.add_argument("--min-success-rate", type=float, default=100)
    parser.add_argument("--max-p95", type=float, help="Optional maximum successful-request p95 in seconds")
    parser.add_argument("--byparr-url")
    parser.add_argument("--load-media", action="store_true", help="Include images, media and fonts for comparison")
    args = parser.parse_args(argv)
    try:
        args.selected_regions = select_regions(args.regions)
        if not 1 <= args.rounds <= 10 or not 1 <= args.timeout <= 300 or not 1 <= args.parse_iterations <= 100:
            raise ValueError("Rounds: 1-10; timeout: 1-300; parse iterations: 1-100")
        if not math.isfinite(args.pause) or not 0 <= args.pause <= 30:
            raise ValueError("Pause must be between 0 and 30 seconds")
        if not math.isfinite(args.min_success_rate) or not 0 <= args.min_success_rate <= 100:
            raise ValueError("Success rate must be between 0 and 100")
        if args.max_p95 is not None and (not math.isfinite(args.max_p95) or args.max_p95 <= 0):
            raise ValueError("Maximum p95 must be positive")
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / ".artifacts" / ("qa-" + stamp + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    (ROOT / ".tmp").mkdir(exist_ok=True)
    versions = {}
    for package in ("curl_cffi", "python-dotenv"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    report = {"schema_version": 1, "started_at": now(), "status": "running",
              "environment": {"python": platform.python_version(), "os": platform.system(),
                              "os_release": platform.release(), "packages": versions},
              "config": {"regions": [region.code for region in args.selected_regions], "rounds": args.rounds,
                         "timeout": args.timeout, "pause": args.pause, "parse_iterations": args.parse_iterations,
                         "min_success_rate": args.min_success_rate, "max_p95": args.max_p95,
                         "block_media": not args.load_media,
                         "offline": args.offline, "benchmark_only": args.benchmark_only}, "checks": []}
    print(f"REPORT {folder / 'report.md'}", flush=True)
    write_report(folder, report)
    exit_code = 1
    try:
        if not args.benchmark_only:
            print("TESTS: running offline Vinted suite", flush=True)
            report["tests"] = run_tests(folder)
            write_report(folder, report)
            print(f"TESTS: {report['tests']['status']} ({report['tests']['total']} tests)", flush=True)
            if report["tests"]["status"] != "passed":
                report["status"] = "failed"
                return 1
        if not args.offline:
            local_env = {}
            if (ROOT / ".env").is_file():
                from dotenv import dotenv_values
                local_env = dotenv_values(ROOT / ".env", interpolate=False)
            endpoint = vinted.validate_endpoint(args.byparr_url or os.environ.get("BYPARR_URL")
                                               or local_env.get("BYPARR_URL") or "http://127.0.0.1:8191")
            def update(benchmark):
                report["benchmark"] = benchmark
                write_report(folder, report)
            benchmark = run_benchmark(args.selected_regions, rounds=args.rounds, timeout=args.timeout,
                                      pause=args.pause, endpoint=endpoint, folder=folder, update=update,
                                      block_media=not args.load_media)
            benchmark_parser(benchmark, args.selected_regions, folder, args.parse_iterations, update)
            report["checks"] = quality_checks(benchmark, args.min_success_rate, args.max_p95)
        report["status"] = "passed" if all(check["passed"] for check in report["checks"]) else "failed"
        exit_code = 0 if report["status"] == "passed" else 1
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        exit_code = 130
    except Exception as error:
        # Raw exception strings can contain endpoint credentials or transient tokens.
        report["status"] = "error"
        report["error"] = "Runner error: " + type(error).__name__ + ". Check dependencies, Docker and arguments."
        exit_code = 2
    finally:
        report["finished_at"] = now()
        write_report(folder, report)
        print(f"RESULT: {report['status']}; {folder / 'report.md'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
