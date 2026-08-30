"""PyInstaller-friendly entry point for the MobiAgent PC client."""

import argparse
import json
from pathlib import Path

from pc_client.app import main
from pc_client.runtime_paths import DEFAULT_CASE_PATH, DEFAULT_RUNNER_ROOT
from pc_client.service import PcEvaluationMode, PcEvaluationRequest, run_pc_evaluation
from utils.load_md_prompt import validate_runtime_prompt_assets


def _entry() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-mock", action="store_true")
    parser.add_argument("--test-case", type=Path, default=DEFAULT_CASE_PATH)
    parser.add_argument("--output-dir", type=Path)
    args, _unknown = parser.parse_known_args()
    if not args.smoke_mock:
        return main()
    if args.output_dir is None:
        raise SystemExit("--smoke-mock requires --output-dir")
    runtime_prompts = validate_runtime_prompt_assets()
    result = run_pc_evaluation(
        PcEvaluationRequest(
            mode=PcEvaluationMode.MOCK,
            test_case_path=args.test_case,
            output_dir=args.output_dir,
            mock_scenario="pass",
            runner_root=DEFAULT_RUNNER_ROOT,
        )
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "pc_client_smoke_result.json").write_text(
        json.dumps(
            {
                **result.as_dict(),
                "runtime_prompt_assets": {
                    "status": "PASS",
                    "count": len(runtime_prompts),
                    "names": list(runtime_prompts),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_entry())
