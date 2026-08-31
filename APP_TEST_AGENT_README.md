# App Test Agent Quickstart

`app_test_agent/` is the active App functional testing layer. The production
path keeps test cases at user level while reusing the original MobiAgent
Decider/Grounder and action handlers; it is not a locator replacement or a
Mock-only bridge.

## What Changed

The new layer treats the App as the object under test.

```text
app-test-case-v1
  -> runtime contract
  -> original MobiAgent Decider/Grounder (one user step at a time)
  -> Step Gate (order, dispatch evidence, observation, retry safety)
  -> direct App behavior verifier
  -> constrained read-only Verification Runner when evidence is unknown
  -> App behavior verifier over verification observations
  -> attribution report
```

The important distinction is:

- `APP_FAIL` means the declared steps completed, but the App did not show the
  expected observable effect.
- `TEST_EXECUTION_FAIL` means the executor did not complete the declared test
  steps, so the App should not be blamed.
- `ENV_BLOCKED` means login, permission, network, account, device, or similar
  environment state blocked the test.
- `INCONCLUSIVE` means evidence is insufficient.

Runner `done` is never an App verdict. Likewise, `ACTION_DISPATCHED`,
`CONFORMANT`, and Step Gate `CONTINUE` cannot produce `APP_PASS`. A dispatched
write, `INPUT`, or side-effecting GOAL micro-action is never repeated as a whole;
the system may only add safe observation or return `INCONCLUSIVE`.

Current offline baseline (2026-08-31): `226 passed`; the protected six-trace
cohort is `6/6` with exact accuracy `1.0` and zero false pass, false fail, or
attribution error. A HarmonyOS Xiaohongshu read-only pilot has passed. An
explicitly authorized chat-send pilot correctly ended as `TEST_EXECUTION_FAIL`
instead of treating a historical matching message and Runner `done` as
`APP_PASS`; the unsafe Harmony IME `ENTER` confirmation fallback found by that
pilot has been removed. Further business writes remain explicitly
user-authorized acceptance work.

OpenAI-compatible providers that expose the Responses wire API are selected with
`MOBIAGENT_WIRE_API=responses`. The original Decider/Grounder and the verifier
model clients then share `MOBIAGENT_BASE_URL`, `MOBIAGENT_MODEL`, optional
`MOBIAGENT_REASONING_EFFORT`, and privacy-preserving
`MOBIAGENT_DISABLE_RESPONSE_STORAGE=true`. Chat Completions remains the default.

## Mock Smoke Test

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
$env:PYTHONPATH = $root

python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\post_create_app_test.json" `
  --output-dir "D:\Lab\app_test_agent_smoke_pass" `
  --mock-scenario pass
```

The lower-level App-test CLI remains available when you also want to emit a
legacy runner plan:

```powershell
python -m app_test_agent.run `
  --test-case "$root\examples\post_create_app_test.json" `
  --output-dir "D:\Lab\app_test_agent_smoke_pass" `
  --mock-scenario pass
```

Try the failure attribution paths:

```powershell
python -m app_test_agent.run `
  --test-case "$root\examples\post_create_app_test.json" `
  --output-dir "D:\Lab\app_test_agent_smoke_app_fail" `
  --mock-scenario app_fail

python -m app_test_agent.run `
  --test-case "$root\examples\post_create_app_test.json" `
  --output-dir "D:\Lab\app_test_agent_smoke_execution_fail" `
  --mock-scenario execution_fail
```

Each output directory contains:

- `test_case.normalized.json`
- `execution_timeline.jsonl`
- `test_execution_manifest.json`
- `app_test_contract.json`
- `execution_result.json`
- `direct_app_behavior_result.json`
- `app_behavior_result.json`
- `business_offline_review.json`
- `attribution_result.json`
- `run_envelope.json`
- `verification_runner_result.json` and `verification_offline_review.json` when used
- `mobiagent_step_trace/model_events.jsonl` for ordered Decider/Grounder request,
  response, validation, retry, and latency events during real execution
- `report.json`
- `report.md`

## Stage 1 Protocol Boundary

`app-test-case-v1` currently accepts only a small intentional surface:

- Atomic actions: `OPEN_APP`, `CLICK`, `INPUT`, `WAIT`, `BACK`
- Goal action: `GUI_TASK` with `step_mode=GOAL`; internal micro-actions remain
  bounded to that one user step
- Assertions: `TEXT_VISIBLE`, `TEXT_ABSENT`, `STATE_CHANGED`, `SUCCESS_SIGNAL`

Unsupported action or assertion types are rejected when the test case is loaded.
`INPUT` steps must use `value` or `value_ref`, and all references into
`test_data` must resolve before execution starts.

## Stage 2 Mock Scenarios

The mock executor is only for control-flow validation.  It currently supports:

`pass`, `app_fail`, `execution_fail`, `wrong_order`, `input_mismatch`,
`env_blocked`, `inconclusive`, `forbidden_effect`, and `unsupported`.

## Stage 3 Manifest Replay

Every App-test run writes `test_execution_manifest.json`.  This is the first
step-level evidence contract that a real MobiAgent step executor must satisfy.

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\post_create_app_test.json" `
  --app-test-executor manifest `
  --execution-manifest "D:\Lab\app_test_agent_smoke_pass\test_execution_manifest.json" `
  --output-dir "D:\Lab\app_test_agent_manifest_replay"
```

## Stage 4 MobiAgent Preflight And Real Execution

Without `--execute-runner`, the MobiAgent adapter performs a no-device preflight
and writes the step-bound payload plus a pre-dispatch manifest:

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case "$root\examples\post_create_app_test.json" `
  --app-test-executor mobiagent `
  --runner-root $root `
  --output-dir "D:\Lab\stage4_mobiagent_preflight"
```

This produces `mobiagent_step_payload.json` and a pre-dispatch
`test_execution_manifest.json`.  It does not mutate the device and does not call
the model provider.

With `--execute-runner`, the same CLI starts `MobiAgentStepExecutor` and the real
read-only `MobiAgentVerificationRunner`. The default business path leaves
`step_decider`, `target_locator`, and legacy target hints unset, so the original
Decider builds the current-step action and the original Grounder refines click
geometry. Coordinates remain runtime evidence and are not required in the test
case. Every model decision, target geometry, dispatch fact, pre/post frame and
Step Gate decision is retained in the trace.

The source CLI keeps the original live model-response logging. The windowed PC
client mirrors the same structured events into its log widget. Independently of
the UI, `model_events.jsonl` is the durable source for model-returned reasoning,
raw response text, validation failures and retries; it deliberately omits
prompts, screenshot payloads, message content and credentials.

## Stage 5 Split Verifier

The active App-test path now compiles an `app_test_contract.json` before
verification.  `execution_result.json` records step conformance, while
`app_behavior_result.json` records App oracle results.  `report.json` combines
them with attribution.

The App oracle evaluates both `expected_results` and `forbidden_effects`.
Forbidden effects are required absence constraints and a visible forbidden
value produces `APP_FAIL` when execution evidence is sufficient.

The Verification Runner call policy is explicit at the test-case level:

```json
{
  "verification_runner_policy": "IF_DIRECT_UNKNOWN"
}
```

When a phone run pauses on `info` or `call_user`, resume it from another host
terminal with:

```powershell
$env:PYTHONPATH = "D:\Lab\MobiAgent-verifier-enhanced"
python -m app_test_agent.harmony_native_runner `
  --app-test-device-serial <serial> `
  --user-response "<response>"
```

Supported values are:

- `NEVER`: never start the read-only Verification Runner; unresolved evidence remains `INCONCLUSIVE`.
- `IF_DIRECT_UNKNOWN` (default): start it only when direct App evidence is `UNKNOWN_EVIDENCE`.
- `REQUIRED_FOR_RESULT`: start it after conformant business execution even when direct evidence is already decisive.

The policy is frozen into `app_test_contract.json`, copied into `report.json` and
`run_envelope.json`, and does not allow the Runner's self-report to change it.
The older assertion-level `requires_verification_runner` remains accepted for
compatibility, but it is not a replacement for the top-level policy.

After the business executor stops, the execution verifier performs a small
terminal flow check before the App oracle is allowed to run. It rechecks the
frozen action type, retry budget, post-observation boundary, final Step Gate
decision, and Goal terminal state. A `done` signal is accepted only when the
current Goal is already confirmed; it is never treated as App success. These
checks produce execution failure/inconclusive results and do not decide App
behavior.

The real Verification Runner uses the test case's observation policy as a full
post-action burst. Every attempt records its pre-frame, dispatch state, action,
observation frame ids, capture errors and retry decision in
`verification_actions.json` (schema v2) and in the final report. Only a proven
`PRE_DISPATCH` lookup failure may consume the bounded retry budget. A returned
device action, a device call with uncertain outcome, or a post-dispatch capture
failure never causes `NAVIGATE`, `BACK`, `REFRESH`, or `SCROLL` to be sent again.
Capture recovery is observation-only. NAVIGATE also requires an explicit
read-only role, exact semantic candidates and a matching visible/enabled runtime
hit node; coordinates alone fail closed.

For raw traces, the offline App review also adapts final `TEXT_VISIBLE`
assertions to the existing `verification_benchmark` criterion checker registry.
Its semantic and page-domain evidence is recorded under
`verification_benchmark_legacy_checker`. It can resolve missing text/OCR
evidence, but it cannot bypass an unreached surface, freshness boundary,
required Verification Runner, or invalid trace. Legacy checker VLM calls are
opt-in with `APP_TEST_ENABLE_LEGACY_CHECKER_VLM=1`.

The run envelope also records unified temporal boundaries under
`temporal_boundaries`: each business action's pre/post frame boundary, the
explicit runner completion frame when the executor provides one, the first
verification frame reported as reaching the target surface, and the selected
result observation window for each assertion. A missing explicit completion
frame is reported as unknown with an inferred terminal frame; it is not
silently treated as a verified `done` event.

Runtime Step Gate evidence now also records whether the next business-step
target was resolved from the post-frame hierarchy or an injected runtime
locator. This resolution is used for real MobiAgent execution; legacy replay
inputs without it retain the older text-based compatibility heuristic. The
legacy checker adapter records advisory layered `state_evidence` for
`STATE_CHANGED` assertions without inventing a desired control state.

GOAL business steps retain the original MobiAgent `swipe` capability. The
adapter delegates swipe coordinate conversion and device dispatch to the
original `handle_swipe_action`, then checks the emitted direction and the
post-observation result in the Step Gate. This is separate from the read-only
Verification Runner's `SCROLL` action.

The step-bound adapter also preserves a run-level Decider history and passes it
to each subsequent original Decider call. Original `long_press` and
`press_home` handlers are available inside GOAL steps. `info`, `call_user`, and
`abort` are retained as control trace events; unhandled intervention or abort
events stop safe progression as `INCONCLUSIVE` rather than being treated as
successful completion.

If deterministic flow evidence is insufficient, an optional single-call VLM
fallback can review the declared steps, action evidence, Gate summaries, and
available post-action screenshots. Enable it with
`APP_TEST_ENABLE_FLOW_VLM=1` and configure the existing OpenAI-compatible model
environment variables such as `APP_TEST_FLOW_VERIFIER_BASE_URL`,
`APP_TEST_FLOW_VERIFIER_MODEL`, and `MOBIAGENT_API_KEY`. The model must return
`CONFORMANT`, `NONCONFORMANT`, or
`INCONCLUSIVE`; low-confidence or failed calls remain inconclusive. Hard
deterministic flow failures never reach this fallback.

`test_data` may use runtime templates such as:

```json
{
  "post_content": "app_test_${run_id}"
}
```

The run resolves those templates before compiling the contract, so expected
assertions and input values refer to the actual value used in that run.

For freshness-sensitive assertions, declare `after_step` and
`historical_match_not_sufficient`:

```json
{
  "assertion_id": "post_content_visible",
  "type": "TEXT_VISIBLE",
  "expected_value_ref": "post_content",
  "after_step": "submit_post",
  "historical_match_not_sufficient": true
}
```

The App verifier then uses post-action observation frames selected by
`observation_policy`. If the text only exists in the initial/final state and
cannot be tied to the post-action evidence, the result is `INCONCLUSIVE`, not
`APP_PASS`.

For real-device runs, `observation_policy.adaptive_capture=true` makes
non-terminal actions stop after the first stable post-action frame. Steps named
by an `after_step` assertion always retain the full declared observation window,
so eventual result evidence is never shortened by this optimization.

Manifest replay through
`verification_benchmark.tools.run_automated_evaluation --app-test-executor manifest`
uses the App-test manifest intake adapter under
`verification_benchmark.evaluation_framework.app_test_manifest_intake`; it
validates the test-case hash and contract hash before replaying evidence.

## Harmony Report Export And PC Recheck

The official `ohosTest` runner writes a report bundle into the test app sandbox.
Export it with the bundle debug-directory option, then let the PC intake replay
the typed manifest and offline evidence review:

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
$out = "D:\Lab\harmony-offline-review"
$remote = "/data/storage/el2/base/files/reports/<report-file>.json"

$env:PYTHONPATH = $root
python -m app_test_agent.harmony_intake `
  --device-serial 5ZU0226122004500 `
  --device-bundle com.zengxq.mobiagentprobe `
  --remote-report $remote `
  --output-dir $out
```

The command runs `hdc file recv -b` internally, validates the phone testcase,
contract, manifest, and run-envelope hashes, then writes
`pc_offline_review.json`, `pc_execution_result.json`,
`pc_app_behavior_result.json`, and `pc_final_report.json`. Phone-origin hashes
remain recorded separately from the PC semantic bridge hashes.

For multiple root-level reports exported from the phone `reports` directory,
use batch intake:

```powershell
$env:PYTHONPATH = $root
python -m app_test_agent.harmony_intake `
  --report-dir "D:\Lab\phone_reports" `
  --output-dir "D:\Lab\harmony-batch-offline-review"
```

Each report receives an isolated output directory and `batch_summary.json`
contains all final summaries.

## Legacy Compatibility: Existing Whole-Task Runner Plan

The bridge writes a compatibility plan that the current
`verification_benchmark.tools.run_automated_evaluation` can preflight or run on
device.

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
$env:PYTHONPATH = $root

python -m app_test_agent.run `
  --test-case "$root\examples\post_create_app_test.json" `
  --output-dir "D:\Lab\app_test_agent_bridge" `
  --mock-scenario pass `
  --emit-legacy-plan "D:\Lab\app_test_agent_plan.json" `
  --legacy-run-id "app_test_agent_$(Get-Date -Format yyyyMMdd_HHmmss)" `
  --legacy-raw-trace-root "D:\Lab\app_test_agent_raw" `
  --legacy-intake-root "D:\Lab\app_test_agent_intake" `
  --device-serial "YOUR_DEVICE_SERIAL" `
  --os-version "OpenHarmony-6.1.1.120"

python -m verification_benchmark.tools.run_automated_evaluation `
  --plan "D:\Lab\app_test_agent_plan.json" `
  --runner-root $root `
  --output-dir "D:\Lab\app_test_agent_legacy_preflight_report" `
  --diagnostics
```

This compatibility route still uses the existing whole-task runner internally.
The new App-test report remains the source of the clearer App/executor/env
attribution semantics.
