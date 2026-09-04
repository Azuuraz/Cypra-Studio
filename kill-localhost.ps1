#Requires -Version 5.1
<#
.SYNOPSIS
  Kill Localhost — stops user programs listening on localhost (127.0.0.1 / ::1).

.DESCRIPTION
  Finds TCP listeners bound to loopback, excludes Windows system processes,
  shows a confirmation list, then terminates the remaining program processes.

  Double-click the desktop shortcut or run:
    powershell -ExecutionPolicy Bypass -File kill-localhost.ps1
#>
param(
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$SystemProcessNames = [System.Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
)
@(
    "System", "Registry", "smss", "csrss", "wininit", "services", "lsass",
    "svchost", "dwm", "fontdrvhost", "winlogon", "spoolsv", "SearchIndexer",
    "MsMpEng", "SecurityHealthService", "WmiPrvSE", "dllhost", "conhost",
    "RuntimeBroker", "ApplicationFrameHost", "sihost", "taskhostw", "explorer"
) | ForEach-Object { [void]$SystemProcessNames.Add($_) }

function Get-LoopbackListeners {
    $rows = @()
    $seen = @{}

    $connections = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1") }

    foreach ($conn in $connections) {
        $processId = [int]$conn.OwningProcess
        if ($processId -le 4) { continue }
        if ($seen.ContainsKey($processId)) {
            $seen[$processId].Ports += $conn.LocalPort
            continue
        }

        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($SystemProcessNames.Contains($proc.ProcessName)) { continue }

        $path = ""
        try { $path = $proc.Path } catch { }

        $seen[$processId] = [pscustomobject]@{
            PID         = $processId
            Name        = $proc.ProcessName
            Path        = $path
            Ports       = [System.Collections.Generic.List[int]]::new()
        }
        $seen[$processId].Ports.Add([int]$conn.LocalPort)
    }

    foreach ($entry in $seen.Values) {
        $entry.Ports = ($entry.Ports | Sort-Object -Unique) -join ", "
        $rows += $entry
    }

    return $rows | Sort-Object Name, PID
}

function Show-SummaryDialog($targets) {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Kill Localhost"
    $form.StartPosition = "CenterScreen"
    $form.Size = New-Object System.Drawing.Size(640, 460)
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.BackColor = [System.Drawing.Color]::FromArgb(28, 28, 30)
    $form.ForeColor = [System.Drawing.Color]::White

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Stop localhost servers started by programs?"
    $title.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
    $title.AutoSize = $true
    $title.Location = New-Object System.Drawing.Point(18, 16)
    $form.Controls.Add($title)

    $subtitle = New-Object System.Windows.Forms.Label
    $subtitle.Text = "These processes are listening on 127.0.0.1 or ::1. System services are excluded."
    $subtitle.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $subtitle.AutoSize = $true
    $subtitle.Location = New-Object System.Drawing.Point(18, 48)
    $form.Controls.Add($subtitle)

    $list = New-Object System.Windows.Forms.ListBox
    $list.Location = New-Object System.Drawing.Point(18, 78)
    $list.Size = New-Object System.Drawing.Size(590, 280)
    $list.Font = New-Object System.Drawing.Font("Consolas", 9)
    $list.BackColor = [System.Drawing.Color]::FromArgb(18, 18, 20)
    $list.ForeColor = [System.Drawing.Color]::FromArgb(255, 120, 120)
    $list.BorderStyle = "FixedSingle"
    foreach ($t in $targets) {
        $label = "{0} (PID {1}) ports: {2}" -f $t.Name, $t.PID, $t.Ports
        if ($t.Path) { $label += "  |  $([System.IO.Path]::GetFileName($t.Path))" }
        [void]$list.Items.Add($label)
    }
    $form.Controls.Add($list)

    $killBtn = New-Object System.Windows.Forms.Button
    $killBtn.Text = "Kill All"
    $killBtn.Size = New-Object System.Drawing.Size(120, 34)
    $killBtn.Location = New-Object System.Drawing.Point(388, 372)
    $killBtn.BackColor = [System.Drawing.Color]::FromArgb(220, 60, 60)
    $killBtn.ForeColor = [System.Drawing.Color]::White
    $killBtn.FlatStyle = "Flat"
    $killBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Controls.Add($killBtn)

    $cancelBtn = New-Object System.Windows.Forms.Button
    $cancelBtn.Text = "Cancel"
    $cancelBtn.Size = New-Object System.Drawing.Size(120, 34)
    $cancelBtn.Location = New-Object System.Drawing.Point(518, 372)
    $cancelBtn.BackColor = [System.Drawing.Color]::FromArgb(60, 60, 64)
    $cancelBtn.ForeColor = [System.Drawing.Color]::White
    $cancelBtn.FlatStyle = "Flat"
    $cancelBtn.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.Add($cancelBtn)

    $form.AcceptButton = $killBtn
    $form.CancelButton = $cancelBtn
    return ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)
}

function Stop-TargetProcesses($targets) {
    $killed = @()
    $failed = @()

    foreach ($target in $targets) {
        try {
            Stop-Process -Id $target.PID -Force -ErrorAction Stop
            $killed += $target
        } catch {
            $failed += [pscustomobject]@{
                Target = $target
                Error  = $_.Exception.Message
            }
        }
    }

    return [pscustomobject]@{
        Killed = $killed
        Failed = $failed
    }
}

$targets = @(Get-LoopbackListeners)

if ($targets.Count -eq 0) {
    if (-not $Quiet) {
        [System.Windows.Forms.MessageBox]::Show(
            "No user program listeners found on localhost.",
            "Kill Localhost",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
    }
    exit 0
}

$confirmed = $Force.IsPresent
if (-not $confirmed -and -not $Quiet) {
    $confirmed = Show-SummaryDialog $targets
}

if (-not $confirmed) {
    exit 0
}

$result = Stop-TargetProcesses $targets

if (-not $Quiet) {
    $lines = @()
    if ($result.Killed.Count -gt 0) {
        $lines += "Stopped $($result.Killed.Count) process(es):"
        foreach ($k in $result.Killed) {
            $lines += "  - $($k.Name) (PID $($k.PID)) on port(s) $($k.Ports)"
        }
    }
    if ($result.Failed.Count -gt 0) {
        $lines += ""
        $lines += "Failed to stop $($result.Failed.Count):"
        foreach ($f in $result.Failed) {
            $lines += "  - $($f.Target.Name) (PID $($f.Target.PID)): $($f.Error)"
        }
    }

    [System.Windows.Forms.MessageBox]::Show(
        ($lines -join "`n"),
        "Kill Localhost",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        $(if ($result.Failed.Count -gt 0) { [System.Windows.Forms.MessageBoxIcon]::Warning } else { [System.Windows.Forms.MessageBoxIcon]::Information })
    ) | Out-Null
}

exit $(if ($result.Failed.Count -gt 0) { 1 } else { 0 })