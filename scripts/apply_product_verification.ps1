[CmdletBinding()]
param(
    [string]$Repository = 'C:\Users\danie\Projects\NiBot',
    [string]$PythonExecutable = 'C:\Users\danie\anaconda3\envs\swarmforge\python.exe',
    [string]$PatchDirectory,
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($PatchDirectory)) { $PatchDirectory = $PSScriptRoot }
$repositoryPath = (Resolve-Path -LiteralPath $Repository).Path
$patchPath = Join-Path $PatchDirectory 'swarmforge-product-verification.patch'
$repairPath = Join-Path $PatchDirectory 'swarmforge-product-verification-fix.patch'
if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf)) {
    throw 'The product verification patch must be beside this script, or supplied with -PatchDirectory.'
}
$originUrl = & git -C $repositoryPath remote get-url origin
if ($LASTEXITCODE -ne 0 -or $originUrl -notmatch 'github\.com[:/]bigdan88/swarmforge(?:\.git)?$') {
    throw 'This is not the expected SwarmForge repository. No changes were applied.'
}

function Test-VerificationPatch {
    param([string]$Path, [switch]$Reverse)
    $gitArguments = @('-C', $repositoryPath, 'apply', '--check')
    if ($Reverse) {
        # This only detects installed content; it does not write the old lines
        # back. Existing whitespace in an old version is not a new patch error.
        $gitArguments += @('--reverse', '--whitespace=nowarn')
    }
    else { $gitArguments += '--whitespace=error-all' }
    $gitArguments += $Path
    # Nonzero exit is expected while detecting installation state. Capture its
    # diagnostics instead of printing an alarming error for every existing file.
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $diagnostics = (& git @gitArguments 2>&1 | Out-String)
        $checkExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    return [pscustomobject]@{ Applies = ($checkExitCode -eq 0); Diagnostics = $diagnostics }
}

$alreadyInstalled = (Test-VerificationPatch -Path $patchPath -Reverse).Applies
$hasRepair = Test-Path -LiteralPath $repairPath -PathType Leaf
if (-not $alreadyInstalled -and $hasRepair) {
    $alreadyInstalled = (Test-VerificationPatch -Path $repairPath -Reverse).Applies
}
$selectedPatch = $null
if ($alreadyInstalled) {
    Write-Output 'Product verification changes are already installed. No patch changes were needed.'
}
else {
    $repairCheck = $null
    if ($hasRepair) { $repairCheck = Test-VerificationPatch -Path $repairPath }
    if ($repairCheck -and $repairCheck.Applies) {
        $selectedPatch = $repairPath
    }
    else {
        $fullCheck = Test-VerificationPatch -Path $patchPath
        if ($fullCheck.Applies) {
            $selectedPatch = $patchPath
        }
        else {
            $details = if ($repairCheck) { $repairCheck.Diagnostics } else { $fullCheck.Diagnostics }
            throw ('The checkout has conflicting or partially applied changes. No patch was applied. Review these conflicts:' + [Environment]::NewLine + $details)
        }
    }
    & git -C $repositoryPath apply --whitespace=error-all $selectedPatch
    if ($LASTEXITCODE -ne 0) { throw 'Git could not apply the selected patch. Review its output before continuing.' }
    if ($selectedPatch -eq $repairPath) {
        Write-Output 'Applied the product verification repair to the existing installation.'
    }
    else {
        Write-Output 'Applied product verification changes without modifying runtime settings or the user database.'
    }
}

if ($RunTests) {
    $previousPythonPath = $env:PYTHONPATH
    Push-Location -LiteralPath $repositoryPath
    try {
        $env:PYTHONPATH = Join-Path $repositoryPath 'src'
        if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
        & $PythonExecutable -m pytest -q tests/test_product_verification.py tests/test_product_gate.py tests/test_verification_installer.py tests/test_projects.py tests/test_project_autonomy.py tests/test_queue.py
        if ($LASTEXITCODE -ne 0) {
            throw 'The changes are installed, but validation failed. Review the test output before restarting SwarmForge. This installer is safe to rerun.'
        }
        & $PythonExecutable scripts/check_docs.py
        if ($LASTEXITCODE -ne 0) { throw 'The changes are installed, but documentation validation failed.' }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
        Pop-Location
    }
}
Write-Output 'Restart SwarmForge with your usual launch command to load these changes.'
