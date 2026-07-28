"""Full Phase 5 VLM verifier and external MobiFlow engine comparison."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import requests

from .phase5_ground_truth import (
    ground_truth_verdict,
    validate_frozen_ground_truth,
)
from .phase5_trace_case import (
    CasePaths,
    _find_run_manifest,
    _first_source_sort_frame,
    _input_texts,
    _load_actions,
    _load_json,
    _open_app_targets,
    _trace_dir,
)
from .phase5_intake import (
    CLAIM_BOUNDARY,
    Phase5IntakeError,
    file_sha256,
    semantic_sha256,
    source_file_manifest,
)


FULL_VERIFIER_VERSION = "harmony-eval-phase5-full-vlm-verifier-v2"
COMPARISON_REPORT_SCHEMA_VERSION = (
    "harmony-eval-phase5-full-verifier-comparison-report-v1"
)
DEFAULT_BASE_URL = "https://api.horizon1123.top/v1"
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_API_KEY_ENV = "MOBIAGENT_API_KEY"
STATUSES = {
    "SATISFIED",
    "VIOLATED",
    "UNKNOWN_EVIDENCE",
    "UNSUPPORTED_CAPABILITY",
}


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)
    timeout: float = 90.0
    max_retries: int = 1
    transport: str = "raw_http"

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("provider base_url must not be empty")
        if not self.model.strip():
            raise ValueError("provider model must not be empty")
        if not self.api_key_env.strip() or not self.api_key:
            raise ValueError("provider API key and environment variable are required")
        if self.timeout <= 0:
            raise ValueError("provider timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("provider max_retries must be non-negative")
        if self.transport != "raw_http":
            raise ValueError("only raw_http transport is supported")


class VisionCallRecorder:
    def __init__(self, provider: ProviderConfig, cache_dir: Path | None = None) -> None:
        self.provider = provider
        self.cache_dir = None if cache_dir is None else cache_dir.resolve()
        self.calls: list[dict[str, Any]] = []

    def _image_part(self, image: Path) -> dict[str, Any]:
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        suffix = image.suffix.lower()
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }

    def _extract_json(self, text: str) -> Mapping[str, Any]:
        cleaned = text.strip()
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
            if fenced:
                value = json.loads(fenced.group(1))
            else:
                match = re.search(r"\{.*\}", cleaned, re.DOTALL)
                if not match:
                    raise
                value = json.loads(match.group(0))
        if not isinstance(value, Mapping):
            raise ValueError("VLM response JSON root must be an object")
        return value

    def judge_json(
        self,
        *,
        prompt: str,
        images: Sequence[Path],
        call_label: str,
        schema_hint: str,
    ) -> Mapping[str, Any]:
        image_digests = [file_sha256(image) for image in images]
        cache_identity = {
            "schema": "mobiagent-verifier-model-cache-v1",
            "base_url": self.provider.base_url.rstrip("/"),
            "model": self.provider.model,
            "prompt": prompt,
            "schema_hint": schema_hint,
            "image_sha256": image_digests,
        }
        cache_key = hashlib.sha256(
            json.dumps(
                cache_identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        cache_path = (
            None if self.cache_dir is None else self.cache_dir / f"{cache_key}.json"
        )
        if cache_path is not None and cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("identity") != cache_identity or not isinstance(
                cached.get("response"), Mapping
            ):
                raise RuntimeError("model cache identity mismatch or corrupt response")
            self.calls.append(
                {
                    "label": call_label,
                    "image_count": len(images),
                    "image_sha256": image_digests,
                    "latency_ms": 0.0,
                    "usage": {},
                    "ok": True,
                    "cache_hit": True,
                    "cache_key": cache_key,
                }
            )
            return cached["response"]

        content: list[Mapping[str, Any]] = [
            {
                "type": "text",
                "text": (prompt + "\n\nReturn JSON only. " + schema_hint),
            }
        ]
        content.extend(self._image_part(image) for image in images)
        payload = {
            "model": self.provider.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
        }
        last_error: str | None = None
        started = time.perf_counter()
        for attempt in range(self.provider.max_retries + 1):
            try:
                response = requests.post(
                    self.provider.base_url.rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.provider.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.provider.timeout,
                )
                if response.status_code >= 400 and "response_format" in payload:
                    fallback = dict(payload)
                    fallback.pop("response_format", None)
                    response = requests.post(
                        self.provider.base_url.rstrip("/") + "/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.provider.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=fallback,
                        timeout=self.provider.timeout,
                    )
                response.raise_for_status()
                body = response.json()
                text = body["choices"][0]["message"]["content"]
                parsed = self._extract_json(str(text))
                latency_ms = round((time.perf_counter() - started) * 1000, 3)
                self.calls.append(
                    {
                        "label": call_label,
                        "image_count": len(images),
                        "image_sha256": image_digests,
                        "latency_ms": latency_ms,
                        "usage": body.get("usage") or {},
                        "response_id": body.get("id"),
                        "ok": True,
                        "cache_hit": False,
                        "cache_key": cache_key,
                    }
                )
                if cache_path is not None:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    rendered = (
                        json.dumps(
                            {"identity": cache_identity, "response": parsed},
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                            allow_nan=False,
                        )
                        + "\n"
                    )
                    try:
                        with cache_path.open("x", encoding="utf-8", newline="\n") as stream:
                            stream.write(rendered)
                    except FileExistsError:
                        if cache_path.read_text(encoding="utf-8") != rendered:
                            raise RuntimeError("refusing to overwrite different model cache")
                return parsed
            except Exception as exc:  # noqa: BLE001 - explicit error in report.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.provider.max_retries:
                    break
                time.sleep(1.0)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self.calls.append(
            {
                "label": call_label,
                "image_count": len(images),
                "image_sha256": [file_sha256(image) for image in images],
                "latency_ms": latency_ms,
                "usage": {},
                "ok": False,
                "error": last_error,
            }
        )
        raise RuntimeError(last_error or "VLM call failed")


def _status(value: Any) -> str:
    text = str(value or "UNKNOWN_EVIDENCE").upper()
    return text if text in STATUSES else "UNKNOWN_EVIDENCE"


def _criterion_from_vlm(
    parsed: Mapping[str, Any], *, default_reason: str
) -> Mapping[str, Any]:
    return {
        "status": _status(parsed.get("status")),
        "reason": str(parsed.get("reason") or default_reason),
        "vlm_confidence": parsed.get("confidence"),
        "visible_evidence": parsed.get("visible_evidence"),
    }


def _criterion(
    status: str, reason: str, evidence: Mapping[str, Any] | None = None
) -> Mapping[str, Any]:
    return {"status": status, "reason": reason, "evidence": evidence}


def _compose_all_of_dependency(
    independent: Mapping[str, Any],
    prerequisites: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Compose a child judgment without allowing it to re-judge its parents."""

    own_status = _status(independent.get("status"))
    parent_statuses = {
        criterion_id: _status(record.get("status"))
        for criterion_id, record in prerequisites.items()
    }
    if own_status == "VIOLATED" or all(
        status == "SATISFIED" for status in parent_statuses.values()
    ):
        return independent
    if any(status == "VIOLATED" for status in parent_statuses.values()):
        return _criterion(
            "VIOLATED",
            "one or more prerequisite criteria are violated",
            {
                "independent_condition": independent,
                "prerequisite_statuses": parent_statuses,
            },
        )
    return _criterion(
        "UNKNOWN_EVIDENCE",
        "prerequisite evidence is unresolved",
        {
            "independent_condition": independent,
            "prerequisite_statuses": parent_statuses,
        },
    )


def _full_verdict(criteria: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [row["status"] for row in criteria.values()]
    if "VIOLATED" in statuses:
        return "FAIL"
    if any(status != "SATISFIED" for status in statuses):
        return "ABSTAIN"
    return "PASS"


def _image(trace: Path, index: int) -> Path:
    path = trace / f"{index}.jpg"
    if not path.is_file():
        raise Phase5IntakeError(f"missing evidence frame: {path}")
    return path


def _validate_source_tree(paths: CasePaths, run_dir: Path) -> Mapping[str, Any]:
    intake = _load_json(
        paths.intake_receipt.resolve(strict=True), "Phase 5 intake receipt"
    )
    files = list(source_file_manifest(run_dir))
    expected = semantic_sha256(files)
    if intake.get("source_tree_sha256") != expected:
        raise Phase5IntakeError("intake source tree hash drift")
    return {"source_tree_sha256": expected}


def evaluate_full_case(
    paths: CasePaths,
    recorder: VisionCallRecorder,
    *,
    skip_sales_sort_state: bool = False,
) -> Mapping[str, Any]:
    before_calls = len(recorder.calls)
    run_dir = paths.run_dir.resolve(strict=True)
    run = _find_run_manifest(run_dir)
    run_id = str(run["run_id"])
    task_id = str(run["task_id"])
    trace = _trace_dir(run_dir, run)
    action_payload, actions = _load_actions(trace)
    source_tree = _validate_source_tree(paths, run_dir)
    input_texts = _input_texts(actions)
    source_query = input_texts[0] if input_texts else ""
    target_query = input_texts[-1] if len(input_texts) >= 2 else ""
    target_app = str(run.get("target_app") or "")
    open_targets = _open_app_targets(actions)
    sort_frame = _first_source_sort_frame(actions, target_app)
    if sort_frame is None:
        raise Phase5IntakeError(f"{run_id}: cannot derive source evidence frame")
    terminal_frame = int(
        actions[-1].get("action_index") or action_payload.get("action_count") or 0
    )
    if terminal_frame <= 0:
        raise Phase5IntakeError(f"{run_id}: cannot derive terminal frame")

    source_image = _image(trace, sort_frame)
    terminal_image = _image(trace, terminal_frame)
    task_text = str(
        action_payload.get("task_description")
        or action_payload.get("old_task_description")
        or ""
    )

    source_page = recorder.judge_json(
        call_label=f"{run_id}:full:source_page",
        images=[source_image],
        prompt=(
            "You are a strict mobile-agent verifier. Inspect this Taobao screenshot only. "
            f"The task is: {task_text}\n"
            f"Expected source query: {source_query!r}. Later transferred product phrase: {target_query!r}.\n"
            "Judge: (1) whether the source query is visibly present, "
            + (
                ""
                if skip_sales_sort_state
                else "(2) whether the requested sort control is ACTIVATED/SELECTED, not merely visible, "
            )
            + "(3) whether the transferred phrase is visibly supported by a non-ad source card, "
            "and (4) whether that phrase comes from the first eligible non-ad/non-live product card "
            "under the visible card ordering required by the task. For judgment (4), inspect only "
            "card order, exclusion labels, and the transferred card identity. Do not use the sort "
            "control's selected/inactive appearance to decide source_selection_rule; that state is "
            "a separate criterion. "
            + (
                "The control-state criterion is evaluated by the generic layered state checker; do not judge it here."
                if skip_sales_sort_state
                else "Be fail-closed about a clearly inactive requested control."
            )
        ),
        schema_hint=(
            '{"source_query_visible":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."},'
            + (
                ""
                if skip_sales_sort_state
                else '"sales_sort_activated":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."},'
            )
            +
            '"transfer_phrase_source_supported":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."},'
            '"source_selection_rule":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."}}'
        ),
    )
    target_page = recorder.judge_json(
        call_label=f"{run_id}:full:target_page",
        images=[terminal_image],
        prompt=(
            "You are a strict mobile-agent verifier. Inspect this terminal Xiaohongshu screenshot only. "
            f"The task is: {task_text}\n"
            f"Transferred product phrase / target query: {target_query!r}.\n"
            "Judge whether the target app/search page visibly contains the transferred phrase "
            "and whether public results include same-product or clearly related evidence. "
            "Do not trust the agent's self-report."
        ),
        schema_hint=(
            '{"target_query_visible":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."},'
            '"same_product_target_evidence":{"status":"SATISFIED|VIOLATED|UNKNOWN_EVIDENCE","reason":"..."}}'
        ),
    )

    criteria = {
        "trace.integrity": _criterion(
            "SATISFIED", "intake source tree hash matches raw run", source_tree
        ),
        "process.source_query_visible": _criterion_from_vlm(
            (
                source_page.get("source_query_visible")
                if isinstance(source_page.get("source_query_visible"), Mapping)
                else {}
            ),
            default_reason="source query visual judgment",
        ),
        "process.sales_sort_activated": (
            _criterion(
                "UNKNOWN_EVIDENCE",
                "control state is delegated to the generic layered state checker",
            )
            if skip_sales_sort_state
            else _criterion_from_vlm(
                (
                    source_page.get("sales_sort_activated")
                    if isinstance(source_page.get("sales_sort_activated"), Mapping)
                    else {}
                ),
                default_reason="requested control-state visual judgment",
            )
        ),
        "process.transfer_phrase_source_supported": _criterion_from_vlm(
            (
                source_page.get("transfer_phrase_source_supported")
                if isinstance(
                    source_page.get("transfer_phrase_source_supported"), Mapping
                )
                else {}
            ),
            default_reason="transfer phrase source support judgment",
        ),
        "process.target_app_open": _criterion(
            "SATISFIED" if target_app in open_targets else "VIOLATED",
            (
                "target app open_app action is present"
                if target_app in open_targets
                else "target app open_app action missing"
            ),
            {"target_app": target_app, "open_app_targets": open_targets},
        ),
        "process.target_query_visible": _criterion_from_vlm(
            (
                target_page.get("target_query_visible")
                if isinstance(target_page.get("target_query_visible"), Mapping)
                else {}
            ),
            default_reason="target query visual judgment",
        ),
        "outcome.same_product_target_evidence": _criterion_from_vlm(
            (
                target_page.get("same_product_target_evidence")
                if isinstance(target_page.get("same_product_target_evidence"), Mapping)
                else {}
            ),
            default_reason="same-product target evidence judgment",
        ),
        "termination.done_after_target": _criterion(
            (
                "SATISFIED"
                if actions[-1].get("type") == "done" and target_app in open_targets
                else "VIOLATED"
            ),
            (
                "done is final after target app was requested"
                if actions[-1].get("type") == "done" and target_app in open_targets
                else "done finality failed"
            ),
            {"last_action_type": actions[-1].get("type")},
        ),
    }
    independent_selection = _criterion_from_vlm(
        (
            source_page.get("source_selection_rule")
            if isinstance(source_page.get("source_selection_rule"), Mapping)
            else {}
        ),
        default_reason="first eligible source-card selection judgment",
    )
    criteria["process.source_selection_rule"] = (
        independent_selection
        if skip_sales_sort_state
        else _compose_all_of_dependency(
            independent_selection,
            {
                "process.sales_sort_activated": criteria[
                    "process.sales_sort_activated"
                ]
            },
        )
    )
    return {
        "run_id": run_id,
        "task_id": task_id,
        "experiment_id": run["experiment_id"],
        "verifier": "PHASE5_FULL_VLM",
        "verifier_version": FULL_VERIFIER_VERSION,
        "verdict": _full_verdict(criteria),
        "criteria": criteria,
        "evidence_frames": {"source": sort_frame, "terminal": terminal_frame},
        "request_count": len(recorder.calls) - before_calls,
    }


def _write_text_new(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(text.encode("utf-8"))


def _mobiflow_rule_text(task_id: str) -> str:
    return f"""task_id: {task_id}
app_id: phase5_cross_app
task_type: phase5_read_only_cross_app
description: Phase 5 cross-app read-only product research verification.
nodes:
  - id: source_query_visible
    name: 淘宝来源搜索词可见
    condition:
      type: escalate
      params:
        ocr:
          all: ["通勤双肩包"]
        llm:
          prompt: 当前或下一帧是否显示淘宝搜索结果页，且搜索框中可见任务的来源查询词？
          expected_true: true
  - id: sales_sort_activated
    deps: [source_query_visible]
    name: 销量排序已激活
    condition:
      type: escalate
      params:
        llm:
          prompt: 当前或下一帧是否显示淘宝搜索结果页中的“销量”排序已被激活/选中（例如红色或高亮），而不是仅显示灰色未选中的“销量”文字？
          expected_true: true
  - id: target_same_product_evidence
    deps: [sales_sort_activated]
    name: 小红书同品证据
    condition:
      type: escalate
      params:
        ocr:
          any: ["双肩包", "背包"]
        llm:
          prompt: 当前或下一帧是否显示小红书搜索结果页，并且搜索词/结果与从淘宝来源卡片转移的具体商品身份短语一致或明显同品相关？
          expected_true: true
success:
  all_of: [source_query_visible, sales_sort_activated, target_same_product_evidence]
"""


def _bool_from_vlm(parsed: Mapping[str, Any]) -> bool | None:
    result = str(parsed.get("result") or parsed.get("status") or "").lower()
    if result in {"yes", "true", "satisfied", "sat"}:
        return True
    if result in {"no", "false", "violated", "fail"}:
        return False
    return None


def run_mobiflow_case(
    *,
    paths: CasePaths,
    mobiflow_root: Path,
    output_dir: Path,
    recorder: VisionCallRecorder,
) -> Mapping[str, Any]:
    run_dir = paths.run_dir.resolve(strict=True)
    run = _find_run_manifest(run_dir)
    trace = _trace_dir(run_dir, run)
    run_id = str(run["run_id"])
    rule_path = output_dir / "mobiflow_rules" / f"{run_id}.yaml"
    _write_text_new(rule_path, _mobiflow_rule_text(f"{run_id}_phase5_comparison"))

    root = mobiflow_root.resolve(strict=True)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from avdag.types import VerifierOptions  # type: ignore
    from avdag.verifier import verify_task_folder  # type: ignore

    def _llm(ctx: Mapping[str, Any]) -> Optional[bool]:
        frame = ctx.get("frame") if isinstance(ctx, Mapping) else {}
        params = ctx.get("params") if isinstance(ctx, Mapping) else {}
        if not isinstance(frame, Mapping) or not isinstance(params, Mapping):
            return None
        images: list[Path] = []
        for key in ("image",):
            value = frame.get(key)
            if isinstance(value, str) and Path(value).is_file():
                images.append(Path(value))
        next_frame = frame.get("_next")
        if isinstance(next_frame, Mapping):
            value = next_frame.get("image")
            if isinstance(value, str) and Path(value).is_file():
                images.append(Path(value))
        if not images:
            return None
        parsed = recorder.judge_json(
            call_label=f"{run_id}:mobiflow:{params.get('prompt','node')[:40]}",
            images=images[:2],
            prompt=(
                "You are evaluating one MobiFlow DAG node for a mobile trace. "
                f"Global task: {frame.get('task_description') or ''}\n"
                f"Node requirement: {params.get('prompt') or ''}\n"
                "Use the screenshots as primary evidence. Return yes only if the node is visibly satisfied."
            ),
            schema_hint='{"result":"yes|no|unknown","reason":"brief visible-evidence reason"}',
        )
        return _bool_from_vlm(parsed)

    def _ocr(frame: Mapping[str, Any]) -> str | None:
        xml = frame.get("xml_text") if isinstance(frame, Mapping) else ""
        if not isinstance(xml, str):
            return None
        texts = re.findall(r'(?:text|content-desc|hint)="([^"]+)"', xml)
        return " ".join(texts) or None

    before_calls = len(recorder.calls)
    started = time.perf_counter()
    result = verify_task_folder(
        str(rule_path),
        str(trace),
        VerifierOptions(
            ocr=_ocr,
            llm=_llm,
            force_llm_verification=False,
            escalation_order=["ocr", "llm"],
            log_decisions=True,
            max_llm_retries=1,
        ),
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "run_id": run_id,
        "task_id": str(run["task_id"]),
        "experiment_id": run["experiment_id"],
        "verifier": "MOBIFLOW_EXTERNAL_ENGINE",
        "mobiflow_root": str(root),
        "rule_sha256": file_sha256(rule_path),
        "verdict": "PASS" if result.ok else "FAIL",
        "ok": result.ok,
        "matched_nodes": [match.node_id for match in result.matched],
        "matched_frames": [
            {"node_id": match.node_id, "frame_index": match.frame_index}
            for match in result.matched
        ],
        "reason": result.reason,
        "manual_review_needed": result.manual_review_needed,
        "total_score": result.total_score,
        "latency_ms": latency_ms,
        "request_count": len(recorder.calls) - before_calls,
        "decision_logs": [
            {
                "frame_index": log.frame_index,
                "node_id": log.node_id,
                "decision": log.decision,
                "checker_type": log.checker_type,
                "checker_result": log.checker_result,
                "matched_keywords": log.matched_keywords,
                "unmatched_keywords": log.unmatched_keywords,
            }
            for log in result.logs
        ],
    }


def _load_gt_after_decisions(
    paths: CasePaths, *, run_id: str, task_id: str
) -> Mapping[str, Any]:
    if paths.ground_truth is None:
        raise Phase5IntakeError("evaluation/comparison mode requires ground truth")
    gt_path = paths.ground_truth.resolve(strict=True)
    gt = _load_json(gt_path, "Phase 5 single-operator GT")
    validate_frozen_ground_truth(gt, run_id=run_id, task_id=task_id)
    return {
        "verdict": ground_truth_verdict(gt),
        "failure_codes": gt.get("failure_codes", []),
        "file_sha256": file_sha256(gt_path),
        "semantic_sha256": semantic_sha256(gt),
        "publication_eligible": False,
    }


def build_full_comparison_report(
    *,
    cases: Sequence[CasePaths],
    provider: ProviderConfig,
    mobiflow_root: Path,
    output_dir: Path,
) -> Mapping[str, Any]:
    full_recorder = VisionCallRecorder(provider)
    mobiflow_recorder = VisionCallRecorder(provider)
    rows = []
    for case in cases:
        full = evaluate_full_case(case, full_recorder)
        mobiflow = run_mobiflow_case(
            paths=case,
            mobiflow_root=mobiflow_root,
            output_dir=output_dir,
            recorder=mobiflow_recorder,
        )
        gt = _load_gt_after_decisions(
            case,
            run_id=full["run_id"],
            task_id=full["task_id"],
        )
        rows.append(
            {
                "run_id": full["run_id"],
                "task_id": full["task_id"],
                "experiment_id": full["experiment_id"],
                "ground_truth": gt,
                "phase5_full_vlm": full,
                "mobiflow_external": mobiflow,
                "phase5_match_gt": full["verdict"] == gt["verdict"],
                "mobiflow_match_gt": mobiflow["verdict"] == gt["verdict"],
            }
        )
    summary = {
        "total": len(rows),
        "gt_pass": sum(row["ground_truth"]["verdict"] == "PASS" for row in rows),
        "gt_fail": sum(row["ground_truth"]["verdict"] == "FAIL" for row in rows),
        "phase5_correct": sum(row["phase5_match_gt"] is True for row in rows),
        "mobiflow_correct": sum(row["mobiflow_match_gt"] is True for row in rows),
        "phase5_predicted_fail": sum(
            row["phase5_full_vlm"]["verdict"] == "FAIL" for row in rows
        ),
        "mobiflow_predicted_fail": sum(
            row["mobiflow_external"]["verdict"] == "FAIL" for row in rows
        ),
    }
    return {
        "schema_version": COMPARISON_REPORT_SCHEMA_VERSION,
        "claim_boundary": CLAIM_BOUNDARY,
        "publication_eligible": False,
        "ground_truth_consumed_by_verifiers": False,
        "ground_truth_consumed_after_verifier_decision_for_reporting": True,
        "provider": {
            "base_url": provider.base_url,
            "model": provider.model,
            "api_key_env": provider.api_key_env,
            "transport": provider.transport,
        },
        "phase5_full_vlm_version": FULL_VERIFIER_VERSION,
        "mobiflow_root": str(mobiflow_root.resolve(strict=True)),
        "external_model_calls": len(full_recorder.calls) + len(mobiflow_recorder.calls),
        "phase5_full_vlm_calls": full_recorder.calls,
        "mobiflow_calls": mobiflow_recorder.calls,
        "rows": rows,
        "summary": summary,
        "note": (
            "Full VLM development comparison on two already frozen single-operator GT cases. "
            "This is not a publication-ready performance claim."
        ),
    }


__all__ = [name for name in globals() if not name.startswith("_")]
