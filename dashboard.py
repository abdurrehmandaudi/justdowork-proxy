# -*- coding: utf-8 -*-
# Copyright (c) 2026 abdurrehmandaudi
# Required Notice: Copyright (c) 2026 abdurrehmandaudi -- justdowork-proxy
# Licensed under the PolyForm Noncommercial License 1.0.0 -- commercial
# use is not permitted without a separate written commercial license.
# See LICENSE or https://polyformproject.org/licenses/noncommercial/1.0.0
r"""dashboard.py -- the live dashboard served by ccproxy at `/`.

ccproxy.py imports this. It lives in its own file so ccproxy.py stays readable,
and so that a broken dashboard cannot take the proxy down (ccproxy falls back to
a plain page if this module fails to import).

The page polls `/stats.json` every 2 seconds. No CDN, no internet access --
everything is inside this one file.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ccproxy dashboard</title>
<style>
  :root{
    --bg:#0e1116; --panel:#161a22; --panel2:#1c212b; --line:#252c38;
    --fg:#e6e9ef; --muted:#8b93a7; --accent:#5b9dff;
    --good:#3ecf8e; --warn:#ffb454; --bad:#ff6b6b; --violet:#a78bfa;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
  a{color:var(--accent);text-decoration:none}
  .wrap{max-width:1180px;margin:0 auto;padding:18px 16px 60px}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;
         justify-content:space-between;padding:14px 16px;background:var(--panel);
         border:1px solid var(--line);border-radius:14px;margin-bottom:14px}
  .brand{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:650}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--good);
       box-shadow:0 0 0 4px rgba(62,207,142,.15)}
  .dot.off{background:var(--bad);box-shadow:0 0 0 4px rgba(255,107,107,.15)}
  .meta{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:var(--muted)}
  .meta b{color:var(--fg);font-weight:600}
  h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
     margin:22px 0 10px;font-weight:650}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:10px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 13px}
  .card .k{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  .card .v{font-size:23px;font-weight:680;margin-top:3px;font-variant-numeric:tabular-nums}
  .card .s{font-size:11.5px;color:var(--muted);margin-top:2px}
  .v.good{color:var(--good)} .v.bad{color:var(--bad)} .v.warn{color:var(--warn)}
  .v.accent{color:var(--accent)} .v.violet{color:var(--violet)}
  .grid2{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}
  @media(max-width:820px){.grid2{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .bar{display:flex;height:26px;border-radius:8px;overflow:hidden;background:var(--panel2);
       border:1px solid var(--line)}
  .bar span{display:block;height:100%;min-width:2px}
  .legend{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:12px;font-size:12.5px}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}
  .legend b{font-variant-numeric:tabular-nums;font-weight:600}
  .legend .m{color:var(--muted)}
  .strip{display:flex;align-items:flex-end;gap:2px;height:56px;margin-top:6px}
  .strip div{flex:1;min-width:2px;border-radius:2px 2px 0 0;background:var(--accent);opacity:.75}
  .strip div.bad{background:var(--bad);opacity:.9}
  .strip div.srv{background:var(--violet)}
  .chips{display:flex;flex-wrap:wrap;gap:7px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
        padding:4px 11px;font-size:12.5px}
  .chip b{font-variant-numeric:tabular-nums}
  .tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
  table{border-collapse:collapse;width:100%;font-size:12.5px;white-space:nowrap}
  th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line)}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
     letter-spacing:.05em;position:sticky;top:0;background:var(--panel)}
  tbody tr:last-child td{border-bottom:none}
  tbody tr:hover{background:var(--panel2)}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .pill{border-radius:999px;padding:1px 8px;font-size:11px;border:1px solid var(--line)}
  .pill.ok{color:var(--good);border-color:rgba(62,207,142,.4)}
  .pill.no{color:var(--bad);border-color:rgba(255,107,107,.4)}
  .pill.st{color:var(--accent);border-color:rgba(91,157,255,.4)}
  .pill.dr{color:var(--warn);border-color:rgba(255,180,84,.4)}
  .muted{color:var(--muted)}
  .err{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
  .empty{color:var(--muted);padding:16px;text-align:center}
  .hint{font-size:12px;color:var(--muted);margin-top:9px}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:8px;padding:5px 12px;font-size:12.5px;cursor:pointer}
  button:hover{border-color:var(--accent);color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="brand">
      <span class="dot" id="dot"></span> ccproxy
      <span class="muted" id="uptime" style="font-weight:400;font-size:13px"></span>
    </div>
    <div class="meta" id="meta"></div>
    <div style="display:flex;gap:8px">
      <button id="pause">Pause</button>
      <button id="reset">Reset</button>
    </div>
  </header>

  <h2>Token spend</h2>
  <div class="cards" id="cards"></div>

  <h2>Where the data goes</h2>
  <div class="grid2">
    <div class="panel">
      <div class="bar" id="bar"></div>
      <div class="legend" id="legend"></div>
      <div class="hint" id="barhint"></div>
      <div class="hint" id="budget"></div>
      <div class="strip" id="strip"></div>
      <div class="hint">Each bar is one request's input (height = tokens).
        <span style="color:var(--violet)">Violet</span> = the request used a web tool,
        <span style="color:var(--bad)">red</span> = failed.</div>
    </div>
    <div class="panel">
      <div class="muted" style="font-size:11.5px;text-transform:uppercase;letter-spacing:.05em">
        Tool usage</div>
      <div class="chips" id="tools" style="margin-top:10px"></div>
      <div class="muted" style="font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;margin-top:16px">
        Disk</div>
      <div class="chips" id="disk" style="margin-top:10px"></div>
    </div>
  </div>

  <h2>Recent requests</h2>
  <div class="tblwrap">
    <table>
      <thead><tr>
        <th>#</th><th>Time</th><th>Kind</th><th class="num">Msgs</th>
        <th class="num">Input</th><th class="num">Output</th><th class="num">Sec</th>
        <th>Tools</th><th class="num">Drops</th><th>Note</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

  <div id="errbox"></div>

</div>

<script>
"use strict";
var paused = false, last = null, tick = 0;

function el(id){ return document.getElementById(id); }

function fmt(n){
  n = Number(n) || 0;
  if (n >= 1e9) return (n/1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n/1e6).toFixed(2) + "M";
  if (n >= 1e4) return (n/1e3).toFixed(1) + "k";
  return String(n);
}
function bytes(n){
  n = Number(n) || 0;
  if (n >= 1073741824) return (n/1073741824).toFixed(2) + " GB";
  if (n >= 1048576) return (n/1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n/1024).toFixed(1) + " KB";
  return n + " B";
}
function uptime(s){
  s = Number(s) || 0;
  var d = Math.floor(s/86400), h = Math.floor(s%86400/3600),
      m = Math.floor(s%3600/60), x = Math.floor(s%60);
  if (d) return d + "d " + h + "h";
  if (h) return h + "h " + m + "m";
  if (m) return m + "m " + x + "s";
  return x + "s";
}
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function sum(o){ var t = 0, k; for (k in o) if (o.hasOwnProperty(k)) t += Number(o[k]) || 0; return t; }

function card(k, v, sub, cls){
  return '<div class="card"><div class="k">' + esc(k) + '</div>' +
         '<div class="v ' + (cls || "") + '">' + v + '</div>' +
         (sub ? '<div class="s">' + sub + '</div>' : "") + '</div>';
}

function render(s){
  last = s;
  var reqs = s.total_reqs || 0, ok = s.ok || 0, fail = s.fail || 0;

  el("dot").className = "dot" + (s.key_set ? "" : " off");
  el("uptime").textContent = "| up " + uptime(s.uptime_s + tick);
  el("meta").innerHTML =
    "<span>upstream <b>" + esc(s.upstream) + "</b></span>" +
    "<span>model <b>" + esc(s.model) + "</b></span>" +
    "<span>key <b>" + (s.key_set ? "set" : "MISSING") + "</b></span>" +
    "<span>avg <b>" + (reqs ? (Number(s.duration||0)/reqs).toFixed(1) : "0") + "s</b></span>";

  var avgIn = reqs ? Math.round((s.in_tok||0)/reqs) : 0;
  var totalTok = (s.in_tok||0) + (s.out_tok||0);
  el("cards").innerHTML =
    card("Requests", fmt(reqs), ok + " ok &middot; " + fail + " failed",
         fail ? "" : "good") +
    card("Total tokens", fmt(totalTok), "input + output, what the relay bills", "accent") +
    card("Input tokens", fmt(s.in_tok), "avg " + fmt(avgIn) + " / request", "accent") +
    card("Output tokens", fmt(s.out_tok), "written by the model", "violet") +
    card("Avoided by trimming", fmt(s.saved_tok),
         "never sent, so never billed", "good") +
    (s.phantom_tok > 0
      ? card("Phantom (relay)", fmt(s.phantom_tok),
             "baseline " + fmt(s.usage_baseline_tokens) + " subtracted", "warn")
      : card("Relay overhead", "~10.4k",
             "every request, inside input", "warn")) +
    card("Tool calls", fmt(sum(s.tool_uses) + sum(s.server_tools)),
         "web " + sum(s.server_tools) + " &middot; client " + sum(s.tool_uses)) +
    card("Dropped calls", fmt(s.drops_total),
         s.drops_total ? "check the log" : "none", s.drops_total ? "bad" : "good") +
    card("Upstream calls", fmt(s.upstream_calls),
         (s.retries_total||0) + " retry", (s.retries_total ? "warn" : "")) +
    // Measured 2026-10-07 with a direct probe: the relay reports a cache read
    // of exactly 10278 tokens whether the cacheable prefix is 1.3k chars or
    // 13.5k, and only on a random share of requests -- so that number is the
    // relay's own hidden prefix, never the conversation's. The card is here so
    // this stays visible instead of being a thing you have to go and measure.
    card("Cache reads", fmt(s.cache_read_tok),
         (s.cache_read_tok
            ? s.cache_hit_reqs + " of " + reqs +
              " requests &middot; a fixed ~10.3k, not your prefix"
            : "none &mdash; the relay ignores the breakpoint"),
         s.cache_read_tok ? "warn" : "");

  // ---- where the data goes ----
  var parts = [
    ["System prompt", s.sys_tok, "#5b9dff"],
    ["Tool definitions", s.tools_tok, "#a78bfa"],
    ["History", s.hist_tok, "#3ecf8e"],
    ["Model output", s.out_tok, "#ffb454"]
  ];
  var tot = 0, i;
  for (i = 0; i < parts.length; i++) tot += Number(parts[i][1]) || 0;
  var bar = "", leg = "";
  for (i = 0; i < parts.length; i++){
    var pct = tot ? (Number(parts[i][1])||0) * 100 / tot : 0;
    if (pct > 0.4) bar += '<span style="width:' + pct.toFixed(2) + '%;background:' + parts[i][2] + '" ' +
                         'title="' + esc(parts[i][0]) + ': ' + fmt(parts[i][1]) + '"></span>';
    leg += '<div><i style="background:' + parts[i][2] + '"></i>' + esc(parts[i][0]) +
           ' <b>' + fmt(parts[i][1]) + '</b> <span class="m">(' + pct.toFixed(1) + '%)</span></div>';
  }
  if (!tot) bar = '<span style="width:100%;background:var(--panel2)"></span>';
  el("bar").innerHTML = bar;
  el("legend").innerHTML = leg;
  el("barhint").textContent = tot
    ? "Total " + fmt(tot) + " tokens. This is what the proxy itself sent upstream."
    : "No requests yet.";
  if (s.usage_baseline_tokens > 0)
    el("barhint").textContent += " The relay's phantom " + fmt(s.usage_baseline_tokens) +
                                  " tokens are shown separately.";

  // ---- how much of the budget each request actually used ----
  var budgetEl = el("budget");
  if (reqs && s.max_history_chars > 0){
    var sentAvg = Math.round(((s.sys_tok||0)+(s.tools_tok||0)+(s.hist_tok||0))/reqs);
    var histAvg = Math.round((s.hist_tok||0)/reqs);
    var rawAvg  = Math.round((s.raw_hist_tok||0)/reqs);
    var limitTok = Math.round(s.max_history_chars/4);
    budgetEl.innerHTML =
      "Average request: <b>" + fmt(sentAvg) + "</b> tokens sent (history window limit ~" +
      fmt(limitTok) + "). History averaged <b>" + fmt(histAvg) + "</b> tokens kept out of <b>" +
      fmt(rawAvg) + "</b> offered &mdash; " +
      (rawAvg > histAvg
        ? "<b>" + fmt(rawAvg - histAvg) + "</b> tokens per request were trimmed away."
        : "nothing needed trimming yet.");
  } else if (budgetEl) {
    budgetEl.innerHTML = "";
  }

  var rec = (s.recent || []).slice(0, 60).reverse();
  var mx = 1;
  for (i = 0; i < rec.length; i++) mx = Math.max(mx, Number(rec[i].in_tok)||0);
  var st = "";
  for (i = 0; i < rec.length; i++){
    var r = rec[i], h = Math.max(2, Math.round((Number(r.in_tok)||0) * 100 / mx));
    var cls = !r.ok ? "bad" : (Number(r.server_tools) ? "srv" : "");
    st += '<div class="' + cls + '" style="height:' + h + '%;" title="#' + r.n + " " +
          esc(r.ts) + " - " + fmt(r.in_tok) + " in / " + fmt(r.out_tok) + ' out"></div>';
  }
  el("strip").innerHTML = st || '<div class="muted" style="height:auto">no data yet</div>';

  // ---- tools ----
  var tc = "", name;
  var all = {};
  for (name in s.tool_uses) if (s.tool_uses.hasOwnProperty(name)) all[name] = s.tool_uses[name];
  for (name in s.server_tools) if (s.server_tools.hasOwnProperty(name))
    all[name] = (all[name]||0) + s.server_tools[name];
  var keys = Object.keys(all).sort(function(a,b){ return all[b]-all[a]; });
  for (i = 0; i < keys.length; i++)
    tc += '<span class="chip">' + esc(keys[i]) + ' <b>' + fmt(all[keys[i]]) + '</b></span>';
  el("tools").innerHTML = tc || '<span class="muted">nothing yet</span>';

  el("disk").innerHTML =
    '<span class="chip">log <b>' + bytes(s.disk.log_bytes) + '</b></span>' +
    '<span class="chip">debug_dump <b>' + bytes(s.disk.dump_bytes) + '</b> ' +
    '<span class="muted">(' + s.disk.dump_files + ' files)</span></span>' +
    '<span class="chip">dumps ' +
    (s.dump_requests ? '<b style="color:var(--warn)">ON</b>' : '<b>off</b>') + '</span>';

  // ---- table ----
  var rows = "";
  var list = (s.recent || []);
  for (i = 0; i < list.length; i++){
    var r = list[i];
    var pills = "";
    if (!r.ok) pills += '<span class="pill no">fail ' + r.status + '</span> ';
    if (r.stream) pills += '<span class="pill st">stream</span> ';
    if (Number(r.server_tools)) pills += '<span class="pill dr">web</span> ';
    if (Number(r.cache_read_tok))
      pills += '<span class="pill dr" title="relay reported a cache read of ' +
               r.cache_read_tok + ' tokens">cache</span> ';
    var nm = (r.names || []).join(", ");
    rows += "<tr>" +
      "<td>" + r.n + "</td>" +
      "<td>" + esc(r.ts) + "</td>" +
      "<td>" + (pills || '<span class="muted">-</span>') + "</td>" +
      '<td class="num">' + r.client_msgs + "&rarr;" + r.sent_msgs + "</td>" +
      '<td class="num">' + fmt(r.in_tok) + "</td>" +
      '<td class="num">' + fmt(r.out_tok) + "</td>" +
      '<td class="num">' + r.dur + "</td>" +
      "<td>" + (nm ? esc(nm) : '<span class="muted">-</span>') + "</td>" +
      '<td class="num">' + (r.drops ? '<span class="pill dr">' + r.drops + "</span>" : "-") + "</td>" +
      "<td>" + (r.note ? esc(r.note) : '<span class="muted">ok</span>') + "</td>" +
      "</tr>";
  }
  el("rows").innerHTML = rows ||
    '<tr><td colspan="10" class="empty">No requests yet. Point Claude Code at this proxy ' +
    '(ANTHROPIC_BASE_URL=http://127.0.0.1:8181) and ask it something.</td></tr>';

  // ---- errors ----
  var errs = s.errors || [];
  var eb = el("errbox");
  if (errs.length){
    var h = '<h2>Recent errors</h2><div class="panel err">';
    for (i = 0; i < errs.length; i++)
      h += '<div style="padding:3px 0">#' + errs[i].n + " " + esc(errs[i].ts) +
           ' <span class="pill no">' + errs[i].status + "</span> " + esc(errs[i].note) + "</div>";
    eb.innerHTML = h + "</div>";
  } else {
    eb.innerHTML = "";
  }
}

function poll(){
  fetch("/stats.json", {cache: "no-store"})
    .then(function(r){ return r.json(); })
    .then(function(s){ render(s); tick = 0; })
    .catch(function(){ el("dot").className = "dot off"; });
}

el("pause").onclick = function(){
  paused = !paused;
  this.textContent = paused ? "Resume" : "Pause";
};
el("reset").onclick = function(){
  if (!confirm("Reset all counters? The recent-requests list will be cleared too.")) return;
  fetch("/stats/reset", {method: "POST"}).then(poll);
};

poll();
setInterval(function(){
  tick++;
  if (!paused) poll();
  else if (last) render(last);     // keep the uptime ticking
}, 2000);
</script>
</body>
</html>
"""
