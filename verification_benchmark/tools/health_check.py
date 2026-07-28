#!/usr/bin/env python3
"""Phase-0 health check for MobiAgent verification benchmark.

The script is read-only with respect to existing project code. It scans rules,
configs, and runner task definitions, then writes machine-readable and Markdown
reports under verification_benchmark/reports/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
MOBIFLOW_ROOT = REPO_ROOT / "MobiFlow"
BENCHMARK_ROOT = REPO_ROOT / "verification_benchmark"
REPORTS_DIR = BENCHMARK_ROOT / "reports"

if str(MOBIFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(MOBIFLOW_ROOT))

from avdag.dag import DAG  # noqa: E402
from avdag.loader import load_task  # noqa: E402


APP_ALIASES = {
    "bilibili": "bilibili",
    "淘宝": "taobao",
    "网易云音乐": "cloudmusic",
    "小红书": "xiaohongshu",
    "高德": "gaode",
    "饿了么": "ele",
}


def normalize_runner_app(app: str, task_type: str) -> str:
    if app == "携程":
        if task_type in {"type6", "type7", "type8", "type9"}:
            return "xiechen-jiudian"
        return "xiechen"
    return APP_ALIASES.get(app, app)


def normalize_task_type(task_type: str) -> str:
    text = str(task_type)
    if text.isdigit():
        return f"type{text}"
    return text


def infer_type_from_rule_name(path: Path) -> Optional[str]:
    match = re.search(r"type(\d+)", path.name)
    if not match:
        return None
    return f"type{match.group(1)}"


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_config_rule_path(rules_base: Optional[str], rule_file: Optional[str]) -> Path:
    if not rule_file:
        return MOBIFLOW_ROOT / str(rules_base or "")

    rule = Path(str(rule_file))
    if rule.is_absolute():
        return rule

    base = Path(str(rules_base or ""))
    if base.is_absolute():
        return base / rule

    return MOBIFLOW_ROOT / base / rule


def config_coverage_keys(path: Path, task_name: Optional[str], rules_base: Optional[str]) -> List[str]:
    keys = []
    stem = path.stem
    if stem == "task_config_template":
        return keys

    keys.append(stem)
    if stem.endswith("_auto"):
        keys.append(stem[: -len("_auto")])

    if task_name and task_name != "示例任务配置":
        keys.append(task_name)
        if task_name.endswith("_auto"):
            keys.append(task_name[: -len("_auto")])

    if rules_base:
        base_app = Path(rules_base).name
        if base_app and base_app != "task_rules":
            keys.append(base_app)

    deduped = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def scan_rule_files() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, List[str]]]]:
    results: List[Dict[str, Any]] = []
    coverage: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))

    rule_roots = [
        ("mobiflow", MOBIFLOW_ROOT / "task_rules"),
        ("benchmark", BENCHMARK_ROOT / "rules"),
    ]

    for source, root in rule_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.yaml")):
            app = path.parent.name
            inferred_type = infer_type_from_rule_name(path)
            entry: Dict[str, Any] = {
                "path": rel(path),
                "source": source,
                "app": app,
                "inferred_type": inferred_type,
                "load_ok": False,
                "dag_ok": False,
                "warnings": [],
                "error": None,
            }

            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    task = load_task(str(path))
                    entry["task_id"] = task.task_id
                    entry["task_type"] = getattr(task, "task_type", None)
                    entry["node_count"] = len(task.nodes)
                    entry["success"] = {
                        "any_of": task.success.any_of if task.success else None,
                        "all_of": task.success.all_of if task.success else None,
                    }
                    entry["load_ok"] = True
                    DAG(task.nodes)
                    entry["dag_ok"] = True
                    entry["warnings"] = [str(item.message) for item in caught]
            except Exception as exc:  # noqa: BLE001 - report all rule failures
                entry["error"] = f"{type(exc).__name__}: {exc}"

            if inferred_type:
                coverage[app][inferred_type].append(rel(path))
            results.append(entry)

    return results, coverage


def scan_task_configs() -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Dict[str, Any]]]]:
    results: List[Dict[str, Any]] = []
    coverage: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

    for path in sorted((MOBIFLOW_ROOT / "task_configs").glob("*.json")):
        entry: Dict[str, Any] = {
            "path": rel(path),
            "parse_ok": False,
            "task_name": None,
            "task_types": [],
            "errors": [],
        }
        try:
            cfg = read_json(path)
            entry["parse_ok"] = True
            task_name = cfg.get("task_name")
            entry["task_name"] = task_name
            rules_base = cfg.get("rules_base_dir")
            app_keys = config_coverage_keys(path, task_name, rules_base)
            entry["coverage_keys"] = app_keys
            task_types = cfg.get("task_types") or {}

            for raw_type, type_cfg in task_types.items():
                norm_type = normalize_task_type(raw_type)
                rule_file = type_cfg.get("rule_file")
                rule_path = resolve_config_rule_path(rules_base, rule_file)
                exists = rule_path.exists()
                type_entry = {
                    "raw_type": raw_type,
                    "normalized_type": norm_type,
                    "rule_file": rule_file,
                    "rule_path": rel(rule_path),
                    "rule_exists": exists,
                    "key_style_mismatch": raw_type != norm_type,
                }
                entry["task_types"].append(type_entry)
                for app_key in app_keys:
                    coverage[app_key][norm_type] = type_entry
                if not exists:
                    entry["errors"].append(f"{raw_type}: missing rule file {rel(rule_path)}")
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(f"{type(exc).__name__}: {exc}")

        results.append(entry)

    return results, coverage


def load_runner_tasks() -> List[Dict[str, Any]]:
    path = REPO_ROOT / "runner" / "mobiagent" / "task_mobiflow.json"
    if not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def build_runner_coverage(
    runner_tasks: Iterable[Dict[str, Any]],
    rule_coverage: Dict[str, Dict[str, List[str]]],
    config_coverage: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    matrix: List[Dict[str, Any]] = []
    for item in runner_tasks:
        runner_app = item.get("app", "")
        runner_type = str(item.get("type", ""))
        normalized_app = normalize_runner_app(runner_app, runner_type)
        normalized_type = normalize_task_type(runner_type)
        tasks = item.get("tasks") or []

        rule_files = rule_coverage.get(normalized_app, {}).get(normalized_type, [])
        config_entry = config_coverage.get(normalized_app, {}).get(normalized_type)

        matrix.append(
            {
                "runner_app": runner_app,
                "runner_type": runner_type,
                "normalized_app": normalized_app,
                "normalized_type": normalized_type,
                "task_count": len(tasks) if isinstance(tasks, list) else 0,
                "has_rule_file": bool(rule_files),
                "rule_files": rule_files,
                "has_config_mapping": bool(config_entry),
                "config_rule_exists": bool(config_entry and config_entry.get("rule_exists")),
                "config_key_style_mismatch": bool(config_entry and config_entry.get("key_style_mismatch")),
            }
        )
    return matrix


def load_mvp_tasks() -> List[Dict[str, Any]]:
    path = BENCHMARK_ROOT / "configs" / "mvp_tasks.json"
    if not path.exists():
        return []
    data = read_json(path)
    return data.get("mvp_tasks", []) if isinstance(data, dict) else []


def build_mvp_readiness(
    mvp_tasks: Iterable[Dict[str, Any]],
    rule_results: Dict[str, Dict[str, Any]],
    config_coverage: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for task in mvp_tasks:
        rule_path = str(task.get("rule_file", ""))
        absolute_rule = REPO_ROOT / rule_path
        rule_key = rel(absolute_rule)
        rule_status = rule_results.get(rule_key)
        config_entry = config_coverage.get(task.get("app", ""), {}).get(task.get("runner_type", ""))

        rows.append(
            {
                "benchmark_task_id": task.get("benchmark_task_id"),
                "display_name": task.get("display_name"),
                "app": task.get("app"),
                "runner_type": task.get("runner_type"),
                "rule_file": rule_key,
                "rule_exists": absolute_rule.exists(),
                "rule_load_ok": bool(rule_status and rule_status.get("load_ok")),
                "rule_dag_ok": bool(rule_status and rule_status.get("dag_ok")),
                "has_existing_mobiflow_config": bool(config_entry),
                "existing_config_rule_exists": bool(config_entry and config_entry.get("rule_exists")),
                "trace_root": task.get("trace_root"),
            }
        )
    return rows


def summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    rules = report["rules"]
    configs = report["configs"]
    coverage = report["runner_task_coverage"]

    return {
        "rule_file_count": len(rules),
        "rule_load_fail_count": sum(1 for r in rules if not r["load_ok"]),
        "rule_dag_fail_count": sum(1 for r in rules if r["load_ok"] and not r["dag_ok"]),
        "rule_warning_count": sum(1 for r in rules if r.get("warnings")),
        "config_file_count": len(configs),
        "config_missing_rule_count": sum(
            1
            for cfg in configs
            for item in cfg.get("task_types", [])
            if not item.get("rule_exists")
        ),
        "runner_task_group_count": len(coverage),
        "runner_task_total_count": sum(item.get("task_count", 0) for item in coverage),
        "runner_group_with_rule_count": sum(1 for item in coverage if item.get("has_rule_file")),
        "runner_group_with_config_count": sum(1 for item in coverage if item.get("has_config_mapping")),
    }


def write_markdown(report: Dict[str, Any], path: Path) -> None:
    summary = report["summary"]
    bad_rules = [r for r in report["rules"] if not r["load_ok"] or not r["dag_ok"]]
    warning_rules = [r for r in report["rules"] if r.get("warnings")]
    missing_config_rules = [
        (cfg, item)
        for cfg in report["configs"]
        for item in cfg.get("task_types", [])
        if not item.get("rule_exists")
    ]
    missing_coverage = [row for row in report["runner_task_coverage"] if not row["has_rule_file"] or not row["has_config_mapping"]]

    lines: List[str] = []
    lines.append("# Phase-0 Health Check Report")
    lines.append("")
    lines.append(f"Generated at: `{report['generated_at']}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    for key, value in summary.items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")

    lines.append("## MVP Readiness")
    lines.append("")
    lines.append("| benchmark_task_id | rule_load_ok | rule_dag_ok | existing_config | notes |")
    lines.append("|---|---:|---:|---:|---|")
    for row in report["mvp_readiness"]:
        notes = []
        if not row["has_existing_mobiflow_config"]:
            notes.append("uses dedicated benchmark config")
        if not row["rule_dag_ok"]:
            notes.append("rule not ready")
        lines.append(
            "| {benchmark_task_id} | {rule_load_ok} | {rule_dag_ok} | {has_existing_mobiflow_config} | {notes} |".format(
                **row,
                notes=", ".join(notes) if notes else "",
            )
        )
    lines.append("")

    lines.append("## Rule Failures")
    lines.append("")
    if not bad_rules:
        lines.append("No rule load/DAG failures.")
    else:
        lines.append("| path | load_ok | dag_ok | error |")
        lines.append("|---|---:|---:|---|")
        for item in bad_rules:
            lines.append(f"| `{item['path']}` | {item['load_ok']} | {item['dag_ok']} | {item.get('error') or ''} |")
    lines.append("")

    lines.append("## Rule Warnings")
    lines.append("")
    if not warning_rules:
        lines.append("No DAG consistency warnings.")
    else:
        lines.append("| path | warning_count | first_warning |")
        lines.append("|---|---:|---|")
        for item in warning_rules:
            first = (item.get("warnings") or [""])[0].replace("\n", "<br>")
            lines.append(f"| `{item['path']}` | {len(item.get('warnings') or [])} | {first} |")
    lines.append("")

    lines.append("## Config Rule Missing")
    lines.append("")
    if not missing_config_rules:
        lines.append("No config points to a missing rule file.")
    else:
        lines.append("| config | type | missing_rule |")
        lines.append("|---|---|---|")
        for cfg, item in missing_config_rules:
            lines.append(f"| `{cfg['path']}` | `{item['raw_type']}` | `{item['rule_path']}` |")
    lines.append("")

    lines.append("## Runner Task Coverage Gaps")
    lines.append("")
    if not missing_coverage:
        lines.append("All runner task groups have both a rule and a config mapping.")
    else:
        lines.append("| runner_app | runner_type | normalized_app | has_rule | has_config | notes |")
        lines.append("|---|---|---|---:|---:|---|")
        for row in missing_coverage:
            notes = []
            if row["config_key_style_mismatch"]:
                notes.append("config key style mismatch")
            if not row["has_rule_file"]:
                notes.append("no rule inferred")
            if not row["has_config_mapping"]:
                notes.append("no config mapping")
            lines.append(
                f"| {row['runner_app']} | {row['runner_type']} | {row['normalized_app']} | {row['has_rule_file']} | {row['has_config_mapping']} | {', '.join(notes)} |"
            )
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(output_dir: Path) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    rules, rule_coverage = scan_rule_files()
    configs, config_coverage = scan_task_configs()
    runner_tasks = load_runner_tasks()
    runner_coverage = build_runner_coverage(runner_tasks, rule_coverage, config_coverage)
    rule_by_path = {item["path"]: item for item in rules}
    mvp_readiness = build_mvp_readiness(load_mvp_tasks(), rule_by_path, config_coverage)

    report: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root": str(REPO_ROOT),
        "rules": rules,
        "configs": configs,
        "runner_task_coverage": runner_coverage,
        "mvp_readiness": mvp_readiness,
    }
    report["summary"] = summarize(report)

    json_path = output_dir / "phase0_health_check.json"
    md_path = output_dir / "phase0_health_check.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run phase-0 rule/config health checks.")
    parser.add_argument(
        "--output-dir",
        default=str(REPORTS_DIR),
        help="Directory for JSON and Markdown reports.",
    )
    args = parser.parse_args()

    report = run(Path(args.output_dir))
    summary = report["summary"]
    print("Phase-0 health check complete.")
    print(f"Reports: {Path(args.output_dir).resolve()}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # Non-zero exit would make known non-MVP rule failures block progress.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
