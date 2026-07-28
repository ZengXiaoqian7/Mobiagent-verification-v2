#!/usr/bin/env python3
"""Evaluate MobiFlow verification results against Ground Truth labels.

This is a runnable skeleton for the benchmark phase. With no labels/traces it
will produce an empty report. After trace collection, it verifies each labeled
trace and computes TP/TN/FP/FN plus derived metrics.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import requests
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
MOBIFLOW_ROOT = REPO_ROOT / "MobiFlow"
BENCHMARK_ROOT = REPO_ROOT / "verification_benchmark"
DEFAULT_LABELS = BENCHMARK_ROOT / "labels.jsonl"
DEFAULT_MVP_CONFIG = BENCHMARK_ROOT / "configs" / "mvp_tasks.json"
DEFAULT_OUTPUT_DIR = BENCHMARK_ROOT / "reports"
TOOLS_DIR = Path(__file__).resolve().parent

if str(MOBIFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(MOBIFLOW_ROOT))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from avdag.types import VerifierOptions  # noqa: E402
from avdag.verifier import make_llm_options, verify_task_folder  # noqa: E402
from check_trace_schema import inspect_trace  # noqa: E402


@dataclass
class EvalCounts:
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0
    ambiguous: int = 0
    missing: int = 0
    errors: int = 0


def safe_div(num: int, den: int) -> Optional[float]:
    if den == 0:
        return None
    return num / den


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    """Two-sided Wilson score interval for a binomial proportion."""
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + (z * z) / total
    center = (proportion + (z * z) / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1 - proportion) / total)
            + (z * z) / (4 * total * total)
        )
        / denominator
    )
    return [max(0.0, center - margin), min(1.0, center + margin)]


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_labels(path: Path) -> List[Dict[str, Any]]:
    labels: List[Dict[str, Any]] = []
    if not path.exists():
        return labels
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                labels.append(
                    {
                        "_label_parse_error": f"line {line_no}: {exc}",
                        "ground_truth": "ambiguous",
                    }
                )
                continue
            labels.append(item)
    return labels


def load_mvp_index(path: Path) -> Dict[str, Dict[str, Any]]:
    data = read_json(path)
    tasks = data.get("mvp_tasks", []) if isinstance(data, dict) else []
    return {task["benchmark_task_id"]: task for task in tasks if "benchmark_task_id" in task}


def first_nonempty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def masked(value: Optional[str]) -> str:
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _extract_xml_visible_text(xml_text: str) -> str:
    values: List[str] = []
    for attr in ("text", "content-desc", "hint", "resource-id"):
        values.extend(re.findall(rf'{attr}="([^"]+)"', xml_text))
    values.append(re.sub(r"<[^>]+>", " ", xml_text))
    return " ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def _fixture_ocr(frame: Dict[str, Any]) -> Optional[str]:
    pieces = [
        _extract_xml_visible_text(str(frame.get("xml_text") or "")),
        str(frame.get("reasoning") or ""),
    ]
    action = frame.get("action") or {}
    if isinstance(action, dict):
        pieces.extend(str(action.get(key) or "") for key in ("type", "text", "target_element"))
    text = " ".join(piece for piece in pieces if piece)
    return text or None


def _fixture_task_keyword(task_description: str) -> Optional[str]:
    known_keywords = [
        "机械键盘",
        "机器学习教程",
        "Python教程",
        "原神",
        "周杰伦",
    ]
    for keyword in known_keywords:
        if keyword in task_description:
            return keyword
    match = re.search(r"(?:搜索|播放|打开|查看)([^，,。；; ]{2,20})", task_description)
    if match:
        return match.group(1).strip()
    return None


def _fixture_llm(ctx: Dict[str, Any]) -> Optional[bool]:
    frame = ctx.get("frame") or {}
    params = ctx.get("params") or {}
    prompt = str(params.get("prompt") or "")
    task_description = str(frame.get("task_description") or "")
    keyword = _fixture_task_keyword(task_description)
    visible_text = " ".join(
        [
            str(frame.get("text") or ""),
            str(frame.get("reasoning") or ""),
            _fixture_ocr(frame) or "",
        ]
    )

    has_keyword = keyword is None or keyword in visible_text

    if "执行了搜索操作" in prompt:
        return has_keyword and any(word in visible_text for word in ["input", "输入", "搜索"])
    if "B站内容列表" in prompt:
        return has_keyword and all(word in visible_text for word in ["综合", "番剧", "直播", "用户"])
    if "淘宝搜索结果列表" in prompt:
        return has_keyword and any(word in visible_text for word in ["销量", "人付款", "元", "天猫", "店铺"])
    if "商品详情页" in prompt:
        return has_keyword and all(word in visible_text for word in ["店铺", "客服", "收藏"])

    if "搜索输入框" in prompt:
        return any(word in visible_text for word in ["鎼滅储", "鎼滅储鍘嗗彶", "鐑悳", "鐚滀綘鎯虫悳"])
    if ("输入" in prompt) and ("关键词" in prompt or "搜索框" in prompt):
        return has_keyword and any(word in visible_text for word in ["input", "杈撳叆", "鎼滅储"])
    if "搜索结果" in prompt:
        return has_keyword and all(word in visible_text for word in ["閿€閲?", "澶╃尗", "搴楅摵"])
    if "商品详情" in prompt:
        return has_keyword and all(word in visible_text for word in ["搴楅摵", "瀹㈡湇", "鏀惰棌"])
    has_bili_search_markers = all(word in visible_text for word in ["综合", "番剧", "直播", "用户"])
    has_bili_play_markers = all(word in visible_text for word in ["简介", "评论", "弹幕", "关注"])
    has_taobao_results_markers = any(word in visible_text for word in ["销量", "人付款", "元", "天猫", "店铺"])
    has_taobao_detail_markers = all(word in visible_text for word in ["店铺", "客服", "收藏"])

    if "搜索输入界面" in prompt or "激活搜索" in prompt:
        return any(word in visible_text for word in ["搜索", "搜索历史", "热搜", "猜你想搜"])
    if "输入" in prompt and ("关键词" in prompt or "搜索框" in prompt):
        return has_keyword and any(word in visible_text for word in ["input", "输入", "搜索"])
    if "筛选" in prompt or "排序" in prompt:
        task_requires_filter = any(word in task_description for word in ["价格", "销量", "筛选", "排序", "最便宜", "最高", "最低"])
        if not task_requires_filter:
            return False
        sort_evidence = any(
            word in visible_text
            for word in ["销量最高", "销量排序", "按销量", "从低到高", "从高到低", "价格最低", "价格最高"]
        )
        return has_keyword and sort_evidence
    if "搜索结果" in prompt or "内容列表" in prompt:
        return has_keyword and (has_bili_search_markers or has_taobao_results_markers)
    if "商品详情" in prompt:
        return has_keyword and has_taobao_detail_markers
    if "播放" in prompt or "点击了一个视频" in prompt:
        return has_keyword and has_bili_play_markers
    return None


def _json_from_text(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        obj = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if obj:
            cleaned = obj.group(0).strip()
    return json.loads(cleaned)


def _make_text_llm(api_key: str, base_url: str, model: str):
    endpoint = base_url.rstrip("/") + "/chat/completions"

    def _llm(ctx: Dict[str, Any]) -> Optional[bool]:
        frame = ctx.get("frame") or {}
        params = ctx.get("params") or {}
        prompt = str(params.get("prompt") or "请判断该步骤是否达成预期。")
        prev_frame = frame.get("_prev") or {}
        next_frame = frame.get("_next") or {}
        action = frame.get("action") or {}
        task_desc = frame.get("task_description") or ""

        def visible(fr: Dict[str, Any]) -> str:
            return " ".join(
                part for part in [
                    _fixture_ocr(fr) or "",
                    str(fr.get("text") or ""),
                    str(fr.get("reasoning") or ""),
                ] if part
            )

        user_prompt = (
            "你是移动端任务状态验证器。请只判断当前节点是否被证据支持，不要相信 Agent 的 done 自报。\n"
            "请优先检查任务中的动态参数，例如关键词、排序/筛选条件、目标视频或商品是否一致。\n\n"
            f"全局任务: {task_desc}\n"
            f"当前节点要求: {prompt}\n"
            f"当前动作: {json.dumps(action, ensure_ascii=False)}\n\n"
            f"上一帧可见文本: {visible(prev_frame)}\n"
            f"当前帧可见文本: {visible(frame)}\n"
            f"下一帧可见文本: {visible(next_frame)}\n\n"
            "输出严格 JSON："
            '{"result":"yes|no","reason":"简要原因"}'
        )
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "你是严格、保守的移动端状态验证助手。"},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 300,
                },
                timeout=40,
            )
            if response.status_code >= 400:
                frame["_last_llm_result"] = {
                    "success": False,
                    "reason": f"HTTP {response.status_code}: {response.text[:300]}",
                }
                return None
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            parsed = _json_from_text(content)
            result = str(parsed.get("result", "")).strip().lower()
            reason = str(parsed.get("reason", "")).strip()
            ok = result in {"yes", "true", "是", "通过"}
            frame["_last_llm_result"] = {"success": ok, "reason": reason or content}
            return ok
        except Exception as exc:  # noqa: BLE001
            frame["_last_llm_result"] = {"success": False, "reason": f"{type(exc).__name__}: {exc}"}
            return None

    return _llm


def create_options(
    mode: str,
    *,
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> VerifierOptions:
    if mode == "deterministic":
        return VerifierOptions(ocr=None, llm=None)

    if mode in {"fixture", "fixture_force_llm"}:
        return VerifierOptions(
            ocr=_fixture_ocr,
            llm=_fixture_llm,
            force_llm_verification=(mode == "fixture_force_llm"),
        )

    if mode in {"llm_text", "llm_text_force"}:
        api_key = first_nonempty(llm_api_key, os.getenv("MOBIFLOW_LLM_API_KEY"), os.getenv("OPENAI_API_KEY"))
        base_url = first_nonempty(llm_base_url, os.getenv("MOBIFLOW_LLM_BASE_URL"), os.getenv("OPENAI_BASE_URL"))
        model = first_nonempty(llm_model, os.getenv("MOBIFLOW_LLM_MODEL"), os.getenv("OPENAI_MODEL"), "gpt-5.4")
        if not api_key or not base_url:
            raise RuntimeError(
                "llm_text mode requires MOBIFLOW_LLM_API_KEY and MOBIFLOW_LLM_BASE_URL "
                "or explicit --llm-api-key/--llm-base-url."
            )
        print("[llm_text] base_url:", base_url)
        print("[llm_text] model:", model)
        print("[llm_text] api_key:", masked(api_key))
        return VerifierOptions(
            ocr=_fixture_ocr,
            llm=_make_text_llm(api_key, base_url, model),
            force_llm_verification=(mode == "llm_text_force"),
        )

    if mode == "ocr":
        try:
            from avdag.ocr_processor import create_standard_ocr_functions, get_ocr_processor

            processor = get_ocr_processor()
            engines = [
                getattr(processor, "_engine", None),
                getattr(processor, "_engine_paddle", None),
                getattr(processor, "_engine_tess", None),
            ]
            has_working_engine = any(
                engine is not None
                and (
                    getattr(engine, "_paddle", None) is not None
                    or bool(getattr(engine, "_has_tesseract", False))
                )
                for engine in engines
            )
            if not processor.is_available() or not has_working_engine:
                raise RuntimeError("no PaddleOCR or Tesseract engine is available")
            ocr_func, _ = create_standard_ocr_functions()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "OCR mode was requested but OCR initialization failed. "
                "Refusing to emit an OCR-labelled report using deterministic fallback."
            ) from exc
        if ocr_func is None:
            raise RuntimeError(
                "OCR mode was requested but the OCR factory returned no callable. "
                "Refusing to emit a misleading OCR-labelled report."
            )
        return VerifierOptions(ocr=ocr_func, llm=None)

    if mode == "llm":
        api_key = first_nonempty(llm_api_key, os.getenv("MOBIFLOW_LLM_API_KEY"), os.getenv("OPENAI_API_KEY"))
        base_url = first_nonempty(llm_base_url, os.getenv("MOBIFLOW_LLM_BASE_URL"), os.getenv("OPENAI_BASE_URL"))
        model = first_nonempty(llm_model, os.getenv("MOBIFLOW_LLM_MODEL"), os.getenv("OPENAI_MODEL"), "gpt-5.4")

        if not api_key or not base_url:
            raise RuntimeError(
                "LLM mode requires MOBIFLOW_LLM_API_KEY and MOBIFLOW_LLM_BASE_URL "
                "or explicit --llm-api-key/--llm-base-url."
            )
        print("[llm] base_url:", base_url)
        print("[llm] model:", model)
        print("[llm] api_key:", masked(api_key))
        opts = make_llm_options(
            api_key=api_key,
            base_url=base_url,
            model=model,
            force_llm=False,
        )
        opts.ocr = None
        return opts

    raise ValueError(f"unknown mode: {mode}")


def validate_label(label: Dict[str, Any], mvp_index: Dict[str, Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
    required = [
        "trace_id",
        "benchmark_task_id",
        "app",
        "task_type",
        "task_description",
        "ground_truth",
        "evidence_frames",
    ]
    for key in required:
        if key not in label:
            errors.append(f"missing label field: {key}")
    truth = label.get("ground_truth")
    if truth not in {"success", "fail", "ambiguous"}:
        errors.append("ground_truth must be success, fail, or ambiguous")
    if truth == "fail" and not label.get("failure_type"):
        errors.append("failure_type is required for failed samples")
    if label.get("benchmark_task_id") not in mvp_index:
        errors.append(f"unknown benchmark_task_id: {label.get('benchmark_task_id')}")
    return errors


def update_counts(counts: EvalCounts, truth: str, predicted_ok: Optional[bool], error: Optional[str] = None) -> None:
    if truth == "ambiguous":
        counts.ambiguous += 1
        return
    if predicted_ok is None:
        if error:
            counts.errors += 1
        else:
            counts.missing += 1
        return
    if truth == "success" and predicted_ok:
        counts.tp += 1
    elif truth == "success" and not predicted_ok:
        counts.fn += 1
    elif truth == "fail" and predicted_ok:
        counts.fp += 1
    elif truth == "fail" and not predicted_ok:
        counts.tn += 1


def compute_metrics(counts: EvalCounts) -> Dict[str, Any]:
    decided = counts.tp + counts.tn + counts.fp + counts.fn
    total = decided + counts.ambiguous + counts.missing + counts.errors
    failure_total = counts.fp + counts.tn
    success_total = counts.fn + counts.tp
    return {
        "tp": counts.tp,
        "tn": counts.tn,
        "fp": counts.fp,
        "fn": counts.fn,
        "ambiguous": counts.ambiguous,
        "missing": counts.missing,
        "errors": counts.errors,
        "total": total,
        "decided": decided,
        "accuracy": safe_div(counts.tp + counts.tn, decided),
        "false_pass_rate": safe_div(counts.fp, counts.fp + counts.tn),
        "false_pass_rate_95ci": wilson_interval(counts.fp, failure_total),
        "false_fail_rate": safe_div(counts.fn, counts.fn + counts.tp),
        "false_fail_rate_95ci": wilson_interval(counts.fn, success_total),
        "success_recall": safe_div(counts.tp, counts.tp + counts.fn),
        "failure_recall": safe_div(counts.tn, counts.tn + counts.fp),
        "coverage": safe_div(decided, total),
    }


def counts_from_rows(rows: Iterable[Dict[str, Any]]) -> EvalCounts:
    counts = EvalCounts()
    for row in rows:
        update_counts(counts, str(row.get("ground_truth")), row.get("predicted_ok"), row.get("error"))
    return counts


def grouped_metrics(results: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in results:
        value = row.get(key)
        if value is None or value == "":
            value = "<none>"
        groups.setdefault(str(value), []).append(row)
    return {name: compute_metrics(counts_from_rows(rows)) for name, rows in sorted(groups.items())}


def _schema_summary(schema: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": bool(schema.get("ok")),
        "errors": schema.get("errors") or [],
        "warnings": schema.get("warnings") or [],
        "action_count": schema.get("action_count"),
        "react_count": schema.get("react_count"),
        "extra_artifacts": schema.get("extra_artifacts") or [],
        "missing_jpg": schema.get("missing_jpg") or [],
        "missing_xml": schema.get("missing_xml") or [],
    }


def _serialize_logs(logs: Any, limit: int = 12) -> List[Dict[str, Any]]:
    serialized: List[Dict[str, Any]] = []
    for item in list(logs or [])[-limit:]:
        serialized.append(
            {
                "frame_index": getattr(item, "frame_index", None),
                "node_id": getattr(item, "node_id", None),
                "strategy": getattr(item, "strategy", None),
                "decision": getattr(item, "decision", None),
                "checker_type": getattr(item, "checker_type", None),
                "checker_result": getattr(item, "checker_result", None),
                "matched_keywords": getattr(item, "matched_keywords", None),
                "unmatched_keywords": getattr(item, "unmatched_keywords", None),
            }
        )
    return serialized


def evaluate(
    labels: Iterable[Dict[str, Any]],
    mvp_index: Dict[str, Dict[str, Any]],
    mode: str,
    *,
    trace_root: Path = BENCHMARK_ROOT / "traces",
    llm_api_key: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
) -> Dict[str, Any]:
    options = create_options(
        mode,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
    )
    counts = EvalCounts()
    results: List[Dict[str, Any]] = []

    for label in labels:
        row: Dict[str, Any] = {
            "trace_id": label.get("trace_id"),
            "benchmark_task_id": label.get("benchmark_task_id"),
            "app": label.get("app"),
            "task_type": label.get("task_type"),
            "ground_truth": label.get("ground_truth"),
            "failure_type": label.get("failure_type"),
            "expected_slots": label.get("expected_slots") or {},
            "evidence_frames": label.get("evidence_frames") or [],
            "notes": label.get("notes") or "",
            "predicted_ok": None,
            "matched_nodes": [],
            "score": 0,
            "reason": "",
            "decision_logs_tail": [],
            "trace_schema": {},
            "error": None,
            "execution_time": 0.0,
        }

        label_errors = validate_label(label, mvp_index)
        if label_errors:
            row["error"] = "; ".join(label_errors)
            update_counts(counts, label.get("ground_truth", "ambiguous"), None, row["error"])
            results.append(row)
            continue

        if label["ground_truth"] == "ambiguous":
            update_counts(counts, "ambiguous", None)
            results.append(row)
            continue

        mvp_task = mvp_index[label["benchmark_task_id"]]
        trace_dir = trace_root / label["trace_id"]
        rule_file = REPO_ROOT / mvp_task["rule_file"]

        if not trace_dir.exists():
            row["error"] = f"trace directory not found: {rel(trace_dir)}"
            update_counts(counts, label["ground_truth"], None)
            results.append(row)
            continue
        schema = inspect_trace(trace_dir)
        row["trace_schema"] = _schema_summary(schema)
        if not schema.get("ok"):
            row["error"] = "; ".join(schema.get("errors") or ["trace schema check failed"])
            update_counts(counts, label["ground_truth"], None, row["error"])
            results.append(row)
            continue
        if not rule_file.exists():
            row["error"] = f"rule file not found: {rel(rule_file)}"
            update_counts(counts, label["ground_truth"], None, row["error"])
            results.append(row)
            continue

        started = time.time()
        try:
            verify_result = verify_task_folder(str(rule_file), str(trace_dir), options)
            row["predicted_ok"] = verify_result.ok
            row["matched_nodes"] = [
                {"node_id": item.node_id, "frame_index": item.frame_index}
                for item in verify_result.matched
            ]
            row["score"] = verify_result.total_score
            row["reason"] = verify_result.reason or ""
            row["manual_review_needed"] = bool(verify_result.manual_review_needed)
            row["decision_logs_tail"] = _serialize_logs(verify_result.logs)
            update_counts(counts, label["ground_truth"], verify_result.ok)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
            update_counts(counts, label["ground_truth"], None, row["error"])
        finally:
            row["execution_time"] = round(time.time() - started, 4)

        results.append(row)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "metrics": compute_metrics(counts),
        "grouped_metrics": {
            "benchmark_task_id": grouped_metrics(results, "benchmark_task_id"),
            "app": grouped_metrics(results, "app"),
            "failure_type": grouped_metrics(results, "failure_type"),
        },
        "trace_schema_warnings": [
            {
                "trace_id": row.get("trace_id"),
                "warnings": (row.get("trace_schema") or {}).get("warnings") or [],
            }
            for row in results
            if (row.get("trace_schema") or {}).get("warnings")
        ],
        "trace_schema_errors": [
            {
                "trace_id": row.get("trace_id"),
                "errors": (row.get("trace_schema") or {}).get("errors") or [],
            }
            for row in results
            if (row.get("trace_schema") or {}).get("errors")
        ],
        "false_passes": [
            row for row in results
            if row.get("ground_truth") == "fail" and row.get("predicted_ok") is True
        ],
        "false_fails": [
            row for row in results
            if row.get("ground_truth") == "success" and row.get("predicted_ok") is False
        ],
        "results": results,
    }


def write_markdown(report: Dict[str, Any], path: Path) -> None:
    metrics = report["metrics"]
    lines = [
        "# Benchmark Evaluation Report",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"Mode: `{report['mode']}`",
        "",
        "## Metrics",
        "",
    ]
    for key, value in metrics.items():
        if isinstance(value, float):
            lines.append(f"- `{key}`: {value:.4f}")
        else:
            lines.append(f"- `{key}`: {value}")

    def append_group_table(title: str, groups: Dict[str, Dict[str, Any]]) -> None:
        lines.extend(["", f"## {title}", ""])
        if not groups:
            lines.append("No groups.")
            return
        lines.append("| group | total | TP | TN | FP | FN | accuracy | FPR | FNR |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for group, values in groups.items():
            def fmt(name: str) -> str:
                value = values.get(name)
                if isinstance(value, float):
                    return f"{value:.4f}"
                if value is None:
                    return ""
                return str(value)
            lines.append(
                f"| `{group}` | {fmt('total')} | {fmt('tp')} | {fmt('tn')} | {fmt('fp')} | {fmt('fn')} | "
                f"{fmt('accuracy')} | {fmt('false_pass_rate')} | {fmt('false_fail_rate')} |"
            )

    grouped = report.get("grouped_metrics") or {}
    append_group_table("By Benchmark Task", grouped.get("benchmark_task_id") or {})
    append_group_table("By Failure Type", grouped.get("failure_type") or {})

    lines.extend(["", "## Trace Schema Warnings", ""])
    schema_warnings = report.get("trace_schema_warnings") or []
    if schema_warnings:
        lines.append("| trace_id | warnings |")
        lines.append("|---|---|")
        for item in schema_warnings:
            warnings_text = "<br>".join(item.get("warnings") or [])
            lines.append(f"| `{item.get('trace_id')}` | {warnings_text} |")
    else:
        lines.append("None.")

    lines.extend(["", "## False Passes", ""])
    false_passes = report.get("false_passes") or []
    if false_passes:
        lines.append("| trace_id | task | failure_type | evidence | notes |")
        lines.append("|---|---|---|---|---|")
        for row in false_passes:
            lines.append(
                f"| `{row.get('trace_id')}` | `{row.get('benchmark_task_id')}` | `{row.get('failure_type')}` | "
                f"{row.get('evidence_frames') or []} | {row.get('notes') or ''} |"
            )
    else:
        lines.append("None.")

    lines.extend(["", "## False Fails", ""])
    false_fails = report.get("false_fails") or []
    if false_fails:
        lines.append("| trace_id | task | expected_slots | evidence | notes |")
        lines.append("|---|---|---|---|---|")
        for row in false_fails:
            slots = json.dumps(row.get("expected_slots") or {}, ensure_ascii=False)
            lines.append(
                f"| `{row.get('trace_id')}` | `{row.get('benchmark_task_id')}` | `{slots}` | "
                f"{row.get('evidence_frames') or []} | {row.get('notes') or ''} |"
            )
    else:
        lines.append("None.")

    lines.extend(["", "## Per-Trace Results", ""])
    if not report["results"]:
        lines.append("No labels were found. Add records to `verification_benchmark/labels.jsonl`.")
    else:
        lines.append("| trace_id | task | truth | predicted | score | evidence | matched_nodes | error |")
        lines.append("|---|---|---|---:|---:|---|---|---|")
        for row in report["results"]:
            matched = ", ".join(
                f"{item.get('node_id')}@{item.get('frame_index')}"
                for item in row.get("matched_nodes") or []
            )
            lines.append(
                f"| `{row.get('trace_id')}` | `{row.get('benchmark_task_id')}` | {row.get('ground_truth')} | "
                f"{row.get('predicted_ok')} | {row.get('score')} | {row.get('evidence_frames') or []} | "
                f"{matched} | {row.get('error') or ''} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate benchmark traces against Ground Truth labels.")
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--mvp-config", default=str(DEFAULT_MVP_CONFIG))
    parser.add_argument("--trace-root", default=str(BENCHMARK_ROOT / "traces"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--mode",
        choices=["deterministic", "ocr", "llm", "llm_text", "llm_text_force", "fixture", "fixture_force_llm"],
        default="deterministic",
        help="Verification option set. llm is multimodal screenshot mode; llm_text is real API text mode for XML-only fixtures.",
    )
    parser.add_argument("--llm-api-key", default=None, help="OpenAI-compatible API key. Prefer environment variables.")
    parser.add_argument("--llm-base-url", default=None, help="OpenAI-compatible base URL, e.g. https://api.example.com/v1.")
    parser.add_argument("--llm-model", default=None, help="Model name, e.g. gpt-5.4.")
    parser.add_argument(
        "--avdag-log-level",
        default="WARNING",
        help="Console log level for MobiFlow/AVDAG internals. Use DEBUG for trace-level diagnosis.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    from avdag.logger import configure_logging

    configure_logging(level=args.avdag_log_level, use_colors=False, show_time=False, show_module=True)

    labels = load_labels(Path(args.labels))
    mvp_index = load_mvp_index(Path(args.mvp_config))
    report = evaluate(
        labels,
        mvp_index,
        args.mode,
        trace_root=Path(args.trace_root).resolve(),
        llm_api_key=args.llm_api_key,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
    )

    json_path = output_dir / f"benchmark_eval_{args.mode}.json"
    md_path = output_dir / f"benchmark_eval_{args.mode}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print("Benchmark evaluation complete.")
    print(f"Labels: {Path(args.labels).resolve()}")
    print(f"Reports: {json_path.resolve()} ; {md_path.resolve()}")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
