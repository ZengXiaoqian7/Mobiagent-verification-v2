# MobiAgent Verification

Windows desktop client and command-line runner for Android and HarmonyOS application tests.

## Start

Use Python 3.10 or later.

```powershell
git clone https://github.com/ZengXiaoqian7/Mobiagent-verification-v2.git
Set-Location .\Mobiagent-verification-v2

python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-runtime.txt
```

For HarmonyOS, install the Harmony SDK and make `hdc` available on `PATH`. For Android, install platform-tools and make `adb` available on `PATH`.

Configure the model service in the same terminal. Keep credentials outside the repository.

```powershell
$env:MOBIAGENT_API_KEY_FILE = "C:\secure\mobiagent-key.txt"
$env:MOBIAGENT_BASE_URL = "https://<openai-compatible-endpoint>/v1"
$env:MOBIAGENT_MODEL = "<vision-model>"
$env:MOBIAGENT_WIRE_API = "responses"
$env:MOBIAGENT_DISABLE_RESPONSE_STORAGE = "true"
```

## Desktop client

```powershell
python -m pc_client
```

The client detects connected HarmonyOS and Android devices automatically. Select a test case, run a device preflight first, then confirm before starting a test that can send, publish, delete, or otherwise change app data. Select a local credential file in the client if the model variables were not set before launch.

Results are written to the selected output folder. Open `report.md` for the summary and `report.json` for the full result.

## Command line

Run a no-device Mock check:

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case .\examples\post_create_app_test.json `
  --app-test-executor mock `
  --mock-scenario pass `
  --output-dir .\output\mock-run
```

Create a HarmonyOS preflight without operating the device:

```powershell
python -m verification_benchmark.tools.run_automated_evaluation `
  --app-test-case .\examples\xiaohongshu_publish_hello_world_with_runner_app_test.json `
  --app-test-executor mobiagent `
  --app-test-device Harmony `
  --app-test-device-serial "<device-serial>" `
  --runner-root . `
  --output-dir .\output\preflight
```

Add `--execute-runner` only after confirming that the test case is allowed to operate the selected device.

## Build the Windows client

```powershell
python -m pip install -r requirements-package.txt
.\build_pc_client.ps1
```

The client is created in `dist\MobiAgentVerifierPC\`.
