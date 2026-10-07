@echo off
rem Copyright (c) 2026 abdurrehmandaudi
rem Required Notice: Copyright (c) 2026 abdurrehmandaudi -- justdowork-proxy
rem Licensed under the PolyForm Noncommercial License 1.0.0 -- commercial
rem use is not permitted without a separate written commercial license.
rem See LICENSE or https://polyformproject.org/licenses/noncommercial/1.0.0
rem ---------------------------------------------------------------------------
rem  Run Claude Code through ccproxy on Windows -- WITHOUT touching your global
rem  %USERPROFILE%\.claude\settings.json. Both setups can be used side by side.
rem
rem    run-claude.bat                 start Claude Code through the proxy
rem    run-claude.bat --continue      any normal claude flag passes straight through
rem
rem  Override the defaults if you need to:
rem      set PROXY=http://127.0.0.1:8181
rem      set MODEL=claude-opus-4-8
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if "%PROXY%"=="" set "PROXY=http://127.0.0.1:8181"
if "%MODEL%"=="" set "MODEL=claude-opus-4-8"

where claude >nul 2>&1
if errorlevel 1 (
  echo.
  echo !! Claude Code was not found on your PATH.
  echo    Install it with:   npm install -g @anthropic-ai/claude-code
  echo.
  pause
  exit /b 1
)

rem The proxy has to be up first, otherwise Claude Code fails with a confusing
rem connection error and the real cause stays invisible.
curl -s -o nul --max-time 3 "%PROXY%/health"
if errorlevel 1 (
  echo.
  echo !! ccproxy is not answering on %PROXY%
  echo    Start it first, in another window:
  echo        start.bat
  echo.
  pause
  exit /b 1
)

rem Write the per-session settings to a file. Passing a JSON *string* through
rem cmd.exe means fighting its quoting rules; a file path has no such problem.
if "%ANTHROPIC_API_KEY%"=="" if not "%UPSTREAM_API_KEY%"=="" set "ANTHROPIC_API_KEY=%UPSTREAM_API_KEY%"
if "%ANTHROPIC_API_KEY%"=="" set "ANTHROPIC_API_KEY=dummy"

set "SETTINGS=%TEMP%\ccproxy-claude-settings.json"
> "%SETTINGS%" echo {"env":{"ANTHROPIC_BASE_URL":"%PROXY%","ANTHROPIC_MODEL":"%MODEL%","ANTHROPIC_API_KEY":"%ANTHROPIC_API_KEY%","ENABLE_TOOL_SEARCH":"false"}}

rem Set the env vars too: whichever one Claude Code gives priority to, both
rem point at the proxy.
set "ANTHROPIC_BASE_URL=%PROXY%"
set "ANTHROPIC_MODEL=%MODEL%"
set "ENABLE_TOOL_SEARCH=false"

echo Claude Code  -^>  %PROXY%   (model: %MODEL%)
echo Your global settings.json is NOT modified.
echo Watch the traffic:  type ccproxy_log.txt
echo.

claude --settings "%SETTINGS%" %*
