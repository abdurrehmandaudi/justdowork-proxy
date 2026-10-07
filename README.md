# justdowork-proxy

Run **Claude Code** against a OpenAI/Anthropic-compatible relay that isn't a
faithful Anthropic endpoint.

The relay this was built for is `https://api.justwoker.icu`, but the messy parts
it fixes are generic: relays that drop tool calls, ignore the `tools` array, or
mangle streaming.

**You point Claude Code at this proxy on `http://127.0.0.1:8181` instead of at
the relay.** The proxy translates both ways, repairs the model's JSON, runs
web search itself, and hands Claude Code a clean, standards-shaped stream.

> **License:** free for personal, hobby, educational, research, non-profit and
> government use. **Commercial use — including reselling or hosting it as a
> paid service — requires a separate written license.** See [License](#license).

---

## What's new

### v3.0.0 — 2026-10-07

Three fixes, each measured rather than assumed.

**It remembers, and it asks.** The conversation is shortened to fit the relay's
payload limit — and that shortening used to be silent: `857 messages in, 163 out`,
694 gone on *every* request of a long session, with nothing in the payload to say
so. The model, seeing an original task and then a disconnected recent tail,
answered the wrong question. Now the human's own turns get a reserved slice of
the budget, the newest messages are always kept, and **anything still dropped
leaves a marker** where the cut is — plus a `Context notice` in the system prompt.
Asked to recall facts that had been trimmed it answered *"the other three were in
earlier messages that got trimmed out — I can't now verify those from memory and
don't want to vouch for them as fact"*, which is exactly the point. The system
prompt also tells it to ask when the request is ambiguous or the decision is
really the user's, and to report the real error when stuck instead of calling the
same failing tool again.

**It sees the file it is editing.** `max_tool_result_chars` was `4000`, so *every*
tool result was cut to 4,000 characters — a `Read` of a 30,000-char file reached
the model as its first 4,000, and the model then edited a file whose end it had
never seen. That is a surface-level fix, mechanically. It is now `32000`, and it
is cheap because Claude Code itself replaces every *older* tool result with a
~28-char first line, so only the newest one is ever large. Verified on a real
30,256-character file whose bug sat at character 29,853: the model read it whole,
named the exact function and line, changed only the two wrong branches, and said
the thirty helper functions around it were *"just a distraction, there was no bug
in them"*.

**It stopped wasting seconds.** The relay drops ~26% of requests with an empty
`403`/`503` — measured, and it is *not* a cooldown: a retry sent at **0.0s** after
a `403` returns `200`. The old code slept a fixed **2 seconds** before its first
retry, which across one real log was 7–16 minutes of dead sleep. Retries are now
immediate, `403` counts as retryable, and three cheap attempts recovered **30/30**
requests in a test that used to lose about one in five. A reply asking for three
web searches no longer makes three round-trips either — they run together
(3 × 0.3s of work in **0.31s**).

Also new: **adaptive extended thinking**. The relay does support it — a real
signed thinking block comes back — but thinking tokens are *output* tokens, and at
this relay's measured ~29.5 tokens/s a 2,000-token budget costs **~68 seconds**.
So a budget goes to the turn that *plans* (a fresh user message) and not to the
turns that only execute that plan. Claude Code's own extended-thinking setting, if
it is on, takes priority.

And **prompt caching is now visible rather than mysterious**. The breakpoint was
already there and the relay already answered with a cache block, so it looked like
it worked. It does not: the read it reports is 10,278 tokens whatever you send — a
1,336-char prefix and a 13,569-char prefix both come back as 10,278 — so that is
the relay's own hidden prefix, never the conversation's, and it arrives on a random
request instead of after a first write. The dashboard now shows a **Cache reads**
card and marks each hit, and the proxy forwards the read to Claude Code while
withholding the relay's invented write count. See
[Prompt caching](#prompt-caching--it-is-in-here-and-it-does-nothing).

`test_offline.py` grew from 70 to **137 checks**.

### v1.0.1 — PolyForm Noncommercial license

### v1.0.0 — first release

---

## What it fixes

| Problem at the relay | What the proxy does |
|---|---|
| Only honours tools literally named `read`/`write`/`edit`/`bash` | Sends those four natively with real schemas; every other tool goes through a `<tool_call>` text protocol |
| Model writes JSON with literal newlines and bad escapes, call is dropped | 4-step repair: strict parse → control chars → escape repair → bracket balancing |
| Streaming drops text and tool blocks (thinking deltas only) | Synthesises the SSE stream itself, with a ping every 3s so nothing times out |
| Cannot run `WebSearch` / `WebFetch` | The proxy runs DuckDuckGo / opens the page itself, and loops until the model answers |
| Huge histories cause 524 timeouts | Fits the conversation into a char budget — keeping the human's own turns, and marking the gap when it has to drop something |
| The model forgets the task after a few turns | Never drops history silently: it keeps every user message it can, and tells the model when something was cut, so the model says "I've lost that" instead of guessing |
| The model never asks, just guesses | The system prompt tells it to ask when the request is ambiguous or the decision is the user's |
| The model patches a file it cannot see the end of | Tool output was cut at 4000 chars, so a `Read` of a 30k file arrived as its first 4k — raise to `32000` and the model sees what it is editing |
| ~26% of requests come back as an empty `403`/`503` | The relay rejects at random with no cooldown, so the proxy retries **immediately** (the old fixed 2s/5s/10s backoff slept 2s for nothing) |
| Thinking is supported by the relay but never requested | Adaptive extended thinking on the turn that *plans*; the mechanical turns stay fast |
| A reply asking for three web searches made three round-trips | The proxy runs them together and feeds all the results back at once |
| Adds ~10.4k phantom input tokens to every request | `usage_baseline_tokens` corrects the reported number (the real cost stays — that's the relay's) |
| Accepts `cache_control` and answers with a cache block, so caching *looks* like it works | It does not — the read it reports is a constant 10,278 tokens whatever you send. The proxy forwards the read and shows hit rate on the dashboard, so this is visible instead of something you have to go and measure |

---

## Requirements

* **Python 3.9 or newer** — [python.org/downloads](https://www.python.org/downloads/)
* An API key for the relay you're pointing at
* [Claude Code](https://claude.com/claude-code) (only if you want to run Claude Code through it)

No compiler, no Docker, no system packages. `flask` and `requests` are pure
Python and get installed into a local `.venv` for you.

---

## Quick start — macOS / Linux

```sh
# 1. get the code
git clone https://github.com/abdurrehmandaudi/justdowork-proxy.git
cd justdowork-proxy

# 2. put your relay API key in
export UPSTREAM_API_KEY='sk-...'

# 3. start it (first run creates .venv and installs flask + requests)
sh start.sh
```

You should see:

```
Starting ccproxy ... the exact URL is printed on the next line.
Press Ctrl+C to stop.

[20:18:39] ccproxy ready -> http://127.0.0.1:8181/v1   (upstream: https://api.justwoker.icu)
```

The `ccproxy ready ->` line is the authoritative one — it uses whatever
`listen_port` you set in `config.json`.

Leave that terminal running. Now open a **second** terminal:

```sh
cd justdowork-proxy
sh run-claude.sh
```

That starts Claude Code pointed at the proxy — **your global
`~/.claude/settings.json` is not modified**, so your other Claude Code setup
keeps working exactly as before.

> If `sh start.sh` says `python3: command not found`:
> macOS — `brew install python3`. Linux — `sudo apt install python3 python3-venv`.

---

## Quick start — Windows

**Before anything else:** install Python from
[python.org/downloads](https://www.python.org/downloads/) and, on the **first
screen of the installer**, tick:

```
[x] Add python.exe to PATH
```

If you miss that box, `start.bat` will tell you Python wasn't found — re-run the
installer, choose *Modify*, and add it.

Then, in **Command Prompt** or **PowerShell**:

```bat
:: 1. get the code
git clone https://github.com/abdurrehmandaudi/justdowork-proxy.git
cd justdowork-proxy

:: 2. put your relay API key in (this sets it permanently for your user)
setx UPSTREAM_API_KEY "sk-..."

::    IMPORTANT: setx only affects NEW windows. Close this one and open a new one.
```

Now start the proxy — you can just **double-click `start.bat`**, or run:

```bat
cd justdowork-proxy
start.bat
```

The first run creates `.venv` and installs the dependencies (about a minute).
When you see the "Starting ccproxy" banner, open a **second** window:

```bat
cd justdowork-proxy
run-claude.bat
```

`run-claude.bat` writes a temporary settings file (`%TEMP%\ccproxy-claude-settings.json`)
and passes it to Claude Code with `--settings`. Your global
`%USERPROFILE%\.claude\settings.json` is **not** modified.

> **Windows Firewall:** the proxy listens on `127.0.0.1` only, so Windows
> normally does not prompt. If you *do* get a firewall dialog, you can safely
> click Cancel — a loopback-only listener doesn't need an exception.

---

## Getting an API key

The key is the relay's key, not an Anthropic key. For the relay this was built
against, sign up at [api.justwoker.icu](https://api.justwoker.icu) and copy the
`sk-...` key from the dashboard.

You can give it to the proxy in **either** of two ways:

**Option A — environment variable** (recommended; keeps it out of the repo)

| | |
|---|---|
| macOS / Linux | `export UPSTREAM_API_KEY='sk-...'` (add to `~/.zshrc` to persist) |
| Windows | `setx UPSTREAM_API_KEY "sk-..."` then open a new terminal |

**Option B — `config.json`**

```jsonc
{
  "upstream_base_url": "https://api.justwoker.icu",
  "api_key": "sk-...",          // <-- here
  ...
}
```

`config.json` is git-ignored by default in spirit — **do not commit your key**.
The version in the repo ships with `"api_key": ""`.

---

## Check that it works

Open **<http://127.0.0.1:8181/>** in a browser. You get a live dashboard showing
where your tokens are going: requests, input/output tokens, the relay's phantom
tokens, tool calls, dropped calls, retries, and a bar per request. It refreshes
every 2 seconds.

Open **<http://127.0.0.1:8181/health>** for a machine-readable check:

```json
{"ok": true, "upstream": "https://api.justwoker.icu", "model": "claude-opus-4-8", "key_set": true}
```

`"key_set": true` means the proxy found your key. If it's `false`, see
[Getting an API key](#getting-an-api-key).

You can also run the offline test suite — it needs **no** API key, because it
starts its own mock upstream:

```sh
sh run_tests.sh          # macOS / Linux
```
```bat
run-tests.bat            :: Windows
```

Expected:

```
RESULT:  137 pass, 0 fail
```

---

## Running Claude Code through it

`run-claude.sh` (macOS/Linux) and `run-claude.bat` (Windows) do the same four
things:

1. check that the proxy is actually answering, and tell you clearly if it isn't
2. set `ANTHROPIC_BASE_URL` to the proxy
3. set `ANTHROPIC_API_KEY=dummy` — the *real* key lives inside the proxy, so
   Claude Code never needs to see it
4. set `ENABLE_TOOL_SEARCH=false` — the proxy relies on this being off

Any normal Claude Code flag passes straight through:

```sh
sh run-claude.sh --continue
sh run-claude.sh --model claude-opus-4-8
```

**Doing it by hand** (if you'd rather not use the scripts):

```sh
export ANTHROPIC_BASE_URL='http://127.0.0.1:8181'
export ANTHROPIC_API_KEY='dummy'
export ENABLE_TOOL_SEARCH='false'
claude
```
```bat
set ANTHROPIC_BASE_URL=http://127.0.0.1:8181
set ANTHROPIC_API_KEY=dummy
set ENABLE_TOOL_SEARCH=false
claude
```

---

## Memory: it does not forget, and it asks

The relay answers `524` on big payloads, so the conversation has to be shortened
before it is forwarded. Doing that badly is what makes a model "forget after two
prompts". The old code kept the first message and the newest ones that fitted and
deleted everything between — in a real log, **857 messages in, 163 out**, 694
gone on *every* request of a long session, with nothing in the payload to admit
it. The model, seeing an original task and then a disconnected recent tail,
answered the wrong question or repeated the user's own words back.

What it does now:

1. **The human's turns are kept.** They are small, and they are what the model
   must not forget. A slice of the budget (`history_user_reserve`) is reserved
   for them.
2. **The newest messages are always kept** (`keep_recent_messages`) — the live
   working set, whatever the budget says.
3. **Whatever is still dropped leaves a mark.** A block saying *"N earlier
   messages were omitted here"* sits at the cut, and the system prompt carries a
   `Context notice` with the counts. The model therefore knows the conversation
   is incomplete, and can say so.
4. **Sizes are measured at the size things are really forwarded at.** Tool
   output is truncated to `max_tool_result_chars` before it goes upstream — so
   the budget is measured on the truncated size too. Measuring raw sizes is why
   the old code threw away ~80% of a session to stay inside a budget the
   survivors never filled: on one long session it fitted **~18 messages where
   this one fits ~148**.

And the system prompt now carries a short `## How this session works` block, so
the model:

* asks instead of guessing when a request is ambiguous or the decision is really
  the user's (*"A: … or B: …"*), and waits for the answer;
* stops and reports the real error when something is stuck, instead of calling
  the same failing tool again;
* says when a step was skipped or not verified, rather than describing a result
  it never got.

All of it is real, not theoretical — with the budget forced down to 2500 chars,
the model answered: *"Only the code word is actually in my visible context …
the other three were in earlier messages that got trimmed out — I can't now
verify those from memory and don't want to vouch for them as fact."* That is the
behaviour this section exists for: it reported the loss instead of inventing
around it.

Turn `working_rules` off in `config.json` if you want the old, quieter prompt.

---

## Speed, and looking before it changes

Two complaints that turn out to have very different answers.

**It is slow.** Measured against the real relay, that is almost entirely the
relay *generating*. Over 626 logged turns:

| output tokens | mean latency |
|---|---|
| 0–20 | 4.9s |
| 150–400 | 11.6s |
| 400–1000 | 14.9s |
| 1000+ | 43.8s |

Median throughput **29.5 tokens/s**. Of 154 minutes of wall time, **146.5 minutes
(95%)** were spent waiting for generated tokens. Input is nearly free by
comparison — 6,000 extra input tokens cost about 1.4s. So there is no trick that
makes the relay generate faster, and prompt caching does not help either: with
`cache_control` the relay does report `cache_read_input_tokens`, but the latency
is identical.

What *was* slow for no reason:

* **The relay drops ~26% of requests.** In one real log: 940 × `200`, 349 × `503`,
  28 × `403`, 3 × `524`. It is not a cooldown and not header-related — a retry
  sent at **0.0s** after a `403` returned `200`. The old code slept a fixed
  **2 seconds** before its first retry, so a quarter of all requests paid 2s for
  nothing (194 retries ≈ 7–16 minutes of dead sleep). Now the first retry is
  immediate, `403` counts as retryable, and there are three cheap attempts:
  **30/30 requests succeeded** in a test that used to lose about one in five.
* **Three web searches took three round-trips.** A reply can ask for several
  server-side tools at once; they now run together and come back in one message.
  Three searches of 0.3s each finish in **0.31s**.

**It only fixes the surface.** This one is not a personality problem — it was a
config value. `max_tool_result_chars` was `4000`, so **every** tool result was cut
to 4,000 characters. A `Read` of a 30,000-char file reached the model as its first
4,000, and the model then edited a file whose end it had never seen. That is
"shallow fix" described literally.

It is cheap to raise, because Claude Code itself replaces every *older* tool
result with a ~28-character first line — only the newest one is ever large. Now
`32000`.

It was tested for real: a three-file project whose `store.py` was padded to
**30,256 characters** with 30 plausible helper functions, the actual bug
(`live`/`expired` swapped in `stats()`) sitting at **character 29,853**. Real
Claude Code through the proxy:

* read the file whole — the request's history jumped from 2,430 to **11,326**
  tokens on the read (it would have been ~3,430 under the old cap);
* named the exact function and line, fixed those two branches and nothing else,
  and reported that the thirty helper functions were *"just a distraction, there
  was no bug in them"* — it told signal from noise across the whole file;
* the test passed afterwards. The whole eight-turn session took **57 seconds**,
  every request first-try, nothing trimmed.

The system prompt also gained a `## Look before you change` block: read enough to
be sure before editing, fix the cause rather than the symptom, and say what you
actually checked.

### Extended thinking

The relay does support it — `{"thinking": {"type": "enabled", "budget_tokens":
N}}` returns a real signed thinking block. But thinking tokens are *output*
tokens, and at ~30 tokens/s a 2,000-token budget adds **~68 seconds** to the turn
that uses it. Always-on thinking would make the proxy far slower, not smarter.

So it is spent where it decides something. `thinking_adaptive` gives a budget to
the model's reply to a **fresh user message** — the turn that decides what to do —
and gives none to the turns that only carry out that plan (each of which starts
with a `tool_result`). Plan deeply once, then execute mechanically.

If Claude Code itself turns extended thinking on, its setting wins: the budget is
forwarded, and the thinking blocks are handed back, because a client that asked
for them expects them. A budget the *proxy* decided on is deliberately not handed
back — the client never asked for it and would have to store and replay a signed
block it has no use for. The reasoning still did its job: it changed what the
model went on to do.

```jsonc
"thinking_enabled": true,
"thinking_adaptive": true,       // think on a fresh user turn, not on tool_result turns
"thinking_budget_tokens": 2000,  // ~68s per thinking turn at this relay's speed
"thinking_max_budget_tokens": 8000,
```

Set `thinking_enabled: false` for the fastest possible proxy.

### Prompt caching — it is in here, and it does nothing

`cache_system_prefix` puts a `cache_control: {"type": "ephemeral"}` breakpoint on
the last system block, which is where Anthropic's own clients put one (the cache
prefix runs tools → system → messages, so it covers the tool schemas too).

The relay **accepts** it and answers with a `cache_creation_input_tokens` /
`cache_read_input_tokens` block — which is why this looks like it works. It does
not. Measured directly against the relay on 2026-10-07:

```
 lines   sys chars   ~sys tok  cache?   latency   in_tok  cache_write  cache_read
     2         131         32      no     3.27s    10409        10407        None
   200       13569       3392     yes     3.28s    15161         4881       10278
   900       61459      15364      no     5.13s    31961        31959        None
   200       13569       3392     yes     4.48s    15161         4881       10278
    20        1336        334     yes     3.07s    10841        10839        None
    20        1336        334     yes     2.51s    10841          561       10278
```

Three things fall out of that:

* **The read is a constant.** 10,278 tokens, whether the cacheable prefix is
  1,336 chars or 13,569. It cannot be your prefix — it is the relay's own hidden
  one. Your system prompt, tool schemas and history are never served from cache.
* **The write is `input_tokens - 2`.** On every request with no `cache_control`,
  `cache_write = 15159` against `in_tok = 15161`. That is "all of it was a
  write", on a request that had nothing to reuse. It is not a measurement.
* **Nothing gets faster.** The requests that reported a read were not quicker
  than the ones that did not.

Through the proxy, eight identical requests, live:

```
cache_hit_reqs    1        <- one of eight
cache_read_tok    10278    <- and it is the same 10278 as always
cache_write_tok   109730   <- 15001 = in_tok - 2, seven times
```

The breakpoint is kept because it costs nothing and would start helping if the
relay ever implements the cache properly. What the proxy does *not* do is pass
the relay's invented `cache_creation_input_tokens` on to Claude Code — telling it
a cache is being filled on every single request would be worse than saying
nothing. The read **is** forwarded when one appears.

The dashboard now shows a **Cache reads** card (total tokens, and how many of
your requests saw one) and marks each hit with a `cache` pill in the request
table, so this stays visible rather than being something you have to go and
re-measure.

```jsonc
"cache_system_prefix": true,   // harmless; no measurable benefit at this relay
```

---

## What works

| Feature | How it works |
|---|---|
| `Read` `Write` `Edit` `Bash` | the relay's **native** tools (`read`/`write`/`edit`/`bash`) |
| `Agent` (subagents, parallel), `Monitor`, `Task*`, `TodoWrite`, MCP tools | the `<tool_call>` text protocol |
| `WebSearch` | the **proxy itself** searches DuckDuckGo |
| `WebFetch` | the **proxy itself** opens the page |
| `fetch_image` | the **proxy itself** fetches the image |
| `/v1/messages/count_tokens` | local estimate (the relay returns 404) |
| `/v1/models` | available |
| Streaming | SSE + a **ping every 3 seconds** (no dead air, no timeout) |

Verified by running real Claude Code through the proxy:

* four `Agent` subagents launched in parallel in one message, each ran its own
  live web search, each wrote a separate standalone HTML file — four real files
  on disk, all well-formed and different;
* a six-turn session in which turn 1 stated three facts and turn 6 asked for
  them back (all three, plus the list of files created) — and a nine-turn
  session with the budget deliberately squeezed, to check that the trim path
  behaves;
* the "asks when it should" path: told to write `p7.txt` about *"topic number
  7"* with no such topic defined, the model asked *"which subject would you like
  p7.txt to cover?"* and created nothing. Turns 7-10 of that session produced no
  files, on purpose;
* a real bug hunt in a 30,256-character file whose fault sat at character
  29,853, past the old 4,000-char tool-output cap. The model read the file whole,
  named the exact function and line, changed only the two branches that were
  wrong, and said the thirty helper functions around it were *"just a
  distraction, there was no bug in them"*. Eight turns, 57 seconds, and the test
  passed afterwards.

---

## Troubleshooting

**`UPSTREAM_API_KEY is not set`**
Set it (see [Getting an API key](#getting-an-api-key)). On Windows remember
`setx` only applies to **new** terminals.

**`ccproxy is not answering on http://127.0.0.1:8181`**
The proxy isn't running, or it's on another port. Start it with `start.sh` /
`start.bat` and leave that window open.

**The model says it lost something, or you see `[... earlier message(s) ... were
omitted here ...]` in the transcript**
That is the history budget doing its job, and the model being honest about it.
Raise `max_history_chars` in `config.json` (e.g. `220000` → `320000`) and restart
the proxy. If you raise it far enough to get `524`s again, that is the ceiling.

**Claude Code says a tool doesn't exist (Agent, WebSearch, …)**
Check the model isn't talking about the relay's own default tools
(`read_tabular`, `system_todo_write`, …). Those are not this session's tools.
Restart the proxy so the tool preamble is rebuilt, and start a **fresh** Claude
Code session — a long contaminated history can keep the model confused.

**Getting `Upstream 524` timeouts**
The payload is too big. In `config.json` lower `max_history_chars` (e.g.
`220000` → `120000`) and set `max_tool_result_chars` to `2000`.

**Getting `Upstream 400`**
The proxy already retries with a slim payload (system as a plain string, no
`cache_control`). If it still fails, check `ccproxy_log.txt`.

**A tool call is being dropped**
Search `ccproxy_log.txt` for `DROP tool_call` or `BAD JSON tool_call`. The JSON
on that line is the case to add to the repair.

**Port 8181 is taken**
Change `listen_port` in `config.json`, then set `PROXY=http://127.0.0.1:<port>`
before running `run-claude.sh` / `run-claude.bat`.

**Where are the logs?**
`ccproxy_log.txt` — the live log. It rotates at 2 MB into `ccproxy_log.txt.1`.

---

## Configuration and token cost

Everything lives under `features` in `config.json`:

```jsonc
"max_history_chars": 220000,   // history budget, ~55k tokens. THE big lever.
"keep_recent_messages": 24,    // newest messages always kept, whatever the budget
"history_user_reserve": 0.35,  // slice of the budget reserved for your own messages
"max_tool_result_chars": 32000,// how much of a large read/output the model gets to see
"compact_tools": true,         // false = full tool descriptions (many more tokens)
"working_rules": true,         // tell the model to ask when stuck / when there is a choice
"thinking_adaptive": true,     // deep thinking on the planning turn only
"upstream_retries": 3,         // the relay drops ~26% of requests; these retries are cheap
"usage_baseline_tokens": 0,    // set to 10380 to hide this relay's phantom tokens
"dump_requests": false,        // true = save every request to debug_dump/ (uses disk)
```

**The biggest lever is `max_history_chars`.** Claude Code sends the entire
session on every request. Going from `220000` (~55k tokens) to `120000`
(~30k tokens) roughly halves the cost — at the price of the model remembering
less of the earlier conversation. When the budget does bite, the log says so:

```
===== REQ #857 | client_msgs=857 sent_msgs=163 dropped=694 | ...
```

`dropped` is how many messages were left behind on that request. If it is
non-zero all the time, raise `max_history_chars` — memory is what you are
buying. Raising it too far brings back the `524`s.

`keep_recent_messages` and `history_user_reserve` decide *what* survives when
the budget bites: the newest messages, and your own instructions. Lowering
`history_user_reserve` keeps more raw tool output but risks the model losing
track of what you asked for — which is the whole point of it existing.

`dump_requests: true` writes every request to `debug_dump/`. It is very useful
while chasing a bug and it **grows to hundreds of MB fast** — turn it back off
afterwards.

---

## Files

| File | Purpose |
|---|---|
| `ccproxy.py` | the proxy — **this is the one you run** |
| `dashboard.py` | the dashboard served at `/` (imported by ccproxy) |
| `config.json` | settings; the API key can go here or in `UPSTREAM_API_KEY` |
| `requirements.txt` | flask + requests |
| `start.sh` / `start.bat` | start the proxy (macOS/Linux · Windows) |
| `run-claude.sh` / `run-claude.bat` | start Claude Code against the proxy |
| `run_tests.sh` / `run-tests.bat` | run the offline test suite |
| `test_offline.py` | the test suite, with its own mock upstream |
| `names_probe.py` | probe that finds which tool names a relay implements natively |
| `agent_proxy.py` | **old version — kept for reference only, use `ccproxy.py`** |
| `ccproxy_log.txt` | the live log (created at runtime) |

---

## License

**PolyForm Noncommercial License 1.0.0** — the full text is in [LICENSE](LICENSE).

**Free to use for:** personal use, hobby projects, study, research, experiment,
teaching, and by charitable, educational, public research, public safety/health,
environmental and government organizations.

**Not permitted without a separate written commercial license:**

* reselling, sublicensing or rebranding this software, or a modified copy of it
* running it as a hosted or paid service (SaaS, paid API, managed deployment)
* embedding it in a commercial product
* using it internally at a for-profit company in support of revenue-generating work
* monetising it with ads or subscriptions

Anyone who receives a copy from you must also receive the license terms and the
`Required Notice:` line — see the [Notices](LICENSE#notices) section.

For a **commercial license**, open an issue or contact
[@abdurrehmandaudi](https://github.com/abdurrehmandaudi).

This project is **source-available, not open source**. The source is public on
purpose: so you can read it, audit it, and check for yourself that a proxy
sitting between you and your API key isn't doing anything hidden.

---

## Notes

* Everything is local. The proxy binds `127.0.0.1`; your relay key never leaves
  your machine except in requests to the relay you configured.
* `agent_proxy.py` is the earlier, simpler proxy. It works for
  `Read`/`Write`/`Edit`/`Bash` but drops many other tool calls and cannot run
  web search. `ccproxy.py` replaces it.
* The model name in `ANTHROPIC_MODEL` is passed through to the relay as-is. Set
  it to whatever model string your relay expects.
