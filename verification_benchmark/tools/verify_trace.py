"""Verify one collected trace with the integrated MobiFlow-upgrade verifier."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

from verification_benchmark.evaluation_framework.mobiflow_compat import (
    MobiFlowBaselineAdapter,
)
from verification_benchmark.evaluation_framework.phase5_full_verifier_comparison import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ProviderConfig,
    VisionCallRecorder,
)
from verification_benchmark.evaluation_framework.phase5_intake import (
    Phase5IntakeError,
    write_new_json,
)
from verification_benchmark.evaluation_framework.phase5_trace_case import (
    CasePaths,
    find_run_manifest,
)
from verification_benchmark.evaluation_framework.upgraded_verifier import (
    UpgradedVerifierConfig,
    verify_trace_case,
)
from verification_benchmark.evaluation_framework.jit_contract_compiler import (
    JitAppMetadata,
    JitCompileRequest,
)
from verification_benchmark.evaluation_framework.jit_model_proposer import (
    OpenAICompatibleJitProposer,
)
from verification_benchmark.evaluation_framework.task_spec import TaskSpec


RESULT_FILE = "verification_result.json"
DIAGNOSTIC_DIR = "diagnostics"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intake-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--task-contract", type=Path)
    parser.add_argument("--selection-key")
    parser.add_argument("--contract-freeze", type=Path)
    parser.add_argument(
        "--enable-validated-jit",
        action="store_true",
        help=(
            "compile a task-only validated JIT contract for an existing trace "
            "when no registry/template contract matches"
        ),
    )
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument(
        "--deterministic-only",
        action="store_true",
        help="Run structured/local checks only; unresolved semantic criteria abstain.",
    )
    parser.add_argument("--with-mobiflow-comparison", action="store_true")
    parser.add_argument("--mobiflow-root", type=Path)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--transport", default=None)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def _provider(args: argparse.Namespace) -> ProviderConfig:
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        raise Phase5IntakeError(
            f"required API key environment variable is unset: {args.api_key_env}"
        )
    transport = args.transport or os.environ.get("MOBIAGENT_LLM_TRANSPORT", "raw_http")
    return ProviderConfig(
        base_url=args.base_url,
        model=args.model,
        api_key_env=args.api_key_env,
        api_key=api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        transport=transport,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise Phase5IntakeError(
            f"refusing to overwrite verifier output directory: {output_dir}"
        )
    if args.with_mobiflow_comparison and args.mobiflow_root is None:
        raise Phase5IntakeError(
            "--mobiflow-root is required with --with-mobiflow-comparison"
        )
    if args.mobiflow_root is not None and not args.with_mobiflow_comparison:
        raise Phase5IntakeError(
            "--mobiflow-root has no effect without --with-mobiflow-comparison"
        )
    if args.deterministic_only and args.with_mobiflow_comparison:
        raise Phase5IntakeError(
            "MobiFlow VLM comparison is unavailable in --deterministic-only mode"
        )
    if args.enable_validated_jit and args.deterministic_only:
        raise Phase5IntakeError(
            "--enable-validated-jit requires model access; remove --deterministic-only"
        )
    if args.enable_validated_jit and args.contract_freeze is not None:
        raise Phase5IntakeError(
            "--enable-validated-jit cannot be combined with --contract-freeze"
        )

    provider = None if args.deterministic_only else _provider(args)
    case = CasePaths(
        run_dir=args.run_dir,
        intake_receipt=args.intake_receipt,
        task_contract=args.task_contract,
        contract_freeze=args.contract_freeze,
    )
    recorder = (
        None
        if provider is None
        else (
            VisionCallRecorder(provider)
            if args.cache_dir is None
            else VisionCallRecorder(provider, args.cache_dir)
        )
    )
    task_spec = None
    jit_request = None
    jit_proposer = None
    selection_key = args.selection_key
    if args.enable_validated_jit:
        assert provider is not None
        task_spec = TaskSpec.from_run_manifest(
            find_run_manifest(args.run_dir.resolve(strict=True))
        )
        jit_request = JitCompileRequest(
            task_description=task_spec.task_text,
            app_metadata=JitAppMetadata(
                app_id=task_spec.initial_app or "unknown-app",
                app_name=task_spec.initial_app or "unknown-app",
                platform="HarmonyOS",
                task_family=(
                    None
                    if task_spec.task_family == "unseen"
                    else task_spec.task_family
                ),
                risk_tier={
                    "read_only": "LOW",
                    "low_risk_write": "MEDIUM",
                    "high_risk": "HIGH",
                }[task_spec.risk_level],
            ),
        )
        if selection_key is None:
            selection_key = jit_request.selection_key
        jit_proposer = OpenAICompatibleJitProposer(
            base_url=provider.base_url,
            model=provider.model,
            api_key=provider.api_key,
            timeout=provider.timeout,
            max_retries=provider.max_retries,
        )
    verification = verify_trace_case(
        case,
        UpgradedVerifierConfig(
            provider=provider,
            selection_key=selection_key,
            enable_validated_jit=args.enable_validated_jit,
            jit_request=jit_request,
            jit_proposer=jit_proposer,
            task_spec=task_spec,
            include_diagnostics=args.diagnostics,
            continue_on_error=not args.fail_fast,
            cache_dir=args.cache_dir,
        ),
        recorder=recorder,
    )
    result_payload = verification.result.as_dict()
    if args.with_mobiflow_comparison:
        assert provider is not None
        baseline = MobiFlowBaselineAdapter(args.mobiflow_root, output_dir)
        try:
            comparison = baseline.verify(case, VisionCallRecorder(provider))
            result_payload["mobiflow_baseline"] = {
                "ok": comparison["ok"],
                "verdict": comparison["verdict"],
                "reason": comparison["reason"],
            }
        except Exception as exc:  # noqa: BLE001 - baseline is never primary.
            result_payload["mobiflow_baseline"] = {
                "ok": False,
                "verdict": "ABSTAIN",
                "reason": f"baseline error: {type(exc).__name__}: {exc}",
            }

    result_path = output_dir / RESULT_FILE
    write_new_json(result_path, result_payload)
    diagnostic_path = None
    if args.diagnostics:
        run_id = str(verification.diagnostics.get("run_id") or "unknown-run")
        diagnostic_path = output_dir / DIAGNOSTIC_DIR / f"{run_id}.json"
        write_new_json(diagnostic_path, verification.diagnostics)
    print(
        json.dumps(
            {
                "status": "UPGRADED_TRACE_VERIFICATION_COMPLETE",
                "result": str(result_path),
                "verdict": verification.result.verdict,
                "ok": verification.result.ok,
                "diagnostics": (
                    None if diagnostic_path is None else str(diagnostic_path)
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
