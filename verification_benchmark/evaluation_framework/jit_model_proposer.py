"""Production task-only proposer for the validated JIT Contract compiler."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from .jit_contract_compiler import (
    JIT_PROPOSAL_SCHEMA_VERSION,
    JitProposalResponse,
    JitStructuredOutputSpec,
)


JIT_MODEL_PROPOSER_VERSION = "mobiagent-openai-compatible-jit-proposer-v1"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


@dataclass(frozen=True)
class OpenAICompatibleJitProposer:
    """Call an OpenAI-compatible model without exposing execution evidence."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 90.0
    max_retries: int = 1
    proposer_id: str = "mobiagent-task-only-contract-model"
    proposer_version: str = JIT_MODEL_PROPOSER_VERSION
    supports_compiler_feedback: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("base_url", self.base_url),
            ("model", self.model),
            ("api_key", self.api_key),
            ("proposer_id", self.proposer_id),
            ("proposer_version", self.proposer_version),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError(f"JIT proposer {name} is invalid")
        if not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise ValueError("JIT proposer timeout must be positive")
        if (
            not isinstance(self.max_retries, int)
            or isinstance(self.max_retries, bool)
            or self.max_retries < 0
        ):
            raise ValueError("JIT proposer max_retries must be non-negative")

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    @staticmethod
    def _content(body: Mapping[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("JIT model response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        if not isinstance(message, Mapping):
            raise ValueError("JIT model response has no message")
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip():
            return json.dumps({"__refusal__": refusal.strip()}, ensure_ascii=False)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("JIT model response content is empty")
        return content.strip()

    def propose(
        self,
        request: Mapping[str, Any],
        *,
        response_format: JitStructuredOutputSpec,
    ) -> JitProposalResponse:
        # The compiler supplies task_description and app_metadata, plus optional
        # local compiler feedback after a failed proposal. No trace path,
        # screenshot, action, reasoning or verdict is accepted by this transport.
        raw_input = _plain(request)
        allowed_keys = {"task_description", "app_metadata", "compiler_feedback"}
        if not {"task_description", "app_metadata"}.issubset(raw_input) or not set(
            raw_input
        ).issubset(allowed_keys):
            raise ValueError("JIT proposer received non-task-only input")
        feedback = raw_input.get("compiler_feedback")
        if feedback is not None and (
            not isinstance(feedback, list)
            or any(not isinstance(item, str) for item in feedback)
        ):
            raise ValueError("JIT proposer compiler_feedback must be a string array")
        task_only = {
            "task_description": raw_input["task_description"],
            "app_metadata": raw_input["app_metadata"],
        }
        prompt = (
            "Compile the supplied mobile task into a conservative verification "
            "Contract proposal. Describe only success conditions observable from "
            "screenshots, raw hierarchy, actions, and timestamps. Never assume the "
            "agent succeeded. Prefer UNKNOWN-capable evidence over weak negative "
            "heuristics. Use only checker/capability enum values allowed by the "
            "provided JSON schema. Return exactly one JSON object.\n\n"
            "Capability values are a closed enum. The top-level "
            "required_capabilities array and every criteria[].required_capabilities "
            "array may contain only these exact strings: ACTIONS, "
            "HIERARCHY_RAW_JSON, HIERARCHY_XML, SCREENSHOT, TIMESTAMPS. Never use "
            "UI_TREE, HIERARCHY, OCR, VISION, IMAGE, TEXT, LLM, or other capability "
            "names.\n\n"
            "The returned root object must contain exactly these seven keys and "
            "no wrapper object: schema_version, task_family, justification, "
            "required_capabilities, criteria, g1_bindings, dag. Do not return "
            "ContractIR runtime fields such as contract_id, source, "
            "compiler_provenance, contract_sha256, or a nested proposal/contract "
            "object. The schema_version value must be exactly "
            f"{JIT_PROPOSAL_SCHEMA_VERSION!r}. The justification value must be "
            "one single line and exactly one sentence: do not use any period, "
            "question mark, or exclamation mark inside it, and put exactly one "
            "sentence terminator at the end.\n\n"
            "Every criteria item must contain exactly criterion_id, "
            "temporal_semantics, required, allow_obscured_persistence, "
            "required_capabilities, and description. Use temporal_semantics values "
            "from the schema such as PERSISTENT_STATE or PROCESS_OBLIGATION, and "
            "use required_capabilities as an array even when empty. For ordinary "
            "terminal-screen, search-result, opened-page, selected-tab, or visible "
            "confirmation tasks, prefer semantic criteria with SCREENSHOT and set "
            "dag to null. For ordinary search/query/lookup/navigation/filter tasks, "
            "do not add a PROCESS_OBLIGATION criterion merely to prove that a search "
            "or submit button was tapped; the terminal result/detail/filter state is "
            "the primary success evidence.\n\n"
            "For tasks that require an effectful user action such as sending, "
            "submitting, posting, commenting, replying, following, liking, "
            "collecting, adding to cart, or buying, require evidence that the "
            "action is submitted or the requested effect is visible. A draft text "
            "inside an editor/input box, an item detail page before add-to-cart, "
            "or an unconfirmed selection is not success. Include a required "
            "PROCESS_OBLIGATION criterion with ACTIONS when a submit/tap/process "
            "step is essential, and include a terminal PERSISTENT_STATE criterion "
            "that describes the completed/effect-visible state when such state is "
            "observable.\n\n"
            "Every g1_bindings item must contain exactly criterion_id, checker, "
            "and rois. Every roi item must contain exactly roi_id, bounds, "
            "coordinate_space, and reference_size. G1 binding checker is a closed "
            "enum and may contain only ROI_STABILITY, NO_BLOCKING_OVERLAY, or "
            "NOT_LOADING. Never use text, xml, regex, ocr, or llm as a "
            "g1_bindings[].checker value. If the task needs semantic text, XML, "
            "OCR, regex, or LLM evidence, set g1_bindings to [] unless ROI "
            "stability or overlay/loading checks are needed, and express those "
            "semantic checks under dag.nodes[].checkers instead.\n\n"
            "Every dag node must contain exactly node_id, condition_operator, "
            "score, and checkers. The score value must be a JSON integer number "
            "from 0 through 100, never a float such as 100.0, never a string, "
            "and never true/false. Within one dag node, checker_id values must "
            "be unique: do not include two llm checkers, two text checkers, or "
            "any repeated checker_id in the same node. Combine same-type "
            "conditions into one checker parameters object, or split them into "
            "separate DAG nodes. Every dag edge must contain exactly parent_id, "
            "child_id, and kind. The dag success object must contain exactly "
            "operator and node_ids. DAG checker_id is also a closed enum and may "
            "contain only these exact lowercase strings: llm, ocr, regex, text, "
            "xml. Never use screenshot, hierarchy, actions, state, visual, "
            "vision, UI_TREE, HIERARCHY_XML, or capability names as "
            "dag.nodes[].checkers[].checker_id values.\n\n"
            "DAG checker parameter rules are strict: text uses only any/all with "
            "none empty and pattern/ignore_case/prompt/expected_true null; xml uses "
            "any/all/none with pattern/ignore_case/prompt/expected_true null; regex "
            "uses pattern plus ignore_case and empty any/all/none; ocr uses any/all "
            "and optional pattern plus ignore_case, with none empty and prompt/"
            "expected_true null; llm uses only prompt plus expected_true, with "
            "any/all/none empty and pattern/ignore_case null. Even when a field "
            "is unused, include it with the required empty array or null value "
            "defined by the schema.\n\n"
            "Use dag only when the requested outcome can be checked with a small "
            "deterministic text/xml/regex/ocr/llm checker graph. If dag is non-null, "
            "include a required criterion whose criterion_id is exactly "
            "jit.dag_execution; otherwise set dag to null.\n\n"
            + (
                "PREVIOUS_LOCAL_COMPILER_ERRORS:\n"
                + json.dumps(feedback, ensure_ascii=False, sort_keys=True)
                + "\n\n"
                if feedback
                else ""
            )
            +
            "TASK_ONLY_INPUT:\n"
            + json.dumps(task_only, ensure_ascii=False, sort_keys=True)
        )
        schema = _plain(response_format.schema)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the trace-blind ContractIR compiler for an "
                        "independent mobile-agent verifier. You do not receive and "
                        "must not infer any execution outcome."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 4000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format.name,
                    "strict": response_format.strict,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for _ in range(self.max_retries + 1):
            try:
                response = requests.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=float(self.timeout),
                )
                if getattr(response, "status_code", 200) >= 400:
                    # Some OpenAI-compatible gateways expose JSON mode but not
                    # Structured Outputs. The compiler still enforces the exact
                    # generated schema and shared validation funnel locally.
                    fallback = dict(payload)
                    fallback["response_format"] = {"type": "json_object"}
                    response = requests.post(
                        self.endpoint,
                        headers=headers,
                        json=fallback,
                        timeout=float(self.timeout),
                    )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, Mapping):
                    raise ValueError("JIT model response root is not an object")
                content = self._content(body)
                parsed = json.loads(content)
                if (
                    isinstance(parsed, Mapping)
                    and set(parsed) == {"__refusal__"}
                    and isinstance(parsed["__refusal__"], str)
                ):
                    return JitProposalResponse(refusal=parsed["__refusal__"])
                return JitProposalResponse(
                    json_bytes=json.dumps(
                        parsed,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                )
            except Exception as exc:  # noqa: BLE001 - typed by compiler boundary.
                last_error = exc
        assert last_error is not None
        raise RuntimeError("task-only JIT model request failed") from last_error


__all__ = ["JIT_MODEL_PROPOSER_VERSION", "OpenAICompatibleJitProposer"]
