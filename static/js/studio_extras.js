(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
  async function api(path, opts = {}) {
    const response = await fetch(path, opts);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || response.statusText || "Request failed");
    return data;
  }
  const jsonPost = (path, body) => api(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const setText = (id, text) => { const el = $(id); if (el) el.textContent = String(text ?? "—"); };
  const setBar = (id, pct) => { const el = $(id); if (el) el.style.width = `${Math.max(0, Math.min(100, Number(pct) || 0))}%`; };
  const fmtMb = (n) => Number.isFinite(Number(n)) ? `${Math.round(Number(n)).toLocaleString()} MB` : "—";
  const fmtTime = (seconds) => {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };

  /* ── Watchtower ─────────────────────────────────────────────── */
  const watch = { active:false, timer:null, sample:null, diag:null, lastSignature:null, events:[], busy:false };
  function watchSetStatus(text, kind = "idle") {
    setText("#watch-status", text);
    const dot = $("#watch-live-dot");
    if (dot) { dot.classList.toggle("live", kind === "live"); dot.classList.toggle("error", kind === "error"); }
  }
  function watchEvent(kind, detail) {
    const key = `${kind}:${detail}`;
    const last = watch.events[0];
    if (last && last.key === key) return;
    watch.events.unshift({key, kind, detail, at:new Date()});
    watch.events = watch.events.slice(0, 40);
    renderWatchEvents();
  }
  function renderWatchEvents() {
    const box = $("#watch-events"); if (!box) return;
    if (!watch.events.length) { box.innerHTML = '<div class="watch-empty">No state changes observed yet.</div>'; return; }
    box.innerHTML = watch.events.map((e) => `<div class="watch-event"><time>${esc(e.at.toLocaleTimeString([], {hour12:false}))}</time><b>${esc(e.kind)}</b><span>${esc(e.detail)}</span></div>`).join("");
  }
  function snapshotSignature(data) {
    const r = data.runtime || {}, o = data.operations || {};
    return JSON.stringify({ok:!!r.ok, level:r.level, model:r.resident_model || "", warm:!!r.warming, tight:!!r.tight, running:o.running||0, queued:o.queued||0, failed:o.failed||0});
  }
  function noteWatchTransitions(data) {
    const r = data.runtime || {}, o = data.operations || {};
    const signature = snapshotSignature(data);
    if (watch.lastSignature === null) {
      watchEvent("START", `Runtime ${r.ok ? "online" : "offline"} · ${r.resident_model || r.chat_model || "no model"}`);
    } else if (signature !== watch.lastSignature) {
      const prior = watch.sample || {}, pr = prior.runtime || {}, po = prior.operations || {};
      if (!!pr.ok !== !!r.ok) watchEvent("RUNTIME", r.ok ? "Ollama came online" : "Ollama became unreachable");
      if ((pr.resident_model || "") !== (r.resident_model || "")) watchEvent("MODEL", r.resident_model ? `${r.resident_model} is resident` : "No model resident");
      if (!!pr.tight !== !!r.tight) watchEvent("VRAM", r.tight ? "VRAM entered tight range" : "VRAM left tight range");
      if ((po.running||0) !== (o.running||0) || (po.queued||0) !== (o.queued||0)) watchEvent("QUEUE", `${o.running||0} running · ${o.queued||0} queued`);
      if ((po.failed||0) !== (o.failed||0)) watchEvent("TASK", `${o.failed||0} failed / blocked total`);
    }
    watch.lastSignature = signature;
  }
  function renderWatch(data, roundTripMs) {
    const r = data.runtime || {}, sys = data.system || {}, o = data.operations || {}, rag = data.rag || {}, mem = data.memory || {};
    const runtimeCard = $("#watch-runtime")?.closest(".watch-card");
    if (runtimeCard) runtimeCard.dataset.watchLevel = r.ok ? (r.level || "ok") : "bad";
    setText("#watch-runtime", r.ok ? String(r.level || "ONLINE").toUpperCase() : "OFFLINE");
    setText("#watch-runtime-detail", r.line || r.error || "Runtime status unavailable");
    const used = Number(r.vram_used_mb), total = Number(r.vram_mb), vp = total > 0 && Number.isFinite(used) ? used / total * 100 : 0;
    setText("#watch-vram", total ? `${Number.isFinite(used) ? Math.round(used).toLocaleString() : "—"} / ${Math.round(total).toLocaleString()} MB` : "—"); setBar("#watch-vram-bar", vp); setText("#watch-gpu", r.gpu || "GPU");
    setText("#watch-cpu", sys.cpu_percent == null ? "WARMING" : `${Number(sys.cpu_percent).toFixed(1)}%`); setBar("#watch-cpu-bar", sys.cpu_percent || 0); setText("#watch-cpu-detail", `${sys.cpu_count || "—"} logical CPU(s)`);
    setText("#watch-ram", sys.ram_percent == null ? "—" : `${Number(sys.ram_percent).toFixed(1)}%`); setBar("#watch-ram-bar", sys.ram_percent || 0); setText("#watch-ram-detail", `${fmtMb(sys.ram_used_mb)} / ${fmtMb(sys.ram_total_mb)}`);
    setText("#watch-model", r.resident_model || r.chat_model || "NO MODEL"); setText("#watch-model-detail", r.resident_model ? `resident · ${r.quantization || "quant unknown"}` : r.warming ? `warming · ${r.warm_stage || "loading"}` : "configured · idle");
    setText("#watch-queue", `${o.running || 0} RUN · ${o.queued || 0} WAIT`); setText("#watch-queue-detail", `${o.total || 0} total · ${o.failed || 0} failed/blocked`);
    setText("#watch-running", o.running || 0); setText("#watch-queued", o.queued || 0); setText("#watch-failed", o.failed || 0); setText("#watch-completed", o.completed || 0);
    setText("#watch-rag-sources", rag.sources ?? rag.documents ?? 0); setText("#watch-rag-chunks", rag.chunks ?? 0); setText("#watch-memory-notes", mem.notes ?? mem.count ?? 0);
    setText("#watch-latency", `${Math.round(roundTripMs)} ms · srv ${data.sample_ms ?? "—"}`); setText("#watch-sample-age", `UP ${fmtTime(data.uptime_s)}`);
  }
  async function refreshWatch(force = false) {
    if (watch.busy || (!watch.active && !force)) return;
    watch.busy = true; watchSetStatus("Sampling live system state…", "live");
    const start = performance.now();
    try {
      const data = await api("/api/watchtower/status", {cache:"no-store"});
      const ms = performance.now() - start;
      noteWatchTransitions(data); watch.sample = data; renderWatch(data, ms);
      watchSetStatus(`Live · sampled ${new Date().toLocaleTimeString([], {hour12:false})} · ${Math.round(ms)} ms round trip`, "live");
    } catch (err) {
      watchSetStatus(`Watchtower sample failed · ${err.message || err}`, "error"); watchEvent("ERROR", err.message || String(err));
    } finally { watch.busy = false; }
  }
  async function runDiagnostics() {
    const btn = $("#watch-diagnostics"); if (btn) btn.disabled = true;
    setText("#watch-diag-badge", "RUNNING");
    try {
      const data = await api("/api/studio/vitals", {cache:"no-store"}); watch.diag = data;
      const box = $("#watch-checks");
      if (box) box.innerHTML = (data.checks || []).map((row) => `<div class="watch-check" data-level="${esc(row.level || (row.ok ? "ok" : "bad"))}"><i class="watch-check-dot"></i><strong>${esc(row.label || row.code)}</strong><span title="${esc(row.detail || "")}">${esc(row.detail || "")}</span></div>`).join("") || '<div class="watch-empty">No diagnostic checks returned.</div>';
      setText("#watch-diag-badge", data.ok ? `HEALTHY · ${data.summary || "PASS"}` : `${data.critical || 0} CRITICAL · ${data.warnings || 0} WARN`);
      watchEvent("DIAG", data.ok ? (data.summary || "Diagnostics healthy") : `${data.critical || 0} critical · ${data.warnings || 0} warnings`);
    } catch (err) {
      setText("#watch-diag-badge", "FAILED"); watchEvent("DIAG", `Diagnostics failed: ${err.message || err}`);
    } finally { if (btn) btn.disabled = false; }
  }
  function setWatchPolling() {
    clearInterval(watch.timer); watch.timer = null;
    if (watch.active && $("#watch-auto")?.checked) watch.timer = setInterval(() => refreshWatch(false), 4000);
  }
  function activateWatchtower() {
    watch.active = true; refreshWatch(true); if (!watch.diag) runDiagnostics(); setWatchPolling();
  }
  function deactivateWatchtower() { watch.active = false; clearInterval(watch.timer); watch.timer = null; watchSetStatus("Watchtower paused while tab is hidden."); }

  /* ── Sandbox ────────────────────────────────────────────────── */
  const sandbox = { history:[], lastReply:"", lastMeta:null, busy:false, initialized:false };
  function sandboxStatus(text) { setText("#sandbox-status", text); }
  function renderSandbox() {
    const box = $("#sandbox-log"), empty = $("#sandbox-empty"); if (!box) return;
    if (empty) empty.hidden = sandbox.history.length > 0;
    box.querySelectorAll(".sandbox-msg").forEach((n) => n.remove());
    for (const row of sandbox.history) {
      const div = document.createElement("div"); div.className = `sandbox-msg ${row.role}`;
      const head = document.createElement("div"); head.className = "sandbox-msg-head"; head.textContent = row.role === "user" ? "YOU · TRANSIENT" : `SANDBOX · ${row.agent || "AGENT"}`;
      const body = document.createElement("div"); body.textContent = row.content;
      div.append(head, body); box.appendChild(div);
    }
    box.scrollTop = box.scrollHeight;
    const canUse = !!sandbox.lastReply;
    if ($("#sandbox-copy-last")) $("#sandbox-copy-last").disabled = !canUse;
    if ($("#sandbox-promote")) $("#sandbox-promote").disabled = !canUse;
  }
  function modelId(row) { return String(row?.id || row?.name || row?.model || "").trim(); }
  async function initSandbox() {
    if (sandbox.initialized) return; sandbox.initialized = true;
    try {
      const [agentsData, library, state] = await Promise.all([api("/api/matrix/agents?limit=1000"), api("/api/llm/library"), api("/api/state")]);
      const agentSelect = $("#sandbox-agent");
      if (agentSelect) {
        const current = String(state?.settings?.matrix_agent || "cypra");
        agentSelect.innerHTML = (agentsData.agents || []).map((a) => `<option value="${esc(a.slug || a.id || "")}">${esc(a.label || a.name || a.slug || "Agent")} · ${esc(a.slug || "")}</option>`).join("");
        if ([...agentSelect.options].some((o) => o.value === current)) agentSelect.value = current;
      }
      const modelSelect = $("#sandbox-model");
      if (modelSelect) {
        const rows = (library.models || []).filter((m) => modelId(m));
        modelSelect.innerHTML = rows.map((m) => `<option value="${esc(modelId(m))}">${esc(m.label || m.name || m.id || m.model)}</option>`).join("");
        const current = String(library.active?.chat || state?.settings?.ollama_chat_model || state?.settings?.chat_model || "");
        if ([...modelSelect.options].some((o) => o.value === current)) modelSelect.value = current;
        else if (!rows.length) modelSelect.innerHTML = '<option value="">No installed models found</option>';
      }
      sandboxStatus("Ready · transient only");
    } catch (err) { sandboxStatus(`Sandbox configuration failed · ${err.message || err}`); }
  }
  async function runSandbox() {
    if (sandbox.busy) return;
    const input = $("#sandbox-input"), text = String(input?.value || "").trim(); if (!text) { sandboxStatus("Enter a prompt first."); input?.focus(); return; }
    const payload = {
      message:text,
      history:sandbox.history.slice(-20).map(({role,content}) => ({role,content})),
      agent:$("#sandbox-agent")?.value || "",
      model:$("#sandbox-model")?.value || "",
      temperature:Number($("#sandbox-temperature")?.value || 0.7),
      use_rag:!!$("#sandbox-rag")?.checked,
      think_mode:$("#sandbox-think")?.value || "off",
    };
    sandbox.history.push({role:"user", content:text}); sandbox.lastReply = ""; sandbox.lastMeta = null; renderSandbox();
    if (input) input.value = "";
    sandbox.busy = true; const button = $("#sandbox-run"); if (button) { button.disabled = true; button.textContent = "RUNNING…"; }
    sandboxStatus("Running disposable experiment · nothing is being saved to normal chat…");
    try {
      const data = await jsonPost("/api/sandbox/run", payload);
      sandbox.lastReply = String(data.reply || ""); sandbox.lastMeta = data;
      sandbox.history.push({role:"assistant", content:sandbox.lastReply, agent:data.agent || payload.agent}); renderSandbox();
      setText("#sandbox-meta-model", data.model || "—"); setText("#sandbox-meta-agent", data.agent || "—"); setText("#sandbox-meta-time", data.elapsed_ms != null ? `${(Number(data.elapsed_ms)/1000).toFixed(2)} s` : "—"); setText("#sandbox-meta-rag", (data.rag_hits || []).length);
      sandboxStatus(`Complete · transient · ${(data.rag_hits || []).length} RAG hit(s) · not saved`);
    } catch (err) {
      sandbox.history.push({role:"assistant", content:`[Sandbox error] ${err.message || err}`, agent:"SYSTEM"}); renderSandbox(); sandboxStatus(`Run failed · ${err.message || err}`);
    } finally { sandbox.busy = false; if (button) { button.disabled = false; button.textContent = "RUN"; } }
  }
  function resetSandbox() {
    sandbox.history = []; sandbox.lastReply = ""; sandbox.lastMeta = null; renderSandbox();
    setText("#sandbox-meta-model", "—"); setText("#sandbox-meta-agent", "—"); setText("#sandbox-meta-time", "—"); setText("#sandbox-meta-rag", "0"); sandboxStatus("Reset · transient workspace cleared");
  }
  function exportSandbox() {
    const data = {format:"cypra-sandbox-experiment", version:1, exported_at:new Date().toISOString(), config:{agent:$("#sandbox-agent")?.value || "", model:$("#sandbox-model")?.value || "", temperature:Number($("#sandbox-temperature")?.value || .7), think_mode:$("#sandbox-think")?.value || "off", read_rag:!!$("#sandbox-rag")?.checked}, history:sandbox.history};
    const blob = new Blob([JSON.stringify(data,null,2)], {type:"application/json"}); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href=url; a.download=`cypra-sandbox-${new Date().toISOString().replace(/[:.]/g,"-")}.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),5000); sandboxStatus("Experiment exported locally · Cypra state unchanged");
  }
  async function promoteSandbox() {
    if (!sandbox.lastReply) return;
    if (!confirm("Save the LAST Sandbox assistant result into persistent RAG? This is an intentional knowledge write and cannot be undone by Sandbox Reset.")) return;
    const btn = $("#sandbox-promote"); if (btn) btn.disabled = true; sandboxStatus("Promoting last result to persistent RAG…");
    try {
      await jsonPost("/api/rag/chat-knowledge", {text:sandbox.lastReply, role:"assistant", label:`Sandbox · ${sandbox.lastMeta?.agent || "agent"} · ${sandbox.lastMeta?.model || "model"}`}); sandboxStatus("Promoted · last result is now persistent RAG knowledge");
      if (typeof window.showStudioToast === "function") window.showStudioToast("SANDBOX → RAG", "Last assistant result saved as persistent knowledge.", "ok");
    } catch (err) { sandboxStatus(`Promotion failed · ${err.message || err}`); }
    finally { if (btn) btn.disabled = false; }
  }

  function bind() {
    $("#watch-refresh")?.addEventListener("click", () => refreshWatch(true));
    $("#watch-auto")?.addEventListener("change", setWatchPolling);
    $("#watch-diagnostics")?.addEventListener("click", runDiagnostics);
    $("#watch-clear-events")?.addEventListener("click", () => { watch.events=[]; renderWatchEvents(); });
    $("#watch-copy")?.addEventListener("click", async () => { if (!watch.sample) return; try { await navigator.clipboard.writeText(JSON.stringify({watchtower:watch.sample, diagnostics:watch.diag}, null, 2)); watchSetStatus("Latest Watchtower snapshot copied.", "live"); } catch { watchSetStatus("Clipboard access failed.", "error"); } });
    $("#sandbox-temperature")?.addEventListener("input", (e) => setText("#sandbox-temperature-value", Number(e.target.value).toFixed(2)));
    $("#sandbox-run")?.addEventListener("click", runSandbox);
    $("#sandbox-input")?.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runSandbox(); } });
    $("#sandbox-reset")?.addEventListener("click", resetSandbox);
    $("#sandbox-export")?.addEventListener("click", exportSandbox);
    $("#sandbox-copy-last")?.addEventListener("click", async () => { if (!sandbox.lastReply) return; try { await navigator.clipboard.writeText(sandbox.lastReply); sandboxStatus("Last result copied · still transient"); } catch { sandboxStatus("Clipboard access failed"); } });
    $("#sandbox-promote")?.addEventListener("click", promoteSandbox);
    window.addEventListener("cypra:primary-view", (event) => {
      const view = event.detail?.view;
      if (view === "watchtower") activateWatchtower(); else if (watch.active) deactivateWatchtower();
      if (view === "sandbox") initSandbox();
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind, {once:true}); else bind();
})();
