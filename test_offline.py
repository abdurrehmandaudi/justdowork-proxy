#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 abdurrehmandaudi
# Required Notice: Copyright (c) 2026 abdurrehmandaudi -- justdowork-proxy
# Licensed under the PolyForm Noncommercial License 1.0.0 -- commercial
# use is not permitted without a separate written commercial license.
# See LICENSE or https://polyformproject.org/licenses/noncommercial/1.0.0
"""
test_offline.py -- tests everything in ccproxy without a real API key.

How to run:
    /Users/abdurrehmandaudigmail.com/deepseek-proxy/.venv/bin/python3 test_offline.py

It uses the actual payloads that used to be DROPPED by the old proxy (taken from
debug_dump), and starts a mock upstream to check the whole
request -> tool_call -> response cycle.
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("UPSTREAM_API_KEY", "sk-test-offline")
os.environ.setdefault("TARGET_URL", "http://127.0.0.1:8799")

import ccproxy  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))


# --------------------------------------------------------------------------- #
# 1. JSON repair -- this was the multi-agent killer
# --------------------------------------------------------------------------- #
print("\n=== 1. JSON repair (the payloads that really failed) ===")

# (a) Agent: LITERAL NEWLINES in the prompt -> strict json.loads used to fail
agent_body = ('{"description": "Explore repo", "prompt": "Read these files:\n'
              '- a.py\n- b.py\n\nThen summarize the findings.", '
              '"subagent_type": "Explore"}')
obj = ccproxy.loads_tool_json(agent_body)
check("Agent payload (literal newlines) parsed",
      obj is not None and obj.get("subagent_type") == "Explore")
check("Agent prompt kept its newlines",
      obj is not None and "\n- a.py" in obj.get("prompt", ""))

# (b) Monitor: INVALID ESCAPES `\.` and `\]` -> this is what got dropped 3049 times
monitor_body = ('{"command": "until grep -qE \\"Installed\\. Open|warn\\]\\" '
                '/tmp/x.output 2>/dev/null; do sleep 1; done", "description": "wait"}')
obj = ccproxy.loads_tool_json(monitor_body)
check("Monitor payload (invalid escapes \\. and \\]) parsed", obj is not None)
check("Monitor regex survived (backslash still alive)",
      obj is not None and "Installed\\. Open" in obj.get("command", ""),
      repr(obj.get("command", "")[:60]) if obj else "")

# (c) trailing comma
check("Trailing comma fixed",
      ccproxy.loads_tool_json('{"a": 1, "b": [1,2,],}') == {"a": 1, "b": [1, 2]})

# (d) extra closing bracket
check("Extra closing bracket ignored",
      ccproxy.loads_tool_json('{"a": 1}}]') == {"a": 1})

# (e) missing closing brace
check("Missing closing brace added",
      ccproxy.loads_tool_json('{"a": {"b": 2}') == {"a": {"b": 2}})

# (f) VALID JSON must not be corrupted
good = '{"command": "grep -E \\"a\\\\.b\\" file", "n": 3, "flag": true}'
obj = ccproxy.loads_tool_json(good)
check("Valid JSON came back exactly as it was (no corruption)",
      obj == {"command": 'grep -E "a\\.b" file', "n": 3, "flag": True}, repr(obj))

# (g) Write tool: literal newlines plus code in the content
write_body = '{"file_path": "x.py", "content": "def f():\n    return 1\n\nprint(f())"}'
obj = ccproxy.loads_tool_json(write_body)
check("Write content (multi-line code) parsed",
      obj is not None and "def f():" in obj.get("content", ""))


# --------------------------------------------------------------------------- #
# 2. parse_assistant_text -- whole response -> blocks
# --------------------------------------------------------------------------- #
print("\n=== 2. Response parsing ===")

valid = {"Agent", "Monitor", "Bash", "WebSearch", "Read"}

# (a) one Agent call, with newlines
txt = "Launching the agent now.\n\n" + '<tool_call name="Agent">' + agent_body + "</tool_call>"
blocks = ccproxy.parse_assistant_text(txt, valid_names=valid, strict=True)
tools = [b for b in blocks if b["type"] == "tool_use"]
check("Agent tool_use block created (used to be dropped)",
      len(tools) == 1 and tools[0]["name"] == "Agent")
check("The text around the Agent call survived too",
      any(b["type"] == "text" and "Launching" in b["text"] for b in blocks))

# (b) parallel calls in one reply
txt2 = ('<tool_call name="WebSearch">{"query": "a"}</tool_call>\n'
        '<tool_call name="Monitor">' + monitor_body + "</tool_call>")
tools2 = [b for b in ccproxy.parse_assistant_text(txt2, valid, True) if b["type"] == "tool_use"]
check("Two parallel tool calls -> two blocks", len(tools2) == 2,
      str([t["name"] for t in tools2]))

# (c) works even when the closing tag is missing
txt3 = '<tool_call name="Read">{"file_path": "a.py"}'
tools3 = [b for b in ccproxy.parse_assistant_text(txt3, valid, True) if b["type"] == "tool_use"]
check("Works without a closing </tool_call>",
      len(tools3) == 1 and tools3[0]["input"] == {"file_path": "a.py"})

# (d) native XML <invoke><parameter>
txt4 = ('<invoke name="Bash"><parameter name="command">ls -la</parameter>'
        '<parameter name="description">list</parameter></invoke>')
tools4 = [b for b in ccproxy.parse_assistant_text(txt4, valid, True) if b["type"] == "tool_use"]
check("Native <invoke> XML form also parses",
      len(tools4) == 1 and tools4[0]["input"].get("command") == "ls -la")

# (e) a name outside the list produces no call (strict)
txt5 = '<tool_call name="SomethingElse">{"x": 1}</tool_call>'
tools5 = [b for b in ccproxy.parse_assistant_text(txt5, valid, True) if b["type"] == "tool_use"]
check("A name outside the list produces no tool_use (strict)", len(tools5) == 0)

# (f) plain text keeps working
blocks6 = ccproxy.parse_assistant_text("Just a normal answer.", valid, True)
check("Plain text passed through correctly",
      len(blocks6) == 1 and blocks6[0]["type"] == "text")


# --------------------------------------------------------------------------- #
# 2b. Tool results must survive the pipeline
#
# Regression: `_is_empty_content()` counted only `text` and `image` blocks as
# content. A `tool_result` block has no `text` field, so it looked empty and
# `normalize_alternating()` deleted the whole message. The model never saw what
# its tool returned, so it called the same tool again on every turn -- the
# "mkdir forever" loop. Every native Read/Write/Edit/Bash result was lost.
# --------------------------------------------------------------------------- #
print("\n=== 2b. Tool results survive to the payload ===")

check("A tool_result block is not 'empty'",
      not ccproxy._is_empty_content(
          [{"type": "tool_result", "tool_use_id": "t1", "content": "output"}]))

check("A tool_use block is not 'empty'",
      not ccproxy._is_empty_content(
          [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]))

check("An empty tool_result is still kept (the model must see the tool ran)",
      not ccproxy._is_empty_content(
          [{"type": "tool_result", "tool_use_id": "t1", "content": ""}]))

check("A genuinely empty message is still dropped",
      ccproxy._is_empty_content([{"type": "text", "text": "   "}]))

_loop_req = {
    "model": "claude-opus-4-8",
    "messages": [
        {"role": "user", "content": "make a folder"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "OK."},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "mkdir -p /tmp/x"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "(folder created)"}]},
    ],
}
_loop_feats = dict(ccproxy.FEATS)
_loop_feats["tool_injection"] = False
_loop_payload = ccproxy.build_payload(_loop_req, _loop_feats)
_loop_msgs = _loop_payload["messages"]

check("The tool_result message reached the payload (3 msgs, not 2)",
      len(_loop_msgs) == 3, f"(got {len(_loop_msgs)})")
check("The tool_result block itself is in the last message",
      any(isinstance(b, dict) and b.get("type") == "tool_result"
          for b in (_loop_msgs[-1].get("content") or [])))
check("The assistant kept its native tool_use",
      any(isinstance(b, dict) and b.get("type") == "tool_use"
          and b.get("name") == "bash"
          for b in (_loop_msgs[1].get("content") or [])))


# --------------------------------------------------------------------------- #
# 3. History trimming -- 1.65MB -> a budget
# --------------------------------------------------------------------------- #
print("\n=== 3. Token saving (history trim) ===")

feats = dict(ccproxy.FEATS)
feats["max_history_chars"] = 200000
feats["max_tool_result_chars"] = 4000

# a synthetic 1300-message history (like the real dump)
big = [{"role": "user", "content": "Start working on the project."}]
for i in range(1300):
    big.append({"role": "assistant", "content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "ls"}}]})
    big.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"t{i}",
         "content": "x" * 2000}]})

before = sum(ccproxy._msg_chars(m) for m in big)
trimmed = ccproxy.trim_history(big, feats)
after = sum(ccproxy._msg_chars(m) for m in trimmed)
check(f"1300-message history trimmed: {before} -> {after} chars",
      after < 200000 and after < before // 4,
      f"({after * 100 // before}% left)")
check("The first message is still from the user", trimmed[0]["role"] == "user")

_used = {x.get("id") for m in trimmed if isinstance(m.get("content"), list)
         for x in m["content"] if isinstance(x, dict) and x.get("type") == "tool_use"}
_orphans = [b for m in trimmed if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
            and b.get("tool_use_id") not in _used]
check("No orphaned tool_results after trimming", not _orphans,
      f"{len(_orphans)} orphans found" if _orphans else "")

# the full payload builds too
req = {"model": "claude-opus-4-8", "max_tokens": 64000, "stream": True,
       "system": [{"type": "text", "text": "You are Claude Code." * 200}],
       "tools": [{"name": "Agent", "description": "Launch agent\n\nLong text " * 40,
                  "input_schema": {"type": "object", "properties": {
                      "description": {"type": "string"}, "prompt": {"type": "string"}},
                      "required": ["description", "prompt"]}}],
       "messages": big}
pl = ccproxy.build_payload(req, feats)
pl.pop("_breakdown", None)
size = len(json.dumps(pl))
check(f"The whole payload became compact ({before + 40000} -> {size} chars)",
      size < 250000, f"{size} chars (~{size // 4} tokens)")
check("A cache breakpoint was set on the system block",
      isinstance(pl.get("system"), list) and pl["system"][0].get("cache_control") is not None)
check("The short tool description was used in compact mode",
      "Launch agent" in pl["system"][0]["text"] and "Long text Long text" not in pl["system"][0]["text"])

# 2c. History is never dropped silently any more. The old proxy deleted ~80% of
# a long session (a real log line read `client_msgs=857 sent_msgs=163`) and the
# model, seeing an original task then a disconnected recent tail, forgot the job
# and started repeating itself.
_st = {}
_t2 = ccproxy.trim_history(big, feats, _st)
check("trim_history reports what it dropped and kept",
      _st["dropped"] > 0 and _st["kept"] == len(_t2) and _st["client"] == len(big),
      str(_st))
_marker = [m for m in _t2 if "were omitted" in json.dumps(m)]
check("Exactly one marker is left where the history was cut", len(_marker) == 1,
      f"{len(_marker)} markers")
check("The marker says how many messages went missing",
      str(_st["dropped"]) in json.dumps(_marker[0]) if _marker else False)
check("The model is warned that the conversation is incomplete",
      _marker and "incomplete" in json.dumps(_marker[0]))

# 2d. The human's own turns survive. They are small, and they are the thing the
# model must not forget -- losing them is exactly "2 prompt baad bhool jata hai".
_hist = [{"role": "user", "content": "TASK: build the thing"}]
for _i in range(400):
    _hist.append({"role": "assistant",
                  "content": [{"type": "text", "text": "step " + "z" * 3000}]})
    _hist.append({"role": "user", "content": f"INSTRUCTION {_i}: do the next part"})
_st2 = {}
_t3 = ccproxy.trim_history(_hist, dict(feats, max_history_chars=60000), _st2)
_user_kept = [m for m in _t3 if m.get("role") == "user"]
check("Older user instructions are kept, not dropped", len(_user_kept) > 20,
      f"{len(_user_kept)} user messages kept out of 401")
check("The original task is still the first message",
      "TASK: build the thing" in json.dumps(_t3[0]))

# 2e. Sizes are measured at the size a message is really forwarded at. Measuring
# raw sizes made the budget look full when it was not: on a session of 12k-char
# tool results the old proxy fitted ~18 messages where this one fits ~148.
_big_msg = {"role": "user", "content": [
    {"type": "tool_result", "tool_use_id": "x", "content": "q" * 50000}]}
check("_msg_chars measures a tool result at the forwarding cap",
      ccproxy._msg_chars(_big_msg, 4000) < 5000 < ccproxy._msg_chars(_big_msg),
      f"capped={ccproxy._msg_chars(_big_msg, 4000)} raw={ccproxy._msg_chars(_big_msg)}")
_capped = ccproxy._cap_blocks([{"type": "text", "text": "q" * 50000}], 4000)
check("_cap_blocks truncates a list-content tool result",
      len(_capped[0]["text"]) < 5000, f"{len(_capped[0]['text'])} chars")
check("_cap_blocks leaves an image alone (there is no text to cut)",
      ccproxy._cap_blocks([{"type": "image", "source": {"data": "a" * 9000}}],
                          4000)[0]["type"] == "image")

# 2f. The session rules. The user's complaint was also that the model never
# asked before doing something -- so the rules have to reach the system prompt.
_rules = ccproxy.build_payload(
    {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}],
     "tools": [{"name": "Agent", "description": "d",
                "input_schema": {"type": "object", "properties": {}}}]}, feats)
_rsys = _rules["system"][0]["text"] if isinstance(_rules.get("system"), list) \
    else (_rules.get("system") or "")
check("The session rules reach the system prompt", "How this session works" in _rsys)
check("...and tell the model to offer concrete options", '"A: ... or B: ..."' in _rsys)
check("...and tell it not to repeat a failing call", "already failed" in _rsys)
check("A short conversation gets no context notice", "Context notice" not in _rsys)
check("A trimmed conversation does get a context notice",
      "Context notice" in pl["system"][0]["text"])

# Native tools have to travel in the `tools` array. Without this the relay never
# offers them, and Read/Write/Edit/Bash silently stop working.
req_native = {"model": "claude-opus-4-8", "max_tokens": 4096, "stream": False,
              "system": "You are Claude Code.",
              "tools": [
                  {"name": "Read", "description": "Read a file.",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                    "required": ["file_path"]}},
                  {"name": "Write", "description": "Write a file.",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path": {"type": "string"},
                                                   "content": {"type": "string"}},
                                    "required": ["file_path", "content"]}},
                  {"name": "Agent", "description": "Launch agent", "input_schema":
                   {"type": "object", "properties": {"prompt": {"type": "string"}}}}],
              "messages": [{"role": "user", "content": "hi"}]}
pl2 = ccproxy.build_payload(req_native, feats)
_sent = sorted(t["name"] for t in (pl2.get("tools") or []))
check("Read/Write are sent as native tools in the payload",
      _sent == ["read", "write"], str(_sent))
check("Agent is NOT sent natively (it goes through <tool_call>)",
      "agent" not in _sent, str(_sent))
check("The native schema rode along (the relay needs the parameters)",
      any((t.get("input_schema") or {}).get("properties", {}).get("file_path")
          for t in (pl2.get("tools") or [])))
_sys2 = pl2["system"][0]["text"] if isinstance(pl2.get("system"), list) else (pl2.get("system") or "")
check("The prompt names the native lowercase tools",
      "`read`" in _sys2 and "`write`" in _sys2)
check("The prompt tells the model to ignore the relay's fake tool names",
      "read_tabular" in _sys2 and "system_todo_write" in _sys2)
check("Agent is still described through <tool_call>", "`Agent`" in _sys2)
check("A forced tool_choice is mapped to the native name",
      (ccproxy.build_payload(dict(req_native, tool_choice={"type": "tool", "name": "Read"}),
                             feats).get("tool_choice") or {}).get("name") == "read")


# --------------------------------------------------------------------------- #
# 4. Full integration -- against a mock upstream
# --------------------------------------------------------------------------- #
print("\n=== 4. Integration (mock upstream) ===")

from flask import Flask, Response, request as freq  # noqa: E402

mock = Flask("mock-upstream")
MOCK = {"calls": [], "script": []}


@mock.route("/v1/messages", methods=["POST"])
def mock_messages():
    body = freq.get_json(force=True)
    MOCK["calls"].append(body)
    idx = len(MOCK["calls"]) - 1
    script = MOCK["script"]
    entry = script[idx] if idx < len(script) else "done"
    # an int in the script means "answer with that HTTP status and an empty
    # body" -- this is what the real relay does to ~26% of requests, so the
    # tests can replay it exactly
    if isinstance(entry, int):
        return Response("", status=entry)
    text = entry
    return Response(json.dumps({
        "id": f"msg_mock{idx}", "type": "message", "role": "assistant",
        "model": body.get("model"), "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 12345, "output_tokens": 42}}),
        content_type="application/json")


threading.Thread(target=lambda: mock.run(port=8799, threaded=True, debug=False),
                 daemon=True).start()
threading.Thread(target=lambda: ccproxy.app.run(port=8798, threaded=True, debug=False),
                 daemon=True).start()
time.sleep(2.0)

import requests  # noqa: E402

PROXY = "http://127.0.0.1:8798/v1/messages"


def pcall(payload):
    return requests.post(PROXY, json=payload, timeout=120)


base_req = {"model": "claude-opus-4-8", "max_tokens": 4096, "stream": False,
            "system": "You are Claude Code.",
            "tools": [{"name": "Bash", "description": "run", "input_schema":
                       {"type": "object", "properties": {"command": {"type": "string"}}}},
                      {"name": "Agent", "description": "launch", "input_schema":
                       {"type": "object", "properties": {"prompt": {"type": "string"}}}}],
            "messages": [{"role": "user", "content": "hello"}]}

# (a) a normal request
MOCK["calls"].clear()
MOCK["script"] = []
r = pcall(dict(base_req))
check("Non-stream request returned 200 OK", r.status_code == 200, r.text[:150])

_sent_tools = [t.get("name") for t in (MOCK["calls"][0].get("tools") or [])]
check("The upstream call carried the native `bash` tool",
      _sent_tools == ["bash"], str(_sent_tools))

# (b) the Agent call made it through (this used to be dropped)
MOCK["calls"].clear()
MOCK["script"] = ["Sending the agent now.\n\n" + '<tool_call name="Agent">' + agent_body + "</tool_call>"]
r = pcall(dict(base_req))
j = r.json()
tus = [b for b in j.get("content", []) if b.get("type") == "tool_use"]
check("Agent tool_use reached the client", len(tus) == 1 and tus[0]["name"] == "Agent")
check("stop_reason = tool_use", j.get("stop_reason") == "tool_use")
check("The Agent prompt is complete (newlines intact)",
      len(tus) == 1 and "\n- a.py" in tus[0]["input"].get("prompt", ""))

# (c) WebSearch is run by the proxy itself (network call monkeypatched)
called = {}
orig_search = ccproxy.run_web_search


def fake_search(q, f):
    called["q"] = q
    return "Web search results for: %s\n1. Example\n   https://example.com\n   snippet" % q


ccproxy.run_web_search = fake_search
MOCK["calls"].clear()
MOCK["script"] = ['<tool_call name="WebSearch">{"query": "flutter build error"}</tool_call>',
                  "According to the search, this error comes from X."]
r = pcall(dict(base_req))
j = r.json()
check("The proxy ran WebSearch itself", called.get("q") == "flutter build error")
check("The client only saw the final answer (no tool_use)",
      not any(b.get("type") == "tool_use" for b in j.get("content", [])))
check("The final answer came after the search",
      "comes from X" in json.dumps(j.get("content", [])), json.dumps(j.get("content"))[:120])
check("The search results were sent back to the model (2 upstream calls)",
      len(MOCK["calls"]) == 2 and "Web search results" in
      json.dumps(MOCK["calls"][1].get("messages", []))[:100000])
ccproxy.run_web_search = orig_search

# (d) streaming
MOCK["calls"].clear()
MOCK["script"] = ["The streaming answer is fine."]
r = requests.post(PROXY, json=dict(base_req, stream=True), timeout=120)
body = r.text
check("SSE stream returned 200 OK", r.status_code == 200)
check("message_start received", "event: message_start" in body)
check("ping received (no dead air)", "event: ping" in body)
check("text_delta received", "text_delta" in body and "streaming answer" in body)
check("message_stop received", "event: message_stop" in body)

# (e) count_tokens
r = requests.post("http://127.0.0.1:8798/v1/messages/count_tokens",
                  json={"messages": [{"role": "user", "content": "x" * 400}]}, timeout=30)
check("count_tokens endpoint works (used to be 404)",
      r.status_code == 200 and r.json().get("input_tokens", 0) > 50)

# (f) health
r = requests.get("http://127.0.0.1:8798/health", timeout=30)
check("/health OK", r.status_code == 200 and r.json().get("ok"))


# --------------------------------------------------------------------------- #
# 5. Dashboard (served at /)
# --------------------------------------------------------------------------- #
print("\n=== 5. Dashboard ===")

check("dashboard.py loaded (not the fallback page)",
      bool(getattr(ccproxy, "DASHBOARD_HTML", None)))

r = requests.get("http://127.0.0.1:8798/", timeout=30)
check("/ returns HTML", r.status_code == 200 and "text/html" in r.headers.get("content-type", ""))
check("The page mentions ccproxy and /stats.json",
      "ccproxy" in r.text and "/stats.json" in r.text)

r = requests.get("http://127.0.0.1:8798/stats.json", timeout=30)
check("/stats.json returned 200 OK", r.status_code == 200, r.text[:120])
st = r.json() if r.status_code == 200 else {}
for key in ("total_reqs", "ok", "fail", "in_tok", "out_tok", "duration",
            "server_tools", "tool_uses", "disk", "recent", "errors",
            "saved_tok", "sent_tok", "raw_hist_tok", "raw_tools_tok",
            "max_history_chars", "max_tool_result_chars"):
    if key not in st:
        check(f"stats contains '{key}'", False)
        break
else:
    check("stats contains every required field", True)

check("Requests were counted (total_reqs > 0)", st.get("total_reqs", 0) > 0,
      f"total={st.get('total_reqs')}")
check("The recent list is not empty", len(st.get("recent") or []) > 0)
check("The Agent tool_use was counted", (st.get("tool_uses") or {}).get("Agent", 0) > 0,
      str(st.get("tool_uses")))
check("The WebSearch server tool was counted",
      sum((st.get("server_tools") or {}).values()) > 0, str(st.get("server_tools")))
check("Disk accounting is present", "log_bytes" in (st.get("disk") or {}))
_avg = st.get("duration", 0) / max(1, st.get("total_reqs", 1))
check(f"Average latency was computed ({round(_avg, 2)}s)", _avg >= 0)

r = requests.post("http://127.0.0.1:8798/stats/reset", timeout=30)
check("/stats/reset worked", r.status_code == 200 and r.json().get("ok"))
st2 = requests.get("http://127.0.0.1:8798/stats.json", timeout=30).json()
check("Counters are zero after reset", st2.get("total_reqs") == 0 and not st2.get("recent"))

# the "avoided by trimming" counter has to actually move on a big history
_before_saved = st2.get("saved_tok", 0)
bigmsgs = [{"role": "user", "content": "Start."}]
for i in range(300):
    bigmsgs.append({"role": "assistant", "content": [
        {"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": "ls"}}]})
    bigmsgs.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"b{i}", "content": "y" * 3000}]})
_raw_chars = sum(ccproxy._msg_chars(m) for m in bigmsgs)

MOCK["calls"].clear()
MOCK["script"] = ["done"]
r = pcall(dict(base_req, messages=bigmsgs))
check("A big-history request still returned 200 OK", r.status_code == 200, r.text[:150])

_sent_chars = len(json.dumps(MOCK["calls"][0].get("messages", [])))
check(f"Upstream payload was much smaller than the client sent "
      f"({_raw_chars} -> {_sent_chars} chars)", _sent_chars < _raw_chars // 3)

st3 = requests.get("http://127.0.0.1:8798/stats.json", timeout=30).json()
check("saved_tok counted the tokens avoided by trimming",
      st3.get("saved_tok", 0) > _before_saved,
      f"saved={st3.get('saved_tok')} tokens")
check("sent_tok shows what was actually sent",
      st3.get("sent_tok", 0) > 0, f"sent={st3.get('sent_tok')} tokens")
_last = (st3.get("recent") or [{}])[0]
check("The per-request row compares kept vs offered history",
      _last.get("raw_hist_tok", 0) > _last.get("hist_tok", 0),
      f"kept={_last.get('hist_tok')} of offered={_last.get('raw_hist_tok')}")

# --------------------------------------------------------------------------- #
# 6. Retrying the relay's random rejection
# --------------------------------------------------------------------------- #
print("\n=== 6. Retry against random 403/503 ===")

# The real relay drops ~26% of requests with an empty 403 or 503, and does so at
# random -- a retry sent immediately after is served normally. So the first
# retry must not sleep, and a 403 must count as retryable.
import inspect  # noqa: E402
_src = inspect.getsource(ccproxy.call_upstream)
check("403 is in the retryable set", "403" in _src.split("retryable = {")[1].split("}")[0],
      _src.split("retryable = {")[1].split("}")[0].strip()[:80])
check("The backoff list starts at zero",
      "backoff = [0.0" in _src, _src.split("backoff =")[1].split("\n")[0].strip())

MOCK["calls"].clear()
MOCK["script"] = [403, "recovered"]
_t0 = time.time()
r = pcall(dict(base_req))
_dt = time.time() - _t0
check("A 403 was retried and the request still succeeded",
      r.status_code == 200 and "recovered" in r.text, f"{r.status_code} {r.text[:80]}")
check("The upstream really was called twice", len(MOCK["calls"]) == 2,
      f"calls={len(MOCK['calls'])}")
check("The retry did not sleep before trying again (no 2s stall)",
      _dt < 2.0, f"took {_dt:.2f}s")

MOCK["calls"].clear()
MOCK["script"] = [503, 403, "third time"]
r = pcall(dict(base_req))
check("Two rejections in a row are both survived (3 attempts)",
      r.status_code == 200 and "third time" in r.text, f"{r.status_code} {r.text[:80]}")
check("All three attempts reached upstream", len(MOCK["calls"]) == 3,
      f"calls={len(MOCK['calls'])}")

MOCK["calls"].clear()
MOCK["script"] = [401, "never"]
r = pcall(dict(base_req))
check("An empty 401 is NOT retried (it would never succeed)",
      len(MOCK["calls"]) == 1, f"calls={len(MOCK['calls'])}")

# --------------------------------------------------------------------------- #
# 7. Extended thinking: spent on the turn that plans, not the ones that execute
# --------------------------------------------------------------------------- #
print("\n=== 7. Adaptive extended thinking ===")

_FE = dict(ccproxy.FEATS)
ccproxy.FEATS.update({"thinking_enabled": True, "thinking_adaptive": True,
                      "thinking_budget_tokens": 2000,
                      "thinking_max_budget_tokens": 8000})

def _payload_for(messages, **over):
    req = {"model": "claude-opus-4-8", "max_tokens": 4096, "messages": messages}
    req.update(over)
    return ccproxy.build_payload(req, ccproxy.FEATS, log_breakdown=False)

_fresh = _payload_for([{"role": "user", "content": "plan this out properly"}])
check("A fresh user turn gets a thinking budget",
      isinstance(_fresh.get("thinking"), dict) and _fresh["thinking"].get("type") == "enabled",
      str(_fresh.get("thinking")))
check("The adaptive budget is the configured one",
      _fresh.get("thinking", {}).get("budget_tokens") == 2000,
      str(_fresh.get("thinking")))
check("max_tokens was raised to leave room past the thinking budget",
      _fresh.get("max_tokens", 0) > 2000, str(_fresh.get("max_tokens")))

_mid = _payload_for([
    {"role": "user", "content": "do the thing"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                       "input": {"file_path": "/x"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "file body"}]}])
check("A tool_result turn gets NO thinking budget (it is executing, not planning)",
      "thinking" not in _mid, str(_mid.get("thinking")))

_cl = _payload_for([{"role": "user", "content": "hi"}],
                   thinking={"type": "enabled", "budget_tokens": 5000})
check("A client-requested budget is forwarded",
      _cl.get("thinking", {}).get("budget_tokens") == 5000, str(_cl.get("thinking")))

_cap = _payload_for([{"role": "user", "content": "hi"}],
                    thinking={"type": "enabled", "budget_tokens": 99999})
check("An oversized client budget is capped, not passed through",
      _cap.get("thinking", {}).get("budget_tokens") == 8000, str(_cap.get("thinking")))

ccproxy.FEATS.update({"thinking_enabled": False})
_off = _payload_for([{"role": "user", "content": "hi"}])
check("thinking_enabled=false sends no thinking at all",
      "thinking" not in _off, str(_off.get("thinking")))
_dis = _payload_for([{"role": "user", "content": "hi"}], thinking={"type": "disabled"})
check("An explicit client `disabled` beats the adaptive default",
      "thinking" not in _dis, str(_dis.get("thinking")))
ccproxy.FEATS.clear()
ccproxy.FEATS.update(_FE)

# whose thinking is it? a proxy-decided budget stays hidden from the client
check("A proxy-decided budget is NOT handed back to the client",
      ccproxy._client_wants_thinking({"messages": []}, ccproxy.FEATS) is False)
check("A client-requested budget IS handed back",
      ccproxy._client_wants_thinking(
          {"thinking": {"type": "enabled", "budget_tokens": 1000}}, ccproxy.FEATS) is True)
_th = {"type": "thinking", "thinking": "reasoning", "signature": "sig"}
_up = {"content": [_th, {"type": "text", "text": "answer"}]}
_kinds = [b.get("type") for b in ccproxy.process_upstream(
    _up, {"messages": []}, ccproxy.FEATS, set())[0].get("content", [])]
check("process_upstream drops the thinking block by default",
      "thinking" not in _kinds, str(_kinds))
_kinds2 = [b.get("type") for b in ccproxy.process_upstream(
    _up, {"thinking": {"type": "enabled", "budget_tokens": 1000}},
    ccproxy.FEATS, set())[0].get("content", [])]
check("process_upstream keeps it when the client asked for thinking",
      "thinking" in _kinds2, str(_kinds2))

# --------------------------------------------------------------------------- #
# 8. The model can see the file it is editing
# --------------------------------------------------------------------------- #
print("\n=== 8. Tool results reach the model whole ===")

_big_file = "def f():\n    return 1\n" * 1200          # ~27k chars
check("The shipped cap is big enough to hold a real source file",
      ccproxy.FEATS.get("max_tool_result_chars", 0) >= 30000,
      str(ccproxy.FEATS.get("max_tool_result_chars")))

def _tool_result_len(cap):
    _fe = dict(ccproxy.FEATS)
    _fe["max_tool_result_chars"] = cap
    _p = ccproxy.build_payload({
        "model": "claude-opus-4-8", "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t9",
                                               "name": "Read", "input": {"file_path": "/x"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9",
                                          "content": _big_file}]}]}, _fe, log_breakdown=False)
    body = json.dumps(_p["messages"])
    return body.count("def f()"), len(body)

_n_old, _len_old = _tool_result_len(4000)
_n_new, _len_new = _tool_result_len(32000)
check("At the old 4000 cap the model saw only part of the file",
      _n_old < 400, f"{_n_old} of 1200 lines reached the model")
check("At the shipped 32000 cap it sees the whole file",
      _n_new > 1100, f"{_n_new} of 1200 lines reached the model")
check("...and the payload did not explode doing it",
      _len_new < _len_old * 8, f"{_len_old} -> {_len_new} chars")

check("The system prompt now tells the model to look before it changes",
      "Look before you change" in ccproxy.WORKING_RULES)
check("...and why a symptom patch is not a fix",
      "not a fix" in ccproxy.WORKING_RULES)

# --------------------------------------------------------------------------- #
# 9. Several server-side web tools in one reply run together
# --------------------------------------------------------------------------- #
print("\n=== 9. Parallel server tools ===")

_seen = []
_real_exec = ccproxy.execute_server_tool

def _fake_exec(name, inp, feats):
    _seen.append((name, inp.get("query") or inp.get("url") or ""))
    time.sleep(0.3)                      # stands in for a live search
    return {"text": f"[result for {inp.get('query') or inp.get('url')}]"}

ccproxy.execute_server_tool = _fake_exec
try:
    MOCK["calls"].clear()
    MOCK["script"] = ['<tool_call name="WebSearch">{"query": "alpha"}</tool_call>\n'
                      '<tool_call name="WebSearch">{"query": "beta"}</tool_call>\n'
                      '<tool_call name="WebSearch">{"query": "gamma"}</tool_call>',
                      "all done"]
    _t0 = time.time()
    r = pcall(dict(base_req))
    _dt = time.time() - _t0
    check("A reply asking for three web searches still completed",
          r.status_code == 200 and "all done" in r.text, f"{r.status_code} {r.text[:70]}")
    check("All three searches actually ran", len(_seen) == 3, str(_seen))
    check("They ran at the same time, not one after another",
          _dt < 0.75, f"took {_dt:.2f}s for 3 x 0.3s of work")
    check("...and the results came back in one follow-up, not three",
          len(MOCK["calls"]) == 2, f"upstream calls={len(MOCK['calls'])}")
    _last_msg = (MOCK["calls"][-1].get("messages") or [])[-1]
    _txt = json.dumps(_last_msg)
    check("The follow-up carried every result",
          all(q in _txt for q in ("alpha", "beta", "gamma")), _txt[-160:])
finally:
    ccproxy.execute_server_tool = _real_exec
    MOCK["script"] = []

# --------------------------------------------------------------------------- #
# 10. Prompt caching
#
# The relay DOES accept `cache_control` and answers with a cache_* usage block,
# which is why this looked like it worked. It does not: measured directly
# against the relay on 2026-10-07, the cache read it reports is 10278 tokens
# whether the cacheable prefix is 1,336 chars or 13,569 -- a constant, i.e. the
# relay's own hidden prefix, never the conversation's. Without cache_control it
# reports cache_creation = input_tokens - 2 on every request, which is
# "everything was a write" and equally meaningless. These checks pin down what
# the proxy does with all that, so the behaviour cannot drift silently.
# --------------------------------------------------------------------------- #
print("\n=== 10. Prompt caching ===")

_cache_req = {"model": "claude-opus-4-8", "max_tokens": 100,
              "system": "You are a helpful assistant. " * 40,
              "messages": [{"role": "user", "content": "hi"}]}

_on = ccproxy.build_payload(dict(_cache_req), ccproxy.FEATS, log_breakdown=False)
_sys_on = _on.get("system")
check("With cache_system_prefix on, system is sent as a block list",
      isinstance(_sys_on, list) and _sys_on and _sys_on[0].get("type") == "text",
      type(_sys_on).__name__)
check("...carrying a cache_control breakpoint",
      isinstance(_sys_on, list) and _sys_on[0].get("cache_control") == {"type": "ephemeral"},
      str(_sys_on[0].get("cache_control") if isinstance(_sys_on, list) else None))
check("...and the caller's own system text survives inside it",
      isinstance(_sys_on, list) and _cache_req["system"] in _sys_on[0].get("text", ""),
      f"sent {len(_sys_on[0].get('text',''))} chars vs {len(_cache_req['system'])} given"
      if isinstance(_sys_on, list) else "")
check("The breakpoint is NOT put on the messages (nothing else is marked)",
      all("cache_control" not in json.dumps(m)
          for m in (_on.get("messages") or [])))

_feats_off = dict(ccproxy.FEATS, cache_system_prefix=False)
_off = ccproxy.build_payload(dict(_cache_req), _feats_off, log_breakdown=False)
check("With cache_system_prefix off, system stays a plain string",
      isinstance(_off.get("system"), str), type(_off.get("system")).__name__)
check("...and no cache_control appears anywhere in the payload",
      "cache_control" not in json.dumps(_off))

# --- what the client is told -------------------------------------------------
_u = ccproxy._usage_for_client(
    {"input_tokens": 15161, "output_tokens": 7,
     "cache_creation_input_tokens": 4881, "cache_read_input_tokens": 10278}, 15161)
check("A cache read the relay reports IS forwarded to Claude Code",
      _u.get("cache_read_input_tokens") == 10278, str(_u))
check("cache_creation_input_tokens is NOT forwarded (it is input-2, a constant)",
      "cache_creation_input_tokens" not in _u, str(_u))
check("...input and output tokens still pass through",
      _u.get("input_tokens") == 15161 and _u.get("output_tokens") == 7)

_u2 = ccproxy._usage_for_client({"input_tokens": 100, "output_tokens": 3}, 100)
check("No cache fields in, no cache fields out",
      set(_u2) == {"input_tokens", "output_tokens"}, str(_u2))

# --- what the proxy records --------------------------------------------------
_rec = ccproxy.make_rec(9001, {"breakdown": {}, "snap0": {}, "stream": False,
                               "client_msgs": 1, "t0": time.time(),
                               "payload_chars": 10, "n_tools": 0},
                        {"usage": {"input_tokens": 15161, "output_tokens": 7,
                                   "cache_creation_input_tokens": 4881,
                                   "cache_read_input_tokens": 10278}})
check("A request record carries the cache read the relay claimed",
      _rec.get("cache_read_tok") == 10278, str(_rec.get("cache_read_tok")))
check("...and the cache write, even though it is not forwarded to the client",
      _rec.get("cache_write_tok") == 4881, str(_rec.get("cache_write_tok")))

_before = dict(ccproxy.STATS)
ccproxy.record(_rec)
check("Recording it adds to the cache-read total",
      ccproxy.STATS["cache_read_tok"] - _before.get("cache_read_tok", 0) == 10278)
check("...and counts it as one request that saw a cache read",
      ccproxy.STATS["cache_hit_reqs"] - _before.get("cache_hit_reqs", 0) == 1)

_rec2 = ccproxy.make_rec(9002, {"breakdown": {}, "snap0": {}, "stream": False,
                                "client_msgs": 1, "t0": time.time(),
                                "payload_chars": 10, "n_tools": 0},
                         {"usage": {"input_tokens": 15161, "output_tokens": 7,
                                    "cache_creation_input_tokens": 15159}})
_hit_before = ccproxy.STATS.get("cache_hit_reqs", 0)
ccproxy.record(_rec2)
check("A request with no cache read does NOT count as a hit",
      ccproxy.STATS.get("cache_hit_reqs", 0) == _hit_before,
      f"{ccproxy.STATS.get('cache_hit_reqs')} vs {_hit_before}")
check("...but its cache write still lands in the total",
      ccproxy.STATS["cache_write_tok"] - _before.get("cache_write_tok", 0) == 4881 + 15159)

# The record is fed the CLIENT-facing message, whose usage has had
# cache_creation_input_tokens stripped by _usage_for_client. So the cache fields
# must be read from the raw upstream usage carried on the context -- reading the
# client-facing copy reported "0 cache writes" on a relay that sends that field
# every single time (this bug was live until the live traffic caught it).
_ctx_raw = {"breakdown": {}, "snap0": {}, "stream": False, "client_msgs": 1,
            "t0": time.time(), "payload_chars": 10, "n_tools": 0,
            "raw_usage": {"input_tokens": 15161, "output_tokens": 7,
                          "cache_creation_input_tokens": 4881,
                          "cache_read_input_tokens": 10278}}
_rec3 = ccproxy.make_rec(9003, _ctx_raw,
                         {"usage": ccproxy._usage_for_client(
                             _ctx_raw["raw_usage"], 15161)})
check("Cache numbers are read from the RAW usage, not the stripped client copy",
      _rec3.get("cache_write_tok") == 4881 and _rec3.get("cache_read_tok") == 10278,
      f"write={_rec3.get('cache_write_tok')} read={_rec3.get('cache_read_tok')}")
check("...while in/out tokens still come from the client-facing usage",
      _rec3.get("in_tok") == 15161 and _rec3.get("out_tok") == 7)

with open(os.path.join(HERE, "ccproxy.py"), encoding="utf-8") as _fh:
    _src = _fh.read()
check("Both the streaming and non-streaming paths put raw usage on the context",
      _src.count('ctx["raw_usage"] = upstream.get("usage") or {}') == 2,
      f"found {_src.count('ctx[\"raw_usage\"] = upstream.get(\"usage\") or {}')}")

_snap = ccproxy.stats_snapshot()
check("/stats.json exposes the cache numbers to the dashboard",
      all(k in _snap for k in ("cache_read_tok", "cache_write_tok", "cache_hit_reqs")),
      str([k for k in _snap if k.startswith("cache")]))

with open(os.path.join(HERE, "dashboard.py"), encoding="utf-8") as _fh:
    _dash = _fh.read()
check("The dashboard shows a cache card", 'card("Cache reads"' in _dash)
check("...and marks per-request cache hits in the table",
      "r.cache_read_tok" in _dash)

for _k, _v in _before.items():
    ccproxy.STATS[_k] = _v

# --------------------------------------------------------------------------- #
print("\n" + "=" * 60)
print(f"RESULT:  {len(PASS)} pass, {len(FAIL)} fail")
if FAIL:
    print("Failed:")
    for f in FAIL:
        print("   -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
