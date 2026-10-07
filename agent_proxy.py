import os
import re
import copy
import json
import uuid
from datetime import datetime
import requests
from flask import Flask, request, Response, stream_with_context

app = Flask(__name__)

TARGET = os.environ.get("TARGET_URL", "https://api.justwoker.icu").rstrip("/")
KEY = re.sub(r"[^\x21-\x7e]", "", os.environ.get("UPSTREAM_API_KEY", ""))
PORT = int(os.environ.get("PORT", "8181"))
LOG = "debug_log.txt"
DUMP_DIR = "debug_dump"
os.makedirs(DUMP_DIR, exist_ok=True)
counter = {"n": 0}

ALLOWED_TOP = ["model", "max_tokens", "messages", "system", "tools", "tool_choice",
               "temperature", "top_p", "top_k", "stop_sequences"]

# Relay sirf yehi tool names natively pass karta hai (test se confirm hua).
# Baaki saare tools (Task/subagents, Grep, Glob, WebSearch, TodoWrite, MCP...) text protocol se chalte hain.
NAME_MAP = {"Read": "read", "Write": "write", "Edit": "edit", "Bash": "bash"}
_extra = os.environ.get("NAME_MAP_EXTRA")  # e.g. '{"Grep":"grep"}' agar probe mein naya naam mile
if _extra:
    NAME_MAP.update(json.loads(_extra))
REV_MAP = {v: k for k, v in NAME_MAP.items()}

CALL_RE = re.compile(r'<tool_call\s+name="([^"]+)"\s*>(.*?)</tool_call>', re.DOTALL)
DESC_LIMIT = 2500


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def head(x, n=300):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:n].replace("\n", "\\n")


def strip_key(obj, key):
    if isinstance(obj, dict):
        return {k: strip_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [strip_key(v, key) for v in obj]
    return obj


def flatten_result(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text", ""))
        elif t == "tool_reference":
            parts.append(f"[tool: {b.get('tool_name', '')}]")
        else:
            parts.append(f"[{t}]")
    return "\n".join(parts)


def clean_inner(content):
    """tool_result ke andar ke blocks saaf karo."""
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("thinking", "redacted_thinking"):
            continue
        if t == "tool_reference":
            out.append({"type": "text", "text": f"[tool: {b.get('tool_name', '')}]"})
            continue
        out.append(b)
    return out if out else "(empty)"


def collect_emulated(messages):
    """History mein jo tool_use text-protocol wale tools ke hain, unke id -> name."""
    ids = {}
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if (isinstance(b, dict) and b.get("type") == "tool_use"
                        and b.get("name") not in NAME_MAP):
                    ids[b.get("id")] = b.get("name")
    return ids


def convert_messages(messages, emulated_ids):
    out = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, str):
            out.append({"role": m.get("role"), "content": c})
            continue
        results, others, emu_results = [], [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in ("thinking", "redacted_thinking"):
                continue
            if t == "tool_reference":
                others.append({"type": "text", "text": f"[tool: {b.get('tool_name', '')}]"})
            elif t == "tool_use":
                if b.get("name") in NAME_MAP:
                    nb = dict(b)
                    nb["name"] = NAME_MAP[b["name"]]
                    others.append(nb)
                else:
                    call = json.dumps(b.get("input", {}), ensure_ascii=False)
                    others.append({"type": "text",
                                   "text": f'<tool_call name="{b.get("name")}">{call}</tool_call>'})
            elif t == "tool_result":
                tid = b.get("tool_use_id")
                if tid in emulated_ids:
                    err = ' error="true"' if b.get("is_error") else ""
                    emu_results.append({"type": "text", "text":
                        f'<tool_result name="{emulated_ids[tid]}" id="{tid}"{err}>\n'
                        f'{flatten_result(b.get("content"))}\n</tool_result>'})
                else:
                    nb = dict(b)
                    if isinstance(nb.get("content"), list):
                        nb["content"] = clean_inner(nb["content"])
                    results.append(nb)
            else:
                others.append(b)
        new = results + others + emu_results
        if not new:
            new = [{"type": "text", "text": "(empty)"}]
        out.append({"role": m.get("role"), "content": new})
    return out


def build_extra_prompt(tools):
    extra = [t for t in tools if "input_schema" in t and t.get("name") not in NAME_MAP]
    native = ", ".join(f"{v} (= {k})" for k, v in NAME_MAP.items())
    lines = [
        "## Tool names in this session",
        f"Native tools use lowercase names: {native}. Call them through the normal tool-calling "
        "mechanism, using the same parameters as the original tool of that name. "
        "Ignore any tool named read_tabular or system_todo_write; they are not available.",
    ]
    if extra:
        lines += [
            "",
            "## Extra tools (text protocol)",
            "The tools listed below are NOT in your native tool list. To call one, write a block "
            "exactly like this in your reply:",
            '<tool_call name="TOOL_NAME">{"param": "value"}</tool_call>',
            "- The body must be one valid JSON object matching the tool's input schema.",
            "- You may write normal text before a tool_call and may emit several tool_call blocks "
            "in one reply (for example to run several agents or searches in parallel).",
            "- After the last tool_call block STOP writing. Never guess or invent results.",
            "- The result arrives in the next user message as "
            '<tool_result name="TOOL_NAME" id="...">...</tool_result>.',
            "- Use these tools only through <tool_call> blocks, never through the native mechanism.",
            "",
        ]
        for t in extra:
            desc = (t.get("description") or "")[:DESC_LIMIT]
            schema = json.dumps(strip_key(t["input_schema"], "$schema"), separators=(",", ":"))
            lines.append(f"### {t.get('name')}\n{desc}\nInput schema: {schema}\n")
    return "\n".join(lines)


def sanitize(body, aggressive=False):
    out = {k: copy.deepcopy(body[k]) for k in ALLOWED_TOP if k in body}
    out = strip_key(out, "cache_control")
    emulated_ids = collect_emulated(out.get("messages", []))

    all_tools = [t for t in (out.get("tools") or []) if isinstance(t, dict)]
    extra_text = build_extra_prompt(all_tools) if all_tools else None

    # system
    sysv = out.get("system")
    if isinstance(sysv, list):
        texts = [b.get("text", "") for b in sysv if isinstance(b, dict)]
        sysv = "\n\n".join(texts) if aggressive else [{"type": "text", "text": t} for t in texts]
    if extra_text:
        if sysv is None:
            sysv = extra_text
        elif isinstance(sysv, str):
            sysv = sysv + "\n\n" + extra_text
        else:
            sysv = sysv + [{"type": "text", "text": extra_text}]
    if sysv is not None:
        out["system"] = sysv

    # native tools only
    native = []
    for t in all_tools:
        if "input_schema" not in t or t.get("name") not in NAME_MAP:
            continue
        schema = t["input_schema"]
        if aggressive:
            schema = strip_key(schema, "$schema")
        native.append({"name": NAME_MAP[t["name"]], "description": t.get("description", ""),
                       "input_schema": schema})
    if native:
        out["tools"] = native
        tc = out.get("tool_choice")
        if isinstance(tc, dict) and tc.get("name"):
            if tc["name"] in NAME_MAP:
                tc["name"] = NAME_MAP[tc["name"]]
            else:
                out.pop("tool_choice", None)
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    out["messages"] = convert_messages(out.get("messages", []), emulated_ids)
    out["stream"] = False
    return out


def parse_response(msg, emulated_names):
    """Relay ka jawab -> Claude Code ke liye blocks (native + text-protocol tool calls)."""
    out, has_tool = [], False
    for b in msg.get("content", []):
        t = b.get("type")
        if t == "tool_use":
            name = REV_MAP.get(b.get("name"))
            if not name:
                log(f"DROPPED unknown relay tool_use: {b.get('name')}")
                continue
            b = dict(b)
            b["name"] = name
            out.append(b)
            has_tool = True
        elif t == "text":
            text = b.get("text", "")
            pos, converted = 0, False
            for m in CALL_RE.finditer(text):
                name = m.group(1)
                if name not in emulated_names:
                    continue
                try:
                    inp = json.loads(m.group(2).strip())
                except Exception:
                    log(f"BAD JSON in tool_call {name}: {head(m.group(2), 200)}")
                    continue
                if not isinstance(inp, dict):
                    continue
                pre = text[pos:m.start()]
                if pre.strip():
                    out.append({"type": "text", "text": pre})
                out.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24],
                            "name": name, "input": inp})
                has_tool = True
                converted = True
                pos = m.end()
            if not converted:
                out.append(b)  # koi tool call nahi, text jaisa hai waisa
            # converted hone par last call ke baad ka text (model ke guess kiye results) hata do
    if not out:
        out = [{"type": "text", "text": " "}]
    return out, has_tool


def get_request_api_key():
    key = (request.headers.get("x-api-key") or "").strip()
    if not key:
        auth = (request.headers.get("authorization") or request.headers.get("Authorization") or "").strip()
        if auth:
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
            else:
                key = auth.strip()
    if (not key or key == "dummy") and KEY:
        key = KEY
    return re.sub(r"[^\x21-\x7e]", "", key or "")


def call_upstream(payload, version, api_key=None):
    use_key = api_key or KEY
    headers = {"x-api-key": use_key, "Authorization": f"Bearer {use_key}",
               "anthropic-version": version, "content-type": "application/json"}
    return requests.post(f"{TARGET}/v1/messages", json=payload, headers=headers, timeout=600)


@app.route("/v1/messages", methods=["POST"])
def proxy():
    counter["n"] += 1
    n = counter["n"]
    body = request.get_json(silent=True) or {}
    req_key = get_request_api_key()
    if not req_key:
        return Response(json.dumps({"error": {"message": "No API key provided. Set ANTHROPIC_API_KEY in Claude Code."}}),
                        status=401, content_type="application/json")
    wants_stream = bool(body.get("stream"))
    version = request.headers.get("anthropic-version", "2023-06-01")

    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    emulated_names = {t.get("name") for t in tools
                      if "input_schema" in t and t.get("name") not in NAME_MAP}
    log(f"===== REQUEST #{n} | messages={len(body.get('messages', []))} =====")
    log(f"native tools   : {[t.get('name') for t in tools if t.get('name') in NAME_MAP]}")
    log(f"emulated tools : {sorted(emulated_names)}")
    with open(f"{DUMP_DIR}/req_{n}.json", "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)

    r = None
    for attempt, aggressive in enumerate((False, True), start=1):
        payload = sanitize(body, aggressive=aggressive)
        try:
            r = call_upstream(payload, version, api_key=req_key)
        except Exception as e:
            log(f"attempt {attempt} (aggressive={aggressive}) EXCEPTION: {e}")
            continue
        log(f"attempt {attempt} (aggressive={aggressive}) -> status {r.status_code}")
        if r.status_code == 200:
            break
        log(f"upstream error body: {r.text[:600]}")
        with open(f"{DUMP_DIR}/fail_{n}_attempt{attempt}.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    if r is None or r.status_code != 200:
        text = r.text if r is not None else json.dumps({"error": {"message": "proxy failed"}})
        return Response(text, status=(r.status_code if r is not None else 500),
                        content_type="application/json")

    msg = r.json()
    blocks, has_tool = parse_response(msg, emulated_names)
    msg["content"] = blocks
    msg["stop_reason"] = "tool_use" if has_tool else (msg.get("stop_reason") or "end_turn")
    log(f"relay blocks   : {[b.get('type') for b in r.json().get('content', [])]}")
    for b in blocks:
        if b.get("type") == "tool_use":
            log(f"TOOL_USE       : {b.get('name')} {head(b.get('input'), 200)}")
        else:
            log(f"text           : {head(b.get('text', ''), 200)}")
    log(f"stop_reason    : {msg['stop_reason']}")

    if not wants_stream:
        return Response(json.dumps(msg), status=200, content_type="application/json")

    def gen():
        usage = msg.get("usage", {})
        start = dict(msg)
        start["content"] = []
        start["stop_reason"] = None
        start["usage"] = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0}
        yield sse("message_start", {"type": "message_start", "message": start})
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                yield sse("content_block_start", {"type": "content_block_start", "index": i,
                          "content_block": {"type": "text", "text": ""}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "text_delta", "text": b.get("text", "")}})
            else:
                yield sse("content_block_start", {"type": "content_block_start", "index": i,
                          "content_block": {"type": "tool_use", "id": b.get("id"),
                                            "name": b.get("name"), "input": {}}})
                yield sse("content_block_delta", {"type": "content_block_delta", "index": i,
                          "delta": {"type": "input_json_delta",
                                    "partial_json": json.dumps(b.get("input", {}))}})
            yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
        yield sse("message_delta", {"type": "message_delta",
                  "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                  "usage": {"output_tokens": usage.get("output_tokens", 0)}})
        yield sse("message_stop", {"type": "message_stop"})

    return Response(stream_with_context(gen()), status=200,
                    content_type="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    if not KEY:
        log("No UPSTREAM_API_KEY set; will forward ANTHROPIC_API_KEY from Claude Code.")
    open(LOG, "w").close()
    log(f"Agent proxy v3 on http://127.0.0.1:{PORT} -> {TARGET}")
    log(f"native map: {NAME_MAP}")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)