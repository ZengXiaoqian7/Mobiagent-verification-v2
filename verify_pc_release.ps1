[CmdletBinding()]
param(
    [string]$RealTraceAssetRoot = "",
    [string]$OutputRoot = "",
    [switch]$SkipRealReplay,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RepoRoot
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $RepoRoot "output_pc_acceptance"
}
else {
    $OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

function Invoke-NativeStep {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "`n== $Label =="
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Assert-RuntimePromptSmoke {
    param(
        [object]$SmokeResult,
        [string]$Label
    )
    $PromptAssets = $SmokeResult.runtime_prompt_assets
    if ($null -eq $PromptAssets -or $PromptAssets.status -ne "PASS" -or $PromptAssets.count -ne 7) {
        throw "$Label did not load all 7 MobiAgent runtime prompts"
    }
}

Invoke-NativeStep "Test environment" {
    python -m verification_benchmark.tools.check_pc_environment --profile test --json
}

Invoke-NativeStep "Python regression suite" {
    python -m pytest -q
}

$SourceSmokeDir = Join-Path $OutputRoot "source_mock"
Invoke-NativeStep "Source PC-client Mock smoke" {
    python pc_client_entry.py --smoke-mock --output-dir $SourceSmokeDir
}
$SourceSmoke = Get-Content (Join-Path $SourceSmokeDir "pc_client_smoke_result.json") -Raw | ConvertFrom-Json
if ($SourceSmoke.overall_result -ne "APP_PASS") {
    throw "source Mock smoke did not return APP_PASS"
}
if (-not (Test-Path -LiteralPath (Join-Path $SourceSmokeDir "report.md"))) {
    throw "source Mock smoke did not create report.md"
}
Assert-RuntimePromptSmoke $SourceSmoke "source Mock smoke"

$ReplaySummary = $null
if (-not $SkipRealReplay) {
    if (-not $RealTraceAssetRoot) {
        $RealTraceAssetRoot = Split-Path -Parent $RepoRoot
    }
    $KnownTrace = Join-Path $RealTraceAssetRoot "cloudmusic_create_note_real_run_fixed5\test_execution_manifest.json"
    if (Test-Path -LiteralPath $KnownTrace) {
        $BaselineJson = Join-Path $OutputRoot "real_replay_baseline.json"
        $BaselineMarkdown = Join-Path $OutputRoot "real_replay_baseline.md"
        Invoke-NativeStep "Protected real-trace replay baseline" {
            python -m verification_benchmark.tools.evaluate_app_test_replay_baseline `
                --config verification_benchmark\configs\app_test_real_replay_baseline_20260824.json `
                --asset-root $RealTraceAssetRoot `
                --output-json $BaselineJson `
                --output-markdown $BaselineMarkdown | Out-Null
        }
        $ReplayReport = Get-Content $BaselineJson -Raw | ConvertFrom-Json
        $ReplaySummary = $ReplayReport.summary
        if ($ReplaySummary.evaluated_cases -lt 1) {
            throw "real replay baseline did not evaluate any available trace"
        }
        if ($ReplaySummary.exact_accuracy -lt 0.9) {
            throw "real replay exact accuracy is below 0.9"
        }
        if ($ReplaySummary.false_pass_count -ne 0) {
            throw "real replay baseline contains a false pass"
        }
        if ($ReplaySummary.attribution_error_count -ne 0) {
            throw "real replay baseline contains an attribution error"
        }
    }
    else {
        Write-Host "`n== Protected real-trace replay baseline =="
        Write-Host "SKIPPED: no protected trace cohort under $RealTraceAssetRoot"
    }
}

$PackagedSmoke = $null
if (-not $SkipBuild) {
    Invoke-NativeStep "Package environment" {
        python -m verification_benchmark.tools.check_pc_environment --profile package --json
    }
    Write-Host "`n== Windows PC client build =="
    & (Join-Path $RepoRoot "build_pc_client.ps1")
    $ExePath = Join-Path $RepoRoot "dist\MobiAgentVerifierPC\MobiAgentVerifierPC.exe"
    if (-not (Test-Path -LiteralPath $ExePath)) {
        throw "packaged PC executable is missing"
    }
    $PackagedSmokeDir = Join-Path $OutputRoot "packaged_mock"
    Write-Host "`n== Packaged PC-client Mock smoke =="
    $PackagedProcess = Start-Process `
        -FilePath $ExePath `
        -ArgumentList @("--smoke-mock", "--output-dir", $PackagedSmokeDir) `
        -PassThru `
        -Wait `
        -WindowStyle Hidden
    if ($PackagedProcess.ExitCode -ne 0) {
        throw "packaged PC-client Mock smoke failed with exit code $($PackagedProcess.ExitCode)"
    }
    $PackagedSmoke = Get-Content (Join-Path $PackagedSmokeDir "pc_client_smoke_result.json") -Raw | ConvertFrom-Json
    if ($PackagedSmoke.overall_result -ne "APP_PASS") {
        throw "packaged Mock smoke did not return APP_PASS"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PackagedSmokeDir "report.md"))) {
        throw "packaged Mock smoke did not create report.md"
    }
    Assert-RuntimePromptSmoke $PackagedSmoke "packaged Mock smoke"
}

$Summary = [ordered]@{
    schema_version = "pc-verifier-acceptance-summary-v1"
    status = "PASS"
    regression_suite = "PASS"
    source_mock = $SourceSmoke.overall_result
    real_replay = if ($null -eq $ReplaySummary) { "SKIPPED" } else { "PASS" }
    real_replay_summary = $ReplaySummary
    package = if ($SkipBuild) { "SKIPPED" } else { "PASS" }
    packaged_mock = if ($null -eq $PackagedSmoke) { "SKIPPED" } else { $PackagedSmoke.overall_result }
}
$SummaryPath = Join-Path $OutputRoot "acceptance_summary.json"
$Summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding utf8
Write-Host "`nPC verifier acceptance PASS: $SummaryPath"
