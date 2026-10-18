"""Measure service overhead and browser requests separately, without disk I/O in timers."""

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import grpc
from grpc_client import VintedClient, response_summary
from grpc_common import DEFAULT_TARGET
from qa import distribution
from vinted_regions import select_regions

ROOT = Path(__file__).resolve().parent
METRICS = {
    "rpc_total_ms": "Повний виклик із передаванням HTML",
    "byparr_http_ms": "Запит до Byparr: браузер + Vinted + HTTP-відповідь",
    "gateway_overhead_ms": "Власний gateway + gRPC, поза запитом Byparr",
    "json_decode_ms": "Декодування відповіді Byparr",
    "catalog_validation_ms": "Перевірка HTML у gateway",
    "grpc_transport_ms": "gRPC та підготовка відповіді поза fetch_catalog",
    "browser_navigation_ms": "Навігація браузера до готового каталогу",
    "browser_document_request_ms": "Основний HTTP-запит HTML до Vinted",
    "browser_ttfb_ms": "Від початку HTTP-запиту до першого байта",
}


def measured_row(response, total_ms):
    row = response_summary(response)
    values = row["timings_ms"]
    values["rpc_total_ms"] = total_ms
    # These are nested durations from the same request, not unrelated averages.
    if "byparr_http_ms" in values:
        values["gateway_overhead_ms"] = total_ms - values["byparr_http_ms"]
    if "fetch_total_ms" in values:
        values["grpc_transport_ms"] = total_ms - values["fetch_total_ms"]
    required = set(METRICS)
    row["complete_timings"] = required.issubset(values) and all(values[k] >= 0 for k in required)
    return row


def summaries(rows):
    successful = [row for row in rows if row["outcome"] == "page_loaded"]
    return {metric: {"samples": sum(metric in row["timings_ms"] for row in successful),
                     **distribution([row["timings_ms"][metric] for row in successful
                                     if metric in row["timings_ms"]])}
            for metric in METRICS}


def replace_report(temporary, destination):
    # Windows readers (including report previews) may briefly deny replacement.
    # Retry only this file operation, never a Vinted request or a measurement.
    for attempt in range(6):
        try:
            temporary.replace(destination)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_report(folder, report):
    rows = report["requests"]
    report["successful"] = sum(row["outcome"] == "page_loaded" for row in rows)
    report["summary_ms"] = summaries(rows)
    report["regions"] = {code: summaries([r for r in rows if r["region"] == code])
                         for code in report["config"]["regions"]}
    report["complete"] = (len(rows) == report["planned"] and report["successful"] == report["planned"]
                          and all(row["complete_timings"] for row in rows))
    temporary = folder / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replace_report(temporary, folder / "report.json")
    lines = ["# Вимірювання затримки Vinted gateway", "", f"UTC: {report['started_at']}", "",
             f"Успішні запити: {report['successful']}/{report['planned']}. Статус: {report['status']}.", "",
             "| Етап | Вимірювань | Середнє, мс | Медіана, мс | p95, мс |",
             "|---|---:|---:|---:|---:|"]
    def number(value):
        return "—" if value is None else f"{value:.2f}"
    for metric, label in METRICS.items():
        stats = report["summary_ms"][metric]
        lines.append(f"| {label} | {stats['samples']} | {number(stats['mean'])} | "
                     f"{number(stats['median'])} | {number(stats['p95'])} |")
    lines += ["", "| Регіон | Успішно | Повний виклик, мс | Byparr + Vinted, мс | Gateway + gRPC, мс |",
              "|---|---:|---:|---:|---:|"]
    for code, metrics in report["regions"].items():
        lines.append(f"| {code} | {metrics['rpc_total_ms']['samples']} | "
                     + " | ".join(number(metrics[m]["mean"]) for m in
                                  ("rpc_total_ms", "byparr_http_ms", "gateway_overhead_ms")) + " |")
    probe = report.get("local_probe_ms", {})
    lines += ["", f"ListRegions без браузера: середнє {number(probe.get('mean'))} мс, "
              f"p95 {number(probe.get('p95'))} мс; {report.get('probe_samples', 0)} викликів.", "",
              "Методика:", "",
              "- Запити послідовні через одну прогріту gRPC-сесію. Повний HTML передається клієнту.",
              "- Таймер клієнта зупиняється до обробки статистики й запису звіту; паузи не враховано.",
              "- Gateway + gRPC = час клієнта мінус HTTP-виклик Byparr у тому самому запиті.",
              "- Це час модуля доступу. Telegram, worker, база даних і доставка повідомлення не виміряні.",
              "- Час Byparr включає запуск/закриття браузера, сайт, підготовку й передавання відповіді.",
              "- Навігація включає DOM, JavaScript, читання HTML і перевірку готовності в Byparr.",
              "- HTTP HTML = responseEnd − requestStart: запит основного документа, не всіх ресурсів.",
              "  TTFB включає мережу й очікування сервера; окремий серверний час Vinted невідомий.",
              "- Браузерна діагностика додає один короткий виклик читання таймерів у Byparr.",
              "- Етапи вкладені: їх не можна всі додавати. Невдалі запити виключено зі статистики успішних.",
              "- Відсутній таймер не замінюється нулем. Малий послідовний тест не вимірює граничне навантаження.",
              "- [Визначення браузерних таймерів](https://developer.mozilla.org/en-US/docs/Web/API/PerformanceNavigationTiming).", ""]
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--regions", nargs="+", default=["all"])
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args(argv)
    try:
        regions = select_regions(args.regions)
    except ValueError as error:
        parser.error(str(error))
    folder = ROOT / ".artifacts" / ("latency-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                     + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "config": {"regions": [r.code for r in regions], "rounds": args.rounds,
                         "include_html": True, "pause_seconds": 0.5, "timeout_seconds": 60},
              "planned": len(regions) * args.rounds, "requests": []}
    write_report(folder, report)
    print(f"REPORT {folder / 'report.md'}", flush=True)
    try:
        with VintedClient(args.target) as client:
            client.list_regions()  # Warm transport only; no Vinted warm-up request.
            probes = []
            for _ in range(30):
                started = time.perf_counter()
                client.list_regions()
                probes.append((time.perf_counter() - started) * 1000)
            report["local_probe_ms"] = distribution(probes)
            report["probe_samples"] = len(probes)
            stopped = set()
            for round_index in range(args.rounds):
                for region in regions:
                    if region.code in stopped:
                        continue
                    if report["requests"]:
                        time.sleep(0.5)
                    started = time.perf_counter()
                    try:
                        response = client.fetch_catalog(region.code, include_html=True, measure_timing=True)
                        total_ms = (time.perf_counter() - started) * 1000
                        row = measured_row(response, total_ms)
                    except grpc.RpcError as error:
                        row = {"region": region.code, "outcome": "rpc_error", "grpc_code": error.code().name,
                               "complete_timings": False, "timings_ms": {
                                   "rpc_total_ms": (time.perf_counter() - started) * 1000}}
                    row["round"] = round_index + 1
                    report["requests"].append(row)
                    write_report(folder, report)
                    print(f"{round_index + 1}/{args.rounds} {region.code}: {row['outcome']}; "
                          f"total={row['timings_ms']['rpc_total_ms']:.1f}ms; "
                          f"gateway={row['timings_ms'].get('gateway_overhead_ms', float('nan')):.1f}ms", flush=True)
                    if row["outcome"] in ("blocked", "rate_limited"):
                        stopped.add(region.code)
                    if row["outcome"] in ("service_error", "rpc_error"):
                        report["status"] = "stopped_service_error"
                        return 2
            report["status"] = "passed" if report["complete"] else "incomplete"
            return 0 if report["complete"] else 2
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        return 130
    except grpc.RpcError as error:
        report["status"] = "rpc_error"
        report["grpc_code"] = error.code().name
        return 2
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_report(folder, report)


if __name__ == "__main__":
    raise SystemExit(main())
