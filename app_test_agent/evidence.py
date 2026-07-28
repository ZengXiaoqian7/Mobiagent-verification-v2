"""Deterministic evidence access helpers for App-test verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .executor import ExecutionRecord


@dataclass(frozen=True)
class TextEvidenceSlice:
    source: str
    texts: tuple[str, ...]
    frames: tuple[Mapping[str, Any], ...] = ()
    evidence_sufficient: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "texts": list(self.texts),
            "frames": [dict(frame) for frame in self.frames],
            "evidence_sufficient": self.evidence_sufficient,
        }


class ExecutionEvidence:
    def __init__(self, execution: ExecutionRecord):
        self.execution = execution
        self.frames = tuple(
            frame
            for frame in execution.metadata.get("frames", ())
            if isinstance(frame, Mapping)
        )
        raw_frame_texts = execution.metadata.get("frame_visible_texts", {})
        self.frame_texts = (
            {
                int(frame_id): tuple(str(item) for item in texts if str(item))
                for frame_id, texts in raw_frame_texts.items()
                if str(frame_id).isdigit() and isinstance(texts, list)
            }
            if isinstance(raw_frame_texts, Mapping)
            else {}
        )

    def initial_texts(self) -> tuple[str, ...]:
        raw = self.execution.metadata.get("initial_visible_texts")
        if isinstance(raw, list):
            return tuple(str(item) for item in raw if str(item))
        first_pre_frame = next(
            (
                result.pre_frame
                for result in self.execution.step_results
                if result.pre_frame is not None
            ),
            None,
        )
        if first_pre_frame is not None:
            return self.texts_for_frame(first_pre_frame)
        return ()

    def texts_for_frame(self, frame_id: int) -> tuple[str, ...]:
        if frame_id in self.frame_texts:
            return self.frame_texts[frame_id]
        for frame in self.frames:
            if frame.get("frame_id") != frame_id:
                continue
            return _frame_texts(frame)
        return ()

    def after_step_text_slice(
        self,
        step_id: str,
        observation_policy: Mapping[str, Any],
    ) -> TextEvidenceSlice:
        result = next(
            (item for item in self.execution.step_results if item.step_id == step_id),
            None,
        )
        if result is None:
            return TextEvidenceSlice(
                source="missing_after_step",
                texts=(),
                evidence_sufficient=False,
            )
        max_wait_ms = observation_policy.get("max_wait_ms")
        selected_frames: list[Mapping[str, Any]] = []
        selected_texts: list[str] = []
        for frame_id in result.post_frames:
            frame = self._frame_for_id(frame_id)
            if frame is not None and isinstance(max_wait_ms, int):
                relative = frame.get("relative_to_action_ms")
                if isinstance(relative, int) and relative > max_wait_ms:
                    continue
            frame_texts = self.texts_for_frame(frame_id)
            if frame is not None:
                selected_frames.append(frame)
            elif frame_texts:
                selected_frames.append(
                    {
                        "frame_id": frame_id,
                        "visible_texts": list(frame_texts),
                        "stability": "UNKNOWN",
                    }
                )
            selected_texts.extend(frame_texts)
        return TextEvidenceSlice(
            source=f"after_step:{step_id}",
            texts=tuple(dict.fromkeys(selected_texts)),
            frames=tuple(selected_frames),
            evidence_sufficient=bool(selected_frames and selected_texts),
        )

    def observed_text_slice(self) -> TextEvidenceSlice:
        texts: list[str] = []
        frames = list(self.frames)
        for frame in self.frames:
            texts.extend(_frame_texts(frame))
        if texts:
            return TextEvidenceSlice(
                source="observation_frames",
                texts=tuple(dict.fromkeys(texts)),
                frames=tuple(frames),
                evidence_sufficient=True,
            )
        return TextEvidenceSlice(
            source="final_state_fallback",
            texts=self.execution.final_state.visible_texts,
            evidence_sufficient=self.execution.final_state.evidence_sufficient,
        )

    def _frame_for_id(self, frame_id: int) -> Mapping[str, Any] | None:
        for frame in self.frames:
            if frame.get("frame_id") == frame_id:
                return frame
        return None


def text_contains(haystack: tuple[str, ...], needle: str) -> bool:
    return any(needle in item for item in haystack)


def _frame_texts(frame: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("visible_texts", "ocr_texts"):
        raw = frame.get(key)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if str(item))
    return tuple(values)
