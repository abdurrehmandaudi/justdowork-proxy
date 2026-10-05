# Starts the JDW proxy in the background and keeps a status icon in the Windows tray.
# The API key is read from .env (or an existing UPSTREAM_API_KEY environment variable).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Read-EnvFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }
        $match = [regex]::Match($trimmed, "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
        if (-not $match.Success) {
            continue
        }
        $name = $match.Groups[1].Value
        $value = $match.Groups[2].Value.Trim()
        if (($value.Length -ge 2) -and
            (($value.StartsWith("'") -and $value.EndsWith("'")) -or
             ($value.StartsWith('"') -and $value.EndsWith('"')))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

Read-EnvFile (Join-Path $root ".env")

if ([string]::IsNullOrWhiteSpace($env:UPSTREAM_API_KEY) -or
    $env:UPSTREAM_API_KEY -eq "replace-with-your-jdw-api-key") {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "Create .env in the proxy folder and set UPSTREAM_API_KEY to your JDW API key.",
        "JDW proxy setup",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
    exit 1
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if ([string]::IsNullOrWhiteSpace($python)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "Python was not found. Install Python 3.8+ and run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt",
        "JDW proxy setup",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    ) | Out-Null
    exit 1
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $python
$startInfo.Arguments = "`"$(Join-Path $root 'agent_proxy.py')`""
$startInfo.WorkingDirectory = $root
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$proxy = New-Object System.Diagnostics.Process
$proxy.StartInfo = $startInfo
if (-not $proxy.Start()) {
    throw "Unable to start the JDW proxy."
}

$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Application
$icon.Text = "JDW proxy (running)"
$icon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$status = $menu.Items.Add("JDW proxy is running")
$status.Enabled = $false
$menu.Items.Add("-") | Out-Null
$openLogs = $menu.Items.Add("Open proxy folder")
$stop = $menu.Items.Add("Stop proxy")
$exit = $menu.Items.Add("Exit")
$icon.ContextMenuStrip = $menu

$openLogs.Add_Click({
    Start-Process explorer.exe -ArgumentList "`"$root`""
})
$stop.Add_Click({
    if (-not $proxy.HasExited) {
        $proxy.Kill()
        $proxy.WaitForExit()
    }
    $icon.Text = "JDW proxy (stopped)"
    $stop.Enabled = $false
})
$exit.Add_Click({
    if (-not $proxy.HasExited) {
        $proxy.Kill()
        $proxy.WaitForExit()
    }
    $icon.Visible = $false
    $icon.Dispose()
    [System.Windows.Forms.Application]::ExitThread()
})

$icon.Add_DoubleClick({
    Start-Process explorer.exe -ArgumentList "`"$root`""
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({
    if ($proxy.HasExited) {
        $icon.Text = "JDW proxy (stopped)"
        $stop.Enabled = $false
    }
})
$timer.Start()

try {
    [System.Windows.Forms.Application]::Run()
}
finally {
    $timer.Stop()
    $timer.Dispose()
    if (-not $proxy.HasExited) {
        $proxy.Kill()
        $proxy.WaitForExit()
    }
    if ($icon.Visible) {
        $icon.Visible = $false
    }
    $icon.Dispose()
}
