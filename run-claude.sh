#!/bin/sh
# Copyright (c) 2026 abdurrehmandaudi
# Required Notice: Copyright (c) 2026 abdurrehmandaudi -- justdowork-proxy
# Licensed under the PolyForm Noncommercial License 1.0.0 -- commercial
# use is not permitted without a separate written commercial license.
# See LICENSE or https://polyformproject.org/licenses/noncommercial/1.0.0
# Run Claude Code through ccproxy -- WITHOUT touching ~/.claude/settings.json.
#
# Your global settings (agentrouter.org / deepseek-v4-flash) stay exactly as they
# are. This script overrides the endpoint for THIS terminal only, so both can be
# used at the same time.
#
#   sh run-claude.sh              # start Claude Code through the proxy
#   sh run-claude.sh --continue   # any normal claude flag is passed through
#
# Env overrides if you need them:
#   PROXY=http://127.0.0.1:8181  MODEL=claude-opus-4-8  sh run-claude.sh

cd "$(dirname "$0")" || exit 1

PROXY="${PROXY:-http://127.0.0.1:8181}"
MODEL="${MODEL:-claude-opus-4-8}"

# The proxy has to be up first, otherwise Claude Code fails with a confusing
# connection error and the real cause is invisible.
if ! curl -s -o /dev/null --max-time 3 "$PROXY/health"; then
  echo "!! ccproxy is not answering on $PROXY"
  echo "   Start it first, in another terminal:"
  echo "       cd $(pwd) && sh start.sh"
  exit 1
fi

echo "Claude Code  ->  $PROXY   (model: $MODEL)"
echo "Global ~/.claude/settings.json is NOT modified."
echo "Watch the traffic:  tail -f $(pwd)/ccproxy_log.txt"
echo

# Set the env vars AND pass --settings: whichever one Claude Code gives priority
# to, both point at the proxy.
ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-${UPSTREAM_API_KEY}}"
if [ -z "$ANTHROPIC_KEY" ] && [ -f config.json ]; then
  ANTHROPIC_KEY=$(grep -o '"api_key": "[^"]*' config.json 2>/dev/null | cut -d'"' -f4)
fi
ANTHROPIC_KEY="${ANTHROPIC_KEY:-dummy}"

export ANTHROPIC_BASE_URL="$PROXY"
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_API_KEY="$ANTHROPIC_KEY"
export ENABLE_TOOL_SEARCH="false"         # the proxy relies on this being off

exec claude --settings "{\"env\":{\"ANTHROPIC_BASE_URL\":\"$PROXY\",\"ANTHROPIC_MODEL\":\"$MODEL\",\"ANTHROPIC_API_KEY\":\"$ANTHROPIC_KEY\",\"ENABLE_TOOL_SEARCH\":\"false\"}}" "$@"
