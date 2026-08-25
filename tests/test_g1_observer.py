from __future__ import annotations

import json

from verification_benchmark.evaluation_framework.g1_observer import (
    G1ObservationPolicy,
    describe_g1_frame,
)
from verification_benchmark.evaluation_framework.runner_source import G1FrameContext


def _descriptor(tmp_path, node_attributes: dict[str, str]):
    (tmp_path / "frame.json").write_text(
        json.dumps({"attributes": {}, "children": [{"attributes": node_attributes}]}),
        encoding="utf-8",
    )
    return describe_g1_frame(
        tmp_path,
        G1FrameContext(
            frame_index=1,
            previous_frame_index=None,
            pre_action_index=0,
            screenshot_ref=None,
            hierarchy_raw_json_ref="frame.json",
            hierarchy_xml_ref=None,
            screenshot_size=None,
            artifacts=(),
            raw_context_complete=True,
            missing_context=(),
        ),
        policy=G1ObservationPolicy(),
    )


def test_hidden_loading_container_is_structural_but_not_a_loading_signal(tmp_path):
    descriptor = _descriptor(
        tmp_path,
        {
            "id": "com.example:id/loadingContainer",
            "visible": "true",
            "opacity": "0.000000",
            "bounds": "[0,0][1080,2400]",
        },
    )

    assert descriptor.hierarchy.node_count == 2
    assert descriptor.hierarchy.loading_markers == ()


def test_visible_loading_container_remains_a_loading_signal(tmp_path):
    descriptor = _descriptor(
        tmp_path,
        {
            "id": "com.example:id/loadingContainer",
            "visible": "true",
            "opacity": "1.0",
            "bounds": "[0,0][1080,2400]",
        },
    )

    assert descriptor.hierarchy.loading_markers == ("loading",)
