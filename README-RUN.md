# ccproxy — a complete proxy for Claude Code

This replaces `agent_proxy.py`. It does the same job (running Claude Code's tools
against the JDW relay `api.justwoker.icu`), but it fixes the three things that
were broken.

---

## What was broken (and what happens now)

### 1. Multi-agent / Monitor / Write / Edit calls were silently dropped

The proxy read the tool call out of the model's reply like this:

```python
json.loads(body)      # strict parse
```

The model often writes JSON that contains:

* **literal newlines** — `Agent`'s `prompt`, `Write`'s `content`, `Edit`'s `old_string`
* **invalid escapes** — `warn\]`, `Installed\.`, `grep -E "\d+"`

A strict parse **failed** on that JSON, and the whole tool call was **silently
dropped** (Claude Code only received the plain text). That is why:

> in your `debug_dump`, **`Monitor` was dropped 3049 times**, and `Agent` was dropped too.

**Now:** a 4-step repair — strict → literal control chars → escape repair →
bracket balancing. Whatever JSON the model wrote is recovered, and valid JSON is
left untouched. (Tested in `test_offline.py` sections 1 and 2.)

### 2. 524 timeouts and token burn

Claude Code sends the **entire session** with every request. One of your dumps:

```
debug_dump/fail_5_attempt1.json   =  1,650,689 chars  (~400,000 tokens)
messages                          =  1303
```

The relay cannot handle a payload that size → Cloudflare **524 timeout**. The
small request (2 messages) returned 200 OK. That is why "some things work and
some don't".

**Now (four ways tokens are saved):**

| Setting | Default | What it does |
|---|---|---|
| `max_history_chars` | `220000` | keeps history inside a ~55k token budget |
| `max_tool_result_chars` | `32000` | how much of a large read the model gets to see (see §7) |
| `compact_tools` | `true` | shorter tool descriptions (~20k tokens saved) |
| `cache_system_prefix` | `true` | prompt-cache breakpoint on system + tools |

### 3. Web search / web fetch

There was **not a single** WebSearch attempt in `debug_dump`. The reason: Claude
Code cannot execute these tools itself when it points at a third-party base URL,
and the relay does not either.

**Now this proxy runs them itself.** The model calls `WebSearch` / `WebFetch` /
`fetch_image`, the proxy does a live DuckDuckGo search (or opens the page), feeds
the result back to the model, and loops until the model gives a final answer.
Claude Code only sees the final answer.

### 4. The native tools were missing from the payload

The first version of `ccproxy` described **every** tool — Read/Write/Edit/Bash
included — as text in the system prompt, and sent no `tools` array at all. The
relay only implements the four lowercase native names, so those four stopped
working entirely. `study/agent_proxy.py` had this right.

**Now:** `Read`/`Write`/`Edit`/`Bash` are mapped to `read`/`write`/`edit`/`bash`
and sent in the payload's `tools` array with their real schemas, so the model
calls them through the normal tool mechanism. Everything else stays on the text
protocol — where ccproxy's JSON repair makes it far more reliable than the
original proxy was.

---

### 5. Every tool result was being deleted — the "repeats the same command" loop

**Symptom:** the model runs `mkdir`, then `mkdir` again, then `mkdir` again with a
slightly different folder name, forever. It never gets to the next step.

**Cause:** `_is_empty_content()` in `ccproxy.py` decided whether a message was
worth sending by looking for `text` or `image` blocks:

```python
if (b.get("text") or "").strip():   # a tool_result has no "text" field
    return False
```

A `tool_result` block carries its payload in `content`, **not** `text`. So every
tool-result message looked empty, and `normalize_alternating()` silently dropped
it. The model called a tool, got **nothing** back, and — reasonably — called it
again. This hit **every** native tool result: `Read`, `Write`, `Edit`, `Bash`.

**Now:** `tool_use`, `tool_result`, `image`, `document` and `search_result` all
count as real content. A tool result that is an empty string is still kept —
the model has to see that the tool *ran*.

Verified by running **real Claude Code** through the proxy (not a script):
`claude -p "4 agent laga kar test karo …" --dangerously-skip-permissions`, in a
scratch folder. Claude Code's own model reported *"Chaaron agents ek saath
(parallel) launch hue the"*, and the four files were really on disk afterwards:

```
history-of-coffee.html       13216 bytes   320 lines   different md5
northern-lights.html          9463 bytes   226 lines   different md5
octopus-intelligence.html     9969 bytes   184 lines   different md5
james-webb-telescope.html     8872 bytes   166 lines   different md5
```

All four well-formed (`<!DOCTYPE html>` … `</html>`), each built from its own
live web search.

The signature of a working parallel launch in `ccproxy_log.txt` is four request
lines in the **same second** with `tools=15` (a subagent's smaller tool list),
followed by four `SERVER TOOL : WebSearch` lines with four different queries.
Before this fix, that second turn was another `mkdir`.

---

### 6. The model forgot the task after a few turns, and never asked

Two separate things, both fixed.

**It forgot.** `max_history_chars` exists because the relay 524s on big payloads,
but the trimming was wrong twice over:

* it measured messages at their **raw** size, while the tool output inside them
  was about to be cut to `max_tool_result_chars` anyway. So the budget looked
  full when it was not, and ~80% of the conversation went in the bin to stay
  inside a budget the survivors never filled. On a session of 12k-char tool
  results the old code fitted **18 messages; the new one fits 148**;
* it kept `messages[0]` and the newest messages that fitted, and dropped
  everything in between **silently**. From the real log:

  ```
  REQ #843 | client_msgs=847 sent_msgs=168
  REQ #844 | client_msgs=849 sent_msgs=167
  ...
  REQ #848 | client_msgs=857 sent_msgs=161
  ```

  851 messages in, ~165 out, 690 gone, on every request of that session. Across
  the whole log: **648 of 1315 requests (49%) had history trimmed.**

  The model, seeing an original task and then a disconnected recent tail,
  answered a question nobody had asked, and at one point replied with the user's
  own last message as if it were its own answer.

  Now: the human's turns get a reserved slice of the budget
  (`history_user_reserve`), the newest messages are always kept
  (`keep_recent_messages`), and **anything still dropped leaves a marker** where
  the cut is. The system prompt carries a `Context notice` too. So the model
  knows the conversation is incomplete — and says so, instead of guessing.

  The log now prints the number, so you can see it:

  ```
  ===== REQ #1 | client_msgs=36 sent_msgs=13 dropped=18 | ...
  ```

**It never asked.** The model would pick an interpretation and run with it.
The system prompt now carries a `## How this session works` block: ask when the
request is ambiguous or the decision is really the user's (*"A: … or B: …"*) and
wait; stop and report the real error when something is stuck instead of calling
the same failing tool again; say when something was skipped.

Both were tested for real (see "What works" in `README.md`), and the offline
suite grew from 70 to **84 checks**.

---

### 7. It was slow, and it only fixed the surface

**Slow.** 95% of wall time is the relay *generating* — measured, 626 turns: median
throughput **29.5 tokens/s**, median turn **10.3s**, and 146.5 of 154 minutes
spent waiting on output tokens. Input is nearly free (6k extra input tokens ≈
+1.4s) and prompt caching changes nothing.

But one thing was slow for nothing: **the relay drops ~26% of requests**. One real
log, 1320 upstream attempts:

```
940 x 200    349 x 503    28 x 403    3 x 524
```

It is not a cooldown and not header-related — a retry sent at **0.0s** after a
`403` came back `200`. The old code slept a fixed **2 seconds** before its first
retry. Across the log, 194 retries fired: **7–16 minutes of dead sleep**, plus a
full payload re-sent each time.

Now the first retry is immediate, `403` is retryable, and there are three cheap
attempts. Measured through the proxy: **30/30 requests succeeded**, where the old
policy lost about one in five. The log shows it working:

```
[22:15:12] upstream attempt 1 -> 503 (empty body)
[22:15:12] upstream attempt 2 -> 403 (empty body)
[22:15:12] upstream attempt 3 -> 200
```

Also: a reply that asks for three web searches used to make **three** round-trips,
one search at a time. They now run together and come back in one message —
3 × 0.3s of work finishes in **0.31s**.

**Surface-level fixes.** Not a personality problem — a config value.
`max_tool_result_chars` was `4000`, so **every** tool result was truncated to
4,000 characters. A `Read` of a 30,000-char file reached the model as its first
4,000, and it then edited a file whose end it had never seen.

Raising it is cheap: Claude Code itself replaces every *older* tool result with a
~28-character first line, so only the newest one is ever large. It is now
`32000`.

Tested for real — a three-file project with `store.py` padded to **30,256
characters** (30 plausible helpers) and the actual bug, `live`/`expired` swapped
in `stats()`, at **character 29,853**:

```
REQ #1-2  hist=2,375 -> 2,430       the task
REQ #3    hist=11,326  (+8,896)     the whole file, forwarded
REQ #8    hist=12,356               the answer
```

Under the old cap that third request would have been ~3,430 tokens and the buggy
function would never have arrived. The model named the exact function and line,
changed only the two wrong branches, and reported that the thirty helper
functions were *"just a distraction, there was no bug in them"*. Eight turns,
**57 seconds**, every request first-try, nothing trimmed. The test passed.

**Extended thinking** also works at this relay (a real signed thinking block), but
thinking tokens are output tokens: a 2,000-token budget costs **~68 seconds** at
29.5 tok/s. So it is `thinking_adaptive` — a budget on the turn that *plans* (a
fresh user message), none on the turns that only execute it (each starts with a
`tool_result`). `thinking_enabled: false` turns it off entirely.

The offline suite grew from 84 to **137 checks**.

---

### 8. Prompt caching — present, and doing nothing

`cache_system_prefix` puts a `cache_control: {"type": "ephemeral"}` breakpoint on
the last system block, the same place Anthropic's own clients put one. The relay
**accepts** it and answers with a `cache_creation_input_tokens` /
`cache_read_input_tokens` block. That is why it looked like caching worked.

It does not. Measured directly against the relay on 2026-10-07, sweeping the
cacheable prefix from 131 chars to 61,459:

```
 lines   sys chars   cache?   latency   in_tok  cache_write  cache_read
     2         131      no      3.27s    10409        10407        None
   200       13569     yes      3.28s    15161         4881       10278
   900       61459      no      5.13s    31961        31959        None
    20        1336     yes      2.51s    10841          561       10278
```

* **The read is always 10,278 tokens.** A 1,336-char prefix and a 13,569-char
  prefix both come back as `cache_read = 10278`. That cannot be your prefix — it
  is the relay's own hidden one. Nothing you send is ever served from cache.
* **The write is `input_tokens - 2`, every time.** On requests with no
  `cache_control` at all: `cache_write = 15159` against `in_tok = 15161`. That is
  "everything was a write", which measures nothing.
* **It never appears after a first write.** If the cache worked, request 1 would
  write and requests 2..N would read. Instead the read shows up on a random
  request — 1 of 8 through the proxy, and it was the *first* one.

Live, through the proxy, eight identical requests:

```
cache_hit_reqs    1
cache_read_tok    10278     <- the same 10278 as always
cache_write_tok   109730    <- 15001 = in_tok - 2, seven times over
```

**So the breakpoint is kept** (it costs nothing, and would start helping if the
relay ever implements the cache), but the proxy is honest about the result:

* it **forwards** `cache_read_input_tokens` to Claude Code when one appears;
* it **does not forward** `cache_creation_input_tokens` — that number is a
  constant, and handing Claude Code "the cache just absorbed 15,001 tokens" on
  every request would be a lie;
* the **dashboard shows a Cache reads card** and marks each hit with a `cache`
  pill, so this is visible without re-measuring it.

A bug this turned up: the request record was being built from the *client-facing*
usage block, which deliberately has `cache_creation_input_tokens` stripped — so
the dashboard read 0 cache writes on a relay that sends that field every single
time. The raw upstream usage is now carried on the request context
(`ctx["raw_usage"]`) and read from there. The live traffic caught it; the test
suite now pins it.

---

## How to run it

### 1. API key

Either:

```sh
export UPSTREAM_API_KEY='sk-....'      # option A (recommended)
```

or open `config.json` and fill in `"api_key": "sk-...."`.

### 2. Start it

```sh
cd /Users/abdurrehmandaudigmail.com/deepseek-proxy
sh start.sh
```

`start.sh` uses the bundled `.venv` (flask and requests are already installed,
nothing needs to be installed).

Once it is up you will see:

```
ccproxy ready -> http://127.0.0.1:8181/v1   (upstream: https://api.justwoker.icu)
```

Open `http://127.0.0.1:8181/health` in a browser — you should get `"ok": true`
and `"key_set": true`.

### 2b. The dashboard

Open **<http://127.0.0.1:8181/>** in a browser.

It shows, live, **where the data and tokens are going**:

* **Token spend** — requests, input/output tokens, phantom (the relay's ~10.4k),
  tool calls, dropped calls, retries, average latency
* **Where the data goes** — a bar splitting the total into
  system prompt / tool definitions / history / model output
* **A bar per request** — height = tokens; violet = the request used a web tool;
  red = it failed
* **Recent requests table** — msgs (client→sent), tokens, seconds, tools, drops, note
* **Disk** — how much space `ccproxy_log.txt` and `debug_dump/` are using
* **Upstream calls / retries** — every retry re-sends the whole payload, so this
  number matters for token cost

The page refreshes itself every 2 seconds. `Pause` stops it, `Reset` zeroes the
counters. Raw data: `/stats.json`.

> The dashboard only shows **this proxy's own** counters (since it started). The
> real bill lives with the relay — but you can see clearly here which part is
> costing what.

### 3. Point Claude Code at the proxy

In a second terminal:

```sh
export ANTHROPIC_BASE_URL='http://127.0.0.1:8181'
export ANTHROPIC_API_KEY='dummy'        # the real key lives inside the proxy
claude
```

> Put anything in `ANTHROPIC_API_KEY` — the real key goes upstream from the proxy
> (`config.json` / `UPSTREAM_API_KEY`).

### 4. Run the tests

```sh
sh run_tests.sh
```

This checks everything **without** a real API key — using the very payloads that
used to fail, plus a mock upstream for a full request → tool_call → response cycle.

---

## Tuning token use (`config.json`)

Everything lives under `features`:

```jsonc
"max_history_chars": 220000,   // lower it (e.g. 120000) = fewer tokens, less memory
"max_tool_result_chars": 32000, // how much of a large read the model gets to see
"compact_tools": true,         // false = full tool descriptions (more tokens)
"upstream_retries": 3,         // the relay drops ~26%; these retries are immediate and cheap
"usage_baseline_tokens": 0,    // set to 10380 to hide the relay's ~10.4k phantom tokens
"dump_requests": false,        // true = save requests into debug_dump/ (uses disk)
```

**The biggest lever** is `max_history_chars`. Going from 220000 (~55k tokens) to
120000 (~30k tokens) roughly halves the cost, but the model will remember less of
the earlier conversation.

---

## What works

| Feature | How |
|---|---|
| `Read` `Write` `Edit` `Bash` | the relay's **native** tools (`read`/`write`/`edit`/`bash`) |
| `Agent` (multi-agent), `Monitor`, `Task*`, `TodoWrite`, MCP tools | the `<tool_call>` text protocol |
| `WebSearch` | the **proxy itself** searches DuckDuckGo |
| `WebFetch` | the **proxy itself** opens the page |
| `fetch_image` | the **proxy itself** fetches the image for the model |
| `/v1/messages/count_tokens` | local estimate (used to return 404) |
| `/v1/models` | available |
| Streaming | SSE + a **ping every 3 seconds** (no dead air, no timeout) |

---

## Troubleshooting

**`UPSTREAM_API_KEY is not set`** → `export UPSTREAM_API_KEY='sk-...'`

**Getting `Upstream 524`** → the payload is still too big. Lower
`max_history_chars` (e.g. `120000`) and set `max_tool_result_chars` to `2000`.

**Getting `Upstream 400`** → the proxy retries automatically with a **slim
payload** (system as a plain string, no `cache_control`). If it still fails, look
at `ccproxy_log.txt`.

**A tool call is still being dropped?** → search `ccproxy_log.txt` for
`DROP tool_call` or `BAD JSON tool_call`. Send me the JSON on that line and I will
add that case to the repair.

**Where is the log?** → `ccproxy_log.txt` (rotates at 2MB into `.1`).

---

## Files

| File | Purpose |
|---|---|
| `ccproxy.py` | the actual proxy (run this one) |
| `dashboard.py` | the dashboard served at `/` (imported by ccproxy) |
| `config.json` | settings (the API key can go here, or in `UPSTREAM_API_KEY`) |
| `start.sh` | starts the proxy |
| `run-claude.sh` | starts Claude Code against the proxy, without touching `~/.claude/settings.json` |
| `run_tests.sh` | runs the offline tests |
| `test_offline.py` | the test suite (with a mock upstream) |
| `names_probe.py` | script that finds the relay's native tool names |
| `study/agent_proxy.py` | the **old** proxy, kept as a working reference for how the native tools are sent |
| `ccproxy_log.txt` | the live log (`tail -f` it while debugging) |

`debug_dump/` is not in the list on purpose — `dump_requests` is `false`, so
nothing is written there. Turn it on in `config.json` only while chasing a bug,
then turn it back off; it grows to hundreds of MB fast.
