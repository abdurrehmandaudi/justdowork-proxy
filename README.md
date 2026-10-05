# justdowork-proxy

A small Anthropic-compatible API proxy that forwards requests to an upstream
endpoint. It lets you run Claude Code (`claude`) through your own JDW proxy.

- `agent_proxy.py` — Flask proxy server on `http://127.0.0.1:8181`, with tool-name mapping and streaming support.
- `names_probe.py` — checks which tool names are allowed by the upstream.
- `start_proxy.ps1` — one-click Windows launcher with a notification-area tray icon.

## Requirements

- Python 3.8+
- Windows PowerShell 5.1+ for the tray launcher
- Python packages in `requirements.txt`

## Setup

Create a virtual environment and install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

Create a local `.env` file from the template and put your JDW upstream API key
there:

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `UPSTREAM_API_KEY` to the key supplied by JDW. `.env` is ignored by git;
never commit it or put the real key in source files, documentation, or scripts.

## Start in the Windows tray

After setup, double-click `start_proxy.ps1`, or run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start_proxy.ps1
```

The launcher reads `.env`, starts `agent_proxy.py` as a hidden background
process, and keeps a status icon in the Windows notification area. Right-click
the icon to open the proxy folder, stop the server, or exit the launcher.
Double-clicking the icon opens the proxy folder. The launcher also detects when
the proxy exits and updates the tray status.

## Manual start

If you do not want the tray launcher, set the key in the current PowerShell
session and start the proxy directly:

```powershell
$env:UPSTREAM_API_KEY = "your-key"
python agent_proxy.py
```

Optionally check upstream tool names before starting:

```powershell
python names_probe.py
```

The proxy listens on `http://127.0.0.1:8181` by default.

## Claude Code configuration

In Claude Code settings (for example `~/.claude/settings.json`), configure the
local proxy. `ANTHROPIC_API_KEY` can use the same JDW key:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8181",
    "ANTHROPIC_MODEL": "claude-opus-4-8",
    "ANTHROPIC_API_KEY": "your-key",
    "ENABLE_TOOL_SEARCH": "false"
  }
}
```

Keep `ENABLE_TOOL_SEARCH` set to `"false"`; the proxy relies on this and it
must not be enabled. Then run:

```powershell
claude
```

## GitHub Copilot app (BYOK)

The GitHub Copilot desktop app does not provide a supported CLI or settings
file for importing model providers. Add the provider once through the app:

1. Open **Settings → Model providers → Add provider**.
2. Select **Anthropic** (or **Anthropic-compatible**, if shown).
3. Set the display name to `JDW Local Proxy`.
4. Set the base URL to `http://127.0.0.1:8181`.
5. Enter the JDW API key when prompted. Copilot stores it in the Windows
   credential store; do not put it in this README or a repository file.
6. Select model `claude-opus-4-8` and the Anthropic Messages wire format if
   the app asks for it.

If the provider form requires a complete endpoint instead of a base URL, use
`http://127.0.0.1:8181/v1/messages`. Do not append `/v1/messages` twice.
The proxy must be running on the same computer because `127.0.0.1` is local.
The `JDW Proxy Tray Startup` scheduled task starts it at Windows logon.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `UPSTREAM_API_KEY` | *(required)* | JDW upstream API key |
| `TARGET_URL` | `https://api.justwoker.icu` | Upstream endpoint |
| `PORT` | `8181` | Local proxy port |

The tray launcher loads these variables from `.env` into the proxy process.
Variables already set in PowerShell are used unless `.env` contains the same
variable.
