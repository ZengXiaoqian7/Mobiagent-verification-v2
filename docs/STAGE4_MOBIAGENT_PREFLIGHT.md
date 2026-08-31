# Stage 4 MobiAgent Preflight (Historical) And Current Step Executor

Date: 2026-07-22

Historical branch: `codex/app-test-agent-stage4`; current implementation: `main`

## Current Status (2026-08-31)

Stage 4 originally introduced only the non-mutating preflight contract. Current
`main` retains that mode and also implements real step-bound device execution.
The production chain is:

```text
user-view test case
  -> original MobiAgent Decider/Grounder and action handlers
  -> Step Gate
  -> App Verifier
  -> constrained read-only Verification Runner when needed
```

The default path does not inject a test locator and does not treat Runner
`done`, `CONFORMANT`, or `CONTINUE` as App success. Dispatched write/INPUT/GOAL
side-effect actions are not repeated; insufficient evidence is
`INCONCLUSIVE`.

## Goal

Historical Stage 4 started the MobiAgent adapter without pretending that real
device execution had already been integrated. It produced the step-bound runner
payload and a pre-dispatch execution manifest accepted by Stage 3 intake.

That contract is now consumed by `MobiAgentStepExecutor`; the historical details
below remain useful for understanding preflight artifacts.

## What Is Implemented

New module:

```text
app_test_agent/mobiagent_executor.py
```

It writes:

```text
test_case.normalized.json
mobiagent_step_payload.json
test_execution_manifest.json
```

Payload schema:

```text
app-test-mobiagent-step-payload-v1
```

The payload records:

- `run_id`
- `test_case_id`
- `test_case_sha256`
- device label and optional serial
- App metadata
- preconditions
- fixed test data
- observation policy
- runner constraints
- one payload record per `TestStep`

Each step contains:

- ordinal
- `step_id`
- instruction
- action type
- target
- resolved input value
- timeout and retry budget
- a strict runner prompt for only this step

## Runner Constraints

The preflight payload explicitly requires:

```json
{
  "one_step_per_call": true,
  "preserve_step_order": true,
  "do_not_modify_test_data": true,
  "runner_done_is_step_done_only": true,
  "app_result_not_decided_by_runner": true
}
```

These constraints reflect the core project rule: MobiAgent executes steps; the
verifier decides App behavior.

## Manifest State

The Stage 4 manifest is pre-dispatch:

```json
{
  "dispatch_status": "ACTION_NOT_DISPATCHED",
  "conformance_status": "UNKNOWN",
  "effect_status": "NOT_EVALUATED"
}
```

This remains intentional for a run without `--execute-runner`. The output is
accepted by intake, but it is not an App functional test result. A real run now
records dispatched actions, action ids, pre/post frames, observation bursts,
Step Gate decisions and final App evidence separately.

## CLI

Unified entry:

```powershell
$root = "D:\Lab\MobiAgent-verifier-enhanced"
$env:PYTHONPATH = $root

python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case examples\post_create_app_test.json `
  --app-test-executor mobiagent `
  --runner-root $root `
  --output-dir D:\Lab\stage4_mobiagent_preflight
```

Expected status:

```text
MOBIAGENT_PREFLIGHT_COMPLETE
```

Add `--execute-runner` only when real device mutation is intended and explicitly
authorized:

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case examples\post_create_app_test.json `
  --app-test-executor mobiagent `
  --execute-runner `
  --app-test-device Harmony `
  --app-test-device-serial <device-serial> `
  --runner-root $root `
  --output-dir D:\Lab\stage4_mobiagent_real
```

The preflight and real traces use different evidence states, so a preflight can
never be mistaken for a dispatched test.

## Current Acceptance

Stage 4 is accepted when:

- the MobiAgent preflight payload is generated from the same `TestCaseSpec`;
- the payload binds every step to a stable `step_id`;
- fixed input values are resolved from `test_data`;
- runner prompts prohibit skipping, reordering, modifying test data, and judging
  App success;
- the pre-dispatch manifest passes Stage 3 intake;
- without `--execute-runner`, no device or provider call occurs;
- with `--execute-runner`, the default path calls the original Decider/Grounder
  and Step Gate rather than a test-specific locator;
- the Windows frozen client contains and successfully loads all seven runtime
  Markdown prompts;
- Decider/Grounder responses remain visible in source CLI and the windowed PC
  log, while an ordered `model_events.jsonl` preserves response, validation,
  retry and latency evidence without prompts, screenshots or credentials;
- the real Verification Runner uses an observation burst, records every attempt,
  and retries only failures proven to occur before dispatch;
- real commercial-App writes, sends, posts, and payments remain manual,
  user-triggered acceptance work.

Offline baseline on 2026-08-31: `211 passed`; six protected real traces are
`6/6`, exact accuracy is `1.0`, and false pass, false fail, and attribution error
counts are zero.
