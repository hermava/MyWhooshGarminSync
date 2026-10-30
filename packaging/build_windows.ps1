param([string]$PythonExe = "python", [string]$BuildDirectory = "")

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$tempRoot = [IO.Path]::GetTempPath()
$stage = Join-Path $tempRoot ("MyWhooshGarminSync-build-" + [guid]::NewGuid().ToString("N"))
if ($BuildDirectory) {
    $stage = [IO.Path]::GetFullPath($BuildDirectory)
    if ((Split-Path $stage -Parent).TrimEnd('\') -ne $tempRoot.TrimEnd('\') -or (Split-Path $stage -Leaf) -notmatch '^MyWhooshGarminSync-build-[a-f0-9]{32}$') {
        throw "BuildDirectory must be an isolated app build folder in Windows Temp."
    }
}
$snapshot = Join-Path $stage "source"
New-Item -ItemType Directory -Path $snapshot -Force | Out-Null
Write-Host "Isolated Windows build directory: $stage"

# Copy only source, synthetic tests and build inputs. No private data or config.
foreach ($directory in @("src", "tests", "packaging")) {
    $target = Join-Path $snapshot $directory
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    Get-ChildItem (Join-Path $repo $directory) -Recurse -File |
        Where-Object { $_.Extension -in @(".py", ".spec", ".ps1") -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
        ForEach-Object {
            $relative = $_.FullName.Substring((Join-Path $repo $directory).Length).TrimStart('\', '/')
            $destination = Join-Path $target $relative
            New-Item -ItemType Directory -Path (Split-Path $destination -Parent) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination
        }
}
foreach ($name in @("pyproject.toml", "README.md", "LICENSE", "requirements-dev.lock", "requirements-garmin.lock", "requirements-windows-build.lock")) {
    Copy-Item -LiteralPath (Join-Path $repo $name) -Destination $snapshot
}

$python = Join-Path $stage "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    & $PythonExe -m venv (Join-Path $stage "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated Windows environment." }
}
& $python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: --cache-dir (Join-Path $stage "pip-cache") -r (Join-Path $snapshot "requirements-windows-build.lock")
if ($LASTEXITCODE -ne 0) { throw "Windows dependency installation failed." }
& $python -m pip --isolated install --no-deps --no-build-isolation $snapshot
if ($LASTEXITCODE -ne 0) { throw "Project installation failed." }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Windows dependencies are inconsistent." }

Push-Location $snapshot
$oldSmoke = $env:MYWHOOSH_GUI_SMOKE_TEST
try {
    $env:MYWHOOSH_GUI_SMOKE_TEST = "1"
    & $python -m pytest -q --tb=short tests/test_sessions.py tests/test_accounts.py tests/test_garmin_auth.py tests/test_account_cli.py tests/test_windows_credentials.py tests/test_workspace.py tests/test_sync_state.py tests/test_sync_settings.py tests/test_sync.py tests/test_sync_time.py tests/test_sync_retry.py tests/test_sync_cli.py tests/test_fit_time.py tests/test_fit_timeline.py tests/test_fit_time_cli.py tests/test_gui.py tests/test_gui_sync.py tests/test_desktop_controller.py
    if ($LASTEXITCODE -ne 0) { throw "Native Windows tests failed; executable was not published." }
    & $python -m PyInstaller --noconfirm --clean --distpath (Join-Path $stage "dist") --workpath (Join-Path $stage "work") packaging/windows.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
    $exe = Join-Path $stage "dist\MyWhooshGarminSync.exe"
    $smokeDirectory = Join-Path $stage ("smoke-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $smokeDirectory | Out-Null
    $report = Join-Path $smokeDirectory "smoke-report.json"
    $process = Start-Process -FilePath $exe -ArgumentList @("--build-smoke-test", ('"' + $report + '"')) -PassThru
    if (-not $process.WaitForExit(120000)) {
        $process.Kill()
        throw "Frozen executable smoke check timed out."
    }
    $process.Refresh()
    if (-not (Test-Path -LiteralPath $report)) { throw "Executable did not write a smoke report." }
    $smoke = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($process.ExitCode -ne 0 -or $smoke.status -ne "passed") {
        Get-Content -LiteralPath $report
        throw "Frozen executable smoke check failed."
    }
    $output = Join-Path $repo "dist"
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination $output -Force
    Copy-Item -LiteralPath (Join-Path $snapshot "LICENSE") -Destination (Join-Path $output "LICENSE.txt") -Force
    Copy-Item -LiteralPath $report -Destination (Join-Path $output "windows-smoke-report.json") -Force
    $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $output "MyWhooshGarminSync.exe.sha256") -Value ($hash + "  MyWhooshGarminSync.exe") -Encoding ASCII
    & $python -m pip list --format=json | Set-Content -LiteralPath (Join-Path $output "windows-build-dependencies.json") -Encoding UTF8
    Write-Host "Verified executable: $(Join-Path $output 'MyWhooshGarminSync.exe')"
    Write-Host "SHA256: $hash"
} finally {
    $env:MYWHOOSH_GUI_SMOKE_TEST = $oldSmoke
    Pop-Location
}
