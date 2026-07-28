"""Optional MobiFlow baseline adapter for the packaged verifier.

MobiFlow remains a comparison backend.  It never supplies the primary verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .phase5_full_verifier_comparison import (
    VisionCallRecorder,
    run_mobiflow_case,
)
from .phase5_trace_case import CasePaths


@dataclass(frozen=True)
class MobiFlowBaselineAdapter:
    mobiflow_root: Path
    output_dir: Path

    def verify(
        self, case: CasePaths, recorder: VisionCallRecorder
    ) -> Mapping[str, Any]:
        return run_mobiflow_case(
            paths=case,
            mobiflow_root=self.mobiflow_root,
            output_dir=self.output_dir,
            recorder=recorder,
        )


__all__ = ["MobiFlowBaselineAdapter"]
