$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root

$AppName = "CypraMatrixStudio"
$BuildId = "1.1.15-files-consent-hardening-20260904"
$HostAddress = "127.0.0.1"
$DefaultPort = 8765
$MinimumPython = [Version]"3.11"
$MaximumPythonExclusive = [Version]"3.14"
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"
$DataDir = Join-Path $Root "data"
$ServerLog = Join-Path $DataDir "server.log"
$LaunchLog = Join-Path $DataDir "launch.log"
$MatrixGreen = "Green"
$BootStarted = [Diagnostics.Stopwatch]::StartNew()
$BootProgress = 0

try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $Host.UI.RawUI.BackgroundColor = "Black"
    $Host.UI.RawUI.ForegroundColor = "Green"
} catch {}

function Write-MatrixStatus {
    param([string]$Label, [string]$State, [ConsoleColor]$Color = [ConsoleColor]::Green)
    $width = 68
    $dots = "." * [Math]::Max(3, $width - $Label.Length - $State.Length - 2)
    Write-Host "$Label $dots $State" -ForegroundColor $Color
}

function Write-MatrixProgress {
    param([int]$Percent, [string]$Phase, [string]$Detail = "")
    $value = [Math]::Max($script:BootProgress, [Math]::Max(0, [Math]::Min(100, $Percent)))
    $script:BootProgress = $value
    $filled = [Math]::Floor($value / 4)
    $bar = ("█" * $filled) + ("░" * (25 - $filled))
    $elapsed = "{0,6:0.0}s" -f $BootStarted.Elapsed.TotalSeconds
    $suffix = if ($Detail) { " · $Detail" } else { "" }
    Write-Host ("[BOOT]   [{0}] {1,3}%  {2}  {3}{4}" -f $bar, $value, $elapsed, $Phase, $suffix) -ForegroundColor Green
}

function Write-MatrixHeader {
    Write-Host "╔══════════════════════════════════════════════════════════════════╗" -ForegroundColor Green
    Write-Host "║                    CYPRA // MATRIX CORE                          ║" -ForegroundColor Yellow
    Write-Host "╚══════════════════════════════════════════════════════════════════╝" -ForegroundColor Green
    Write-Host ""
}

function Write-MatrixTelemetry {
    param([int]$Port, [string]$RuntimePath)
    $origin = "127.0.0.1:$Port"
    Write-Host ""
    Write-Host "────────────────────────────────────────────────────────────────────" -ForegroundColor DarkGreen
    Write-Host " MATRIX TELEMETRY" -ForegroundColor Cyan
    Write-Host "────────────────────────────────────────────────────────────────────" -ForegroundColor DarkGreen
    Write-Host ""
    Write-Host "  CORE      CYPRA MATRIX STUDIO" -ForegroundColor Blue
    Write-Host "  STATE     ONLINE" -ForegroundColor Green
    Write-Host "  PORT      $Port" -ForegroundColor Green
    Write-Host "  ORIGIN    $origin" -ForegroundColor Green
    Write-Host "  RUNTIME   $RuntimePath" -ForegroundColor Green
    Write-Host ("  BOOT      {0:0.0}s" -f $BootStarted.Elapsed.TotalSeconds) -ForegroundColor Green
    Write-Host ""
    Write-MatrixStatus "[CORE] HEALTH CHECK" "PASS"
    Write-Host "[MATRIX] SYSTEM RUNNING. LAUNCHER CLOSING" -ForegroundColor Cyan
}

function Set-MatrixConsoleVisible {
    param([bool]$Visible)
    try {
        if (-not ("MatrixConsole.Window" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace MatrixConsole {
    public static class Window {
        [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
        [DllImport("kernel32.dll")] public static extern bool FreeConsole();
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    }
}
"@
        }
        $handle = [MatrixConsole.Window]::GetConsoleWindow()
        if ($handle -ne [IntPtr]::Zero) {
            [MatrixConsole.Window]::ShowWindow($handle, $(if ($Visible) { 5 } else { 0 })) | Out-Null
        }
    } catch {
        # Console visibility is cosmetic; startup must continue if the host does
        # not expose a traditional Win32 console window (for example Terminal).
    }
}

function Close-MatrixConsole {
    try {
        Set-MatrixConsoleVisible $false
        [MatrixConsole.Window]::FreeConsole() | Out-Null
    } catch {}
}

function Stop-WithError {
    param([string]$Message, [int]$Code = 1)
    Write-Host ""
    Write-MatrixStatus "[ERROR] STARTUP FAILURE" "HALTED" Red
    Write-Host $Message -ForegroundColor Red
    if ($env:CYPRA_NO_PAUSE -ne "1") { try { Read-Host "Press Enter to close" | Out-Null } catch {} }
    exit $Code
}

function Get-ProjectIdentity {
    param([string]$ProjectRoot)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $normalized = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\').ToLowerInvariant()
        $hash = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalized))
        return "cypra-" + (($hash[0..7] | ForEach-Object { $_.ToString("x2") }) -join "")
    } finally { $sha.Dispose() }
}

function Test-PortFree {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch { return $false }
    finally { if ($listener) { try { $listener.Stop() } catch {} } }
}

function Get-CypraHealth {
    param([int]$Port)
    try { return Invoke-RestMethod -Uri "http://${HostAddress}:$Port/api/health" -TimeoutSec 1 -ErrorAction Stop }
    catch { return $null }
}

function Select-CypraPort {
    param([int]$PreferredPort, [string]$InstanceId)
    # Reuse an already-running server belonging to this exact project copy,
    # even if it previously had to move above the preferred port.
    for ($port = $PreferredPort; $port -le [Math]::Min(65535, $PreferredPort + 500); $port++) {
        if (Test-PortFree $port) { continue }
        $health = Get-CypraHealth $port
        if ($health -and $health.app_id -eq "cypra-local-bv-chat" -and $health.instance_id -eq $InstanceId -and $health.build_id -eq $BuildId) {
            $script:CypraReusedServer = $true
            Write-MatrixStatus "[NET]    EXISTING CYPRA INSTANCE" "REUSED"
            return $port
        }
    }
    # No matching instance exists: select the first genuinely free port.
    for ($port = $PreferredPort; $port -le [Math]::Min(65535, $PreferredPort + 500); $port++) {
        if (Test-PortFree $port) {
            if ($port -ne $PreferredPort) { Write-MatrixStatus "[WARN]   PORT $PreferredPort OCCUPIED" "USING $port" Yellow }
            return $port
        }
    }
    Stop-WithError "No free localhost port was found from $PreferredPort through $([Math]::Min(65535, $PreferredPort + 500))." 6
}

function Test-Python {
    param([string]$Exe, [string[]]$PrefixArgs = @())
    try {
        $text = & $Exe @PrefixArgs -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor)+'.'+str(sys.version_info.micro))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $text) { return $null }
        $version = [Version]($text | Select-Object -Last 1)
        if ($version -lt $MinimumPython -or $version -ge $MaximumPythonExclusive) { return $null }
        return [PSCustomObject]@{ Exe = $Exe; PrefixArgs = $PrefixArgs; Version = $version }
    } catch { return $null }
}

function Find-Python {
    $candidates = @(
        [PSCustomObject]@{ Command = "py"; Args = @("-3.13") },
        [PSCustomObject]@{ Command = "py"; Args = @("-3.12") },
        [PSCustomObject]@{ Command = "py"; Args = @("-3.11") },
        [PSCustomObject]@{ Command = "python"; Args = @() },
        [PSCustomObject]@{ Command = "python3"; Args = @() }
    )
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $tested = Test-Python $cmd.Source $candidate.Args
        if ($tested) { return $tested }
    }
    return $null
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments, [string]$FailureMessage)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Stop-WithError "$FailureMessage (exit code $LASTEXITCODE)." 4 }
}

function Prepare-PythonEnvironment {
    Write-MatrixProgress 15 "Detecting Python runtime"
    if (Test-Path -LiteralPath $VenvPython) {
        $venv = Test-Python $VenvPython
        if (-not $venv) { Stop-WithError "The project .venv is not usable with supported Python 3.11-3.13. Delete only the .venv folder and launch again." 3 }
        $script:CypraPythonVersion = $venv.Version
        Write-MatrixProgress 20 "Virtual environment detected" "reusing .venv"
    } else {
        $base = Find-Python
        if (-not $base) { Stop-WithError "Supported Python 3.11, 3.12, or 3.13 was not found. Install Python from python.org, enable the Python launcher/PATH option, then run START.bat again." 3 }
        $script:CypraPythonVersion = $base.Version
        Write-MatrixProgress 20 "Creating project virtual environment"
        Write-MatrixStatus "[ENV]    VIRTUAL ENVIRONMENT" "CREATING" Yellow
        $args = @($base.PrefixArgs) + @("-m", "venv", $VenvDir)
        Invoke-Checked $base.Exe $args "Could not create the project virtual environment"
    }

    Write-MatrixProgress 26 "Checking pip"

    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $VenvPython -m pip --version *> $null
    $ErrorActionPreference = $oldPreference
    if ($LASTEXITCODE -ne 0) {
        Write-MatrixStatus "[ENV]    PIP" "REPAIRING" Yellow
        Invoke-Checked $VenvPython @("-m", "ensurepip", "--upgrade") "Could not prepare pip"
    }

    $requirementsHash = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash
    $stamp = Join-Path $VenvDir ".cypra-requirements.sha256"
    $savedHash = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { "" }
    $imports = "import fastapi,uvicorn,openai,requests,httpx,multipart,pydantic,PIL,webview"
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $VenvPython -c $imports *> $null
    $importsOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $oldPreference

    if ($savedHash -ne $requirementsHash -or -not $importsOk) {
        $reason = if ($savedHash -ne $requirementsHash) { "requirements changed" } else { "runtime import missing" }
        Write-MatrixProgress 32 "Installing required packages" $reason
        Write-MatrixStatus "[CORE]   DEPENDENCIES" "INSTALLING" Yellow
        $pipOutput = & $VenvPython -m pip install -r $Requirements --disable-pip-version-check 2>&1
        $pipExitCode = $LASTEXITCODE
        Add-Content -LiteralPath $LaunchLog -Value "`n=== dependency install $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" -Encoding UTF8
        $pipOutput | ForEach-Object { Add-Content -LiteralPath $LaunchLog -Value ([string]$_) -Encoding UTF8 }
        if ($pipExitCode -ne 0) {
            $pipOutput | ForEach-Object { Write-Host ([string]$_) -ForegroundColor Red }
            Stop-WithError "Dependency installation failed (exit code $pipExitCode). Full output: $LaunchLog" 4
        }
        & $VenvPython -c $imports
        if ($LASTEXITCODE -ne 0) { Stop-WithError "Dependencies installed, but the runtime import check still failed." 4 }
        Invoke-Checked $VenvPython @("-m", "pip", "check") "Installed dependency validation failed"
        Set-Content -LiteralPath $stamp -Value $requirementsHash -Encoding ASCII
    } else {
        Write-MatrixProgress 38 "Dependencies verified" "cached environment is current"
    }
    Write-MatrixProgress 42 "Python environment ready" "Python $script:CypraPythonVersion"
    Write-MatrixStatus "[ENV]    PYTHON $script:CypraPythonVersion" "ONLINE"
    Write-MatrixStatus "[ENV]    VIRTUAL ENVIRONMENT" "ACTIVE"
    Write-MatrixStatus "[CORE]   DEPENDENCIES" "VERIFIED"
}

function Get-ConfiguredPort {
    if ($env:CYPRA_PREFERRED_PORT) {
        $candidate = 0
        if ([int]::TryParse($env:CYPRA_PREFERRED_PORT, [ref]$candidate) -and $candidate -ge 1024 -and $candidate -le 65535) { return $candidate }
    }
    $settingsPath = Join-Path $DataDir "settings.json"
    if (Test-Path -LiteralPath $settingsPath) {
        try {
            $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
            $candidate = [int]$settings.port
            if ($candidate -ge 1024 -and $candidate -le 65535) { return $candidate }
        } catch {}
    }
    return $DefaultPort
}

function Get-OllamaPort {
    param([string]$ProjectRoot)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($ProjectRoot).ToLowerInvariant())
        $hash = $sha.ComputeHash($bytes)
        return 11435 + ([BitConverter]::ToUInt32($hash, 0) % 800)
    } finally { $sha.Dispose() }
}

function Test-OllamaReady {
    param([int]$Port)
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/tags" -TimeoutSec 2 -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Prepare-Ollama {
    Write-MatrixProgress 68 "Locating Ollama engine"
    $ollama = $null
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { $ollama = $cmd.Source }
    if (-not $ollama -and $env:LOCALAPPDATA) {
        $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
        if (Test-Path -LiteralPath $candidate) { $ollama = $candidate }
    }
    if (-not $ollama -and $env:ProgramFiles) {
        $candidate = Join-Path $env:ProgramFiles "Ollama\ollama.exe"
        if (Test-Path -LiteralPath $candidate) { $ollama = $candidate }
    }
    if (-not $ollama) { Stop-WithError "Ollama is required but was not found. Install Ollama, then run START.bat again. Cypra does not download executables automatically." 5 }

    $ollamaPort = Get-OllamaPort $Root
    if (-not (Test-OllamaReady $ollamaPort) -and -not (Test-PortFree $ollamaPort)) {
        for ($p = $ollamaPort + 1; $p -le [Math]::Min(65535, $ollamaPort + 100); $p++) {
            if ((Test-OllamaReady $p) -or (Test-PortFree $p)) { $ollamaPort = $p; break }
        }
    }
    $models = Join-Path $Root "OllamaModels"
    New-Item -ItemType Directory -Force -Path $models | Out-Null
    $env:OLLAMA_HOST = "127.0.0.1:$ollamaPort"
    $env:OLLAMA_MODELS = $models
    $env:OLLAMA_NO_CLOUD = "1"
    # Optimize the private runtime for one interactive local chat. Flash
    # attention lowers long-context memory pressure, q8 KV cache usually halves
    # cache memory with negligible quality loss, and single-model/single-request
    # scheduling prevents hidden context multiplication and VRAM contention.
    if (-not $env:OLLAMA_FLASH_ATTENTION) { $env:OLLAMA_FLASH_ATTENTION = "1" }
    if (-not $env:OLLAMA_KV_CACHE_TYPE) { $env:OLLAMA_KV_CACHE_TYPE = "q8_0" }
    if (-not $env:OLLAMA_MAX_LOADED_MODELS) { $env:OLLAMA_MAX_LOADED_MODELS = "1" }
    if (-not $env:OLLAMA_NUM_PARALLEL) { $env:OLLAMA_NUM_PARALLEL = "1" }

    if (-not (Test-OllamaReady $ollamaPort)) {
        Write-MatrixProgress 74 "Starting Ollama engine"
        Write-MatrixStatus "[OLLAMA] ENGINE 127.0.0.1:$ollamaPort" "STARTING" Yellow
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $ollama
        $psi.Arguments = "serve"
        $psi.WorkingDirectory = $Root
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
        [Diagnostics.Process]::Start($psi) | Out-Null
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 250
            if (Test-OllamaReady $ollamaPort) { $ready = $true; break }
        }
        if (-not $ready) { Stop-WithError "Ollama did not become ready on 127.0.0.1:$ollamaPort." 5 }
    } else {
        Write-MatrixProgress 78 "Ollama health verified" "existing engine reused"
    }
    $script:CypraOllamaEndpoint = "127.0.0.1:$ollamaPort"
    Write-MatrixProgress 82 "Ollama ready" $script:CypraOllamaEndpoint
    Write-MatrixStatus "[OLLAMA] ENGINE $script:CypraOllamaEndpoint" "ONLINE"
}

Write-MatrixHeader
Write-MatrixProgress 3 "Matrix core initialization"
Write-MatrixStatus "[MATRIX] BOOT SEQUENCE" "ENGAGED"
Write-MatrixProgress 8 "Resolving project files" $Root
Write-MatrixStatus "[ENV]    PROJECT ROOT" "RESOLVED"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
if (-not (Test-Path -LiteralPath $Requirements)) { Stop-WithError "requirements.txt is missing from the project folder." 2 }

Prepare-PythonEnvironment
Write-MatrixProgress 48 "Resolving instance identity"
$instanceId = Get-ProjectIdentity $Root
$preferredPort = Get-ConfiguredPort
Write-MatrixProgress 54 "Scanning localhost ports" "preferred $preferredPort"
Write-MatrixStatus "[NET]    SCANNING LOCAL PORT" "$preferredPort"
$selectedPort = Select-CypraPort $preferredPort $instanceId
Write-MatrixStatus "[NET]    PORT $selectedPort" "ACQUIRED"
Write-MatrixProgress 62 "Local port acquired" "$HostAddress`:$selectedPort"
$url = "http://${HostAddress}:$selectedPort/"
$env:CYPRA_PORT = [string]$selectedPort
$env:CYPRA_INSTANCE_ID = $instanceId
$env:CYPRA_PROJECT_ROOT = $Root
Prepare-Ollama

Write-MatrixProgress 88 "Launching Cypra server" "waiting for health check"
Write-MatrixStatus "[CORE]   CYPRA SERVER" "STARTING" Yellow
$runtimeDisplay = $VenvPython
if ($runtimeDisplay.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
    $runtimeDisplay = $runtimeDisplay.Substring($Root.Length).TrimStart('\')
}
$script:CypraTelemetryShown = $false
if ($script:CypraReusedServer) {
    Write-MatrixProgress 100 "Existing Cypra instance ready"
    Write-MatrixTelemetry $selectedPort $runtimeDisplay
    $script:CypraTelemetryShown = $true
    Start-Sleep -Milliseconds 350
    Close-MatrixConsole
}

& $VenvPython (Join-Path $Root "app.py") | ForEach-Object {
    $line = [string]$_
    if ($line.StartsWith("[+] Cypra server healthy:", [StringComparison]::Ordinal)) {
        if (-not $script:CypraTelemetryShown) {
            Write-MatrixProgress 100 "Cypra online" "health check passed"
            Write-MatrixTelemetry $selectedPort $runtimeDisplay
            $script:CypraTelemetryShown = $true
            Start-Sleep -Milliseconds 350
            Close-MatrixConsole
        }
    } elseif ($line) {
        Write-Host $line -ForegroundColor DarkGreen
    }
}
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Set-MatrixConsoleVisible $true
    Stop-WithError "Cypra exited with code $exitCode. Review $ServerLog and $(Join-Path $DataDir 'launch.log')." $exitCode
}
