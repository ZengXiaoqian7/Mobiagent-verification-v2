[CmdletBinding()]
param(
    [string]$RealTraceAssetRoot = "",
    [string]$OutputRoot = "",
    [ValidateSet("Offline", "Formal")]
    [string]$AcceptanceLevel = "Offline",
    [ValidateSet("", "android", "harmony")]
    [string]$DeviceProfile = "",
    [string]$DeviceSerial = "",
    [switch]$ProbeModelService,
    [switch]$SkipRealReplay,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RepoRoot
$IsFormal = $AcceptanceLevel -eq "Formal"
if ($IsFormal -and $SkipRealReplay) {
    throw "Formal acceptance cannot use -SkipRealReplay"
}
if ($IsFormal -and $SkipBuild) {
    throw "Formal acceptance cannot use -SkipBuild"
}
if ($IsFormal -and -not $DeviceProfile) {
    throw "Formal acceptance requires -DeviceProfile android or harmony"
}
if ($IsFormal -and -not $DeviceSerial) {
    throw "Formal acceptance requires an explicit -DeviceSerial"
}
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

function Read-JsonFile {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label did not create $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Assert-StrictReplaySummary {
    param([object]$ReplaySummary)

    if ($ReplaySummary.configured_cases -lt 1) {
        throw "real replay baseline has no configured cases"
    }
    if ($ReplaySummary.evaluated_cases -ne $ReplaySummary.configured_cases) {
        throw "real replay baseline did not evaluate the complete configured cohort"
    }
    if ($ReplaySummary.unavailable_cases -ne 0) {
        throw "real replay baseline contains unavailable cases"
    }
    if ($ReplaySummary.exact_accuracy -ne 1.0) {
        throw "real replay exact accuracy is not 1.0"
    }
    if ($ReplaySummary.false_pass_count -ne 0) {
        throw "real replay baseline contains a false pass"
    }
    if ($ReplaySummary.false_fail_count -ne 0) {
        throw "real replay baseline contains a false fail"
    }
    if ($ReplaySummary.attribution_error_count -ne 0) {
        throw "real replay baseline contains an attribution error"
    }
}

function Get-ConnectedDeviceSerials {
    param([string]$Profile)

    if ($Profile -eq "harmony") {
        $RawTargets = & hdc list targets
        if ($LASTEXITCODE -ne 0) {
            throw "hdc list targets failed with exit code $LASTEXITCODE"
        }
        return @(
            $RawTargets |
                ForEach-Object { $_.ToString().Trim() } |
                Where-Object { $_ -and $_ -ne "[Empty]" }
        )
    }
    if ($Profile -eq "android") {
        $RawTargets = & adb devices
        if ($LASTEXITCODE -ne 0) {
            throw "adb devices failed with exit code $LASTEXITCODE"
        }
        return @(
            $RawTargets |
                Select-Object -Skip 1 |
                ForEach-Object { $_.ToString().Trim() } |
                Where-Object { $_ -match "\sdevice$" } |
                ForEach-Object { ($_ -split "\s+")[0] }
        )
    }
    return @()
}

function Invoke-EnvironmentCheck {
    param(
        [string]$Executable,
        [string]$Profile,
        [string]$OutputDirectory,
        [string]$Label,
        [switch]$FrozenExecutable
    )

    Write-Host "`n== $Label =="
    if ($FrozenExecutable) {
        $Process = Start-Process `
            -FilePath $Executable `
            -ArgumentList @("--check-environment", $Profile, "--output-dir", $OutputDirectory) `
            -PassThru `
            -Wait `
            -WindowStyle Hidden
        if ($Process.ExitCode -ne 0) {
            throw "$Label failed with exit code $($Process.ExitCode)"
        }
    }
    else {
        & $Executable pc_client_entry.py --check-environment $Profile --output-dir $OutputDirectory | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    $ReportPath = Join-Path $OutputDirectory "pc_environment_report.json"
    $Report = Read-JsonFile $ReportPath $Label
    if (-not $Report.ready -or $Report.profile -ne $Profile) {
        throw "$Label did not report ready for profile $Profile"
    }
    return $Report
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

$SourceDeviceEnvironment = $null
$ConnectedDeviceSerials = @()
if ($DeviceProfile) {
    $SourceDeviceEnvironment = Invoke-EnvironmentCheck `
        -Executable "python" `
        -Profile $DeviceProfile `
        -OutputDirectory (Join-Path $OutputRoot "source_device_environment") `
        -Label "Source $DeviceProfile device environment"
    $ConnectedDeviceSerials = @(Get-ConnectedDeviceSerials $DeviceProfile)
    if ($IsFormal -and $ConnectedDeviceSerials -notcontains $DeviceSerial) {
        $Observed = if ($ConnectedDeviceSerials.Count) { $ConnectedDeviceSerials -join ", " } else { "none" }
        throw "formal device $DeviceSerial is not connected for $DeviceProfile; observed: $Observed"
    }
}

$ModelServiceProbe = $null
if ($ProbeModelService) {
    $ModelProbePath = Join-Path $OutputRoot "model_service_probe.json"
    Invoke-NativeStep "Sanitized model-service probe (no device interaction)" {
        python -m verification_benchmark.tools.probe_model_service --output-json $ModelProbePath
    }
    $ModelServiceProbe = Read-JsonFile $ModelProbePath "model-service probe"
    if ($ModelServiceProbe.status -ne "PASS" -or $ModelServiceProbe.device_interaction -ne "NONE") {
        throw "model-service probe did not report a safe PASS"
    }
}

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
        Assert-StrictReplaySummary $ReplaySummary
    }
    else {
        if ($IsFormal) {
            throw "Formal acceptance requires the complete protected real-trace cohort under $RealTraceAssetRoot"
        }
        Write-Host "`n== Protected real-trace replay baseline =="
        Write-Host "SKIPPED: no protected trace cohort under $RealTraceAssetRoot"
    }
}

$PackagedSmoke = $null
$PackagedDeviceEnvironment = $null
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

    if ($DeviceProfile) {
        $PackagedDeviceEnvironment = Invoke-EnvironmentCheck `
            -Executable $ExePath `
            -Profile $DeviceProfile `
            -OutputDirectory (Join-Path $OutputRoot "packaged_device_environment") `
            -Label "packaged device environment" `
            -FrozenExecutable
    }
}

$Summary = [ordered]@{
    schema_version = "pc-verifier-acceptance-summary-v2"
    status = "PASS"
    acceptance_level = $AcceptanceLevel.ToUpperInvariant()
    formal_readiness = if ($IsFormal) { "PASS" } else { "NOT_REQUESTED" }
    live_commercial_acceptance = if ($IsFormal) { "PENDING_USER_TRIGGERED_PILOT" } else { "NOT_REQUESTED" }
    model_service_probe = if ($null -eq $ModelServiceProbe) { "NOT_RUN" } else { "PASS" }
    model_service_probe_result = $ModelServiceProbe
    device_interaction = "CONNECTIVITY_CHECK_ONLY"
    regression_suite = "PASS"
    source_mock = $SourceSmoke.overall_result
    real_replay = if ($null -eq $ReplaySummary) { "SKIPPED" } else { "PASS" }
    real_replay_summary = $ReplaySummary
    package = if ($SkipBuild) { "SKIPPED" } else { "PASS" }
    packaged_mock = if ($null -eq $PackagedSmoke) { "SKIPPED" } else { $PackagedSmoke.overall_result }
    device_profile = if ($DeviceProfile) { $DeviceProfile } else { $null }
    device_serial = if ($DeviceSerial) { $DeviceSerial } else { $null }
    connected_device_serials = @($ConnectedDeviceSerials)
    source_device_environment = $SourceDeviceEnvironment
    packaged_device_environment = $PackagedDeviceEnvironment
}
$SummaryPath = Join-Path $OutputRoot "acceptance_summary.json"
$Summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $SummaryPath -Encoding utf8
$PassedLevel = if ($IsFormal) { "formal-readiness" } else { "offline" }
Write-Host "`nPC verifier $PassedLevel acceptance PASS: $SummaryPath"
