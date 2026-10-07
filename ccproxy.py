#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 abdurrehmandaudi
# Required Notice: Copyright (c) 2026 abdurrehmandaudi -- justdowork-proxy
# Licensed under the PolyForm Noncommercial License 1.0.0 -- commercial
# use is not permitted without a separate written commercial license.
# See LICENSE or https://polyformproject.org/licenses/noncommercial/1.0.0
r"""
ccproxy.py -- a complete proxy between Claude Code and the JDW relay (api.justwoker.icu).

This replaces agent_proxy.py. The things that were broken in agent_proxy.py are
fixed here:

  1. DROPPED TOOL CALLS (the real reason multi-agent / Monitor / Write / Edit failed)
     Before: the JSON inside <tool_call> was parsed with a strict `json.loads`.
     The model often writes JSON that contains:
        - literal newlines (Agent.prompt, Write.content, Edit.old_string)
        - invalid escapes (`warn\]`, `Installed\.`, `grep -E "\d+"`)
        - trailing commas or an extra closing bracket
     ...and when that happened the ENTIRE tool call was silently DROPPED (it
     reached the client as plain text) -> "multi-agent doesn't work", "Monitor
     never ran".
     Now: a 4-step repair (strict -> literal control chars -> escape repair ->
     bracket balancing). Whatever JSON the model wrote is recovered.

  2. 524 TIMEOUTS / TOKEN BURN
     Claude Code sends the whole session history with every request. One dump
     was 1.65 MB (~400k tokens) -> the relay answered with a Cloudflare 524 and
     tokens were wasted. Now:
        - history is trimmed to a character budget (max_history_chars)
        - large tool results are truncated (max_tool_result_chars)
        - tool descriptions are shortened (compact mode)
        - a prompt-cache breakpoint is set on the system prefix

  3. WEB SEARCH / WEB FETCH
     Claude Code (with a third-party base URL) cannot execute these tools itself,
     and neither does the relay. So this proxy RUNS THEM ITSELF:
     WebSearch -> live DuckDuckGo search, WebFetch -> page text,
     fetch_image -> the image straight to the model. The model only has to make
     the call; the proxy does the rest. That is why web search now really works.

  4. COUNT_TOKENS / MODELS endpoints, keepalive pings (no more dead air),
     retry with backoff, and salvage of truncated upstream bodies.

How to run:  python3 ccproxy.py       (details: README-RUN.md)
"""

import base64
import concurrent.futures
import copy
import html as _html
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, Response, request, stream_with_context

try:                                    # served at `/` by the dashboard
    from dashboard import DASHBOARD_HTML
except Exception as _dash_err:          # if it breaks, the proxy still runs
    DASHBOARD_HTML = None
    print(f"!! could not load dashboard.py ({_dash_err}) -- serving the plain page.")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "upstream_base_url": "https://api.justwoker.icu",
    "api_key": "",                     # or the UPSTREAM_API_KEY env var
    "model": "claude-opus-4-8",
    "listen_host": "127.0.0.1",
    "listen_port": 8181,
    "upstream_timeout_s": 300,

    # The names the relay understands NATIVELY (confirmed by testing).
    # Every other tool goes through the text protocol (<tool_call>).
    "native_tool_map": {"Read": "read", "Write": "write", "Edit": "edit", "Bash": "bash"},

    "features": {
        # ---- tools ----
        "tool_injection": True,
        "strict_tool_names": True,      # reject any tool name not in the client's list
        "compact_tools": True,          # shorter tool descriptions (saves tokens)
        "tool_desc_chars": 220,         # in compact mode, keep the first N chars

        # ---- token saving (this is where the savings come from) ----
        "max_history_chars": 220000,    # ~55k tokens; 0 = unlimited
        "max_history_messages": 0,      # 0 = off (only the character budget applies)
        "keep_first_user_message": True,
        "keep_recent_messages": 24,     # newest messages always kept, whatever the budget
        "history_user_reserve": 0.35,   # slice of the budget reserved for the human's turns
        # How much of a large tool result reaches the model. This is the knob
        # that decides whether the model can see the file it is editing: at 4000
        # a `Read` of a 20k-char source file arrives as its first 4000 chars, and
        # the model then patches a file whose end it has never seen -- which is
        # what "shallow fix" looks like from the outside. It is cheap to raise:
        # Claude Code itself replaces every *older* tool result with a ~28-char
        # first line, so only the newest one is ever large, and input tokens cost
        # this relay about 1.4s per 6k. 0 = off.
        "max_tool_result_chars": 32000,
        "max_output_tokens": 16000,     # cap on output; 0 = off

        # A cache_control breakpoint on the last system block, which is where
        # Anthropic's own clients put one (it covers the tools array too, since
        # the cache prefix runs tools -> system -> messages).
        #
        # Kept on because it costs nothing, but do not expect it to buy speed.
        # Measured against this relay on 2026-10-07: the cache read it reports
        # is 10278 tokens whether the cacheable prefix is 1,336 chars or 13,569
        # -- a constant, so it is the relay's own hidden prefix and never the
        # conversation's -- and it appears on a random share of requests rather
        # than after the first write. Without cache_control the relay reports
        # cache_creation = input_tokens - 2 on every request, which is
        # "everything was a write" and not a measurement of anything. Across 626
        # logged turns nothing about a cache hit was ever visible in the wall
        # time either. The dashboard's "Cache reads" card shows this live so it
        # does not have to be re-measured.
        "cache_system_prefix": True,

        # ---- behaviour ----
        "working_rules": True,          # tell the model to ask when stuck / when there is a choice
        "strip_thinking": True,         # a proxy-decided budget stays hidden; see _client_wants_thinking
        "enforce_stop_sequences": True,
        "usage_baseline_tokens": 0,     # set to 10380 to hide the relay's ~10.4k phantom tokens

        # ---- extended thinking ----
        # Off would make every turn fast; on costs ~68s per thinking turn at this
        # relay's ~30 tok/s, so it is spent on the one turn that plans and not on
        # the turns that execute the plan.
        "thinking_enabled": True,
        "thinking_adaptive": True,      # think on a fresh user turn, not on tool_result turns
        "thinking_budget_tokens": 2000,
        "thinking_max_budget_tokens": 8000,

        # This relay drops ~26% of requests at random (503/403, no cooldown), so
        # a retry is usually just "ask again right now". Three cheap attempts
        # recover most of it; the backoff lives in call_upstream.
        "upstream_retries": 3,

        # ---- server-side web tools (the proxy runs these itself) ----
        "server_tools_enabled": True,
        "server_tools_max_iters": 3,
        "web_search_max_results": 6,
        "web_fetch_max_chars": 20000,
        "fetch_image_enabled": True,

        # ---- debugging ----
        "dump_requests": False,         # when True, requests are saved into debug_dump/
        "dump_keep_files": 50,
        "log_max_bytes": 2000000,
        "keep_alive_s": 3.0,            # SSE ping interval (prevents dead air)
    },
    "ui_lang": "en",
}


def _deep_merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                _deep_merge(cfg, json.load(f))
        except Exception as e:
            print(f"[config] could not read {CONFIG_PATH}: {e}")
    # env overrides (using the same names as agent_proxy.py so old settings keep working)
    if os.environ.get("UPSTREAM_API_KEY"):
        cfg["api_key"] = os.environ["UPSTREAM_API_KEY"]
    if os.environ.get("TARGET_URL"):
        cfg["upstream_base_url"] = os.environ["TARGET_URL"]
    if os.environ.get("PORT"):
        cfg["listen_port"] = int(os.environ["PORT"])
    # clean the key (strip invisible characters that sneak in when pasting)
    cfg["api_key"] = re.sub(r"[^\x21-\x7e]", "", cfg.get("api_key") or "")
    cfg["upstream_base_url"] = (cfg.get("upstream_base_url") or "").rstrip("/")
    return cfg


CONFIG = load_config()
FEATS = CONFIG["features"]
NAME_MAP = dict(CONFIG.get("native_tool_map") or {})
REV_MAP = {v: k for k, v in NAME_MAP.items()}

app = Flask(__name__)
LOG_LOCK = threading.Lock()
DUMP_LOCK = threading.Lock()
COUNTER = {"n": 0}

# --------------------------------------------------------------------------- #
# Stats -- the dashboard at / is built from these
# --------------------------------------------------------------------------- #
_ST_LOCK = threading.Lock()
_COUNT_LOCK = threading.Lock()

# counters that change while a request is in flight (used to compute per-request deltas)
COUNTS = {"drops": 0, "retries": 0, "upstream_calls": 0,
          "server_tools": {}, "tool_uses": {}}

STATS = {
    "started": time.time(),
    "total_reqs": 0, "ok": 0, "fail": 0, "stream_reqs": 0,
    "in_tok": 0, "out_tok": 0, "raw_in_tok": 0, "phantom_tok": 0,
    "sys_tok": 0, "tools_tok": 0, "hist_tok": 0,
    "raw_hist_tok": 0, "raw_tools_tok": 0, "saved_tok": 0, "sent_tok": 0,
    "client_msgs": 0, "sent_msgs": 0, "duration": 0.0,
    "recent": deque(maxlen=300),
    "errors": deque(maxlen=30),
}


def bump(key, name=None, n=1):
    """Increment a counter. Pass `name` for the per-name buckets (server_tools/tool_uses)."""
    with _COUNT_LOCK:
        if name is None:
            COUNTS[key] = COUNTS.get(key, 0) + n
        else:
            d = COUNTS.setdefault(key, {})
            d[name] = d.get(name, 0) + n


def snap_counts():
    with _COUNT_LOCK:
        return {"drops": COUNTS["drops"], "retries": COUNTS["retries"],
                "upstream_calls": COUNTS["upstream_calls"],
                "server_tools": dict(COUNTS["server_tools"]),
                "tool_uses": dict(COUNTS["tool_uses"])}


def diff_counts(before):
    """The difference between two snapshots -- what one request did."""
    now = snap_counts()
    out = {k: max(0, (now.get(k) or 0) - (before.get(k) or 0))
           for k in ("drops", "retries", "upstream_calls")}
    for k in ("server_tools", "tool_uses"):
        av, bv = before.get(k) or {}, now.get(k) or {}
        out[k] = {nm: bv[nm] - av.get(nm, 0) for nm in bv if bv[nm] - av.get(nm, 0) > 0}
    return out


def make_rec(n, ctx, message=None, ok=True, status=0, note=""):
    """One request's record -- the row shown in the dashboard table."""
    bd = ctx.get("breakdown") or {}
    d = diff_counts(ctx.get("snap0") or {})
    usage = (message or {}).get("usage") or {}
    # The cache numbers come from the RAW upstream usage, not from the usage
    # block the client is handed. _usage_for_client strips
    # cache_creation_input_tokens on purpose, so reading the client-facing copy
    # here would report "0 cache writes" on every request of a relay that sends
    # that field every single time -- which is exactly what it did before this
    # line existed.
    raw_usage = ctx.get("raw_usage") or usage
    base = int(FEATS.get("usage_baseline_tokens", 0) or 0)
    in_tok = int(usage.get("input_tokens", 0) or 0)
    raw_in = in_tok + base if base > 0 else in_tok
    names = list(d["tool_uses"]) + list(d["server_tools"])
    sys_tok = int(bd.get("system_tok", 0) or 0)
    tools_tok = int(bd.get("tools_tok", 0) or 0)
    hist_tok = int(bd.get("history_tok", 0) or 0)
    raw_hist_tok = int(bd.get("raw_history_tok", hist_tok) or 0)
    raw_tools_tok = int(bd.get("raw_tools_tok", tools_tok) or 0)
    saved_tok = max(0, raw_hist_tok - hist_tok) + max(0, raw_tools_tok - tools_tok)
    return {
        "n": n, "ts": datetime.now().strftime("%H:%M:%S"), "t": time.time(),
        "stream": bool(ctx.get("stream")),
        "client_msgs": int(ctx.get("client_msgs", 0)),
        "sent_msgs": int(bd.get("history_msgs", 0) or 0),
        "sys_tok": sys_tok,
        "tools_tok": tools_tok,
        "hist_tok": hist_tok,
        "raw_hist_tok": raw_hist_tok,
        "raw_tools_tok": raw_tools_tok,
        "saved_tok": saved_tok,
        "sent_tok": sys_tok + tools_tok + hist_tok,
        "in_tok": in_tok, "raw_in_tok": raw_in, "out_tok": int(usage.get("output_tokens", 0) or 0),
        "phantom_tok": max(0, raw_in - in_tok),
        # what the relay claimed about caching. Kept so the dashboard can show
        # whether a cache read ever came back -- it is the only way to tell that
        # the breakpoint in build_payload is or is not doing anything.
        "cache_read_tok": int(raw_usage.get("cache_read_input_tokens", 0) or 0),
        "cache_write_tok": int(raw_usage.get("cache_creation_input_tokens", 0) or 0),
        "dur": round(time.time() - float(ctx.get("t0", time.time())), 1),
        "payload_chars": int(ctx.get("payload_chars", 0)),
        "ok": bool(ok), "status": int(status), "tools": int(ctx.get("n_tools", 0)),
        "tool_calls": sum(d["tool_uses"].values()) + sum(d["server_tools"].values()),
        "drops": d["drops"], "retries": d["retries"],
        "names": names, "note": note,
    }


def record(rec):
    with _ST_LOCK:
        STATS["recent"].appendleft(rec)
        STATS["total_reqs"] += 1
        STATS["ok" if rec["ok"] else "fail"] += 1
        if rec["stream"]:
            STATS["stream_reqs"] += 1
        for k in ("in_tok", "out_tok", "raw_in_tok", "phantom_tok", "sys_tok",
                  "tools_tok", "hist_tok", "client_msgs", "sent_msgs",
                  "raw_hist_tok", "raw_tools_tok", "saved_tok", "sent_tok",
                  "cache_read_tok", "cache_write_tok"):
            STATS[k] = STATS.get(k, 0) + int(rec.get(k, 0) or 0)
        # how many requests came back with a cache read at all -- the count
        # matters as much as the token total, because the total is a constant
        # (10278) whenever it appears, from a 1.3k-char prefix or a 13.5k one.
        if rec.get("cache_read_tok"):
            STATS["cache_hit_reqs"] = STATS.get("cache_hit_reqs", 0) + 1
        STATS["duration"] = STATS.get("duration", 0.0) + float(rec.get("dur", 0) or 0)
        if not rec["ok"]:
            STATS["errors"].appendleft({"ts": rec["ts"], "n": rec["n"],
                                        "status": rec["status"], "note": rec["note"]})


def _dir_bytes(path):
    total, files = 0, 0
    try:
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if os.path.isfile(p):
                total += os.path.getsize(p)
                files += 1
    except Exception:
        pass
    return total, files


def stats_snapshot():
    """The full JSON payload behind the dashboard."""
    with _ST_LOCK:
        s = {k: v for k, v in STATS.items() if k not in ("recent", "errors", "started")}
        recent = list(STATS["recent"])
        errors = list(STATS["errors"])
        started = STATS["started"]
    dump_bytes, dump_files = _dir_bytes(os.path.join(HERE, "debug_dump"))
    log_bytes = 0
    for name in ("ccproxy_log.txt", "ccproxy_log.txt.1"):
        try:
            log_bytes += os.path.getsize(os.path.join(HERE, name))
        except Exception:
            pass
    with _COUNT_LOCK:
        server_tools = dict(COUNTS["server_tools"])
        tool_uses = dict(COUNTS["tool_uses"])
        drops, retries = COUNTS["drops"], COUNTS["retries"]
        upstream_calls = COUNTS["upstream_calls"]
    s.update({
        "uptime_s": int(time.time() - started),
        "upstream": CONFIG["upstream_base_url"],
        "model": CONFIG.get("model"),
        "key_set": bool(CONFIG.get("api_key")),
        "usage_baseline_tokens": int(FEATS.get("usage_baseline_tokens", 0) or 0),
        "max_history_chars": int(FEATS.get("max_history_chars", 0) or 0),
        "max_tool_result_chars": int(FEATS.get("max_tool_result_chars", 0) or 0),
        "compact_tools": bool(FEATS.get("compact_tools", True)),
        "dump_requests": bool(FEATS.get("dump_requests", False)),
        "server_tools": server_tools, "tool_uses": tool_uses,
        "drops_total": drops, "retries_total": retries, "upstream_calls": upstream_calls,
        "disk": {"dump_bytes": dump_bytes, "dump_files": dump_files, "log_bytes": log_bytes},
        "recent": recent, "errors": errors,
    })
    return s


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        path = os.path.join(HERE, "ccproxy_log.txt")
        with LOG_LOCK:
            if os.path.exists(path) and os.path.getsize(path) > int(FEATS.get("log_max_bytes", 2_000_000)):
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def head(x, n=260):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:n].replace("\n", "\\n")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# JSON repair -- this is the part that makes multi-agent work again
# --------------------------------------------------------------------------- #

_VALID_ESC = set('"\\/bfnrt')


def _repair_json_text(s):
    r"""Fix the JSON the model wrote without touching valid JSON:
       * invalid escapes (`\]`, `\.`, `\d`) -> make the backslash literal
       * trailing commas (`{"a":1,}`) -> drop them
       * literal control characters are left alone (strict=False handles those)
    """
    out = []
    i, n = 0, len(s)
    in_str = False
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                nxt = s[i + 1] if i + 1 < n else ""
                if nxt in _VALID_ESC:
                    out.append(c)
                    out.append(nxt)
                    i += 2
                elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6] or ""):
                    out.append(s[i:i + 6])
                    i += 6
                else:
                    # invalid escape -> make the backslash literal (`\]` -> `\\]`)
                    out.append("\\\\")
                    i += 1
                continue
            out.append(c)
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == ",":
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in "}]":
                i += 1          # trailing comma drop
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _extract_balanced_json(s):
    """Extract the first balanced {...}. Stray closing brackets are dropped and
    missing closers are appended at the end. Strings and escapes are respected."""
    start = s.find("{")
    if start < 0:
        return None
    pairs = {"}": "{", "]": "["}
    buf, stack = [], []
    in_str = esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            buf.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in "{[":
            stack.append(ch)
            buf.append(ch)
        elif ch in "}]":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
                buf.append(ch)
                if not stack:
                    return "".join(buf)
            else:
                continue        # drop the stray closer
        else:
            buf.append(ch)
    if stack:
        for c in reversed(stack):
            buf.append("}" if c == "{" else "]")
        return "".join(buf)
    return None


def loads_tool_json(raw):
    """The JSON inside a tool call, with the 4-step repair. Returns a dict or None."""
    if not raw or not raw.strip():
        return None
    repaired = _repair_json_text(raw)
    candidates = [raw]
    if repaired != raw:
        candidates.append(repaired)
    for src in (raw, repaired):
        ext = _extract_balanced_json(src)
        if ext and ext not in candidates:
            candidates.append(ext)
    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        try:
            obj = json.loads(cand, strict=False)   # literal newlines/tabs allowed
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------- #
# Tool call parsing (text protocol -> real Anthropic tool_use blocks)
# --------------------------------------------------------------------------- #

CALL_OPEN_RE = re.compile(r'<(?:antml:)?tool_call\s+name\s*=\s*["\']([^"\']+)["\']\s*>')
BARE_OPEN_RE = re.compile(r'<(?:antml:)?tool_call\s*>')
CLOSE_TAG = "</tool_call>"

INVOKE_OPEN_RE = re.compile(r'<(?:antml:)?invoke\s+name\s*=\s*"([^"]+)"\s*>')
PARAM_OPEN_RE = re.compile(r'<(?:antml:)?parameter\s+name\s*=\s*"([^"]+)"\s*>')


def _coerce_param(raw):
    s = raw.strip()
    if s and (s[0] in "[{" or s in ("true", "false", "null")
              or re.fullmatch(r"-?\d+(?:\.\d+)?", s)):
        try:
            return json.loads(s)
        except Exception:
            pass
    return raw


def _parse_invoke_segment(segment):
    """`<invoke name="X"><parameter name="y">v</parameter></invoke>` -> dict."""
    nm = INVOKE_OPEN_RE.search(segment)
    if not nm:
        return None
    name = nm.group(1).strip()
    body = segment[nm.end():]
    close = re.search(r"</(?:antml:)?invoke>", body)
    if close:
        body = body[:close.start()]
    inp = {}
    opens = list(PARAM_OPEN_RE.finditer(body))
    for i, pm in enumerate(opens):
        key = pm.group(1).strip()
        vstart = pm.end()
        vend = opens[i + 1].start() if i + 1 < len(opens) else len(body)
        val = body[vstart:vend]
        val = re.sub(r"</(?:antml:)?parameter>\s*$", "", val)
        inp[key] = _coerce_param(val)
    return {"name": name, "input": inp}


def _tool_input_from(obj, tag_name):
    """Pull the input out of `obj` -- whether it is {"name":..,"input":..} or the input itself."""
    if not isinstance(obj, dict):
        return None
    if "name" in obj and isinstance(obj.get("name"), str):
        inner = obj.get("input", obj.get("arguments", obj.get("parameters")))
        if isinstance(inner, dict):
            return inner
        # it is just {"name": "X"} with no input
        rest = {k: v for k, v in obj.items() if k not in ("name", "input", "arguments", "parameters")}
        return rest
    return obj if tag_name else None


def parse_assistant_text(text, valid_names=None, strict=True, log_drops=True):
    """Split the model's text into Anthropic blocks (text + tool_use).

    Supports: <tool_call name="X">{...}</tool_call> (closing tag optional),
              <tool_call>{...}</tool_call>, native <invoke><parameter> XML,
              and bare JSON {"name":..,"input":..} as a last resort.
    """
    if not text:
        return [{"type": "text", "text": ""}]

    spans = []   # (start, end, name, input)

    def _accept(name, inp, start, end):
        if not name or not isinstance(inp, dict):
            return
        if strict and valid_names is not None and name not in valid_names:
            if log_drops:
                bump("drops")
                log(f"DROP tool_call (not in the client's tool list): {name}")
            return
        spans.append((start, end, name, inp))

    # --- (A) <tool_call name="X"> ... ---
    for m in CALL_OPEN_RE.finditer(text):
        name = m.group(1).strip()
        body_start = m.end()
        close = text.find(CLOSE_TAG, body_start)
        nxt = CALL_OPEN_RE.search(text, body_start)
        nxt2 = BARE_OPEN_RE.search(text, body_start)
        if nxt2 and (nxt is None or nxt2.start() < nxt.start()):
            nxt = nxt2
        if close != -1 and (nxt is None or close < nxt.start()):
            body_end, span_end = close, close + len(CLOSE_TAG)
        elif nxt is not None:
            body_end, span_end = nxt.start(), nxt.start()
        else:
            body_end, span_end = len(text), len(text)
        obj = loads_tool_json(text[body_start:body_end])
        if obj is None:
            log(f"BAD JSON tool_call {name}: {head(text[body_start:body_end], 200)}")
            continue
        _accept(name, _tool_input_from(obj, name), m.start(), span_end)

    # --- (B) <tool_call> {"name":..., "input":...} </tool_call> ---
    if not spans:
        for m in BARE_OPEN_RE.finditer(text):
            if CALL_OPEN_RE.match(text, m.start()):
                continue
            body_start = m.end()
            close = text.find(CLOSE_TAG, body_start)
            nxt = BARE_OPEN_RE.search(text, body_start)
            if close != -1 and (nxt is None or close < nxt.start()):
                body_end, span_end = close, close + len(CLOSE_TAG)
            elif nxt is not None:
                body_end, span_end = nxt.start(), nxt.start()
            else:
                body_end, span_end = len(text), len(text)
            obj = loads_tool_json(text[body_start:body_end])
            if not obj:
                continue
            name = obj.get("name")
            _accept(name, _tool_input_from(obj, name), m.start(), span_end)

    # --- (C) native XML <invoke name="X"><parameter ...> ---
    if not spans and "<invoke" in text:
        opens = list(INVOKE_OPEN_RE.finditer(text))
        for i, om in enumerate(opens):
            seg_end = opens[i + 1].start() if i + 1 < len(opens) else len(text)
            obj = _parse_invoke_segment(text[om.start():seg_end])
            if obj:
                _accept(obj["name"], obj["input"], om.start(), seg_end)

    # --- (D) bare JSON (no tag at all) ---
    if not spans and '"name"' in text:
        for m in re.finditer(r'\{\s*"name"\s*:', text):
            cand = _extract_balanced_json(text[m.start():])
            obj = loads_tool_json(cand) if cand else None
            if not obj:
                continue
            name = obj.get("name")
            inp = _tool_input_from(obj, name)
            if inp is None:
                continue
            if strict and valid_names is not None and name not in valid_names:
                continue
            spans.append((m.start(), m.start() + len(cand), name, inp))
            break

    if not spans:
        return [{"type": "text", "text": text}]

    # spans -> blocks (any text in between becomes its own text block)
    spans.sort(key=lambda s: s[0])
    blocks = []
    pos = 0
    for start, end, name, inp in spans:
        pre = text[pos:start].strip()
        if pre:
            blocks.append({"type": "text", "text": pre})
        blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24],
                       "name": name, "input": inp})
        pos = end
    # any text after the last call (results the model guessed) is deliberately left out
    return blocks or [{"type": "text", "text": text}]


# --------------------------------------------------------------------------- #
# System prompt addendum (compact = token saving)
# --------------------------------------------------------------------------- #

TOOL_OVERRIDE = (
    "## Tools available in this session\n"
    "This endpoint is served through a compatibility layer, which provides tools in\n"
    "two ways: some natively, some relayed as text. Both kinds really run, and both\n"
    "return real results.\n\n"
    "The tools for this session are listed at the end of this message. That list is\n"
    "the session's tool list, and it replaces the endpoint's default one -- so a\n"
    "default tool such as `read_tabular` or `system_todo_write` is not part of this\n"
    "session.\n\n"
    "Use these tools whenever a task needs one.\n"
)

TEXT_PROTOCOL = (
    "\n## Relayed tools\n"
    "These are provided by the compatibility layer instead of natively. To call\n"
    "one, write a block exactly like this in your reply:\n\n"
    '<tool_call name="TOOL_NAME">{"param": "value"}</tool_call>\n\n'
    "The layer runs the tool and returns its real result in the next message as\n"
    '<tool_result name="TOOL_NAME" id="...">...</tool_result>.\n\n'
    "Rules:\n"
    "- The body must be one valid JSON object matching the tool's schema.\n"
    "- Write the block, then stop. Do not invent the result yourself.\n"
    "- You may write normal text before a block, and you may emit several blocks in\n"
    "  one reply -- that is how several agents or searches run in parallel.\n"
    "- Only call a name from the list below.\n"
)


WORKING_RULES = (
    "\n## How this session works\n"
    "The conversation can be shortened to fit the context window, and when it is,\n"
    "a marker is left in the history where the gap is. So if you are not sure what\n"
    "was asked, or a detail you need is not there, say so and ask -- do not guess,\n"
    "and do not quietly start the task again from the beginning.\n\n"
    "Ask when the answer is really the user's to give: an ambiguous request, two\n"
    "reasonable approaches, or anything hard to undo (deleting files, publishing,\n"
    "spending money). Name the options concretely -- \"A: ... or B: ...\" -- then\n"
    "wait. If nothing was asked that needs a decision, just do the work.\n\n"
    "If something is stuck -- the same tool failing twice, a command or file that\n"
    "is not there, a result that looks wrong -- stop and report what you ran and\n"
    "what came back, instead of repeating a call that already failed.\n\n"
    "Report what actually happened: real output, real paths, real errors. If a\n"
    "step was skipped, or you did not verify something, say that too.\n\n"
    "## Look before you change\n"
    "Fix the cause, not the symptom you happened to see first. Before editing,\n"
    "read enough to be sure: the whole of a file you are about to change, how it\n"
    "is called and from where, and what else depends on the thing you touch. A\n"
    "patch that makes one error go away while leaving the reason for it in place\n"
    "is not a fix. Say what you looked at and what you concluded -- if you could\n"
    "not check something, say that, rather than implying you did.\n"
)


def build_context_notice(stats):
    """Told to the model when the proxy had to shorten the conversation, so that
    'I don't have that any more' is an available answer instead of a guess."""
    return (
        "\n## Context notice\n"
        f"{stats.get('dropped', 0)} of the {stats.get('client', 0)} messages in this "
        "conversation were removed on the way here to fit the context window; the rest\n"
        "were kept. A marker sits in the history where the gap is. Work from what you\n"
        "can see, and if the request depends on something you cannot see, ask the user\n"
        "or read the file again rather than assuming it never happened.\n"
    )


def build_native_note(native_pairs):
    """Name the tools the relay already provides natively, so the model calls
    them through the normal tool mechanism instead of the text protocol.
    native_pairs: [(client_name, native_name), ...]"""
    lines = ["\n## Native tools\n",
             "These already exist on this endpoint, under lowercase names:\n"]
    for client, nat in native_pairs:
        lines.append(f"- `{nat}`  -- this is your `{client}` tool")
    lines += [
        "",
        "Call them through the normal tool-calling mechanism, with the same",
        "parameters as the original tool of that name.",
        "Do not put them inside <tool_call> blocks.\n",
    ]
    return "\n".join(lines)


def _compact_fields(schema, depth=1):
    """Turn a schema into a one-line field summary (not the full JSON schema)."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(schema.get("required") or [])
    parts = []
    for pname, pdef in props.items():
        ptype = ""
        if isinstance(pdef, dict):
            ptype = pdef.get("type") or ""
            if ptype == "array" and isinstance(pdef.get("items"), dict):
                items = pdef["items"]
                it = items.get("type") or ""
                if it == "object" and depth > 0:
                    inner = _compact_fields(items, depth - 1)
                    ptype = f"array<{{{inner}}}>" if inner else "array<object>"
                else:
                    ptype = f"array<{it}>" if it else "array"
            elif ptype == "object" and depth > 0:
                inner = _compact_fields(pdef, depth - 1)
                ptype = f"object{{{inner}}}" if inner else "object"
        star = "*" if pname in required else ""
        parts.append(f"{pname}{star}:{ptype}" if ptype else f"{pname}{star}")
    return ", ".join(parts)


def build_tool_block(tools, tool_choice, compact=True, desc_chars=220, native_pairs=None):
    """The system-prompt addendum. `tools` are the text-protocol (extra) tools;
    `native_pairs` are the ones the relay already provides natively."""
    lines = [TOOL_OVERRIDE]
    if native_pairs:
        lines.append(build_native_note(native_pairs))
    if tools:
        lines.append(TEXT_PROTOCOL)
        for t in tools:
            name = t.get("name", "?")
            desc = (t.get("description") or "").strip()
            schema = t.get("input_schema") or {}
            if compact:
                short = desc.split("\n", 1)[0][:desc_chars]
                fields = _compact_fields(schema)
                line = f"- `{name}`"
                if short:
                    line += f": {short}"
                if fields:
                    line += f"\n    params(* = required): {fields}"
                lines.append(line)
            else:
                lines.append(f"\n### `{name}`\n{desc}\n")
                try:
                    lines.append("Input schema:\n```json\n"
                                 + json.dumps(schema, ensure_ascii=False) + "\n```\n")
                except Exception:
                    pass
        names = [t.get("name") for t in tools if t.get("name")]
        if names:
            lines.append("\nValid <tool_call> names (the ONLY ones you may use there): "
                         + ", ".join(f"`{n}`" for n in names))
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "tool" and tool_choice.get("name"):
            lines.append(f"\nThe caller REQUIRES you to call `{tool_choice['name']}` "
                         "now. Reply with exactly one tool_call block for it.")
        elif tool_choice.get("type") == "any":
            lines.append("\nThe caller requires you to call one of the tools now.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# History: trimming + flattening + contract fix
# --------------------------------------------------------------------------- #

def _blocks(content):
    return content if isinstance(content, list) else None


def _flatten_blocks_to_text(content, max_chars=0):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t in ("thinking", "redacted_thinking"):
            continue
        elif t == "tool_use":
            parts.append('<tool_call name="%s">%s</tool_call>'
                         % (b.get("name"), json.dumps(b.get("input", {}), ensure_ascii=False)))
        elif t == "tool_result":
            inner = b.get("content", "")
            if isinstance(inner, list):
                texts = []
                for ib in inner:
                    if isinstance(ib, dict):
                        if ib.get("type") == "text":
                            texts.append(ib.get("text", ""))
                        elif ib.get("type") == "image":
                            texts.append("[image]")
                    else:
                        texts.append(str(ib))
                inner = "\n".join(texts)
            if max_chars and isinstance(inner, str) and len(inner) > max_chars:
                inner = inner[:max_chars] + f"\n...[truncated {len(inner) - max_chars} chars]"
            tag = "TOOL ERROR" if b.get("is_error") else "TOOL RESULT"
            parts.append(f'<tool_result name="" id="{b.get("tool_use_id", "")}">\n{inner}\n</tool_result>')
        elif t == "image":
            parts.append("[image]")
        else:
            try:
                parts.append(json.dumps(b, ensure_ascii=False))
            except Exception:
                pass
    return "\n".join(p for p in parts if p != "")


def collect_emulated_ids(messages):
    """Map the id of every text-protocol tool_use in the history to its name."""
    ids = {}
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and b.get("name") not in NAME_MAP:
                    ids[b.get("id")] = b.get("name")
    return ids


def _cap_blocks(blocks, max_chars):
    """Keep a tool_result's block list inside `max_chars`.

    `max_tool_result_chars` already truncated tool output that arrives as a
    plain string. Output that arrives as a list of blocks was forwarded whole,
    however big -- so the budget was counted against a size the payload never
    actually had (and a huge one could still earn a 524). Text blocks are cut;
    anything else (an image) is passed through, since there is nothing to cut.
    """
    if max_chars <= 0 or not blocks:
        return blocks
    if len(json.dumps(blocks, ensure_ascii=False)) <= max_chars:
        return blocks
    fixed = sum(len(json.dumps(b, ensure_ascii=False)) for b in blocks
                if isinstance(b, dict) and b.get("type") != "text")
    left = max(0, max_chars - fixed)
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            t = b.get("text") or ""
            if len(t) > left:
                cut = len(t) - left
                t = t[:left] + f"\n...[truncated {cut} chars]"
            left = max(0, left - len(t))
            out.append({"type": "text", "text": t})
        else:
            out.append(b)
    return out


def convert_messages(messages, emulated_ids, max_tool_result_chars=0):
    """Native tools (read/write/edit/bash) stay structured; everything else
    (<tool_call>/<tool_result>) becomes text."""
    out = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            out.append({"role": m.get("role"), "content": c})
            continue
        if not isinstance(c, list):
            out.append({"role": m.get("role"), "content": str(c)})
            continue
        results, others, emu = [], [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("thinking", "redacted_thinking"):
                continue
            if t == "tool_use":
                if b.get("name") in NAME_MAP:
                    nb = dict(b)
                    nb["name"] = NAME_MAP[b["name"]]
                    others.append(nb)
                else:
                    others.append({"type": "text",
                                   "text": '<tool_call name="%s">%s</tool_call>' % (
                                       b.get("name"),
                                       json.dumps(b.get("input", {}), ensure_ascii=False))})
            elif t == "tool_result":
                tid = b.get("tool_use_id")
                if tid in emulated_ids:
                    err = ' error="true"' if b.get("is_error") else ""
                    emu.append({"type": "text", "text": _emu_result_text(
                        emulated_ids[tid], tid, b.get("content"), err, max_tool_result_chars)})
                else:
                    nb = dict(b)
                    inner = nb.get("content")
                    if isinstance(inner, list):
                        cleaned = []
                        for ib in inner:
                            if isinstance(ib, dict) and ib.get("type") in ("thinking", "redacted_thinking"):
                                continue
                            if isinstance(ib, dict) and ib.get("type") == "tool_reference":
                                cleaned.append({"type": "text", "text": "[tool ref]"})
                                continue
                            cleaned.append(ib)
                        nb["content"] = _cap_blocks(cleaned, max_tool_result_chars) or [
                            {"type": "text", "text": "(empty)"}]
                    elif isinstance(inner, str) and max_tool_result_chars \
                            and len(inner) > max_tool_result_chars:
                        nb["content"] = inner[:max_tool_result_chars] + \
                            f"\n...[truncated {len(inner) - max_tool_result_chars} chars]"
                    results.append(nb)
            elif t == "image":
                others.append(b)
            elif t == "tool_reference":
                others.append({"type": "text", "text": "[tool ref]"})
            else:
                others.append(b)
        new = results + others + emu
        if not new:
            new = [{"type": "text", "text": "(empty)"}]
        out.append({"role": m.get("role"), "content": new})
    return out


def _emu_result_text(name, tid, content, err, max_chars):
    inner = _flatten_blocks_to_text(content if content is not None else "", max_chars)
    if max_chars and len(inner) > max_chars:
        inner = inner[:max_chars] + f"\n...[truncated {len(inner) - max_chars} chars]"
    return f'<tool_result name="{name}" id="{tid}"{err}>\n{inner}\n</tool_result>'


_TOOL_STUB_OVERHEAD = 80       # the <tool_result ...> wrapper costs something too


def _msg_chars(m, tool_cap=0):
    """Approximate the size of one message, in characters.

    When `tool_cap` is set, a tool payload is measured at the size it will
    really be forwarded at (`max_tool_result_chars`) instead of at its full
    size. Trimming has to measure with the same yardstick as the thing it is
    trimming for: measuring raw sizes made the proxy throw away ~80% of a long
    conversation to stay inside a budget that the survivors never filled.
    """
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    if not isinstance(c, list):
        return 0
    if not tool_cap:
        return len(json.dumps(c, ensure_ascii=False))
    total = 0
    for b in c:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            inner = b.get("content")
            raw = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
            total += min(len(raw), tool_cap) + _TOOL_STUB_OVERHEAD
        else:
            total += len(json.dumps(b, ensure_ascii=False))
    return total


def _newest_is_tool_result(messages):
    """True when the last message is a tool result arriving back from the client.

    That means the model is in the middle of carrying out a plan it already
    made, not deciding what the plan should be -- which is the line this proxy
    draws for spending a thinking budget.
    """
    for m in reversed(messages or []):
        c = m.get("content")
        if isinstance(c, list):
            kinds = [b.get("type") for b in c if isinstance(b, dict)]
            if not kinds:
                continue
            return "tool_result" in kinds
        if isinstance(c, str):
            return False
    return False


def _trim_marker(n):
    """The block that stands where history was cut.

    Without it the model sees the original task, then a recent tail, with no
    sign that anything in between ever existed -- which is how it ends up
    answering a question nobody asked, or repeating the user's own words back.
    """
    return {"role": "user", "content": [{"type": "text", "text": (
        f"[{n} earlier message(s) of this conversation were omitted here to fit the "
        "context window. They held tool output and intermediate steps. The "
        "conversation is incomplete at this point: if you need something from "
        "before, say so and ask, or read the file again.]")}]}


def trim_history(messages, feats, stats=None):
    """Fit the conversation into the relay's budget without the model losing the
    plot.

    The single biggest token saver -- Claude Code sends the whole session (one
    dump was 1.65MB, and the relay answered 524). But the old version kept
    messages[0] and whichever newest messages fitted, and dropped everything in
    between, silently. In the log that reads `client_msgs=857 sent_msgs=163`:
    694 messages gone, on every request of a long session, with nothing in the
    payload to say so. The model then answered the wrong question.

    Now:
      * sizes are measured at the size each message will really be forwarded at,
        so the budget is not eaten by bytes that are about to be truncated;
      * the newest messages are always kept (the live working set), and the
        human's own messages get a reserved slice of the budget -- those are the
        instructions the model must not forget;
      * anything still dropped leaves a marker at the cut, and `stats` reports
        the count so the system prompt can say so too.
    """
    max_chars = int(feats.get("max_history_chars", 0) or 0)
    max_msgs = int(feats.get("max_history_messages", 0) or 0)
    if stats is not None:
        stats["dropped"] = 0
        stats["kept"] = len(messages)
        stats["client"] = len(messages)
    if not messages:
        return messages

    tool_cap = int(feats.get("max_tool_result_chars", 0) or 0)

    keep_head = bool(feats.get("keep_first_user_message", True))
    if keep_head and messages[0].get("role") == "user":
        head, body = [messages[0]], list(messages[1:])
    else:
        head, body = [], list(messages)

    if max_msgs > 0 and len(body) > max_msgs:
        body = body[-max_msgs:]

    dropped = 0
    if max_chars > 0 and body:
        sizes = [_msg_chars(m, tool_cap) for m in body]
        budget = max_chars - sum(_msg_chars(m, tool_cap) for m in head)
        keep = [False] * len(body)
        used = 0

        # 1. the live working set -- the newest messages, always kept, but never
        #    past the budget (the newest one is kept whatever it costs, so there
        #    is always something to answer from).
        recent = int(feats.get("keep_recent_messages", 24) or 0)
        taken, start = 0, len(body)
        for i in range(len(body) - 1, -1, -1):
            if taken >= recent or (taken > 0 and used + sizes[i] > budget):
                break
            keep[i] = True
            used += sizes[i]
            taken += 1
            start = i

        # 2. the human's own messages, out of a reserved slice of the budget.
        #    They are small, and they are exactly what must not be forgotten.
        reserve = int(max(0, budget) * float(feats.get("history_user_reserve", 0.35) or 0))
        user_used = 0
        for i in range(start - 1, -1, -1):
            if body[i].get("role") != "user" or user_used + sizes[i] > reserve:
                continue
            keep[i] = True
            user_used += sizes[i]
            used += sizes[i]         # step 3 must see this budget as spent too

        # 3. whatever budget is left goes to the newest of the rest. Note the
        #    `continue`: one oversized message must not block every older one.
        for i in range(start - 1, -1, -1):
            if keep[i] or used + sizes[i] > budget:
                continue
            keep[i] = True
            used += sizes[i]

        out_body, marker_at = [], None
        for i, m in enumerate(body):
            if keep[i]:
                out_body.append(m)
            else:
                dropped += 1
                if marker_at is None:
                    marker_at = len(out_body)
        if marker_at is not None:
            out_body.insert(marker_at, _trim_marker(dropped))
        body = out_body

    out = head + body
    # the first message must be from the user
    while out and out[0].get("role") != "user":
        out.pop(0)
    # drop orphaned native tool_results (whose tool_use was trimmed away)
    used_ids = set()
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    used_ids.add(b.get("id"))
    cleaned = []
    for m in out:
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c
                 if not (isinstance(b, dict) and b.get("type") == "tool_result"
                         and b.get("tool_use_id") not in used_ids
                         and b.get("tool_use_id") not in (None,))]
            if not c:
                continue
            m = {"role": m.get("role"), "content": c}
        cleaned.append(m)
    if stats is not None:
        stats["dropped"] = dropped
        stats["kept"] = len(cleaned)
    return cleaned


# Blocks that carry real content but have no `text` field. Without these a
# tool_result counts as "empty", the whole message is dropped, and the model
# never sees what its tool returned -- so it calls the same tool again forever.
_CONTENT_BLOCKS = frozenset({
    "tool_use", "tool_result", "image", "document", "search_result",
})


def _is_empty_content(c):
    if c is None:
        return True
    if isinstance(c, str):
        return c.strip() == ""
    if isinstance(c, list):
        if not c:
            return True
        for b in c:
            if isinstance(b, dict):
                if b.get("type") in _CONTENT_BLOCKS:
                    return False
                if (b.get("text") or "").strip():
                    return False
            elif str(b).strip():
                return False
        return True
    return False


def normalize_alternating(messages):
    """The relay's strict contract: no empty messages, first is a user, roles alternate."""
    cleaned = [m for m in messages if not _is_empty_content(m.get("content"))]
    while cleaned and cleaned[0].get("role") != "user":
        cleaned.pop(0)
    merged = []
    for m in cleaned:
        role = m.get("role", "user")
        if merged and merged[-1].get("role") == role:
            a, b = merged[-1].get("content"), m.get("content")
            if isinstance(a, list) or isinstance(b, list):
                la = a if isinstance(a, list) else [{"type": "text", "text": str(a)}]
                lb = b if isinstance(b, list) else [{"type": "text", "text": str(b)}]
                merged[-1]["content"] = la + lb
            else:
                merged[-1]["content"] = (str(a) + "\n\n" + str(b)).strip()
        else:
            merged.append({"role": role, "content": m.get("content")})
    return merged


# --------------------------------------------------------------------------- #
# Server-side web tools (the proxy runs these itself)
# --------------------------------------------------------------------------- #

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Names the proxy intercepts (the client sends these, or we inject them)
SERVER_TOOL_NAMES = {"WebSearch", "WebFetch", "web_search", "web_fetch", "fetch_image"}

SERVER_TOOLS = [
    {
        "name": "WebSearch",
        "description": ("Search the live web and get back result titles, URLs and snippets. "
                        "Use for anything current, or facts you are unsure about."),
        "input_schema": {"type": "object",
                         "properties": {"query": {"type": "string"}},
                         "required": ["query"]},
    },
    {
        "name": "WebFetch",
        "description": ("Open a URL and return its readable text content. Use to read docs, "
                        "API responses, or any page found via WebSearch."),
        "input_schema": {"type": "object",
                         "properties": {"url": {"type": "string"}},
                         "required": ["url"]},
    },
]

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.I)


def _strip_html(s):
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _ddg_search(query, max_results):
    r = requests.post("https://html.duckduckgo.com/html/",
                      data={"q": query, "kl": "wt-wt"},
                      headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"ddg HTTP {r.status_code}")
    body = r.text
    links = re.findall(
        r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.DOTALL)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.DOTALL)
    out = []
    for i, (href, title) in enumerate(links[:max_results]):
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
        if href.startswith("//"):
            href = "https:" + href
        snip = _strip_html(snips[i]) if i < len(snips) else ""
        out.append({"title": _strip_html(title), "url": href, "snippet": snip})
    return out


def _wiki_search(query, max_results):
    r = requests.get("https://en.wikipedia.org/w/api.php",
                     params={"action": "query", "list": "search", "srsearch": query,
                             "format": "json", "srlimit": max_results},
                     headers={"User-Agent": UA}, timeout=20)
    r.raise_for_status()
    hits = (r.json().get("query") or {}).get("search") or []
    return [{"title": h.get("title", ""),
             "url": "https://en.wikipedia.org/wiki/" + h.get("title", "").replace(" ", "_"),
             "snippet": _strip_html(h.get("snippet", ""))} for h in hits]


def run_web_search(query, feats):
    n = int(feats.get("web_search_max_results", 6) or 6)
    errs = []
    for fn in (_ddg_search, _wiki_search):
        try:
            res = fn(query, n)
            if res:
                lines = [f"Web search results for: {query}", ""]
                for i, it in enumerate(res, 1):
                    lines.append(f"{i}. {it['title']}\n   {it['url']}\n   {it['snippet']}")
                return "\n".join(lines)
        except Exception as e:
            errs.append(f"{fn.__name__}: {e}")
    return f"[web_search failed for {query!r}: {'; '.join(errs) or 'no results'}]"


def run_web_fetch(url, feats):
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "*/*"},
                         timeout=30, allow_redirects=True)
    except Exception as e:
        return f"[web_fetch error for {url}: {e}]"
    if r.status_code >= 400:
        return f"[web_fetch HTTP {r.status_code} for {url}]"
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if "html" in ctype:
        text = _strip_html(r.text)
    else:
        text = r.text
    cap = int(feats.get("web_fetch_max_chars", 20000) or 20000)
    if len(text) > cap:
        text = text[:cap] + f"\n...[truncated {len(text) - cap} chars]"
    return f"Content of {url}:\n\n{text}"


def run_fetch_image(url):
    try:
        pr = urlparse(url)
        headers = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"}
        if pr.scheme and pr.netloc:
            headers["Referer"] = f"{pr.scheme}://{pr.netloc}/"
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    except Exception as e:
        return {"text": f"[fetch_image error: {e}]"}
    if r.status_code >= 400:
        return {"text": f"[fetch_image HTTP {r.status_code} for {url}]"}
    ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        low = url.lower()
        for ext, ct in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                        (".png", "image/png"), (".gif", "image/gif"), (".webp", "image/webp")):
            if low.endswith(ext):
                ctype = ct
                break
        else:
            return {"text": f"[fetch_image: unsupported content-type {ctype!r}]"}
    b64 = base64.b64encode(r.content).decode("ascii")
    return {"blocks": [
        {"type": "text", "text": f"Fetched image from {url}:"},
        {"type": "image", "source": {"type": "base64", "media_type": ctype, "data": b64}},
    ]}


def execute_server_tool(name, tool_input, feats):
    if name in ("WebSearch", "web_search"):
        return {"text": run_web_search((tool_input.get("query") or "").strip(), feats)}
    if name in ("WebFetch", "web_fetch"):
        url = (tool_input.get("url") or "").strip()
        if not url:
            return {"text": "[web_fetch: empty url]"}
        return {"text": run_web_fetch(url, feats)}
    if name == "fetch_image":
        url = (tool_input.get("url") or "").strip()
        if not url:
            return {"text": "[fetch_image: empty url]"}
        return run_fetch_image(url)
    return {"text": f"[unknown server tool: {name}]"}


# --------------------------------------------------------------------------- #
# Upstream call
# --------------------------------------------------------------------------- #

def call_upstream(payload, api_key=None, version="2023-06-01"):
    url = CONFIG["upstream_base_url"] + "/v1/messages"
    key = api_key or CONFIG.get("api_key") or ""
    headers = {"content-type": "application/json", "x-api-key": key,
               "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"}
    attempts = max(1, int(FEATS.get("upstream_retries", 3) or 1))
    # This relay rejects a fixed share of requests at random -- measured ~26%
    # (349x 503 + 28x 403 out of 1320 attempts in a real log), landing on
    # request 3, 7, 11, ... regardless of headers, model or payload. It is NOT a
    # cooldown: after a 403 a retry sent at 0.0s delay returned 200. So the
    # useful retry is an immediate one. The old fixed [2.0, 5.0, 10.0] slept 2s
    # before even trying, which on a 26%-rejection relay is 2s wasted on a
    # quarter of all requests, and ~7-16 minutes of dead sleep across the log.
    backoff = [0.0, 0.25, 0.75, 2.0]
    retryable = {403, 408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522,
                 523, 524, 529}
    timeout = float(CONFIG.get("upstream_timeout_s", 300))
    status, data = 0, {}
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            bump("retries")
            time.sleep(backoff[min(attempt - 2, len(backoff) - 1)])
        bump("upstream_calls")
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except Exception as e:
            log(f"upstream attempt {attempt} EXCEPTION: {e}")
            status, data = 0, {"error": {"type": "api_error", "message": str(e)}}
            continue
        status = r.status_code
        raw = r.text or ""
        if not raw.strip():
            data = {"_raw": "", "error": {"type": "api_error",
                                          "message": "upstream returned an empty body"}}
            log(f"upstream attempt {attempt} -> {status} (empty body)")
            # a genuinely empty 200 is worth another try; so is an empty body on
            # a status we would retry anyway. An empty 401/404 is not -- retrying
            # it just burns the payload twice more.
            if attempt < attempts and (status < 400 or status in retryable):
                continue
            return status, data
        try:
            data = r.json()
        except Exception:
            data = {"_raw": raw, "error": {"type": "api_error", "message": raw[:500]}}
            log(f"upstream attempt {attempt} -> {status} (JSON parse fail)")
            if attempt < attempts:
                continue
            return status, data
        log(f"upstream attempt {attempt} -> {status}")
        if status < 400:
            return status, data
        if status not in retryable:
            return status, data
    return status, data


def _raw_text(upstream):
    segs = []
    for b in (upstream.get("content") or []):
        if isinstance(b, dict) and b.get("type") == "text":
            segs.append(b.get("text", ""))
    return "\n\n".join(s for s in segs if s)


def _salvage_truncated(upstream):
    """The relay sometimes cuts the JSON off mid-stream. Salvage whatever text was written."""
    raw = upstream.get("_raw") if isinstance(upstream, dict) else None
    if not isinstance(raw, str) or '"text"' not in raw:
        return ""
    parts = []
    i = 0
    marker = '"text":'
    while True:
        j = raw.find(marker, i)
        if j < 0:
            break
        k = raw.find('"', j + len(marker))
        if k < 0:
            break
        buf, p, n, closed = [], k + 1, len(raw), False
        while p < n:
            c = raw[p]
            if c == "\\" and p + 1 < n:
                buf.append(raw[p:p + 2])
                p += 2
                continue
            if c == '"':
                closed = True
                break
            buf.append(c)
            p += 1
        frag = "".join(buf)
        try:
            frag = json.loads('"' + frag + '"')
        except Exception:
            try:
                frag = json.loads('"' + frag.rstrip("\\") + '"')
            except Exception:
                pass
        if frag:
            parts.append(frag)
        i = p + 1 if closed else n
    return "".join(parts).strip()


def resolve_server_tools(payload, feats, api_key=None, version="2023-06-01"):
    """Call upstream; if the model asked for a server-side web tool, run it here,
    feed the result back, and loop."""
    status, upstream = call_upstream(payload, api_key=api_key, version=version)
    if not feats.get("server_tools_enabled", True):
        return status, upstream
    if status >= 400 or not isinstance(upstream, dict):
        return status, upstream

    max_iters = int(feats.get("server_tools_max_iters", 3) or 3)
    for it in range(max_iters):
        text = _raw_text(upstream)
        # We only care whether a server-side web tool was requested here.
        # Logging a "DROP" for every other tool (Agent/Bash/...) would be noise.
        blocks = parse_assistant_text(text, valid_names=SERVER_TOOL_NAMES,
                                      strict=True, log_drops=False)
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        if not calls:
            break

        def _run(call):
            name = call["name"]
            bump("server_tools", name)
            log(f"SERVER TOOL  : {name} {head(call.get('input'), 200)}")
            try:
                return execute_server_tool(name, call.get("input") or {}, feats)
            except Exception as e:
                return {"text": f"[{name} failed: {e}]"}

        # One reply can ask for several web tools at once. Run them together:
        # each one is a whole extra round-trip if it waits its turn, and this
        # relay serves concurrent requests fine (4 at once measured at 5.2s wall
        # against ~12.7s one after another). Only the results are ordered, so
        # the model still sees them against the calls it made.
        if len(calls) == 1:
            results = [_run(calls[0])]
        else:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(4, len(calls))) as ex:
                results = list(ex.map(_run, calls))

        payload["messages"].append({"role": "assistant", "content": text})
        payload["messages"].append(
            {"role": "user",
             "content": "\n\n".join(r.get("blocks") or r.get("text") or "(empty result)"
                                    for r in results)})
        status, upstream = call_upstream(payload, api_key=api_key, version=version)
        if status >= 400 or not isinstance(upstream, dict):
            return status, upstream
    else:
        payload["messages"].append(
            {"role": "user",
             "content": "The tool limit is reached. Answer with what you already "
                        "know; do not call any tool."})
        status, upstream = call_upstream(payload, api_key=api_key, version=version)
    return status, upstream


def make_fallback_payload(payload):
    """If the relay answers 400 (say it dislikes a system list or cache_control),
    retry with a lighter payload: system as a plain string, no cache_control or
    top_k, and roles normalised again."""
    p = copy.deepcopy(payload)
    sysv = p.get("system")
    if isinstance(sysv, list):
        p["system"] = "\n\n".join(b.get("text", "") for b in sysv if isinstance(b, dict))
    p.pop("top_k", None)
    if isinstance(p.get("messages"), list):
        p["messages"] = normalize_alternating(p["messages"])
        if not p["messages"]:
            p["messages"] = [{"role": "user", "content": "(empty)"}]
    return p


def resolve_with_fallback(payload, feats, api_key=None, version="2023-06-01"):
    status, upstream = resolve_server_tools(payload, feats, api_key=api_key, version=version)
    if status in (400, 422):
        log(f"upstream returned {status} -> retrying with a slim payload")
        status, upstream = resolve_server_tools(make_fallback_payload(payload), feats, api_key=api_key, version=version)
    return status, upstream


# --------------------------------------------------------------------------- #
# Building the payload
# --------------------------------------------------------------------------- #

def build_payload(client_req, feats, log_breakdown=True):
    system_blocks = client_req.get("system")
    if isinstance(system_blocks, str):
        sys_text = system_blocks
    elif isinstance(system_blocks, list):
        sys_text = "\n\n".join(b.get("text", "") for b in system_blocks
                               if isinstance(b, dict) and b.get("type") == "text")
    else:
        sys_text = ""

    tools = [t for t in (client_req.get("tools") or []) if isinstance(t, dict)]
    tool_choice = client_req.get("tool_choice")

    forcing = (isinstance(tool_choice, dict) and tool_choice.get("type") == "tool"
               and bool(tool_choice.get("name")))
    inject = list(tools)
    if forcing:
        only = [t for t in tools if t.get("name") == tool_choice["name"]]
        if only:
            inject = only

    if feats.get("server_tools_enabled", True) and not forcing:
        have = {t.get("name") for t in inject}
        for st in SERVER_TOOLS:
            if st["name"] not in have:
                inject.append(st)
        if feats.get("fetch_image_enabled", True) and "fetch_image" not in have:
            inject.append({
                "name": "fetch_image",
                "description": "Fetch an image by URL so you can SEE it directly.",
                "input_schema": {"type": "object",
                                 "properties": {"url": {"type": "string"}},
                                 "required": ["url"]},
            })

    # The relay only honours tools literally named read/write/edit/bash. Those go
    # in the `tools` array under their lowercase names -- that is what makes
    # Read/Write/Edit/Bash work. Every other tool is described in the system
    # prompt and called through the <tool_call> text protocol.
    native_payload, native_pairs, extra = [], [], []
    for t in inject:
        client_name = t.get("name")
        nat = NAME_MAP.get(client_name)
        if nat and isinstance(t.get("input_schema"), dict):
            native_payload.append({
                "name": nat,
                "description": (t.get("description") or f"The {client_name} tool."),
                "input_schema": t["input_schema"],
            })
            native_pairs.append((client_name, nat))
        else:
            extra.append(t)

    addendum = ""
    if feats.get("tool_injection", True) and (native_pairs or extra):
        addendum = build_tool_block(extra, tool_choice,
                                    compact=feats.get("compact_tools", True),
                                    desc_chars=int(feats.get("tool_desc_chars", 220)),
                                    native_pairs=native_pairs)

    # ---- messages ----
    raw_msgs = client_req.get("messages", []) or []
    raw_hist_chars = sum(_msg_chars(m) for m in raw_msgs)
    trim_stats = {}
    msgs = trim_history(raw_msgs, feats, trim_stats)
    emulated_ids = collect_emulated_ids(msgs)
    msgs = convert_messages(msgs, emulated_ids,
                            int(feats.get("max_tool_result_chars", 0) or 0))
    msgs = normalize_alternating(msgs)

    max_tok = int(client_req.get("max_tokens") or 8192)
    cap = int(feats.get("max_output_tokens", 0) or 0)
    up_max = max(4096, max_tok)
    if cap > 0:
        up_max = min(up_max, cap)

    payload = {"model": client_req.get("model") or CONFIG["model"],
               "max_tokens": up_max, "messages": msgs, "stream": False}

    if native_payload:
        payload["tools"] = native_payload
        if forcing:
            mapped = NAME_MAP.get(tool_choice.get("name"))
            if mapped:
                payload["tool_choice"] = {"type": "tool", "name": mapped}

    # The system prompt carries three things the relay would otherwise never
    # learn: which tools exist, how to behave in this session, and whether the
    # conversation was shortened on the way here.
    system_extra = addendum
    if feats.get("working_rules", True):
        system_extra += WORKING_RULES
    if trim_stats.get("dropped"):
        system_extra += build_context_notice(trim_stats)

    if system_extra:
        # The addendum goes FIRST. Appending it after Claude Code's own ~6k-char
        # system prompt buries it -- the model then sees only the native `tools`
        # array and reports that Agent/WebSearch do not exist.
        payload["system"] = system_extra + "\n\n" + sys_text if sys_text else system_extra
    elif sys_text:
        payload["system"] = sys_text

    for k in ("temperature", "top_p", "top_k"):
        if client_req.get(k) is not None:
            payload[k] = client_req[k]

    # ---- extended thinking ----
    # The relay does support it: `{"thinking":{"type":"enabled","budget_tokens":N}}`
    # returns a real signed thinking block. But thinking tokens are output
    # tokens, and this relay generates at ~30 tok/s -- so a 2000-token budget
    # adds ~68s to the turn that uses it. That makes always-on thinking the
    # wrong trade. What is worth the cost is the one turn that decides *what to
    # do*: the model's own reply to a fresh user message. The turns that only
    # carry out that plan (each one starts with a tool_result) get no budget and
    # stay fast, which is also how the work actually divides -- plan deeply once,
    # then execute mechanically.
    if feats.get("thinking_enabled", True):
        want, budget = False, 0
        th = client_req.get("thinking")
        if isinstance(th, dict) and th.get("type") in ("enabled", "adaptive", "auto"):
            want = True
            budget = int(th.get("budget_tokens") or 0)
        elif isinstance(th, dict) and th.get("type") == "disabled":
            pass                      # the client said no; that outranks our guess
        elif feats.get("thinking_adaptive", True):
            want = not _newest_is_tool_result(raw_msgs)
            budget = int(feats.get("thinking_budget_tokens", 2000) or 0)
        if want and budget > 0:
            cap = int(feats.get("thinking_max_budget_tokens", 8000) or 0)
            payload["thinking"] = {"type": "enabled",
                                   "budget_tokens": min(budget, cap) if cap else budget}
            # the two share one output allowance on a real endpoint; make sure
            # the thinking budget cannot eat the whole reply
            payload["max_tokens"] = max(int(payload.get("max_tokens") or 0),
                                        (min(budget, cap) if cap else budget) + 2048)

    # prompt cache: the system prefix is stable, so put the breakpoint there
    if feats.get("cache_system_prefix", True) and payload.get("system"):
        payload["system"] = [{"type": "text", "text": payload["system"],
                              "cache_control": {"type": "ephemeral"}}]

    if log_breakdown:
        sys_chars = len(payload.get("system", [{}])[0].get("text", "") if isinstance(
            payload.get("system"), list) else (payload.get("system") or ""))
        hist_chars = sum(_msg_chars(m) for m in msgs)
        # native schemas travel in the payload's `tools` array, not in the text
        native_chars = sum(len(json.dumps(t, ensure_ascii=False)) for t in native_payload)
        tool_chars = len(addendum) + native_chars
        # what the full, uncompacted tool descriptions would have cost
        try:
            raw_tool_chars = native_chars + (
                len(build_tool_block(extra, tool_choice, compact=False,
                                     native_pairs=native_pairs))
                if (native_pairs or extra) else 0)
        except Exception:
            raw_tool_chars = tool_chars
        payload["_breakdown"] = {
            "system_tok": max(0, (sys_chars - tool_chars) // 4),
            "tools_tok": tool_chars // 4,
            "history_tok": hist_chars // 4,
            "history_msgs": len(msgs),
            "client_msgs": len(raw_msgs),
            "dropped_msgs": trim_stats.get("dropped", 0),
            "raw_history_tok": raw_hist_chars // 4,
            "raw_tools_tok": raw_tool_chars // 4,
        }
    return payload


# --------------------------------------------------------------------------- #
# Response processing
# --------------------------------------------------------------------------- #

def apply_stop_sequences(text, stops):
    if not stops:
        return text, None
    best, matched = None, None
    for s in stops:
        if not s:
            continue
        i = text.find(s)
        if i != -1 and (best is None or i < best):
            best, matched = i, s
    if best is not None:
        return text[:best], matched
    return text, None


def _client_wants_thinking(client_req, feats):
    """Whether a thinking block from upstream should be handed back to the client.

    A budget the *proxy* decided on is ours, not the client's: Claude Code did
    not ask for thinking, so it has no reason to be handed a signed thinking
    block it must then store and replay. The reasoning still did its job -- it
    changed what the model went on to do.

    A budget the *client* asked for is the opposite case: Claude Code turns
    extended thinking on itself, and then it does expect the blocks back.
    """
    if not feats.get("strip_thinking", True):
        return True
    th = (client_req or {}).get("thinking")
    return isinstance(th, dict) and th.get("type") in ("enabled", "adaptive", "auto")


def process_upstream(upstream, client_req, feats, valid_names):
    text_segs, native = [], []
    for b in (upstream.get("content") or []):
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text_segs.append(b.get("text", ""))
        elif t == "thinking":
            if _client_wants_thinking(client_req, feats):
                native.append(b)
        elif t == "tool_use":
            native.append(b)

    text = "\n\n".join(s for s in text_segs if s != "")
    stop_val = None
    if feats.get("enforce_stop_sequences", True):
        text, stop_val = apply_stop_sequences(text, client_req.get("stop_sequences"))

    strict = bool(feats.get("strict_tool_names", True)) and bool(valid_names)
    if client_req.get("tools"):
        parsed = parse_assistant_text(text, valid_names=valid_names, strict=strict)
    else:
        parsed = [{"type": "text", "text": text}] if text else []

    kept_native = []
    for b in native:
        if b.get("type") == "tool_use":
            if b.get("name") in REV_MAP:
                nb = dict(b)
                nb["name"] = REV_MAP[b["name"]]
                kept_native.append(nb)
            else:
                log(f"DROP relay-native tool: {b.get('name')}")
                continue
        else:
            kept_native.append(b)

    content = kept_native + parsed
    if not content:
        content = [{"type": "text", "text": ""}]
    for b in content:
        if b.get("type") == "tool_use" and b.get("name"):
            bump("tool_uses", b["name"])

    has_tool = any(b.get("type") == "tool_use" for b in content)
    stop_reason = "tool_use" if has_tool else "end_turn"
    if stop_val is not None:
        stop_reason = "stop_sequence"

    usage = upstream.get("usage") or {}
    base = int(feats.get("usage_baseline_tokens", 0) or 0)
    in_tok = int(usage.get("input_tokens", 0) or 0)
    if base > 0 and in_tok >= base:
        in_tok -= base

    return {
        "id": upstream.get("id") or ("msg_" + uuid.uuid4().hex[:16]),
        "type": "message",
        "role": "assistant",
        "model": client_req.get("model") or CONFIG["model"],
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_val,
        "usage": _usage_for_client(usage, in_tok),
    }, has_tool


def _usage_for_client(usage, in_tok):
    """The usage block Claude Code is handed.

    `cache_read_input_tokens` is passed through when the relay reports one --
    it is the only cache signal this relay gives that is worth anything.

    `cache_creation_input_tokens` is deliberately NOT passed through. Measured
    across eight requests: when the payload carries no cache_control the relay
    reports `cache_creation_input_tokens = input_tokens - 2`, exactly, every
    time -- i.e. "all of it was a cache write". That is a constant, not a
    measurement. Forwarding it would tell Claude Code a cache is being filled
    on every request.

    (The read is not much better: see `note_cache` below.)
    """
    out = {"input_tokens": max(0, in_tok),
           "output_tokens": int(usage.get("output_tokens", 0) or 0)}
    rd = usage.get("cache_read_input_tokens")
    if rd:
        out["cache_read_input_tokens"] = int(rd)
    return out


def synthesize_sse(message, emit_start=True):
    usage = message.get("usage") or {}
    if emit_start:
        yield sse("message_start", {"type": "message_start", "message": {
            "id": message.get("id"), "type": "message", "role": "assistant",
            "model": message.get("model"), "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0}}})
        yield sse("ping", {"type": "ping"})
    for i, b in enumerate(message.get("content") or []):
        t = b.get("type")
        if t == "text":
            yield sse("content_block_start", {"type": "content_block_start", "index": i,
                      "content_block": {"type": "text", "text": ""}})
            txt = b.get("text", "")
            for j in range(0, len(txt), 800):
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "text_delta", "text": txt[j:j + 800]}})
        elif t == "tool_use":
            yield sse("content_block_start", {"type": "content_block_start", "index": i,
                      "content_block": {"type": "tool_use", "id": b.get("id"),
                                        "name": b.get("name"), "input": {}}})
            yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                      "delta": {"type": "input_json_delta",
                                "partial_json": json.dumps(b.get("input", {}),
                                                           ensure_ascii=False)}})
        elif t == "thinking":
            yield sse("content_block_start", {"type": "content_block_start", "index": i,
                      "content_block": {"type": "thinking", "thinking": ""}})
            th = b.get("thinking", "")
            if th:
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "thinking_delta", "thinking": th}})
            if b.get("signature"):
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "signature_delta", "signature": b["signature"]}})
        else:
            continue
        yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
    yield sse("message_delta", {"type": "message_delta",
              "delta": {"stop_reason": message.get("stop_reason"),
                        "stop_sequence": message.get("stop_sequence")},
              "usage": {"output_tokens": usage.get("output_tokens", 0)}})
    yield sse("message_stop", {"type": "message_stop"})


def dump_request(n, obj):
    if not FEATS.get("dump_requests", False):
        return
    try:
        d = os.path.join(HERE, "debug_dump")
        os.makedirs(d, exist_ok=True)
        with DUMP_LOCK:
            with open(os.path.join(d, f"req_{n}.json"), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)
            keep = int(FEATS.get("dump_keep_files", 50) or 50)
            files = sorted((os.path.join(d, x) for x in os.listdir(d) if x.endswith(".json")),
                           key=os.path.getmtime, reverse=True)
            for old in files[keep:]:
                try:
                    os.remove(old)
                except Exception:
                    pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def get_request_api_key():
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
        if auth:
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
            else:
                key = auth.strip()
    # If client passed placeholder "dummy" but proxy has a configured key, fallback to configured
    if (not key or key == "dummy") and CONFIG.get("api_key"):
        key = CONFIG.get("api_key")
    return re.sub(r"[^\x21-\x7e]", "", key or "")


@app.route("/v1/messages", methods=["POST"])
def v1_messages():
    COUNTER["n"] += 1
    n = COUNTER["n"]
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or "messages" not in body:
        return Response(json.dumps({"type": "error", "error": {
            "type": "invalid_request_error", "message": "the messages field is required"}}),
            status=400, content_type="application/json")

    api_key = get_request_api_key()
    if not api_key:
        return Response(json.dumps({"type": "error", "error": {
            "type": "authentication_error",
            "message": "No API key found. Pass ANTHROPIC_API_KEY from Claude Code (x-api-key header) or set UPSTREAM_API_KEY in proxy config."}}),
            status=401, content_type="application/json")

    wants_stream = bool(body.get("stream"))
    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    valid_names = {t.get("name") for t in tools if t.get("name")}

    dump_request(n, body)
    payload = build_payload(body, FEATS)
    breakdown = payload.pop("_breakdown", {})
    log(f"===== REQ #{n} | client_msgs={len(body.get('messages', []))} "
        f"sent_msgs={breakdown.get('history_msgs')} "
        f"dropped={breakdown.get('dropped_msgs')} | tools={len(tools)} | "
        f"~tok sys={breakdown.get('system_tok')} tools={breakdown.get('tools_tok')} "
        f"hist={breakdown.get('history_tok')} | stream={wants_stream} =====")

    t0 = time.time()
    ctx = {"breakdown": breakdown, "t0": t0, "stream": wants_stream,
           "client_msgs": len(body.get("messages") or []), "n_tools": len(tools),
           "snap0": snap_counts(),
           "payload_chars": len(json.dumps(payload, ensure_ascii=False))}

    if wants_stream:
        return Response(stream_with_context(
            _stream(payload, body, valid_names, breakdown, t0, n, ctx, api_key=api_key)),
            content_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    try:
        status, upstream = resolve_with_fallback(payload, FEATS, api_key=api_key)
    except Exception as e:
        log(f"REQ #{n} upstream EXCEPTION: {e}")
        record(make_rec(n, ctx, ok=False, status=502, note=f"exception: {e}"))
        return Response(json.dumps({"type": "error", "error": {
            "type": "api_error", "message": f"upstream connection failed: {e}"}}),
            status=502, content_type="application/json")

    if status >= 400 and isinstance(upstream, dict) and upstream.get("content"):
        log(f"REQ #{n}: got error status {status} but usable content -> treating as success")
        status = 200
    if status >= 400:
        salv = _salvage_truncated(upstream)
        if salv:
            log(f"REQ #{n}: salvaged a truncated body ({len(salv)} chars)")
            record(make_rec(n, ctx, ok=True, status=status, note="truncated body salvage"))
            return Response(json.dumps({
                "id": "msg_" + uuid.uuid4().hex[:16], "type": "message",
                "role": "assistant", "model": payload.get("model"),
                "content": [{"type": "text", "text": salv}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}),
                content_type="application/json")
        msg = ""
        if isinstance(upstream, dict):
            msg = ((upstream.get("error") or {}).get("message")
                   or upstream.get("message") or upstream.get("_raw")
                   or json.dumps(upstream, ensure_ascii=False)[:400])
        log(f"REQ #{n} FAIL {status}: {head(msg, 200)}")
        record(make_rec(n, ctx, ok=False, status=status, note=head(msg, 120)))
        return Response(json.dumps({"type": "error", "error": {
            "type": "api_error" if status != 400 else "invalid_request_error",
            "message": f"Upstream {status}: {msg}"}}),
            status=status, content_type="application/json")

    message, has_tool = process_upstream(upstream, body, FEATS, valid_names)
    ctx["raw_usage"] = upstream.get("usage") or {}
    log(f"REQ #{n} OK in {round(time.time() - t0, 1)}s | tool_use={has_tool} | "
        f"stop={message['stop_reason']} | usage={message['usage']} | "
        f"cache read={c_read} cache write={c_write}")
    if not has_tool and valid_names:
        _said = " ".join(b.get("text", "") for b in (message.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text")
        if _said.strip():
            log(f"REQ #{n} called NO tool; the model said: {head(_said, 260)}")
    record(make_rec(n, ctx, message=message, ok=True, status=status))
    return Response(json.dumps(message), content_type="application/json")


def _stream(payload, client_req, valid_names, breakdown, t0, n, ctx, api_key=None):
    """Streaming: send message_start + a ping immediately, then run the upstream
    call in a background thread. That way Claude Code never sees dead air."""
    provisional = "msg_" + uuid.uuid4().hex[:16]
    yield sse("message_start", {"type": "message_start", "message": {
        "id": provisional, "type": "message", "role": "assistant",
        "model": payload.get("model"), "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    yield sse("ping", {"type": "ping"})

    box = {}

    def worker():
        try:
            box["v"] = resolve_with_fallback(payload, FEATS, api_key=api_key)
        except Exception as e:
            box["e"] = e

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    keepalive = float(FEATS.get("keep_alive_s", 3.0) or 3.0)
    while th.is_alive():
        th.join(timeout=keepalive)
        if th.is_alive():
            yield sse("ping", {"type": "ping"})

    if "e" in box:
        log(f"stream upstream EXCEPTION: {box['e']}")
        record(make_rec(n, ctx, ok=False, status=502, note=f"exception: {box['e']}"))
        yield sse("error", {"type": "error", "error": {
            "type": "api_error", "message": f"upstream connection failed: {box['e']}"}})
        return

    status, upstream = box.get("v", (0, {}))
    if status >= 400 and isinstance(upstream, dict) and upstream.get("content"):
        status = 200
    if status >= 400:
        salv = _salvage_truncated(upstream)
        if salv:
            msg = {"id": provisional, "type": "message", "role": "assistant",
                   "model": payload.get("model"),
                   "content": [{"type": "text", "text": salv}],
                   "stop_reason": "end_turn", "stop_sequence": None,
                   "usage": {"input_tokens": 0, "output_tokens": 0}}
            record(make_rec(n, ctx, ok=True, status=status, note="truncated body salvage"))
            for ev in synthesize_sse(msg, emit_start=False):
                yield ev
            return
        txt = ""
        if isinstance(upstream, dict):
            txt = ((upstream.get("error") or {}).get("message")
                   or upstream.get("_raw") or json.dumps(upstream, ensure_ascii=False)[:300])
        log(f"stream FAIL {status}: {head(txt, 180)}")
        record(make_rec(n, ctx, ok=False, status=status, note=head(txt, 120)))
        yield sse("error", {"type": "error", "error": {
            "type": "api_error", "message": f"Upstream {status}: {txt}"}})
        return

    message, has_tool = process_upstream(upstream, client_req, FEATS, valid_names)
    ctx["raw_usage"] = upstream.get("usage") or {}
    log(f"stream OK in {round(time.time() - t0, 1)}s | tool_use={has_tool} | "
        f"stop={message['stop_reason']} | usage={message['usage']} | "
        f"cache read={c_read} cache write={c_write}")
    if not has_tool and valid_names:
        _said = " ".join(b.get("text", "") for b in (message.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text")
        if _said.strip():
            log(f"stream called NO tool; the model said: {head(_said, 260)}")
    record(make_rec(n, ctx, message=message, ok=True, status=status))
    message["id"] = message.get("id") or provisional
    for ev in synthesize_sse(message, emit_start=False):
        yield ev


@app.route("/v1/messages/count_tokens", methods=["POST"])
def count_tokens():
    body = request.get_json(silent=True) or {}
    total = 0
    sysv = body.get("system")
    if isinstance(sysv, str):
        total += len(sysv) // 4
    elif isinstance(sysv, list):
        total += sum(len(b.get("text", "")) for b in sysv if isinstance(b, dict)) // 4
    for m in body.get("messages") or []:
        total += len(_flatten_blocks_to_text(m.get("content"), 0)) // 4
    for t in body.get("tools") or []:
        total += len(json.dumps(t, ensure_ascii=False)) // 4
    return Response(json.dumps({"input_tokens": max(1, total)}),
                    content_type="application/json")


@app.route("/v1/models", methods=["GET"])
def models():
    return Response(json.dumps({"data": [{"type": "model", "id": CONFIG["model"],
                                          "display_name": CONFIG["model"]}]}),
                    content_type="application/json")


@app.route("/health", methods=["GET"])
def health():
    return Response(json.dumps({"ok": True, "upstream": CONFIG["upstream_base_url"],
                                "model": CONFIG["model"],
                                "key_set": bool(CONFIG.get("api_key"))}),
                    content_type="application/json")


@app.route("/stats.json", methods=["GET"])
def stats_json():
    return Response(json.dumps(stats_snapshot(), ensure_ascii=False),
                    content_type="application/json; charset=utf-8")


@app.route("/stats/reset", methods=["POST"])
def stats_reset():
    with _ST_LOCK:
        for k in ("total_reqs", "ok", "fail", "stream_reqs", "in_tok", "out_tok",
                  "raw_in_tok", "phantom_tok", "sys_tok", "tools_tok", "hist_tok",
                  "client_msgs", "sent_msgs", "raw_hist_tok", "raw_tools_tok",
                  "saved_tok", "sent_tok"):
            STATS[k] = 0
        STATS["duration"] = 0.0
        STATS["recent"].clear()
        STATS["errors"].clear()
    with _COUNT_LOCK:
        COUNTS.update({"drops": 0, "retries": 0, "upstream_calls": 0,
                       "server_tools": {}, "tool_uses": {}})
    log("stats reset")
    return Response(json.dumps({"ok": True}), content_type="application/json")


@app.route("/", methods=["GET"])
def index():
    if DASHBOARD_HTML:
        return Response(DASHBOARD_HTML, content_type="text/html; charset=utf-8")
    return Response(
        "<h2>ccproxy is running</h2>"
        "<p>Endpoint: <code>http://127.0.0.1:%d/v1</code></p>"
        "<p>Upstream: <code>%s</code></p>"
        "<p>Health: <a href='/health'>/health</a> | "
        "Stats: <a href='/stats.json'>/stats.json</a></p>"
        "<p class='muted'>dashboard.py did not load -- check the log.</p>"
        % (int(CONFIG.get("listen_port", 8181)), CONFIG["upstream_base_url"]),
        content_type="text/html; charset=utf-8")


def _shutdown(*_):
    log("shutting down...")
    sys.exit(0)


if __name__ == "__main__":
    if not CONFIG.get("api_key"):
        log("No static UPSTREAM_API_KEY set; forwarding ANTHROPIC_API_KEY directly from client requests.")
    else:
        log("Static UPSTREAM_API_KEY is configured as fallback.")
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    host = CONFIG.get("listen_host", "127.0.0.1")
    port = int(CONFIG.get("listen_port", 8181))
    log(f"ccproxy ready -> http://{host}:{port}/v1   (upstream: {CONFIG['upstream_base_url']})")
    log(f"native map: {NAME_MAP} | server tools: {sorted(SERVER_TOOL_NAMES)}")
    app.run(host=host, port=port, debug=False, threaded=True)
