/**
 * Cypra Matrix Studio UI — local chat, agents, review, runtime, voice, and settings.
 */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const CypraVoice = window.CypraVoice;

  // Memory v1 remains dormant. RAG v2 is a separate, explicit external-file knowledge layer.

  const SESSION_KEY = "cypra.session";
  const LEGACY_SESSION_KEY = "brain.session";
  function loadSessionId() {
    const current = localStorage.getItem(SESSION_KEY);
    if (current) return current;
    const legacy = localStorage.getItem(LEGACY_SESSION_KEY);
    if (legacy) {
      localStorage.setItem(SESSION_KEY, legacy);
      localStorage.removeItem(LEGACY_SESSION_KEY);
    }
    return legacy || null;
  }
  function persistSessionId(sessionId) {
    if (sessionId) localStorage.setItem(SESSION_KEY, sessionId);
    else localStorage.removeItem(SESSION_KEY);
    localStorage.removeItem(LEGACY_SESSION_KEY);
  }

  const state = {
    sessionId: loadSessionId(),
    speakReplies: false,
    busy: false,
    abortController: null,
    generationCanceled: false,
    settings: {},
    sessions: [],
    turnFile: null,
    ragDocuments: [],
    ragActiveSourceId: null,
    ragActiveSource: null,
    thinkPlan: null,
  };


  // Live Settings range controls for controls present in the current UI.
  const RANGE_MAP = [
    ["set-chat-temp", "val-chat-temp"],
    ["set-font-scale", "val-font-scale"],
    ["set-chat-font-scale", "val-chat-font-scale"],
    ["set-ollama-chat-tok", "val-ollama-chat-tok"],
    ["set-ollama-hist", "val-ollama-hist"],
    ["set-tts-rate", "val-tts-rate"],
  ];

  const SECTION_LABELS = {
    ai: "AI & runtime",
    rag: "Knowledge / RAG",
    visuals: "Visuals",
    ui: "App & data",
    plugins: "Plugins",
    all: "all",
  };

  const THEME_IDS = [
    "ember",
    "neural",
    "neon",
    "matrix",
    "aurora",
    "solar",
    "ice",
    "void",
    "toxic",
    "sakura",
    "copper",
    "cobalt",
    "plasma",
    "jade",
    "prism",
    "synthwave",
    "mint",
    "solaris",
    "spectral",
    "crimson",
  ];

  const MATRIX_CATEGORY_ORDER = [
    "AI & Computing", "Security", "Networking & Infrastructure", "Data & Analytics",
    "Science & Medicine", "Engineering & Hardware", "Business & Operations",
    "Finance & Economics", "Legal & Governance", "Creative & Design",
    "Education & Humanities", "Specialized & Other", "CUSTOM",
  ];

  function compareMatrixCategories(a, b) {
    const ai = MATRIX_CATEGORY_ORDER.indexOf(String(a));
    const bi = MATRIX_CATEGORY_ORDER.indexOf(String(b));
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi) || String(a).localeCompare(String(b));
  }

  const APPEARANCE_CACHE_KEY = "cypra.studio.appearance.v1";

  function cachePersistedAppearance(settings) {
    const theme = String(settings?.theme_preset || "").toLowerCase();
    if (!THEME_IDS.includes(theme)) return;
    try {
      localStorage.setItem(APPEARANCE_CACHE_KEY, JSON.stringify({
        theme_preset: theme,
        ui_mode: normalizeUiMode(settings?.ui_mode),
        ui_colors: normalizeUiColors(settings?.ui_colors, theme),
      }));
    } catch (_) {}
  }

  function restorePersistedAppearance() {
    try {
      const cached = JSON.parse(localStorage.getItem(APPEARANCE_CACHE_KEY) || "null");
      const theme = String(cached?.theme_preset || "").toLowerCase();
      if (!THEME_IDS.includes(theme)) return;
      state.settings = { ...state.settings, theme_preset: theme, ui_mode: normalizeUiMode(cached?.ui_mode), ui_colors: cached.ui_colors };
      applyUiTheme(state.settings);
    } catch (_) {}
  }

  // Remove the default-theme flash before asynchronous server hydration finishes.

  // ── boot ─────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", async () => {
    bindCriticalChatControls();
    restorePersistedAppearance();
    buildThemePicker();
    bindUiColorControls();
    bindWizardControls();
    try { bindUi(); } catch (err) { console.error("Optional UI binding failed", err); }
    bindKillLocalhost();
    initPoint2Product();
    await refreshState();
    loadStudioModelLibrary().catch(() => {});
    loadMatrixAgents("").catch(() => {});
    restoreLastSession().catch(() => {});
    applyChatBackground();
    loadPluginAssets().catch(() => {});
    await maybeShowWizard();
    if (state.settings?.llm_provider === "ollama") {
      doWarmModel().catch(() => {});
    }
    welcome();
    updateWelcomeWizardStatus().catch(()=>{});
    dismissPoint2Boot();
    initKeyboardMap();
    bindPrimaryStudioViewTabs();
    const pollOllama = () => {
      api("/api/llm/status").then((st) => {
        state.honesty = st.honesty || null;
        window.__cypraHonesty = state.honesty;
        updateOllamaBanner(!!st.ok, st.hint || st.error, st.honesty);
        const h = st.honesty;
        const setInfo = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
        setInfo("runtime-control-quant", String(h?.quantization || "UNKNOWN").toUpperCase());
        setInfo("runtime-control-plan-b", `${h?.tuning_mode === "plan-b-auto" ? "AUTO" : "MANUAL"} · ${h?.num_batch || "—"}`);
        setInfo("runtime-control-kv", String(h?.kv_cache_quantization || "q8_0").toUpperCase());
        setInfo("runtime-control-flash", h?.flash_attention === false ? "OFF" : "ON");
        setInfo("runtime-control-concurrency", `${h?.max_loaded_models || 1} MODEL · ${h?.num_parallel || 1} REQUEST`);
        if (h?.line) setInfo("runtime-control-note", h.line);
        setInfo("studio-live-loaded-model", String(h?.resident_model || (h?.warming ? `LOADING · ${h?.warm_model || h?.chat_model || "MODEL"}` : "NO MODEL LOADED")).toUpperCase());
        const vram = $("#studio-live-vram");
        if (vram && h && h.vram_mb != null) vram.textContent = `${h.vram_used_mb ?? "—"} / ${h.vram_mb} MB`;
      }).catch(() => updateOllamaBanner(false, "Studio could not reach the local runtime."));
    };
    pollOllama();
    setInterval(pollOllama, 8000);
  });

  function closeTopLevelWorkspaceDialogs() {
    for (const id of ["modal-settings"]) {
      const dlg = $("#" + id);
      if (!dlg) continue;
      try { if (dlg.open && typeof dlg.close === "function") dlg.close(); } catch (_) {}
      dlg.removeAttribute("open");
    }
    document.body.classList.remove("settings-tab-active");
  }

  function syncPrimaryStudioTabs(view) {
    const map = { chat: "studio-view-chat", rag: "studio-view-rag", settings: "btn-studio-settings" };
    Object.entries(map).forEach(([key, id]) => {
      const el = $("#" + id); if (!el) return;
      const active = key === view;
      el.classList.toggle("active", active);
      el.setAttribute("aria-selected", active ? "true" : "false");
      el.tabIndex = active ? 0 : -1;
    });
  }

  function setPrimaryStudioView(view) {
    const safeView = ["chat", "rag", "settings"].includes(view) ? view : "chat";
    closeTopLevelWorkspaceDialogs();
    document.body.classList.toggle("studio-primary-view-chat", safeView === "chat");
    document.body.classList.toggle("studio-primary-view-rag", safeView === "rag");
    document.body.classList.toggle("studio-primary-view-settings", safeView === "settings");
    $("#panel-chat")?.classList.toggle("active", safeView === "chat");
    $("#panel-rag")?.classList.toggle("active", safeView === "rag");
    syncPrimaryStudioTabs(safeView);
    if (safeView === "settings") {
      document.body.classList.add("settings-tab-active");
      openSettings({ asTab: true }).catch((err) => console.error("Settings open failed", err));
    } else if (safeView === "rag") {
      refreshRagStatus().catch((err) => setStatus(`RAG status failed · ${err.message || err}`));
    }
  }

  function bindPrimaryStudioViewTabs() {
    const ids = ["studio-view-chat", "studio-view-rag", "btn-studio-settings"];
    const nodes = ids.map((id) => $("#" + id)).filter(Boolean);
    if (!nodes.length) return;
    if (nodes.every((n) => n.dataset.primaryViewBound === "1")) return;
    nodes.forEach((el) => {
      el.dataset.primaryViewBound = "1";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        const view = {
          "studio-view-chat": "chat",
          "studio-view-rag": "rag",
          "btn-studio-settings": "settings",
        }[el.id];
        setPrimaryStudioView(view);
      });
    });
    setPrimaryStudioView("chat");
  }

  function isTypingTarget(target) {
    const tag = (target && target.tagName) || "";
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable;
  }







  window.showStudioToast = function(title, detail = "", kind = "ok") {
    const stack = $("#studio-toast-stack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `studio-toast ${kind}`;
    el.innerHTML = '<span class="studio-toast-icon"></span><span><span class="studio-toast-title"></span><span class="studio-toast-detail"></span></span><button type="button" class="studio-toast-close" aria-label="Dismiss">×</button>';
    el.querySelector(".studio-toast-icon").textContent = kind === "bad" || kind === "warn" ? "!" : "✓";
    el.querySelector(".studio-toast-title").textContent = String(title || "");
    el.querySelector(".studio-toast-detail").textContent = String(detail || "");
    el.querySelector(".studio-toast-close")?.addEventListener("click", () => el.remove());
    stack.appendChild(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 200); }, 2600);
  };

  function initKeyboardMap() {
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && document.querySelector("#modal-settings[open]") && !isTypingTarget(e.target)) {
        e.preventDefault();
        $("#settings-search")?.focus();
        return;
      }
      if (e.key === "Escape") {
        if (typeof window.__cypraStopReview === "function" && window.__cypraReviewBusy) {
          e.preventDefault();
          window.__cypraStopReview();
          return;
        }
        if ($("#talk-mode-quick")?.checked) stopTalkListen();
        if (state.busy) {
          e.preventDefault();
          stopChatGeneration();
          return;
        }
      }
      if ((e.ctrlKey || e.metaKey) && e.key === ",") {
        e.preventDefault();
        openSettings().catch(() => {});
      }
    });
  }

  function setChatBusy(on) {
    window.__cypraStudioBusy = !!on;
    const send = $("#btn-send");
    const input = $("#chat-input");
    if (send) {
      send.disabled = !!on;
      send.classList.toggle("busy", !!on);
      const lab = send.querySelector(".btn-label");
      if (lab) lab.textContent = on ? "Running" : "Send";
    }
    const stop = $("#btn-stop-chat");
    if (stop) {
      stop.hidden = !on;
      stop.disabled = !on;
      stop.classList.toggle("busy", !!on);
    }
    const thinkQuick = $("#think-mode-quick");
    if (thinkQuick) thinkQuick.disabled = !!on;
    document.dispatchEvent(new CustomEvent("cypra:chat-state"));
  }

  function bindChatComposerEnhancements() {
    const input = $("#chat-input");
    const count = $("#studio-input-count");
    if (!input || input.dataset.enhanced === "1") return;
    input.dataset.enhanced = "1";
    const resize = () => {
      input.style.height = "auto";
      const max = Math.min(Math.max(window.innerHeight * 0.28, 90), 220);
      input.style.height = Math.min(input.scrollHeight, max) + "px";
      input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
      if (count) count.textContent = `${input.value.length.toLocaleString()} chars`;
    };
    input.addEventListener("input", resize);
    input.addEventListener("input", () => { try { localStorage.setItem("cypra.chat.draft", input.value); } catch {} });
    window.addEventListener("resize", resize, { passive: true });
    try { const draft=localStorage.getItem("cypra.chat.draft"); if(draft && !input.value) input.value=draft; } catch {}
    resize();
  }

  function stopChatGeneration() {
    if (!state.busy) return;
    state.generationCanceled = true;
    try { state.abortController.abort(); } catch (_) {}
    state.abortController = null;
    setChatBusy(false);
    setStatus("Generation stopped · current partial reply kept");
  }

  async function clearCurrentChat() {
    if (state.settings?.confirm_destructive !== false) {
      if (!confirm("Clear this chat?\n\nOnly this conversation is reset.")) {
        return;
      }
    }
    try {
      const data = await api("/api/sessions/new", { method: "POST" });
      state.sessionId = data.session_id || null;
      persistSessionId(state.sessionId);
    } catch (_) {
      state.sessionId = null;
      persistSessionId(null);
    }
    const log = $("#chat-log");
    if (log) log.innerHTML = "";
    ensureChatEmpty();
    resetThinkTty();
    welcome();
    setStatus("New chat");
  }

  function welcome() {
    ensureChatEmpty();
  }

  function ensureChatEmpty() {
    const log = $("#chat-log");
    if (!log) return;
    let empty = $("#chat-empty");
    if (!empty) {
      empty = document.createElement("div");
      empty.id = "chat-empty";
      empty.className = "chat-empty";
      empty.innerHTML = "<strong>Ready.</strong> Type a message · <kbd>Enter</kbd> sends · <kbd>Esc</kbd> stops · <b>Plain</b> for conversation · <b>Review</b> for a local file.";
      log.prepend(empty);
    }
    updateChatEmpty();
  }
  function bindCriticalChatControls() {
    const send = $("#btn-send");
    if (send && send.dataset.chatBound !== "1") {
      send.dataset.chatBound = "1";
      send.disabled = false;
      send.addEventListener("click", sendChat);
    }
    const stop = $("#btn-stop-chat");
    if (stop && stop.dataset.chatBound !== "1") {
      stop.dataset.chatBound = "1";
      stop.addEventListener("click", stopChatGeneration);
    }
    const input = $("#chat-input");
    if (input && input.dataset.chatSendBound !== "1") {
      input.dataset.chatSendBound = "1";
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendChat();
        }
      });
    }
    window.sendChat = sendChat;
  }

  function bindUi() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".tab").forEach((t) => t.classList.remove("active"));
        $$(".tab-panel").forEach((panel) => panel.classList.remove("active"));
        tab.classList.add("active");
        $(`#panel-${tab.dataset.tab}`)?.classList.add("active");
      });
    });

    $("#studio-live-line")?.addEventListener("click", (e) => {
      if (e.target?.closest?.(".studio-turn-file-clear")) {
        e.preventDefault();
        clearTurnFile();
        setStatus("One-turn file cleared");
      }
    });
    $("#btn-chat-bg")?.addEventListener("click", () => {
      const picker = $("#chat-bg-file");
      if (picker?.showPicker) { try { picker.showPicker(); return; } catch (_) {} }
      picker?.click();
    });
    $("#chat-bg-file")?.addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      try { if (file) await setChatBackgroundFile(file); } catch (err) { setStatus(err.message || err); }
      e.target.value = "";
    });
    $("#chat-bg-strength")?.addEventListener("input", () => {
      localStorage.setItem("cypra.chat.bgStrength", $("#chat-bg-strength").value);
      const out = $("#chat-bg-strength-val");
      if (out) out.textContent = `${$("#chat-bg-strength").value}%`;
      applyChatBackground();
    });
    $("#chat-bg-clear")?.addEventListener("click", async () => {
      try { await api("/api/chat-background", { method: "DELETE" }); applyChatBackground(); setStatus("Chat background cleared"); }
      catch (e) { setStatus(e.message || e); }
    });
    $("#chat-log")?.addEventListener("dragover", (e) => {
      if ([...(e.dataTransfer?.items || [])].some((it) => it.kind === "file" && String(it.type || "").startsWith("image/"))) {
        e.preventDefault(); e.dataTransfer.dropEffect = "copy";
      }
    });
    $("#chat-log")?.addEventListener("drop", async (e) => {
      const file = [...(e.dataTransfer?.files || [])].find((f) => String(f.type || "").startsWith("image/"));
      if (!file) return;
      e.preventDefault();
      try { await setChatBackgroundFile(file); } catch (err) { setStatus(err.message || err); }
    });

    bindCriticalChatControls();
    bindChatComposerEnhancements();

    $("#settings-cancel")?.addEventListener("click", closeSettingsModal);
    $("#settings-save")?.addEventListener("click", saveSettings);
    $("#btn-export-config")?.addEventListener("click", () => exportStudioConfig());
    $("#btn-import-config")?.addEventListener("click", () => $("#config-import-file")?.click());
    $("#config-import-file")?.addEventListener("change", (event) => {
      const file = event.target.files?.[0];
      if (file) importStudioConfigFile(file);
    });
    $("#modal-settings")?.addEventListener("cancel", (e) => { e.preventDefault(); closeSettingsModal(); });
    $("#form-settings")?.addEventListener("submit", (e) => { e.preventDefault(); saveSettings(); });
    enableSettingsDrag();
    $("#set-provider")?.addEventListener("change", () => {
      toggleProviderBlocks($("#set-provider").value);
    });
    $("#think-tty-hide")?.addEventListener("click", () => { if ($("#think-tty")) $("#think-tty").hidden = true; if ($("#think-tty-show")) $("#think-tty-show").hidden = false; });
    $("#think-tty-show")?.addEventListener("click", () => { if ($("#think-tty")) $("#think-tty").hidden = false; if ($("#think-tty-show")) $("#think-tty-show").hidden = true; });

    bindMatrixUi();
    bindRagUi();
    updateStudioChatSnapshot();
    bindStudioInlineSettings();
    $("#btn-install-matrix")?.addEventListener("click", installMatrixRuntime);
    $("#btn-install-daily")?.addEventListener("click", () => installBaseKit("daily"));
    $("#btn-install-quality")?.addEventListener("click", () => installBaseKit("quality"));
    $("#btn-update-models")?.addEventListener("click", updateInstalledModels);
    $("#btn-refresh-recommend")?.addEventListener("click", loadBaseRecommendations);
    $("#btn-pull-named")?.addEventListener("click", pullNamedModel);
    $("#btn-pull-cancel")?.addEventListener("click", async () => {
      try { const r = await api("/api/llm/pull/cancel", { method: "POST" }); setStatus(r.model ? `Cancelling pull · ${r.model}` : "Cancelling pull"); watchPullProgress(); }
      catch (e) { setStatus(e.message || "Could not cancel pull"); }
    });

    $("#btn-plugin-github")?.addEventListener("click", () => installPluginGithub().catch((e) => setPluginStatus(e.message || e, true)));
    $("#btn-plugin-local")?.addEventListener("click", () => installPluginLocal().catch((e) => setPluginStatus(e.message || e, true)));
    $("#btn-plugin-example")?.addEventListener("click", () => installPluginExample().catch((e) => setPluginStatus(e.message || e, true)));
    $("#btn-plugin-refresh")?.addEventListener("click", () => refreshPlugins().catch(() => {}));

    $("#set-theme")?.addEventListener("change", () => applyThemePreset($("#set-theme").value, { syncForm: true, preview: true, forceDefaults: true }));
    $("#set-ui-mode")?.addEventListener("change", async () => {
      const mode = applyUiMode($("#set-ui-mode")?.value, { syncForm: true });
      state.settings = { ...(state.settings || {}), ui_mode: mode };
      cachePersistedAppearance(state.settings);
      try {
        const data = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ui_mode: mode }) });
        if (data?.settings) state.settings = { ...(state.settings || {}), ...data.settings };
        cachePersistedAppearance(state.settings);
        setStatus(`UI mode · ${mode === "modern" ? "Modern" : "Classic"}`);
      } catch (err) {
        setStatus(`UI mode save failed · ${err.message || err}`);
      }
    });
    $$(".stab").forEach((tab) => {
      tab.addEventListener("click", () => {
        $$(".stab").forEach((t) => t.classList.remove("active"));
        $$(".stab-panel").forEach((panel) => panel.classList.remove("active"));
        tab.classList.add("active");
        $(`#stab-${tab.dataset.stab}`)?.classList.add("active");
        if ($("#settings-search")?.value.trim()) filterSettingsSearch($("#settings-search").value);
      });
    });
    $("#settings-search")?.addEventListener("input", (e) => filterSettingsSearch(e.target.value || ""));
    RANGE_MAP.forEach(([id, valueId]) => {
      const el = $(`#${id}`); if (!el) return;
      const sync = () => { const out = $(`#${valueId}`); if (out) out.textContent = el.value; };
      el.addEventListener("input", () => { sync(); livePreviewSettings(); });
      sync();
    });
    $$(".btn-reset-section").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const section = btn.dataset.section || "all";
        const label = SECTION_LABELS[section] || section;
        if (!confirm(section === "all" ? "Reset ALL settings to defaults? Your API key is kept." : `Reset ${label} settings to defaults?`)) return;
        try {
          const data = await api("/api/settings/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ section }) });
          state.settings = data.settings || {};
          fillSettingsForm(state.settings); applyUiTheme(state.settings);
          setStatus(section === "all" ? "All settings reset (API key kept)" : `${label} settings reset to defaults`);
        } catch (e) { alert(e.message); }
      });
    });

    const mic = $("#btn-mic");
    if (mic) {
      mic.addEventListener("mousedown", startPtt);
      mic.addEventListener("mouseup", stopPtt);
      mic.addEventListener("mouseleave", () => { if (window.CypraVoice?.isRecording?.()) stopPtt(); });
      mic.addEventListener("touchstart", (e) => { e.preventDefault(); startPtt(); });
      mic.addEventListener("touchend", (e) => { e.preventDefault(); stopPtt(); });
    }
    window.addEventListener("cypra:tts-state", (event) => setSpeakStopVisible(["synthesizing", "playing"].includes(event.detail?.state)));
  }

  // ── POINT 2 product layer ───────────────────────────────────────
  let point2BootDone = false;
  let point2BootFailsafe = null;

  async function revealPoint2BootArt(art, onProgress) {
    if (!art) return;
    const source = art.textContent || "";
    if (!source.trim()) return;
    const lines = source.replace(/\r/g, "").split("\n");
    art.textContent = "";
    art.classList.add("is-typing");
    const step = lines.length > 80 ? 3 : 2;
    for (let i = 0; i < lines.length; i += step) {
      if (point2BootDone) break;
      const batch = lines.slice(i, Math.min(i + step, lines.length)).join("\n");
      art.textContent += (i ? "\n" : "") + batch;
      if (typeof onProgress === "function") onProgress(i / Math.max(1, lines.length));
      await new Promise((r) => setTimeout(r, 18));
    }
    art.classList.remove("is-typing");
    art.classList.add("is-locked");
  }

  async function initPoint2Product() {
    const splash = $("#boot-splash");
    const art = $("#boot-art");
    const stage = $("#boot-stage");
    const sub = $("#boot-substage");
    const fill = $("#boot-progress-fill");
    const skip = $("#boot-skip");

    const setBoot = (pct, title, detail) => {
      if (fill) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      if (stage) stage.textContent = title;
      if (sub) sub.textContent = detail;
    };

    const skipBoot = () => dismissPoint2Boot();
    skip?.addEventListener("click", (e) => { e.stopPropagation(); skipBoot(); });
    const onKey = (e) => {
      if (e.key === "Escape") skipBoot();
    };
    document.addEventListener("keydown", onKey, { once: true });

    if (point2BootFailsafe) clearTimeout(point2BootFailsafe);
    point2BootFailsafe = setTimeout(() => dismissPoint2Boot(), 12000);

    try {
      splash?.classList.add("is-live");
      setBoot(6, "CYPRA MATRIX", "Loading boot.txt");
      if (splash && art && !art.textContent.trim()) {
        try {
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 2500);
          const r = await fetch("/static/boot.txt?v=392", {
            cache: "no-store",
            signal: controller.signal,
          });
          clearTimeout(timeout);
          if (r.ok) art.textContent = await r.text();
        } catch (_) {}
      }
      setBoot(12, "CYPRA MATRIX", "Rendering local boot art");
      await revealPoint2BootArt(art, (p) => setBoot(12 + p * 48, "CYPRA MATRIX", "Rendering local boot art"));
      if (point2BootDone) return;
      setBoot(66, "Checking Runtime…", "Project-local Ollama · preloading chat model");
      try {
        await Promise.race([
          fetch("/api/health", { cache: "no-store" }),
          new Promise((r) => setTimeout(r, 1200)),
        ]);
      } catch (_) {}
      try { fetch("/api/llm/warm", { method: "POST" }); } catch (_) {}
      if (point2BootDone) return;
      setBoot(82, "Loading Agent Deck…", "Local Modelfile roster");
      await new Promise((r) => setTimeout(r, 280));
      setBoot(100, "CYPRA MATRIX READY", "Local-first multi-agent studio");
      await new Promise((r) => setTimeout(r, 420));
      dismissPoint2Boot();
    } catch (err) {
      console.warn("Point 2 boot recovered:", err);
      dismissPoint2Boot();
    }
  }

  function dismissPoint2Boot() {
    if (point2BootDone) return;
    point2BootDone = true;
    if (point2BootFailsafe) {
      clearTimeout(point2BootFailsafe);
      point2BootFailsafe = null;
    }
    const fill = $("#boot-progress-fill");
    if (fill) fill.style.width = "100%";
    const stage = $("#boot-stage"); if (stage) stage.textContent = "CYPRA MATRIX READY";
    const sub = $("#boot-substage"); if (sub) sub.textContent = "Local-first multi-agent studio";
    setTimeout(() => {
      $("#boot-splash")?.classList.add("hidden");
      $("#chat-input")?.focus();
    }, 900);
  }

  async function updateWelcomeWizardStatus(){
    try{
      const rt=await api("/api/matrix/runtime");
      const tags=await api("/api/llm/library");
      const agents=await api("/api/matrix/status").catch(()=>({count:0}));
      const r=document.querySelector("#wiz-runtime-state"); if(r) r.textContent=rt.ok?"ONLINE":"OFFLINE";
      const m=document.querySelector("#wiz-model-state"); if(m) m.textContent=tags.installed_count?`${tags.installed_count} READY`:"NONE INSTALLED";
      const a=document.querySelector("#wiz-agent-state"); if(a) a.textContent=agents.count?`${agents.count}+ READY`:"CHECKING";
    }catch(_){ }
  }

  // ── API helpers ──────────────────────────────────────────────────
  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = data.detail || data.error || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function updateOllamaBanner(ok, hint, honesty) {
    const bar = $("#studio-runtime-banner");
    if (bar) {
      bar.hidden = true;
      bar.textContent = "";
    }
    const chip = $("#studio-vram-chip");
    const h = honesty || {};
    const line = h.line || hint || "";
    const used = h.vram_used_mb;
    const total = h.vram_mb;
    if (chip) {
      if (ok === false) {
        chip.hidden = false;
        chip.dataset.level = "bad";
        chip.textContent = "OLLAMA DOWN";
        chip.title = line || "Ollama is not running";
      } else if (used != null && total) {
        chip.hidden = false;
        chip.dataset.level = h.level === "warn" || h.tight || h.too_heavy ? "warn" : "ok";
        chip.textContent = `${used}/${total} MB`;
        chip.title = line || `VRAM ${used} / ${total} MB`;
      } else {
        chip.hidden = true;
        chip.textContent = "";
        chip.removeAttribute("data-level");
      }
    }
    const pill = $("#auth-pill");
    if (pill && line) pill.title = line;
  }

  async function refreshState() {
    try {
      const s = await api("/api/state");
      const auth = s.auth || {};
      const pill = $("#auth-pill");
      if (s.llm && typeof s.llm.ok === "boolean") {
        updateOllamaBanner(s.llm.ok, s.llm.hint || s.llm.error, s.llm.honesty);
      }
      if (pill) {
        // The project-local Ollama runtime is authoritative for Studio status.
        // Do not display the local provider auth state as OFFLINE when a local model is actually serving.
        const localActive = s.local?.active_model || s.llm?.chat_model || "";
        const localOnline = s.local?.online !== false;
        const localConfigured = s.llm?.provider === "ollama" || s.llm?.provider === "local" || s.llm?.provider === "hybrid" || !!s.local;
        if (localConfigured && (localOnline || localActive)) {
          pill.textContent = localActive ? `LOCAL · ${localActive}` : "LOCAL · NO MODEL";
          pill.className = localActive ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
          pill.title = localActive ? "Project-local Ollama runtime is online and using this model" : "Project-local Ollama runtime is online; no active model selected";
        } else if (s.llm?.provider === "ollama" || s.llm?.provider === "local") {
          pill.textContent = localActive ? `LOCAL · ${localActive}` : "LOCAL · NO MODEL";
          pill.className = localActive && localOnline ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
          pill.title = localActive ? "Active local Ollama chat model" : "No installed local model is selected";
        } else if (s.llm?.provider === "hybrid") {
          const active = s.local?.active_model || s.llm?.chat_model || "";
          pill.textContent = active ? `LOCAL · ${active}` : "LOCAL · NO MODEL";
          pill.className = active ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
        } else if (auth.ok) {
          pill.textContent = `Local · ${auth.source || "ok"}`;
          pill.className = "pill pill-ok status-online";
        } else if (auth.has_key) {
          pill.textContent = "key invalid";
          pill.className = "pill pill-warn";
        } else {
          pill.textContent = "no API key";
          pill.className = "pill pill-bad status-offline";
        }
      }
      const incomingSettings = s.settings || {};
      const priorAppearance = state.settings || {};
      // Keep the locally restored appearance when an older server payload still
      // reports the Ember defaults; this prevents a startup refresh from erasing
      // a custom theme before the Full Visuals drawer is opened.
      if (String(incomingSettings.theme_preset || "ember").toLowerCase() === "ember" &&
          String(priorAppearance.theme_preset || "").toLowerCase() !== "ember" &&
          priorAppearance.theme_preset) {
        state.settings = { ...incomingSettings, theme_preset: priorAppearance.theme_preset, ui_mode: priorAppearance.ui_mode || incomingSettings.ui_mode, ui_colors: priorAppearance.ui_colors };
      } else {
        state.settings = incomingSettings;
      }
      cachePersistedAppearance(state.settings);
      state.local = s.local || {};
      state.matrix = s.matrix || {};
      window.__cypraSettings = state.settings;
      updateStudioChatSnapshot();
      if (window.CypraVoice?.invalidateProviderCache) {
        CypraVoice.invalidateProviderCache();
      }
      state.speakReplies = !!state.settings.speak_replies;
      applyUiTheme(state.settings);
      setRagToggleState(state.settings.rag_enabled !== false);

      // Prefer the actual local installed model for the top pill.
      const llm = s.llm || {};
      if (llm.provider === "hybrid" && s.local?.active_model) {
        pill.textContent = `LOCAL · ${s.local.active_model}`;
        pill.className = "pill pill-ok active-model-pill status-online";
        pill.title = "Active local Ollama chat model";
      } else if (llm.provider === "hybrid") {
        pill.textContent = "LOCAL · NO MODEL";
        pill.className = "pill pill-warn active-model-pill status-degraded";
        pill.title = "No installed local model is selected";
      } else if (llm.provider === "ollama") {
        const active = s.local?.active_model || "";
        if (active) {
          pill.textContent = `LOCAL · ${active}`;
          pill.className = llm.ok ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
          pill.title = llm.hint || "Ollama";
        } else {
          pill.textContent = "LOCAL · NO MODEL";
          pill.className = "pill pill-warn active-model-pill status-degraded";
          pill.title = llm.hint || "No installed local model";
        }
      } else if (llm.ok) {
        pill.textContent = `Local · ${llm.chat_model || "api"}`;
        pill.className = "pill pill-ok status-online";
      }
      updateMatrixChip(s.matrix || state.settings?.matrix);
      // The local runtime is authoritative for Studio. A healthy /api/tags
      // result means ONLINE even when local provider auth is absent or /api/ps has no
      // currently-loaded model. Keep the top pill from falling back to OFFLINE.
      try {
        const rt = await api("/api/matrix/runtime");
        const rp = $("#auth-pill");
        if (rp && rt.ok) {
          const active = rt.active_model && rt.active_model !== "NO MODEL" ? rt.active_model : (s.local?.active_model || "");
          rp.textContent = active ? `LOCAL · ${active}` : "LOCAL · NO MODEL";
          rp.className = active ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
          rp.title = active ? "Project-local Ollama runtime is online and serving the selected model" : "Project-local Ollama runtime is online; no active model selected";
        }
      } catch (_) {
        // Preserve the last known status instead of claiming OFFLINE during a
        // transient secondary status failure.
      }
    } catch (e) {
      // Do not replace a working local runtime state with an OFFLINE badge just
      // because the general /api/state refresh failed. Probe the project-local
      // runtime directly before changing the top status.
      try {
        const rt = await api("/api/matrix/runtime");
        const rp = $("#auth-pill");
        if (rp && rt.ok) {
          const active = rt.active_model && rt.active_model !== "NO MODEL" ? rt.active_model : "";
          rp.textContent = active ? `LOCAL · ${active}` : "LOCAL · NO MODEL";
          rp.className = active ? "pill pill-ok active-model-pill status-online" : "pill pill-warn active-model-pill status-degraded";
          return;
        }
      } catch (_) {}
      // Keep the current badge/state on secondary API failures.
    }
  }
  function buildThemePicker() {
    const sel = $("#set-theme");
    const gallery = $("#theme-gallery");
    if (!sel) return;
    const list = window.CYPRA_THEMES?.list?.() || THEME_IDS.map((id) => ({ id, name: id, desc: "", background: "#050505", accent: "#ff2a4a" }));
    sel.innerHTML = "";
    for (const theme of list) {
      const option = document.createElement("option");
      option.value = theme.id;
      option.textContent = theme.name;
      sel.appendChild(option);
    }
    if (!gallery) return;
    gallery.innerHTML = "";
    for (const theme of list) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "theme-swatch";
      btn.dataset.theme = theme.id;
      btn.title = `${theme.name || theme.id}${theme.desc ? ` — ${theme.desc}` : ""}`;
      btn.style.background = `linear-gradient(135deg, ${theme.background || "#000"} 40%, ${theme.accent2 || theme.accent || "#fff"} 100%)`;
      btn.innerHTML = `<span class="theme-swatch-dot" style="background:${theme.accent || "#fff"}"></span><span class="theme-swatch-emoji" aria-hidden="true">${theme.emoji || "✦"}</span><span class="theme-swatch-name">${theme.name || theme.id}</span>`;
      btn.addEventListener("click", () => { setVal("#set-theme", theme.id); applyThemePreset(theme.id, { syncForm: true, preview: true, forceDefaults: true }); });
      gallery.appendChild(btn);
    }
  }
  function highlightThemeSwatch(id) {
    $$(".theme-swatch").forEach((button) => button.classList.toggle("active", button.dataset.theme === id));
    const theme = window.CYPRA_THEMES?.get?.(id);
    const desc = $("#theme-desc");
    if (desc && theme) desc.textContent = `${theme.emoji || "✦"} ${theme.name} — ${theme.desc || ""}`;
  }
  function applyThemeUiEmoji(id) {
    const theme = window.CYPRA_THEMES?.get?.(id);
    const emoji = theme?.emoji || "✦";
    document.querySelectorAll("[data-theme-emoji]").forEach((el) => { el.textContent = emoji; el.setAttribute("aria-label", `${theme?.name || id} theme`); });
  }
  function applyThemePreset(name, opts = {}) {
    const id = name || "ember";
    THEME_IDS.forEach((themeId) => document.body.classList.remove("theme-" + themeId));
    document.body.classList.add("theme-" + id);
    applyThemeUiEmoji(id);
    const vars = window.CYPRA_THEMES?.uiVars?.(id) || {};
    for (const [key, value] of Object.entries(vars)) document.documentElement.style.setProperty(key, value);
    if (opts.syncForm || opts.forceDefaults) {
      const colors = uiColorsFromTheme(id);
      colors.enabled = false;
      state.settings = { ...state.settings, theme_preset: id, ui_colors: colors };
      applyUiColorOverrides(colors, id);
      syncUiColorControls(colors);
      cachePersistedAppearance({ theme_preset: id, ui_colors: colors });
      setVal("#set-theme", id);
    }
    highlightThemeSwatch(id);
  }

  const UI_COLOR_KEYS = [
    "background","panel","surface","border","text","muted","accent","accent2",
    "success","warning","danger","chatBackground","userMessage","assistantMessage",
    "thinkingBackground","thinkingText"
  ];
  let uiColorSaveTimer = 0;

  function validHexColor(value) {
    return /^#[0-9a-f]{6}$/i.test(String(value || "").trim());
  }

  function rgbHexFromTriplet(value, fallback = "#9aa7b2") {
    const parts = String(value || "").split(",").map(v => Number.parseInt(v.trim(), 10));
    if (parts.length !== 3 || parts.some(v => !Number.isFinite(v))) return fallback;
    return "#" + parts.map(v => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
  }

  function mixHexColor(a, b, weight = .5) {
    const parse = value => {
      const raw = String(value || "").replace("#", "");
      if (!/^[0-9a-f]{6}$/i.test(raw)) return null;
      const n = Number.parseInt(raw, 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    };
    const ca = parse(a), cb = parse(b);
    if (!ca || !cb) return validHexColor(a) ? String(a) : validHexColor(b) ? String(b) : "#000000";
    const w = Math.max(0, Math.min(1, Number(weight) || 0));
    return "#" + ca.map((v, i) => Math.round(v * w + cb[i] * (1 - w)).toString(16).padStart(2, "0")).join("");
  }
  function uiColorsFromTheme(themeId) {
    const id = themeId || state.settings?.theme_preset || $("#set-theme")?.value || "ember";
    return window.CYPRA_THEMES?.toUiColors?.(id) || {
      enabled: false,
      background: "#050505", panel: "#0a0506", surface: "#12080a", border: "#3d1820",
      text: "#ffe8ec", muted: "#c48a94", accent: "#ff2a4a", accent2: "#ff6b81",
      success: "#39d98a", warning: "#fbbf24", danger: "#ff4d6d",
      chatBackground: "#050505", userMessage: "#1b090d", assistantMessage: "#0d0608",
      thinkingBackground: "#12080a", thinkingText: "#ffe8ec"
    };
  }

  function normalizeUiColors(raw, themeId) {
    const base = uiColorsFromTheme(themeId);
    const incoming = raw && typeof raw === "object" ? raw : {};
    const out = { ...base, ...incoming, enabled: incoming.enabled === true };
    for (const key of UI_COLOR_KEYS) if (!validHexColor(out[key])) out[key] = base[key];
    return out;
  }

  function clearUiColorOverrides() {
    const body = document.body;
    if (!body) return;
    body.classList.remove("ui-custom-colors");
    for (const name of [
      "--bg0","--bg1","--bg2","--bg3","--bg","--panel","--border","--text-muted","--text-dim","--ui-panel","--ui-surface","--glass",
      "--line","--line-strong","--text","--muted","--accent","--accent-rgb","--accent2",
      "--success","--warn","--danger","--pink","--chat-bg","--chat-surface","--chat-user-bg",
      "--chat-assistant-bg","--think-bg","--think-bg-strong","--think-border","--think-text",
      "--think-agent","--think-dot","--accent-wash","--accent-wash-strong","--accent-glow"
    ]) body.style.removeProperty(name);
  }

  function applyUiColorOverrides(raw, themeId) {
    clearUiColorOverrides();
    const colors = normalizeUiColors(raw, themeId);
    if (!colors.enabled || !document.body) return colors;
    const body = document.body;
    const rgb = hex => {
      const n = Number.parseInt(String(hex).slice(1), 16);
      return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
    };
    body.classList.add("ui-custom-colors");
    const vars = {
      "--bg0": colors.background,
      "--bg1": colors.panel,
      "--bg2": colors.surface,
      "--bg3": colors.surface,
      "--bg": colors.background,
      "--panel": colors.panel,
      "--border": colors.border,
      "--text-muted": colors.muted,
      "--text-dim": colors.muted,
      "--ui-panel": colors.panel,
      "--ui-surface": colors.surface,
      "--glass": colors.panel,
      "--line": colors.border,
      "--line-strong": colors.border,
      "--text": colors.text,
      "--muted": colors.muted,
      "--accent": colors.accent,
      "--accent-rgb": rgb(colors.accent),
      "--accent2": colors.accent2,
      "--success": colors.success,
      "--warn": colors.warning,
      "--danger": colors.danger,
      "--pink": colors.accent2,
      "--chat-bg": colors.chatBackground,
      "--chat-surface": colors.assistantMessage,
      "--chat-user-bg": colors.userMessage,
      "--chat-assistant-bg": colors.assistantMessage,
      "--think-bg": colors.thinkingBackground,
      "--think-bg-strong": colors.surface,
      "--think-border": colors.border,
      "--think-text": colors.thinkingText,
      "--think-agent": colors.thinkingText,
      "--think-dot": colors.accent,
      "--accent-wash": `color-mix(in srgb, ${colors.accent} 7%, transparent)`,
      "--accent-wash-strong": `color-mix(in srgb, ${colors.accent} 15%, transparent)`,
      "--accent-glow": `color-mix(in srgb, ${colors.accent} 28%, transparent)`,
    };
    for (const [key, val] of Object.entries(vars)) body.style.setProperty(key, val);
    return colors;
  }

  function syncUiColorControls(raw) {
    const colors = normalizeUiColors(raw, state.settings?.theme_preset);
    const toggle = $("#set-ui-colors-enabled");
    if (toggle) toggle.checked = !!colors.enabled;
    for (const key of UI_COLOR_KEYS) {
      const input = document.querySelector(`[data-ui-color="${key}"]`);
      const output = document.querySelector(`[data-ui-color-value="${key}"]`);
      if (input) input.value = colors[key];
      if (output) output.textContent = String(colors[key]).toUpperCase();
    }
  }

  function collectUiColorsFromForm() {
    const base = normalizeUiColors(state.settings?.ui_colors, state.settings?.theme_preset);
    const enabled = $("#set-ui-colors-enabled")?.checked === true;
    const out = { ...base, enabled };
    for (const key of UI_COLOR_KEYS) {
      const value = document.querySelector(`[data-ui-color="${key}"]`)?.value;
      if (validHexColor(value)) out[key] = value.toLowerCase();
    }
    return out;
  }

  function persistUiColors(colors) {
    cachePersistedAppearance({ ...(state.settings || {}), ui_colors: colors });
    clearTimeout(uiColorSaveTimer);
    uiColorSaveTimer = setTimeout(async () => {
      try {
        const data = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ui_colors: colors }),
        });
        if (data?.settings) state.settings = { ...(state.settings || {}), ...data.settings };
      } catch (_) {}
    }, 220);
  }

  function setUiColors(colors, { persist = true } = {}) {
    const normalized = normalizeUiColors(colors, state.settings?.theme_preset);
    state.settings = { ...(state.settings || {}), ui_colors: normalized };
    syncUiColorControls(normalized);
    applyUiColorOverrides(normalized, state.settings?.theme_preset);
    if (persist) persistUiColors(normalized);
    return normalized;
  }

  function bindUiColorControls() {
    if (document.body?.dataset.uiColorsBound === "1") return;
    if (document.body) document.body.dataset.uiColorsBound = "1";
    document.addEventListener("input", event => {
      const input = event.target?.closest?.("[data-ui-color]");
      if (!input) return;
      const key = input.dataset.uiColor;
      const output = document.querySelector(`[data-ui-color-value="${key}"]`);
      if (output) output.textContent = String(input.value || "").toUpperCase();
      const colors = collectUiColorsFromForm();
      colors.enabled = true;
      const toggle = $("#set-ui-colors-enabled"); if (toggle) toggle.checked = true;
      setUiColors(colors);
    });
    $("#set-ui-colors-enabled")?.addEventListener("change", () => setUiColors(collectUiColorsFromForm()));
    $("#ui-colors-load-theme")?.addEventListener("click", () => {
      const colors = uiColorsFromTheme($("#set-theme")?.value || state.settings?.theme_preset || "ember");
      colors.enabled = true;
      setUiColors(colors);
      setStatus("UI colors loaded from current theme");
    });
    $("#ui-colors-use-theme")?.addEventListener("click", () => {
      const colors = uiColorsFromTheme($("#set-theme")?.value || state.settings?.theme_preset || "ember");
      colors.enabled = false;
      setUiColors(colors);
      applyUiTheme(state.settings || {});
      setStatus("UI returned to theme colors");
    });
  }
  function normalizeUiMode(value) {
    return String(value || "classic").trim().toLowerCase() === "modern" ? "modern" : "classic";
  }
  function applyUiMode(value, { syncForm = false } = {}) {
    const mode = normalizeUiMode(value);
    document.body.classList.remove("ui-mode-classic", "ui-mode-modern");
    document.body.classList.add(`ui-mode-${mode}`);
    document.documentElement.dataset.uiMode = mode;
    if (syncForm) setVal("#set-ui-mode", mode);
    const desc = $("#ui-mode-desc");
    if (desc) desc.textContent = mode === "modern"
      ? "Modern · softer surfaces, cleaner typography, and reduced visual noise."
      : "Classic · terminal-forward Matrix Studio styling.";
    $$(".ui-mode-preview-card").forEach((card) => card.classList.toggle("active", card.classList.contains(`ui-mode-preview-${mode}`)));
    return mode;
  }
  function applyUiTheme(settings) {
    if (!settings) return;
    applyThemePreset(settings.theme_preset || "ember", { syncForm: false, preview: false });
    applyUiMode(settings.ui_mode, { syncForm: false });
    const fontScale = Number(settings.ui_font_scale);
    if (Number.isFinite(fontScale) && fontScale > 0) {
      document.documentElement.style.setProperty("--ui-font-scale", String(fontScale));
      document.documentElement.style.fontSize = `${Math.round(15 * fontScale)}px`;
    }
    const chatFontScale = Number(settings.chat_font_scale);
    if (Number.isFinite(chatFontScale) && chatFontScale > 0) document.documentElement.style.setProperty("--chat-font-scale", String(chatFontScale));
    document.body.classList.toggle("density-compact", settings.ui_density === "compact");
    document.body.classList.toggle("reduce-motion", !!settings.reduce_motion);
    applyUiColorOverrides(settings.ui_colors, settings.theme_preset || "ember");
    highlightThemeSwatch(settings.theme_preset || "ember");
  }

  async function doWarmModel() {
    setStatus("Preparing Ollama model…");
    try {
      const kick = await api("/api/llm/warm", { method: "POST" });
      if (kick.error) {
        setStatus(`Warm error: ${kick.error}`);
        return;
      }
      const target = kick.chat_model || "current model";
      setStatus(`Loading ${target}… BV stays responsive`);
      const started = Date.now();
      for (;;) {
        const st = await api("/api/llm/warm/status");
        if (!st.running) {
          if (st.ok) {
            const sec = Math.max(0, Math.round((Date.now() - started) / 1000));
            setStatus(
              `Warm · ${st.chat_model || target} · ctx ${Number(st.num_ctx || state.settings?.ollama_num_ctx || 8192).toLocaleString()}` +
              `${st.done_reason ? ` · ${st.done_reason}` : ""}` +
              ` · ${sec}s`
            );
          } else if (st.ok === false) {
            setStatus(`Warm failed: ${st.error || "unknown"} · ${st.chat_model || target}`);
          }
          break;
        }
        const elapsed = Number(st.elapsed_s || 0);
        const stage = st.stage === "preparing" ? "freeing VRAM" : "loading model";
        setStatus(`Warm · ${st.chat_model || target} · ${stage} · ${Math.floor(elapsed)}s`);
        await new Promise((r) => setTimeout(r, 1200));
      }
    } catch (e) {
      setStatus("Warm error: " + (e.message || e));
    }
  }

  // ── Plugins bus + install ──────────────────────────────────────────
  window.BV = window.BV || {
    on(ev, fn) {
      window.addEventListener("bv:" + ev, (e) => fn(e.detail));
    },
    emit(ev, detail) {
      window.dispatchEvent(new CustomEvent("bv:" + ev, { detail }));
    },
  };
  const _pluginAssetsLoaded = new Set();

  function setPluginStatus(msg, isErr) {
    const el = $("#plugin-install-status");
    if (el) {
      el.textContent = msg || "";
      el.style.color = isErr ? "#ff8fa3" : "";
    }
    if (msg) setStatus(msg);
  }

  function renderPluginList(plugins) {
    const el = $("#plugin-list");
    if (!el) return;
    const list = Array.isArray(plugins) ? plugins : [];
    if (!list.length) {
      el.innerHTML =
        '<p class="muted">No plugins installed. Try <strong>Install example</strong> or paste a GitHub repo.</p>';
      return;
    }
    el.innerHTML = "";
    for (const p of list) {
      const card = document.createElement("div");
      card.className = "plugin-card" + (p.enabled ? "" : " disabled");
      const head = document.createElement("header");
      const title = document.createElement("div");
      title.innerHTML = `<span class="pname">${esc(p.name || p.id)}</span><span class="pver">v${esc(
        p.version || "?"
      )}</span>`;
      const badge = document.createElement("span");
      badge.className = "stat-chip";
      badge.textContent = p.enabled ? "on" : "off";
      head.appendChild(title);
      head.appendChild(badge);
      card.appendChild(head);
      if (p.description) {
        const d = document.createElement("div");
        d.className = "pdesc";
        d.textContent = p.description;
        card.appendChild(d);
      }
      const meta = document.createElement("div");
      meta.className = "pmeta";
      meta.textContent = [
        p.id,
        p.author ? `by ${p.author}` : "",
        p.source || "",
        p.has_js ? "js" : "",
        p.has_css ? "css" : "",
        p.has_python ? "py" : "",
      ]
        .filter(Boolean)
        .join(" · ");
      card.appendChild(meta);
      const actions = document.createElement("div");
      actions.className = "pactions";
      const btnEn = document.createElement("button");
      btnEn.type = "button";
      btnEn.className = "btn ghost sm";
      btnEn.textContent = p.enabled ? "Disable" : "Enable";
      btnEn.addEventListener("click", () => {
        togglePlugin(p.id, !p.enabled).catch((e) =>
          setPluginStatus(e.message || e, true)
        );
      });
      const btnRm = document.createElement("button");
      btnRm.type = "button";
      btnRm.className = "btn danger sm";
      btnRm.textContent = "Remove";
      btnRm.addEventListener("click", () => {
        if (
          !confirm(
            `Remove plugin "${p.name || p.id}"?\n\nDeletes files under data/plugins/${p.id}/`
          )
        ) {
          return;
        }
        removePlugin(p.id).catch((e) => setPluginStatus(e.message || e, true));
      });
      actions.appendChild(btnEn);
      actions.appendChild(btnRm);
      if (p.homepage) {
        const a = document.createElement("a");
        a.className = "btn ghost sm";
        a.href = p.homepage;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = "GitHub";
        actions.appendChild(a);
      }
      card.appendChild(actions);
      el.appendChild(card);
    }
  }

  async function refreshPlugins() {
    const data = await api("/api/plugins");
    renderPluginList(data.plugins || []);
    await loadPluginAssets(data.assets || []);
    return data;
  }

  async function loadPluginAssets(assets) {
    let list = assets;
    if (!list) {
      try {
        const data = await api("/api/plugins");
        list = data.assets || [];
        if ($("#plugin-list")) renderPluginList(data.plugins || []);
      } catch (_) {
        return;
      }
    }
    for (const a of list || []) {
      if (a.css && !_pluginAssetsLoaded.has("css:" + a.css)) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = a.css;
        link.dataset.plugin = a.id;
        document.head.appendChild(link);
        _pluginAssetsLoaded.add("css:" + a.css);
      }
      if (a.js && !_pluginAssetsLoaded.has("js:" + a.js)) {
        await new Promise((resolve) => {
          const s = document.createElement("script");
          s.src = a.js;
          s.dataset.plugin = a.id;
          s.onload = () => resolve();
          s.onerror = () => resolve();
          document.body.appendChild(s);
          _pluginAssetsLoaded.add("js:" + a.js);
        });
      }
    }
  }

  async function installPluginGithub() {
    const source = ($("#plugin-github-source")?.value || "").trim();
    const ref = ($("#plugin-github-ref")?.value || "").trim() || null;
    if (!source) {
      setPluginStatus("Enter owner/repo or a GitHub URL", true);
      return;
    }
    setPluginStatus("Installing from GitHub…");
    const data = await api("/api/plugins/install-github", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, ref, force: false }),
    });
    renderPluginList(data.plugins || []);
    await loadPluginAssets(data.assets || []);
    setPluginStatus(
      `Installed ${data.plugin?.name || data.plugin?.id || "plugin"}` +
        (data.plugin?.version ? ` v${data.plugin.version}` : "")
    );
  }

  async function installPluginLocal() {
    const path = ($("#plugin-local-path")?.value || "").trim();
    if (!path) {
      setPluginStatus("Enter a local folder or .zip path", true);
      return;
    }
    setPluginStatus("Installing local plugin…");
    const data = await api("/api/plugins/install-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, force: false }),
    });
    renderPluginList(data.plugins || []);
    await loadPluginAssets(data.assets || []);
    setPluginStatus(`Installed ${data.plugin?.name || data.plugin?.id || "plugin"}`);
  }

  async function installPluginExample() {
    setPluginStatus("Installing Hello Status example…");
    const data = await api("/api/plugins/install-example", { method: "POST" });
    renderPluginList(data.plugins || []);
    await loadPluginAssets(data.assets || []);
    setPluginStatus("Example plugin installed · Hello Status");
  }

  async function togglePlugin(id, enabled) {
    const data = await api(`/api/plugins/${encodeURIComponent(id)}/enable`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    renderPluginList(data.plugins || []);
    if (enabled) await loadPluginAssets(data.assets || []);
    setPluginStatus(`${id} ${enabled ? "enabled" : "disabled"}`);
  }

  async function removePlugin(id) {
    const data = await api(`/api/plugins/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
    renderPluginList(data.plugins || []);
    // remove injected tags for this plugin
    const safe = String(id).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    document
      .querySelectorAll(`[data-plugin="${safe}"]`)
      .forEach((n) => n.remove());
    document.getElementById("plugin-hello-status-chip")?.remove();
    setPluginStatus(`Removed ${id}`);
  }

  let _matrixSearchTimer = 0;
  let _matrixRoster = { agents: [], core: [], root: "", count: 0 };
  const _matrixRecent = JSON.parse(localStorage.getItem("cypra.matrix.recent") || "[]");
  let _matrixSelectionRequest = Promise.resolve();

  function bindMatrixUi() {
    const search = $("#set-matrix-search");
    if (search) {
      search.addEventListener("input", () => {
        clearTimeout(_matrixSearchTimer);
        _matrixSearchTimer = setTimeout(
          () => loadMatrixAgents(search.value || ""),
          180
        );
      });
    }
    $("#set-matrix-agent")?.addEventListener("change", async () => {
      const slug = $("#set-matrix-agent").value;
      syncMatrixSelectTooltip($("#set-matrix-agent"));
      try {
        await activateMatrixAgent(slug, { announce: false });
      } catch (e) {
        const fallback = state.settings?.matrix_agent || "cypra";
        if ($("#set-matrix-agent")) $("#set-matrix-agent").value = fallback;
        syncMatrixQuickSelect(fallback);
        setStatus(e.message || "Could not switch Matrix agent");
      }
    });
    $("#set-matrix-enabled")?.addEventListener("change", () => {
      const on = $("#set-matrix-enabled").checked;
      if ($("#matrix-enabled-quick")) $("#matrix-enabled-quick").checked = on;
      updateMatrixChip();
    });
    $("#set-matrix-handoff")?.addEventListener("change", () => {
      const on = !!$("#set-matrix-handoff").checked;
      if (state.settings) state.settings.matrix_handoff = on;
      setStatus(on ? "Agent handoff context on" : "Agent handoff context off");
    });
    $("#matrix-enabled-quick")?.addEventListener("change", async () => {
      const on = !!$("#matrix-enabled-quick").checked;
      if ($("#set-matrix-enabled")) $("#set-matrix-enabled").checked = on;
      try {
        const data = await api("/api/matrix/select", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: on }),
        });
        if (data.settings) {
          state.settings = { ...state.settings, ...data.settings };
        }
        if (data.matrix) state.settings.matrix = data.matrix;
        updateMatrixChip(data.matrix);
        setStatus(on ? "Matrix directives on" : "Matrix directives off");
      } catch (e) {
        setStatus(e.message || "Matrix toggle failed");
      }
    });
    $("#think-mode-quick")?.addEventListener("change", async () => {
      const mode = normalizeThinkMode($("#think-mode-quick")?.value || state.settings?.think_mode || "auto");
      await persistThinkMode(mode, "Chat");
    });
    $("#talk-mode-quick")?.addEventListener("change", () => {
      const on = !!$("#talk-mode-quick").checked;
      const talkButton = $("#btn-tts-test");
      if (talkButton) {
        talkButton.textContent = on ? "Stop talk" : "Start talk";
        talkButton.classList.toggle("primary", on);
      }
      if (on) {
        if ($("#plain-chat-quick")) $("#plain-chat-quick").checked = true;
        state.speakReplies = true;
        state.settings.voice_output_enabled = true;
        state.settings.speak_replies = true;
        state.settings.tts_provider = "edge";
        state.settings.tts_allow_online = true;
        CypraVoice?.invalidateProviderCache?.();
        api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ voice_output_enabled: true, speak_replies: true, tts_provider: "edge", tts_allow_online: true }) }).catch(() => {});
        setStatus("Talk mode on · current agent will converse out loud");
        scheduleTalkListen();
      } else {
        stopTalkListen();
        CypraVoice?.stopSpeak?.({ release: true });
        setStatus("Talk mode off");
      }
    });
    $("#files-mode-quick")?.addEventListener("change", () => {
      const on = !!$("#files-mode-quick").checked;
      const panel = $("#workplace-panel");
      if (panel) panel.hidden = !on;
      if (on) {
        refreshWorkplace();
        setStatus("Files on · " + workplaceSlug() + " workplace");
      } else {
        setStatus("Files off");
      }
    });
    $("#workplace-refresh")?.addEventListener("click", () => refreshWorkplace());
    $("#workplace-open")?.addEventListener("click", async () => {
      try {
        await api("/api/workplace/open", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slug: workplaceSlug() }),
        });
        setStatus("Workplace folder opened");
      } catch (e) { setStatus(e.message || e); }
    });
    $("#plain-chat-quick")?.addEventListener("change", async () => {
      const on = !!$("#plain-chat-quick").checked;
      if ($("#set-plain-chat")) $("#set-plain-chat").checked = on;
      if (state.settings) state.settings.plain_chat = on;
      setStatus(on ? "Plain chat on · natural replies, full length" : "Plain chat off · agent report format");
      try {
        await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plain_chat: on }),
        });
      } catch (e) {
        setStatus(e.message || "Plain chat toggle failed");
      }
    });
    $("#set-plain-chat")?.addEventListener("change", () => {
      const on = !!$("#set-plain-chat").checked;
      if ($("#plain-chat-quick")) $("#plain-chat-quick").checked = on;
    });

    $("#matrix-agent-quick")?.addEventListener("change", async () => {
      const slug = $("#matrix-agent-quick").value;
      syncMatrixSelectTooltip($("#matrix-agent-quick"));
      try {
        await activateMatrixAgent(slug, { announce: false });
      } catch (e) {
        const fallback = state.settings?.matrix_agent || "cypra";
        if ($("#set-matrix-agent")) $("#set-matrix-agent").value = fallback;
        syncMatrixQuickSelect(fallback);
        setStatus(e.message || "Could not switch Matrix agent");
        return;
      }
      const log = $("#workplace-log");
      if (log) log.textContent = "";
      if ($("#files-mode-quick")?.checked) {
        await refreshWorkplace();
        setStatus("Workplace · " + slug);
      } else {
        setStatus("Agent " + slug + " active · directive locked");
      }
    });
    $("#btn-save-agent")?.addEventListener("click", () => saveMatrixAgent());
    $("#btn-save-agent-settings")?.addEventListener("click", () => saveMatrixAgent());
    $("#btn-clear-chat")?.addEventListener("click", () => clearCurrentChat());
    $("#btn-resume-chat")?.addEventListener("click", openResumeChat);
    $("#resume-chat-close")?.addEventListener("click", () => $("#modal-resume-chat")?.close());
    $("#resume-chat-refresh")?.addEventListener("click", loadResumeChats);
    $("#resume-chat-new")?.addEventListener("click", async () => {
      $("#modal-resume-chat")?.close();
      await clearCurrentChat();
    });
    $("#resume-chat-search")?.addEventListener("input", renderResumeChats);
    $("#resume-chat-select-all")?.addEventListener("click", toggleAllResumeChats);
    $("#resume-chat-clear-selection")?.addEventListener("click", clearResumeChatSelection);
    $("#resume-chat-delete-selected")?.addEventListener("click", deleteSelectedChatSessions);
    $("#resume-chat-open-folder")?.addEventListener("click", openChatFolder);
    $("#studio-model-library-refresh")?.addEventListener("click", loadStudioModelLibrary);
    ["#studio-open-start-guide-inline"].forEach(sel=>$(sel)?.addEventListener("click", () => { $("#wizard")?.classList.remove("hidden"); updateWelcomeWizardStatus().catch(() => {}); }));
    $("#resume-chat-filter")?.addEventListener("change", renderResumeChats);
    $("#resume-chat-sort")?.addEventListener("change", renderResumeChats);
  }

  async function saveMatrixAgent() {
    const slug =
      $("#matrix-agent-quick")?.value ||
      $("#set-matrix-agent")?.value ||
      "";
    if (!slug) {
      setStatus("Pick an agent first");
      return;
    }
    try {
      const data = await api("/api/matrix/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: slug, enabled: true, lock: true }),
      });
      if ($("#matrix-enabled-quick")) $("#matrix-enabled-quick").checked = true;
      if ($("#set-matrix-enabled")) $("#set-matrix-enabled").checked = true;
      if ($("#set-matrix-agent")) $("#set-matrix-agent").value = slug;
      if ($("#matrix-agent-quick")) $("#matrix-agent-quick").value = slug;
      if (data.matrix) state.settings.matrix = data.matrix;
      state.settings.matrix_agent = slug;
      state.settings.matrix_enabled = true;
      if (data.settings?.matrix_handoff !== undefined) state.settings.matrix_handoff = !!data.settings.matrix_handoff;
      await previewMatrixAgent(slug);
      await refreshStudioRuntime(false);
      const idx = _matrixRecent.indexOf(slug); if (idx >= 0) _matrixRecent.splice(idx,1); _matrixRecent.unshift(slug); _matrixRecent.splice(12); localStorage.setItem("cypra.matrix.recent", JSON.stringify(_matrixRecent));
      updateMatrixChip(data.matrix);
      setStatus("Saved agent · " + slug + " · directive locked");
    } catch (e) {
      setStatus(e.message || "Could not save agent");
      alert(e.message || "Could not save agent");
    }
  }

  function matrixAgentTooltip(agent) {
    const summary = String(agent?.summary || "").replace(/\s+/g, " ").trim();
    const category = String(agent?.category || (agent?.custom ? "CUSTOM" : "Specialized & Other"));
    const model = String(agent?.from || "Local model");
    return summary
      ? `${summary} | ${category} | ${model}`
      : `${agent?.label || agent?.slug || "Agent"} | ${category} | ${model}`;
  }

  function syncMatrixSelectTooltip(sel) {
    if (!sel) return;
    const option = sel.selectedOptions?.[0];
    sel.title = option?.title || "Select a Matrix agent";
  }

  function syncMatrixQuickSelect(slug) {
    const quick = $("#matrix-agent-quick");
    if (quick && slug) {
      quick.value = slug;
      syncMatrixSelectTooltip(quick);
    }
  }

  function fillMatrixOption(sel, agent, selected, parent = sel) {
    if (!sel || !agent) return;
    const slug = agent.slug || agent.name;
    if (!slug) return;
    const existing = [...sel.options].find((o) => o.value === slug);
    if (existing) {
      existing.title = matrixAgentTooltip(agent);
      if (selected) existing.selected = true;
      return;
    }
    const o = document.createElement("option");
    o.value = slug;
    o.textContent = agent.label || slug;
    o.title = matrixAgentTooltip(agent);
    o.dataset.category = agent.category || (agent.custom ? "CUSTOM" : "Specialized & Other");
    o.selected = !!selected;
    parent.appendChild(o);
  }

  function populateMatrixAgentSelect(sel, data, current) {
    if (!sel) return;
    const agents = [...(data?.agents || [])];
    const bySlug = new Map(agents.map((agent) => [agent.slug || agent.name, agent]));
    const used = new Set();
    const groups = [];
    const top = [];

    for (const slug of data?.core || []) {
      const agent = bySlug.get(slug);
      if (agent && !used.has(slug)) {
        top.push(agent);
        used.add(slug);
      }
    }
    if (top.length) groups.push(["Top Agents", top]);

    const categories = new Map();
    for (const agent of agents) {
      const slug = agent.slug || agent.name;
      if (!slug || used.has(slug)) continue;
      const category = agent.category || (agent.custom ? "CUSTOM" : "Specialized & Other");
      if (!categories.has(category)) categories.set(category, []);
      categories.get(category).push(agent);
    }
    const categoryOrder = [...categories.keys()].sort((a, b) => {
      return compareMatrixCategories(a, b);
    });
    for (const category of categoryOrder) {
      const rows = categories.get(category).sort((a, b) =>
        String(a.label || a.slug || "").localeCompare(String(b.label || b.slug || ""))
      );
      groups.push([category, rows]);
    }

    sel.innerHTML = "";
    for (const [label, rows] of groups) {
      const group = document.createElement("optgroup");
      group.label = label.toUpperCase();
      for (const agent of rows) {
        fillMatrixOption(sel, agent, (agent.slug || agent.name) === current, group);
      }
      sel.appendChild(group);
    }
    if (current && [...sel.options].some((option) => option.value === current)) sel.value = current;
    if (current && ![...sel.options].some((option) => option.value === current)) {
      const selectedGroup = document.createElement("optgroup");
      selectedGroup.label = "SELECTED";
      fillMatrixOption(sel, { slug: current, label: current }, true, selectedGroup);
      sel.insertBefore(selectedGroup, sel.firstChild);
    }
    if (!sel.value && sel.options.length) sel.selectedIndex = 0;
    syncMatrixSelectTooltip(sel);
  }

  function activateMatrixAgent(slug, { announce = true } = {}) {
    if (!slug) return Promise.resolve(null);
    const perform = async () => {
      const data = await api("/api/matrix/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent: slug, enabled: true, lock: true }),
      });
      state.settings = {
        ...(state.settings || {}),
        ...(data.settings || {}),
        matrix_agent: slug,
        matrix_enabled: true,
        matrix_agent_locked: true,
      };
      if (data.matrix) state.settings.matrix = data.matrix;
      if ($("#set-matrix-agent")) $("#set-matrix-agent").value = slug;
      if ($("#matrix-agent-quick")) $("#matrix-agent-quick").value = slug;
      syncMatrixSelectTooltip($("#set-matrix-agent"));
      syncMatrixSelectTooltip($("#matrix-agent-quick"));
      await previewMatrixAgent(slug);
      updateMatrixChip(data.matrix);
      const index = _matrixRecent.indexOf(slug);
      if (index >= 0) _matrixRecent.splice(index, 1);
      _matrixRecent.unshift(slug);
      _matrixRecent.splice(12);
      localStorage.setItem("cypra.matrix.recent", JSON.stringify(_matrixRecent));
      if (announce) setStatus("Agent " + slug + " active and locked");
      document.dispatchEvent(new CustomEvent("cypra:chat-state"));
      return data;
    };
    // Preserve click order. If Lori and Cortana are selected rapidly, the
    // Cortana request is guaranteed to reach the server last and stay active.
    _matrixSelectionRequest = _matrixSelectionRequest.catch(() => null).then(perform);
    return _matrixSelectionRequest;
  }


  async function loadMatrixAgents(query) {
    const sel = $("#set-matrix-agent");
    const quick = $("#matrix-agent-quick");
    try {
      const data = await api(`/api/matrix/agents?q=${encodeURIComponent(query || "")}&limit=1000`);
      _matrixRoster = data;
      const hint = $("#matrix-root-hint");
      if (hint) {
        if (!data.ok) {
          hint.textContent = "No local Matrix folder found. Copy CypraMatrix into this project as MatrixFiles/.";
        } else {
          const ready = data.directive_ready ?? data.shown ?? 0;
          hint.textContent = `LOCAL ROSTER · ${data.count || 0} agents · ${ready}/${data.shown || 0} directives ready${data.query ? ` · ${data.shown || 0} matched` : ""}`;
        }
      }
      const current = $("#set-matrix-agent")?.value || state.settings?.matrix_agent || "cypra";
      if (!query) {
        const quickCurrent = quick?.value || current;
        populateMatrixAgentSelect(sel, data, current);
        populateMatrixAgentSelect(quick, data, quickCurrent);
      }
      renderMatrixCore(data.core || [], current);
      if (current) previewMatrixAgent(current);
    } catch (e) {
      const hint = $("#matrix-root-hint");
      if (hint) hint.textContent = e.message || "Matrix roster failed";
    }
  }

  function renderMatrixCore(core, current) {
    const row = $("#matrix-core-row");
    if (!row) return;
    row.innerHTML = "";
    for (const slug of core || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn ghost sm" + (slug === current ? " active" : "");
      b.textContent = slug;
      b.title = "Use " + slug + " directive";
      b.addEventListener("click", async () => {
        row.querySelectorAll(".btn").forEach((x) => x.classList.toggle("active", x === b));
        try {
          await activateMatrixAgent(slug);
        } catch (e) {
          setStatus(e.message || "Could not switch Matrix agent");
        }
      });
      row.appendChild(b);
    }
  }

  async function previewMatrixAgent(slug) {
    const pre = $("#matrix-directive-preview");
    if (!pre || !slug) return;
    try {
      const data = await api(`/api/matrix/agents/${encodeURIComponent(slug)}`);
      const agent = data.agent || {};
      const directive = agent.directive || "";
      pre.hidden = !directive;
      pre.textContent = directive;
    } catch (e) {
      pre.hidden = false;
      pre.textContent = e.message || "Could not load Matrix directive";
    }
  }

  function updateMatrixChip(info) {
    const host = $("#plugin-status-host");
    if (!host) return;
    let chip = $("#plugin-chip-matrix");
    if (!chip) {
      chip = document.createElement("button");
      chip.type = "button";
      chip.className = "stat-chip plugin-chip";
      chip.id = "plugin-chip-matrix";
      chip.addEventListener("click", () => {
        openSettings().then(() => {
          document.querySelector('.stab[data-stab="ai"]')?.click();
          $("#matrix-settings-card")?.scrollIntoView({ block: "nearest" });
        }).catch((e) => setStatus(e.message || e));
      });
      host.appendChild(chip);
    }
    const mx = info || state.settings?.matrix || {};
    const on = state.settings?.matrix_enabled !== false && mx.ok !== false;
    const slug = mx.agent?.slug || state.settings?.matrix_agent || "cypra";
    const n = mx.count || _matrixRoster.count || 0;
    if (!mx.ok && mx.root == null && n === 0 && info && info.ok === false) {
      chip.textContent = "matrix · missing";
      chip.title = "No local MatrixFiles folder found";
      return;
    }
    chip.textContent = on ? `matrix · ${slug}` : "matrix · off";
    chip.title = mx.root
      ? `Local ${mx.root} · ${n} agents · click for Settings`
      : "CypraMatrix directives · click for Settings";
  }

  const THINK_MODES = new Set(["off", "auto", "standard", "deep"]);
  function normalizeThinkMode(value, fallback = "auto") {
    const mode = String(value || fallback).trim().toLowerCase();
    return THINK_MODES.has(mode) ? mode : fallback;
  }
  function thinkModeLabel(mode) {
    const m = normalizeThinkMode(mode);
    return m === "standard" ? "STANDARD" : m.toUpperCase();
  }
  function syncThinkQuickMode() {
    const mode = normalizeThinkMode(state.settings?.think_mode || "auto");
    const quick = $("#think-mode-quick");
    const settings = $("#studio-settings-think");
    if (quick) quick.value = mode;
    if (settings) settings.value = mode;
  }

  let thinkModeSaveActive = false;
  async function persistThinkMode(value, origin = "Settings") {
    const mode = normalizeThinkMode(value, "auto");
    const previous = normalizeThinkMode(state.settings?.think_mode || "auto");
    const quick = $("#think-mode-quick");
    const settings = $("#studio-settings-think");

    state.settings = { ...(state.settings || {}), think_mode: mode };
    if (quick) quick.value = mode;
    if (settings) settings.value = mode;

    // Prevent overlapping saves from reordering the persisted mode. The selected
    // value is still sent with an in-flight chat turn, so generation cannot race
    // this settings request.
    if (thinkModeSaveActive) {
      if (quick) quick.disabled = true;
      if (settings) settings.disabled = true;
      return;
    }

    thinkModeSaveActive = true;
    if (quick) quick.disabled = true;
    if (settings) settings.disabled = true;
    setStatus(`${origin} Think · saving ${thinkModeLabel(mode)}`);
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ think_mode: mode }),
      });
      const saved = normalizeThinkMode(data.settings?.think_mode || mode);
      state.settings = { ...(state.settings || {}), ...(data.settings || {}), think_mode: saved };
      syncThinkQuickMode();
      setStatus(`Think mode · ${thinkModeLabel(saved)} · persistent`);
    } catch (e) {
      state.settings = { ...(state.settings || {}), think_mode: previous };
      syncThinkQuickMode();
      setStatus(`Think mode save failed · ${e.message || e}`);
    } finally {
      thinkModeSaveActive = false;
      if (quick) quick.disabled = !!state.busy;
      if (settings) settings.disabled = false;
    }
  }

  function currentThinkOverride() {
    return normalizeThinkMode($("#think-mode-quick")?.value || state.settings?.think_mode || "auto");
  }

  function collectStudioSettings() {
    const out = {};
    const put = (key, value) => {
      if (value !== undefined && value !== null && value !== "") out[key] = value;
    };
    if ($("#studio-settings-think")) put("think_mode", normalizeThinkMode($("#studio-settings-think").value));
    if ($("#plain-chat-quick")) put("plain_chat", $("#plain-chat-quick").checked);
    if ($("#matrix-enabled-quick")) put("matrix_enabled", $("#matrix-enabled-quick").checked);
    if ($("#rag-enabled-quick")) put("rag_enabled", $("#rag-enabled-quick").checked);
    if ($("#matrix-agent-quick")?.value) put("matrix_agent", $("#matrix-agent-quick").value);
    if ($("#studio-chat-snapshot-handoff-toggle")) put("matrix_handoff", $("#studio-chat-snapshot-handoff-toggle").checked);
    const bgStrength = Number($("#chat-bg-strength")?.value ?? localStorage.getItem("cypra.chat.bgStrength"));
    if (Number.isFinite(bgStrength)) put("chat_bg_strength", bgStrength);
    return out;
  }
  const STUDIO_CONTEXT_CHOICES = Object.freeze([8192, 16384, 32768, 65536, 131072, 262144]);
  function normalizeStudioContext(value) {
    const requested = Number(value);
    if (!Number.isFinite(requested)) return 8192;
    if (requested <= STUDIO_CONTEXT_CHOICES[0]) return STUDIO_CONTEXT_CHOICES[0];
    if (requested >= STUDIO_CONTEXT_CHOICES[STUDIO_CONTEXT_CHOICES.length - 1]) return STUDIO_CONTEXT_CHOICES[STUDIO_CONTEXT_CHOICES.length - 1];
    if (STUDIO_CONTEXT_CHOICES.includes(requested)) return requested;
    let normalized = STUDIO_CONTEXT_CHOICES[0];
    for (const choice of STUDIO_CONTEXT_CHOICES) {
      if (choice > requested) break;
      normalized = choice;
    }
    return normalized;
  }

  function collectSettingsFromForm() {
    const raw = {
      llm_provider: $("#set-provider")?.value || "ollama",
      chat_model: $("#set-chat-model")?.value || state.settings?.chat_model || "",
      ollama_base_url: $("#set-ollama-url")?.value || state.settings?.ollama_base_url || "http://127.0.0.1:11434",
      ollama_chat_model: state.settings?.ollama_chat_model || undefined,
      ollama_num_ctx: normalizeStudioContext($("#set-ollama-ctx")?.value ?? state.settings?.ollama_num_ctx ?? 8192),
      ollama_keep_alive: $("#set-ollama-keep")?.value || state.settings?.ollama_keep_alive || "-1",
      ollama_num_batch: $("#set-ollama-batch")?.value ? Number($("#set-ollama-batch").value) : null,
      ollama_chat_tokens: Number.isFinite(num("#set-ollama-chat-tok")) ? Math.round(num("#set-ollama-chat-tok")) : -1,
      ollama_history_turns: Math.round(num("#set-ollama-hist") ?? state.settings?.ollama_history_turns ?? 6),
      show_generation_stats: $("#set-show-generation-stats")?.checked !== false,
      rag_enabled: $("#set-rag-enabled") ? !!$("#set-rag-enabled").checked : state.settings?.rag_enabled !== false,
      rag_top_k: Math.round(num("#set-rag-top-k") ?? state.settings?.rag_top_k ?? 4),
      rag_context_chars: Math.round(num("#set-rag-context-chars") ?? state.settings?.rag_context_chars ?? 6000),
      rag_chunk_chars: Math.round(num("#set-rag-chunk-chars") ?? state.settings?.rag_chunk_chars ?? 1800),
      rag_chunk_overlap: Math.round(num("#set-rag-chunk-overlap") ?? state.settings?.rag_chunk_overlap ?? 240),
      rag_min_score: Number(num("#set-rag-min-score") ?? state.settings?.rag_min_score ?? 0.25),
      theme_preset: $("#set-theme")?.value || state.settings?.theme_preset || "ember",
      ui_mode: normalizeUiMode($("#set-ui-mode")?.value || state.settings?.ui_mode || "classic"),
      ui_colors: collectUiColorsFromForm(),
      reduce_motion: !!$("#set-reduce-motion")?.checked,
      speak_replies: !!$("#set-speak")?.checked,
      voice_output_enabled: !!$("#set-voice-output")?.checked,
      conversation_flow: $("#set-conv-flow")?.checked !== false,
      conversation_style: $("#set-conv-style")?.value || "concise",
      error_reduction: $("#set-error-reduction")?.checked !== false,
      tts_provider: $("#set-tts-provider")?.value || state.settings?.tts_provider || "local",
      tts_local_voice: $("#set-tts-local-voice")?.value || state.settings?.tts_local_voice || "en_US-lessac-medium",
      tts_allow_online: !!$("#set-tts-allow-online")?.checked,
      tts_edge_voice: $("#set-tts-edge-voice")?.value || state.settings?.tts_edge_voice || "en-US-AvaNeural",
      tts_online_fallback: $("#set-tts-online-fallback")?.value || "piper",
      tts_rate: num("#set-tts-rate") ?? 1,
      tts_speak_director: $("#set-tts-speak-director")?.checked !== false,
      tts_speak_system: !!$("#set-tts-speak-system")?.checked,
      tts_skip_code: $("#set-tts-skip-code")?.checked !== false,
      tts_skip_urls: $("#set-tts-skip-urls")?.checked !== false,
      tts_max_chars: Math.round(num("#set-tts-max-chars") ?? 1000),
      tts_stop_previous: $("#set-tts-stop-previous")?.checked !== false,
      tts_cpu_threads: Math.round(num("#set-tts-cpu-threads") ?? 2),
      chat_temperature: num("#set-chat-temp") ?? 0.65,
      matrix_enabled: $("#set-matrix-enabled")?.checked !== false,
      matrix_agent: $("#matrix-agent-quick")?.value || $("#set-matrix-agent")?.value || state.settings?.matrix_agent || "cypra",
      matrix_history_mode: "current_chat",
      matrix_history_turns: Math.round(state.settings?.matrix_history_turns ?? 24),
      matrix_handoff: !!$("#set-matrix-handoff")?.checked,
      show_model_thinking: $("#set-show-think")?.checked !== false,
      think_mode: normalizeThinkMode($("#studio-settings-think")?.value || state.settings?.think_mode || "auto"),
      think_budget_tokens: Math.max(128, Math.min(8192, Math.round(num("#set-think-budget") ?? state.settings?.think_budget_tokens ?? 768))),
      plain_chat: $("#plain-chat-quick") ? !!$("#plain-chat-quick").checked : !!$("#set-plain-chat")?.checked,
      ui_density: $("#set-density")?.value || "comfortable",
      ui_font_scale: num("#set-font-scale") ?? 1,
      chat_font_scale: num("#set-chat-font-scale") ?? 1,
      confirm_destructive: $("#set-confirm-destructive")?.checked !== false,
      window_width: num("#set-win-w") || state.settings?.window_width || 1440,
      window_height: num("#set-win-h") || state.settings?.window_height || 900,
    };
    const cleaned = {};
    for (const [key, value] of Object.entries(raw)) {
      if (value === undefined || value === null) continue;
      if (typeof value === "string" && !value.trim() && /model/i.test(key)) continue;
      cleaned[key] = value;
    }
    return { ...cleaned, ...collectStudioSettings() };
  }

  function num(sel) {
    const el = $(sel);
    if (!el) return undefined;
    const v = parseFloat(el.value);
    return Number.isFinite(v) ? v : undefined;
  }
  function fillMatrixForm(settings) {
    if (!settings) return;
    const enabled = settings.matrix_enabled !== false;
    const agent = String(settings.matrix_agent || "cypra").trim() || "cypra";
    const handoff = !!settings.matrix_handoff;
    if ($("#set-matrix-enabled")) $("#set-matrix-enabled").checked = enabled;
    if ($("#matrix-enabled-quick")) $("#matrix-enabled-quick").checked = enabled;
    if ($("#set-matrix-handoff")) $("#set-matrix-handoff").checked = handoff;
    const setAgentIfPresent = (sel) => {
      if (!sel) return;
      const hasAgent = [...sel.options].some((option) => option.value === agent);
      if (hasAgent) sel.value = agent;
      syncMatrixSelectTooltip(sel);
    };
    setAgentIfPresent($("#set-matrix-agent"));
    setAgentIfPresent($("#matrix-agent-quick"));
    updateMatrixChip(settings.matrix);
  }

  function fillSettingsForm(settings) {
    if (!settings) return;
    const provider = settings.llm_provider === "ollama" ? "ollama" : "xai";
    setVal("#set-provider", provider);
    setVal("#set-chat-model", settings.chat_model || "local-4.5");
    setVal("#set-ollama-url", settings.ollama_base_url || "http://127.0.0.1:11434");
    setVal("#set-ollama-ctx", settings.ollama_num_ctx ?? 8192);
    setVal("#set-ollama-keep", settings.ollama_keep_alive || "30m");
    setVal("#set-ollama-batch", settings.ollama_num_batch ? String(settings.ollama_num_batch) : "");
    setVal("#set-ollama-chat-tok", settings.ollama_chat_tokens ?? -1);
    setVal("#set-response-length-preset", String(settings.ollama_chat_tokens ?? -1));
    setVal("#set-ollama-hist", settings.ollama_history_turns ?? 6);
    if ($("#set-show-generation-stats")) $("#set-show-generation-stats").checked = settings.show_generation_stats !== false;
    const ragOn = settings.rag_enabled !== false;
    if ($("#set-rag-enabled")) $("#set-rag-enabled").checked = ragOn;
    if ($("#rag-enabled-quick")) $("#rag-enabled-quick").checked = ragOn;
    setVal("#set-rag-top-k", settings.rag_top_k ?? 4);
    setVal("#set-rag-context-chars", settings.rag_context_chars ?? 6000);
    setVal("#set-rag-chunk-chars", settings.rag_chunk_chars ?? 1800);
    setVal("#set-rag-chunk-overlap", settings.rag_chunk_overlap ?? 240);
    setVal("#set-rag-min-score", settings.rag_min_score ?? 0.25);
    if ($("#set-speak")) $("#set-speak").checked = !!settings.speak_replies;
    if ($("#set-voice-output")) $("#set-voice-output").checked = !!settings.voice_output_enabled;
    if ($("#set-conv-flow")) $("#set-conv-flow").checked = settings.conversation_flow !== false;
    setVal("#set-conv-style", settings.conversation_style || "concise");
    if ($("#set-error-reduction")) $("#set-error-reduction").checked = settings.error_reduction !== false;
    fillMatrixForm(settings);
    if ($("#set-show-think")) $("#set-show-think").checked = settings.show_model_thinking !== false;
    if ($("#studio-settings-think")) $("#studio-settings-think").value = normalizeThinkMode(settings.think_mode || "auto");
    setVal("#set-think-budget", settings.think_budget_tokens ?? 768);
    syncThinkQuickMode();
    if ($("#set-plain-chat")) $("#set-plain-chat").checked = !!settings.plain_chat;
    if ($("#plain-chat-quick")) $("#plain-chat-quick").checked = !!settings.plain_chat;
    setVal("#set-tts-provider", settings.tts_provider || "local");
    setVal("#set-tts-local-voice", settings.tts_local_voice || "en_US-lessac-medium");
    if ($("#set-tts-allow-online")) $("#set-tts-allow-online").checked = !!settings.tts_allow_online;
    setVal("#set-tts-edge-voice", settings.tts_edge_voice || "en-US-AvaNeural");
    setVal("#set-tts-online-fallback", settings.tts_online_fallback || "piper");
    setVal("#set-tts-rate", settings.tts_rate ?? 1);
    if ($("#set-tts-speak-director")) $("#set-tts-speak-director").checked = settings.tts_speak_director !== false;
    if ($("#set-tts-speak-system")) $("#set-tts-speak-system").checked = !!settings.tts_speak_system;
    if ($("#set-tts-skip-code")) $("#set-tts-skip-code").checked = settings.tts_skip_code !== false;
    if ($("#set-tts-skip-urls")) $("#set-tts-skip-urls").checked = settings.tts_skip_urls !== false;
    if ($("#set-tts-stop-previous")) $("#set-tts-stop-previous").checked = settings.tts_stop_previous !== false;
    setVal("#set-tts-max-chars", settings.tts_max_chars ?? 1000);
    setVal("#set-tts-cpu-threads", settings.tts_cpu_threads ?? 2);
    setVal("#set-chat-temp", settings.chat_temperature ?? 0.65);
    setVal("#set-theme", settings.theme_preset || "ember");
    setVal("#set-ui-mode", normalizeUiMode(settings.ui_mode));
    applyUiMode(settings.ui_mode, { syncForm: true });
    syncUiColorControls(settings.ui_colors || uiColorsFromTheme(settings.theme_preset || "ember"));
    if ($("#set-reduce-motion")) $("#set-reduce-motion").checked = !!settings.reduce_motion;
    setVal("#set-density", settings.ui_density || "comfortable");
    setVal("#set-font-scale", settings.ui_font_scale ?? 1);
    setVal("#set-chat-font-scale", settings.chat_font_scale ?? 1);
    if ($("#set-confirm-destructive")) $("#set-confirm-destructive").checked = settings.confirm_destructive !== false;
    setVal("#set-win-w", settings.window_width ?? 1440);
    setVal("#set-win-h", settings.window_height ?? 900);
    toggleProviderBlocks(provider);
    syncTTSProviderUI();
    RANGE_MAP.forEach(([id, valueId]) => {
      const el = $(`#${id}`);
      const out = $(`#${valueId}`);
      if (el && out) out.textContent = el.value;
    });
    highlightThemeSwatch(settings.theme_preset || "ember");
    syncStudioSettingsSummary();
  }

  function setVal(sel, val) {
    const el = $(sel);
    if (el && val !== undefined && val !== null) el.value = val;
  }
  function livePreviewSettings() {
    const partial = collectSettingsFromForm();
    const fontScale = Number(partial.ui_font_scale);
    if (Number.isFinite(fontScale) && fontScale > 0) document.documentElement.style.fontSize = `${Math.round(15 * fontScale)}px`;
    const chatFontScale = Number(partial.chat_font_scale);
    if (Number.isFinite(chatFontScale) && chatFontScale > 0) document.documentElement.style.setProperty("--chat-font-scale", String(chatFontScale));
    document.body.classList.toggle("density-compact", partial.ui_density === "compact");
    document.body.classList.toggle("reduce-motion", !!partial.reduce_motion);
    applyUiMode(partial.ui_mode, { syncForm: false });
  }
  function toggleProviderBlocks(provider) {
    const local = provider === "ollama";
    $("#provider-xai-block")?.classList.toggle("hidden", local);
    $("#provider-ollama-block")?.classList.toggle("hidden", !local);
    const hint = $("#provider-hint");
    if (hint) hint.textContent = local ? "Chat runs on the project-local Ollama runtime." : "Chat uses the configured Local API provider.";
  }
  let _pullTimer = 0;

  async function installMatrixRuntime() {
    const status = $("#matrix-install-status");
    if (status) status.textContent = "Launching Matrix installer…";
    try {
      const r = await api("/api/matrix/install", { method: "POST" });
      if (status) status.textContent = r.path
        ? `Installer launched · ${r.path}`
        : "Matrix installer launched";
      setStatus("Matrix installer launched");
    } catch (e) {
      if (status) status.textContent = e.message || "Matrix installer could not be launched";
      alert(e.message || "Matrix installer could not be launched");
    }
  }

  function renderBaseRecommendations(data) {
    const hw = data.hardware || {};
    const hwEl = $("#base-model-hw");
    if (hwEl) {
      hwEl.textContent =
        data.hint ||
        `${hw.gpu || "GPU"} · ${hw.vram_mb || "?"} MB VRAM`;
    }
    const warn = $("#base-model-warning");
    const fit = data.fit || {};
    if (warn) {
      if (fit.warning) {
        warn.hidden = false;
        warn.textContent = fit.warning;
      } else {
        warn.hidden = true;
        warn.textContent = "";
      }
    }

    const kits = $("#base-kit-row");
    if (kits) {
      kits.innerHTML = "";
      for (const k of data.kits || []) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "btn ghost sm local-preset";
        if (k.installed) b.classList.add("active");
        b.dataset.kit = k.id;
        b.title = k.description || k.label;
        b.textContent = k.installed ? `${k.label} ✓` : k.label;
        b.addEventListener("click", () => installBaseKit(k.id));
        kits.appendChild(b);
      }
    }

    const list = $("#base-model-list");
    if (list) {
      list.innerHTML = "";
      for (const m of (data.models || []).filter((item) => item.role !== "embed" && item.role !== "extract")) {
        const row = document.createElement("div");
        row.className = "base-model-row" + (m.star ? " star" : "");
        const info = document.createElement("div");
        const name = document.createElement("span");
        name.className = "bm-name";
        name.textContent = `${m.star ? "★ " : ""}${m.label}`;
        const note = document.createElement("span");
        note.className = "bm-note";
        note.textContent = `${m.id} · ~${m.size_gb} GB${m.note ? " — " + m.note : ""}`;
        info.appendChild(name);
        info.appendChild(note);
        const fitEl = document.createElement("span");
        fitEl.className = "bm-fit " + (m.fit || "");
        fitEl.textContent = m.installed ? "in" : m.fit || "";
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn ghost sm";
        btn.textContent = m.installed ? "Update" : m.fits === false ? "Skip" : "Install";
        btn.disabled = m.fits === false && !m.installed;
        btn.addEventListener("click", () =>
          startModelPull({ models: [m.id], apply: true })
        );
        row.appendChild(info);
        row.appendChild(fitEl);
        row.appendChild(btn);
        list.appendChild(row);
      }
    }
  }

  async function loadBaseRecommendations() {
    try {
      const data = await api("/api/llm/recommend");
      renderBaseRecommendations(data);
      if (data.pull?.running) watchPullProgress();
    } catch (e) {
      const hwEl = $("#base-model-hw");
      if (hwEl) hwEl.textContent = e.message || "Could not load recommendations";
    }
  }

  function formatPullBytes(done, total) {
    if (!total) return "";
    const mb = (n) => (n / 1048576).toFixed(1);
    const pct = Math.min(100, Math.round((done / total) * 100));
    return ` ${mb(done)}/${mb(total)} MB (${pct}%)`;
  }

  function updateStudioPullProgress(st) {
    const box = $("#studio-pull-status-box");
    const current = $("#studio-pull-current");
    const pct = $("#studio-pull-percent");
    const stage = $("#studio-pull-stage");
    const bytes = $("#studio-pull-bytes");
    const track = $("#studio-pull-track");
    const fill = $("#studio-pull-fill");
    if (!box) return;
    const running = !!st?.running;
    const failed = !!st?.error || st?.stage === "failed";
    const percent = Number.isFinite(Number(st?.percent)) ? Math.max(0, Math.min(100, Number(st.percent))) : 0;
    const currentName = st?.current || (st?.models || [])[0] || "READY";
    if (current) current.textContent = running ? `${currentName} · ${String(st?.status || "PULLING").toUpperCase()}` : (failed ? "PULL FAILED" : (st?.status || "IDLE · READY").toUpperCase());
    if (pct) pct.textContent = `${Math.round(percent)}%`;
    if (stage) stage.textContent = String(st?.stage || (running ? "pulling" : "waiting")).toUpperCase();
    if (bytes) bytes.textContent = formatPullBytes(st?.bytes_done || 0, st?.bytes_total || 0).trim() || "—";
    if (track) track.classList.toggle("indeterminate", running && !(st?.bytes_total > 0));
    if (fill) fill.style.width = `${percent}%`;
    box.classList.toggle("is-running", running);
    box.classList.toggle("is-error", failed);
    const cancelBtn = $("#btn-pull-cancel");
    if (cancelBtn) {
      cancelBtn.hidden = !running;
      cancelBtn.disabled = !running;
    }
  }

  async function watchPullProgress() {
    if (_pullTimer) return;
    const tick = async () => {
      try {
        const st = await api("/api/llm/pull/status");
        const el = $("#pull-progress");
        if (el) {
          const extra = formatPullBytes(st.bytes_done || 0, st.bytes_total || 0);
          el.textContent = st.running
            ? `${st.status || "Pulling…"}${extra}`
            : st.status
              ? `${st.status}${st.error ? " — " + st.error : ""}`
              : "";
        }
        updateStudioPullProgress(st);
        if (st.applied) {
          const s = st.settings || {};
          if (s.llm_provider) {
            setVal("#set-provider", s.llm_provider);
            toggleProviderBlocks(s.llm_provider);
          }
        }
        if (st.running) {
          _pullTimer = setTimeout(tick, 1200);
        } else {
          _pullTimer = 0;
          await loadBaseRecommendations();
          await loadStudioModelLibrary().catch(() => {});
          try {
            await refreshState();
          } catch (_) {}
          syncStudioSettingsSummary();
          if (st.stage === "cancelled") setStatus("Pull cancelled · partial download dropped");
          else if (st.ok) setStatus("Model ready");
        }
      } catch (e) {
        _pullTimer = 0;
        const el = $("#pull-progress");
        if (el) el.textContent = e.message || "Pull status failed";
      }
    };
    _pullTimer = setTimeout(tick, 400);
  }

  async function startModelPull({ models = [], kit = "", apply = true, progressSelector = "#pull-progress" } = {}) {
    const el = $(progressSelector) || $("#pull-progress");
    try {
      if (el) el.textContent = kit ? `Installing ${kit} kit…` : `Pulling ${(models || []).join(", ")}…`;
      updateStudioPullProgress({ running: true, status: kit ? `Installing ${kit} kit…` : `Pulling ${(models || []).join(", ")}…`, stage: "starting" });
      const body = kit ? { kit, apply } : { models, apply };
      const r = await api("/api/llm/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok && r.error) {
        if (el) el.textContent = r.error;
        if (!String(r.error).includes("already running")) {
          alert(r.error);
        }
      }
      watchPullProgress();
    } catch (e) {
      if (el) el.textContent = e.message || "Pull failed";
      alert(e.message || "Could not start pull");
    }
  }

  async function installBaseKit(kitId) {
    if (!kitId) return;
    await startModelPull({ kit: kitId, apply: true });
  }

  async function updateInstalledModels() {
    const el = $("#pull-progress");
    try {
      if (el) el.textContent = "Updating installed models…";
      const r = await api("/api/llm/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: [] }),
      });
      if (!r.ok && r.error) {
        if (el) el.textContent = r.error;
      }
      watchPullProgress();
    } catch (e) {
      if (el) el.textContent = e.message || "Update failed";
      alert(e.message || "Could not update models");
    }
  }

  async function pullNamedModel() {
    const name = ($("#pull-model-name")?.value || "").trim();
    if (!name) {
      alert("Enter an Ollama model tag to install");
      return;
    }
    await startModelPull({ models: [name], apply: true });
  }

  function highlightLocalPreset(id) {
    document.querySelectorAll(".local-preset").forEach((b) => {
      b.classList.toggle("active", !!id && b.dataset.preset === id);
    });
  }

  function resetThinkTty() {
    const tty = $("#think-tty");
    const body = $("#think-tty-body");
    if (body) body.textContent = "";
    if (tty) tty.hidden = true;
  }

  function appendThinkTty(chunk) {
    if (!chunk) return;
    const tty = $("#think-tty");
    const body = $("#think-tty-body");
    if (tty && body) {
      tty.hidden = false;
      body.textContent += chunk;
      body.scrollTop = body.scrollHeight;
    }
  }

  function workplaceSlug() {
    return ($("#matrix-agent-quick")?.value || state.settings?.matrix_agent || "cypra").trim() || "cypra";
  }

  async function refreshWorkplace() {
    const panel = $("#workplace-panel");
    if (!panel || !$("#files-mode-quick")?.checked) return;
    const slug = workplaceSlug();
    const slugEl = $("#workplace-slug");
    if (slugEl) slugEl.textContent = slug;
    panel.hidden = false;
    try {
      const data = await api(`/api/workplace?slug=${encodeURIComponent(slug)}`);
      const files = data.files || [];
      const box = $("#workplace-files");
      if (box) box.textContent = files.length
        ? files.map((f) => `${f.path}  (${f.bytes} b)`).join("\n")
        : "(empty workplace)";
    } catch (e) {
      const box = $("#workplace-files");
      if (box) box.textContent = e.message || "Could not list workplace";
    }
  }

  function logWorkplace(results) {
    const log = $("#workplace-log");
    if (!log || !results?.length) return;
    const lines = results.map((r) => {
      if (r.op === "list") return `LIST · ${(r.files || []).length} file(s)`;
      if (r.op === "read") return r.ok ? `READ ${r.path}` : `READ FAIL ${r.path}: ${r.error}`;
      if (r.op === "write") return r.ok ? `WRITE ${r.path} (${r.bytes} b)` : `WRITE FAIL ${r.path}: ${r.error}`;
      if (r.op === "append") return r.ok ? `APPEND ${r.path}` : `APPEND FAIL ${r.path}: ${r.error}`;
      if (r.op === "delete") return r.ok ? `DELETE ${r.path}` : `DELETE FAIL ${r.path}: ${r.error}`;
      if (r.op === "rename") return r.ok ? `RENAME ${r.from} → ${r.to}` : `RENAME FAIL: ${r.error}`;
      if (r.op === "mkdir") return r.ok ? `MKDIR ${r.path}` : `MKDIR FAIL ${r.path}: ${r.error}`;
      return `${r.op} ${r.ok === false ? "FAIL" : "ok"}`;
    });
    log.textContent = (log.textContent ? log.textContent + "\n" : "") + lines.join("\n");
    log.scrollTop = log.scrollHeight;
  }

  
  async function applyLocalPreset(presetId) {
    if (!presetId) return;
    try {
      const data = await api("/api/llm/preset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: presetId, apply: true }),
      });
      const r = data.resolved || {};
      const s = data.settings || {};
      // Switch UI to local if we were on local provider-only
      if (s.llm_provider) {
        setVal("#set-provider", s.llm_provider);
        toggleProviderBlocks(s.llm_provider);
      }
      setVal("#set-ollama-ctx", s.ollama_num_ctx ?? state.settings?.ollama_num_ctx ?? 8192);
      if (s.ollama_chat_tokens != null) setVal("#set-ollama-chat-tok", s.ollama_chat_tokens);
      highlightLocalPreset(r.preset || presetId);
      RANGE_MAP.forEach(([id, vid]) => {
        const el = $(`#${id}`);
        const v = $(`#${vid}`);
        if (el && v) v.textContent = el.value;
      });
      setStatus(`Local kit · ${r.label || presetId}`);
      try {
        await refreshState();
      } catch (_) {
        /* non-fatal */
      }
    } catch (e) {
      setStatus(e.message || "Preset failed");
      alert(e.message || "Could not apply local preset");
    }
  }

  function fillModelSelectGrouped(sel, models, pref, { emptyLabel } = {}) {
    if (!sel) return;
    const prev = pref || sel.value;
    sel.innerHTML = "";
    if (!models.length) {
      const o = document.createElement("option");
      o.value = prev || "llama3.2:3b";
      o.textContent = emptyLabel || prev || "(no models — is Ollama running?)";
      sel.appendChild(o);
      return;
    }
    const groups = {};
    const order = [];
    for (const m of models) {
      const g = m.group || "Other";
      if (!groups[g]) {
        groups[g] = [];
        order.push(g);
      }
      groups[g].push(m);
    }
    for (const g of order) {
      const og = document.createElement("optgroup");
      og.label = g;
      for (const m of groups[g]) {
        const o = document.createElement("option");
        o.value = m.id || m.name;
        o.textContent =
          m.display ||
          `${m.label || m.name || m.id}${m.parameter_size ? ` (${m.parameter_size})` : ""}`;
        if (m.note) o.title = m.note;
        og.appendChild(o);
      }
      sel.appendChild(og);
    }
    if (prev && [...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    } else if (prev) {
      // Keep saved model even if Ollama briefly missed it
      const o = document.createElement("option");
      o.value = prev;
      o.textContent = `${prev} (saved)`;
      sel.appendChild(o);
      sel.value = prev;
    }
  }

  async function loadStudioModelLibrary() {
    const list = $("#studio-model-library-list");
    const count = $("#studio-model-library-count");
    const pathEl = $("#studio-model-library-path");
    if (!list) return;
    list.innerHTML = '<div class="muted small">Loading local model inventory…</div>';
    try {
      const data = await api("/api/llm/library");
      const models = Array.isArray(data.models) ? data.models : [];
      const runtimeCount = Number(data.runtime_count || 0);
      const cacheCount = Number(data.local_cache_count || 0);
      if (count) count.textContent = `${models.length} local model${models.length === 1 ? "" : "s"}${cacheCount ? ` · ${cacheCount} disk cached` : ""}`;
      if (pathEl) pathEl.textContent = data.store || "OllamaModels";
      list.innerHTML = "";
      if (data.store_exists === false && !models.length) {
        // The empty-state below gives the exact remediation; do not throw a faux API error into the library panel.
      }
      if (!models.length) {
        list.innerHTML = data.store_exists === false
          ? '<div class="muted small">LOCAL MODEL STORE NOT FOUND — OllamaModels has not been created yet.</div>'
          : '<div class="muted small">No project-local models detected. Use Add / install base model above.</div>';
        return;
      }
      const active = new Set([data.active?.chat].filter(Boolean));
      const frag = document.createDocumentFragment();
      models.sort((a,b) => String(a.name || a.model || a).localeCompare(String(b.name || b.model || b)));
      for (const m of models) {
        const name = String(m.name || m.model || m.id || m);
        const row = document.createElement("div"); row.className = "studio-model-row";
        const main = document.createElement("div"); main.className = "studio-model-main";
        const title = document.createElement("strong"); title.textContent = name;
        const meta = document.createElement("span");
        const localCache = !!m.local_cache;
        meta.textContent = [m.size ? formatBytes(m.size) : "LOCAL", active.has(name) ? "ACTIVE" : (localCache ? "LOCAL CACHE" : "INSTALLED")].join(" · ");
        main.append(title, meta);
        const actions = document.createElement("div"); actions.className="studio-model-row-actions";
        const use = document.createElement("button"); use.type = "button"; use.className = "btn ghost sm"; use.textContent = active.has(name) ? "ACTIVE" : "USE"; use.disabled = active.has(name);
        use.addEventListener("click", () => useLocalModel(name));
        const remove = document.createElement("button"); remove.type="button"; remove.className="btn danger sm"; remove.textContent="REMOVE";
        remove.addEventListener("click", () => removeLocalModel(name));
        actions.append(use, remove); row.append(main, actions); frag.appendChild(row);
      }
      list.appendChild(frag);
    } catch (e) {
      if (count) count.textContent = "library unavailable";
      list.innerHTML = `<div class="muted small">${String(e.message || "Could not load model library")}</div>`;
    }
  }

  async function useLocalModel(name) {
    if (!name) return;
    try {
      setStatus(`Switching model · ${name}…`);
      await api("/api/settings", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ llm_provider:"ollama", ollama_chat_model:name }) });
      // Force warm so the newly selected model is resident before the next chat.
      try { await api("/api/llm/warm", { method: "POST" }); } catch (_) {}
      await refreshState();
      await loadStudioModelLibrary();
      // Poll warm status briefly so UI clears NO MODEL
      for (let i = 0; i < 8; i++) {
        try {
          const st = await api("/api/llm/warm/status");
          if (st && !st.running) break;
        } catch (_) {}
        await new Promise(r => setTimeout(r, 750));
        await refreshState();
      }
      setStatus(`Local model selected · ${name}`);
      doWarmModel().catch(() => {});
    } catch (e) { alert(e.message || "Could not select model"); }
  }

  async function removeLocalModel(name) {
    if (!name) return;
    if (!confirm(`Remove local model ${name}?\n\nThis deletes the Ollama model from the project-local runtime.`)) return;
    try {
      const data = await api("/api/llm/remove", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({model:name})});
      await loadStudioModelLibrary();
      await refreshState();
      setStatus(data.already_absent ? `Model already absent · ${data.removed || name}` : `Model removed · ${data.removed || name}`);
    } catch(e) { alert(e.message || "Could not remove model"); }
  }


  async function openResumeChat() {
    const dlg = $("#modal-resume-chat");
    if (!dlg) return;
    state.selectedChatSessions = [];
    dlg.showModal();
    await loadResumeChats();
  }

  async function loadResumeChats() {
    try {
      const data = await api("/api/sessions");
      state.sessions = data.sessions || [];
      renderResumeChats();
    } catch (e) {
      const list = $("#resume-chat-list"); if (list) list.innerHTML = `<div class="muted small">${e.message || "Could not load saved chats"}</div>`;
    }
  }

  function getVisibleResumeSessions() {
    const q = ($("#resume-chat-search")?.value || "").trim().toLowerCase();
    const filter = $("#resume-chat-filter")?.value || "all";
    const sort = $("#resume-chat-sort")?.value || "newest";
    let items = (state.sessions || []).filter(sess => {
      const matches = !q || `${sess.title || "Chat"} ${sess.id || ""} ${sess.preview || ""} ${sess.agent || ""} ${(sess.tags||[]).join(" ")}`.toLowerCase().includes(q);
      if (!matches) return false;
      if(filter==='favorites') return !!sess.favorite && !sess.archived;
      if(filter==='archived') return !!sess.archived;
      if(filter==='active') return !sess.archived;
      return true;
    });
    items.sort((a,b)=>{
      if(sort==='name') return String(a.title||"Chat").localeCompare(String(b.title||"Chat"));
      const av=Number(a.mtime||0), bv=Number(b.mtime||0); return sort==='oldest'?av-bv:bv-av;
    });
    return items;
  }

  function renderResumeChats() {
    const list = $("#resume-chat-list"); if (!list) return;
    const sessions = getVisibleResumeSessions();
    list.innerHTML = "";
    if (!sessions.length) { list.innerHTML = '<div class="muted small">No saved chats match this search.</div>'; updateResumeChatSelectionUi(); return; }
    const selected = new Set(state.selectedChatSessions || []);
    const frag = document.createDocumentFragment();
    for (const s of sessions) {
      const row = document.createElement("div"); row.className = "resume-chat-item" + (selected.has(s.id) ? " is-selected" : "");
      const checkWrap = document.createElement("label"); checkWrap.className = "resume-chat-check"; checkWrap.title = "Select this chat for deletion";
      const check = document.createElement("input"); check.type = "checkbox"; check.checked = selected.has(s.id); check.dataset.sessionId = s.id;
      check.addEventListener("change", () => {
        const ids = new Set(state.selectedChatSessions || []);
        if (check.checked) ids.add(s.id); else ids.delete(s.id);
        state.selectedChatSessions = [...ids];
        row.classList.toggle("is-selected", check.checked);
        updateResumeChatSelectionUi();
      });
      checkWrap.appendChild(check);
      const main = document.createElement("div"); main.className = "resume-chat-main";
      const title = document.createElement("strong"); title.textContent = s.title || "Chat";
      title.title = "Double-click to rename";
      title.addEventListener("dblclick", (ev) => { ev.preventDefault(); ev.stopPropagation(); renameChatSession(s); });
      const when = s.mtime ? new Date(Number(s.mtime) * 1000).toLocaleString() : "";
      const meta = document.createElement("span");
      meta.textContent = `${s.messages || 0} msgs${s.agent ? " · " + s.agent : ""}${s.id === state.sessionId ? " · CURRENT" : ""}${s.archived ? " · ARCHIVED" : ""}${when ? " · " + when : ""}`;
      main.append(title, meta);
      if (s.preview) {
        const prev = document.createElement("em");
        prev.className = "resume-chat-preview";
        prev.textContent = s.preview;
        main.appendChild(prev);
      }
      if (Array.isArray(s.tags) && s.tags.length) { const tags=document.createElement("div"); tags.className="resume-chat-tags"; tags.textContent=s.tags.map(t=>`#${t}`).join(" "); main.appendChild(tags); }
      main.addEventListener("click", (ev) => { if (ev.target.closest("button")) return; resumeChatSession(s.id); });
      const actions = document.createElement("div"); actions.className = "resume-chat-actions";
      const open = document.createElement("button"); open.type="button"; open.className="btn primary sm"; open.textContent="OPEN"; open.addEventListener("click",(ev)=>{ev.stopPropagation(); resumeChatSession(s.id);});
      const rename = document.createElement("button"); rename.type="button"; rename.className="btn ghost sm"; rename.textContent="RENAME"; rename.addEventListener("click",(ev)=>{ev.stopPropagation(); renameChatSession(s);});
      const exp = document.createElement("button"); exp.type="button"; exp.className="btn ghost sm"; exp.textContent="EXPORT"; exp.addEventListener("click",(ev)=>{ev.stopPropagation(); exportSessionById(s.id, s.title);});
      const del = document.createElement("button"); del.type="button"; del.className="btn danger sm"; del.textContent="DELETE"; del.addEventListener("click",(ev)=>{ev.stopPropagation(); deleteChatSession(s.id);});
      const fav = document.createElement("button"); fav.type="button"; fav.className="btn ghost sm"; fav.textContent=s.favorite?"★":"☆"; fav.title=s.favorite?"Remove favorite":"Favorite"; fav.addEventListener("click",(ev)=>{ev.stopPropagation(); toggleSessionFlag(s,"favorite",!s.favorite);});
      const arch = document.createElement("button"); arch.type="button"; arch.className="btn ghost sm"; arch.textContent=s.archived?"RESTORE":"ARCHIVE"; arch.addEventListener("click",(ev)=>{ev.stopPropagation(); toggleSessionFlag(s,"archived",!s.archived);});
      actions.append(open, rename, exp, del, fav, arch); row.append(checkWrap, main, actions); frag.appendChild(row);
    }
    list.appendChild(frag);
    updateResumeChatSelectionUi();
  }

  function updateResumeChatSelectionUi() {
    const selected = state.selectedChatSessions || [];
    const visible = getVisibleResumeSessions();
    const selectedCount = $("#resume-chat-selected-count");
    if (selectedCount) selectedCount.textContent = `${selected.length} SELECTED`;
    const deleteBtn = $("#resume-chat-delete-selected");
    if (deleteBtn) deleteBtn.disabled = selected.length === 0;
    const selectAll = $("#resume-chat-select-all");
    if (selectAll) selectAll.textContent = visible.length && visible.every(s => selected.includes(s.id)) ? "DESELECT ALL" : "SELECT ALL";
  }

  function toggleAllResumeChats() {
    const visible = getVisibleResumeSessions();
    const ids = new Set(state.selectedChatSessions || []);
    const allSelected = visible.length > 0 && visible.every(s => ids.has(s.id));
    visible.forEach(s => allSelected ? ids.delete(s.id) : ids.add(s.id));
    state.selectedChatSessions = [...ids];
    renderResumeChats();
  }

  function clearResumeChatSelection() {
    state.selectedChatSessions = [];
    renderResumeChats();
  }

  async function deleteSelectedChatSessions() {
    const ids = [...new Set(state.selectedChatSessions || [])];
    if (!ids.length) return;
    if (!confirm(`Delete ${ids.length} selected chat${ids.length === 1 ? "" : "s"}?\n\nOnly those saved session files will be removed. The active chat and project files are not changed.`)) return;
    try {
      await api("/api/sessions/bulk-delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ids})});
      if (ids.includes(state.sessionId)) {
        persistSessionId(null);
        state.sessionId = null;
        const log = $("#chat-log"); if (log) log.innerHTML = "";
      }
      state.selectedChatSessions = [];
      await loadResumeChats();
      setStatus(`Deleted ${ids.length} chat${ids.length === 1 ? "" : "s"}`);
    } catch(e) { alert(e.message || "Could not delete selected chats"); }
  }

  async function openChatFolder() {
    try {
      let data;
      try { data = await api("/api/sessions/open-folder", {method:"POST"}); }
      catch (_) { data = await api("/api/sessions/folder/open", {method:"POST"}); }
      setStatus(`Chat folder opened · ${data.path || "data/sessions"}`);
    } catch(e) { alert(e.message || "Could not open chat folder"); }
  }

  async function restoreLastSession() {
    const sid = state.sessionId;
    if (!sid) return;
    try {
      const sess = await api(`/api/sessions/${encodeURIComponent(sid)}`);
      const msgs = sess.messages || [];
      if (!msgs.length) return;
      const log = $("#chat-log");
      if (log) {
        log.innerHTML = "";
        for (const m of msgs) appendMsg(m.role, m.content, {matrixAgent:m.matrix_agent,messageId:m.message_id,feedback:m.feedback,stats:m.stats,sessionId:sess.id||sid,silent:true});
      }
      ensureChatEmpty();
      updateStudioChatSnapshot();
      setStatus(`Resumed last chat · ${sess.title || sid}`);
    } catch (_) {
      state.sessionId = null;
      persistSessionId(null);
    }
  }

  
  function applyChatBackground() {
    const log = $("#chat-log");
    if (!log) return;
    const strength = Number(localStorage.getItem("cypra.chat.bgStrength") ?? $("#chat-bg-strength")?.value ?? 18);
    // The slider controls blur only. Zero remains perfectly sharp.
    const normalizedStrength = Math.max(0, Math.min(40, strength));
    const panel = document.getElementById("panel-chat");
    log.style.setProperty("--chat-bg-blur", `${(normalizedStrength * 0.25).toFixed(2)}px`);
    panel?.style.setProperty("--chat-bg-blur", `${(normalizedStrength * 0.25).toFixed(2)}px`);
    const val = $("#chat-bg-strength-val");
    if (val) val.textContent = `${Math.round(strength)}%`;
    if ($("#chat-bg-strength") && $("#chat-bg-strength").value !== String(strength)) {
      $("#chat-bg-strength").value = String(strength);
    }
    fetch("/api/chat-background/meta")
      .then((r) => r.json())
      .then((meta) => {
        if (meta && meta.set) {
          log.classList.add("has-bg");
          const image = `url("/api/chat-background?t=${meta.mtime || Date.now()}")`;
          log.style.setProperty("--chat-bg-image", image);
          panel?.classList.add("has-chat-bg");
          panel?.style.setProperty("--chat-bg-image", image);
        } else {
          log.classList.remove("has-bg");
          log.style.removeProperty("--chat-bg-image");
          panel?.classList.remove("has-chat-bg");
          panel?.style.removeProperty("--chat-bg-image");
        }
      })
      .catch(() => {
        log.classList.remove("has-bg");
        panel?.classList.remove("has-chat-bg");
      });
  }

  async function setChatBackgroundFile(file) {
    if (!file || !String(file.type || "").startsWith("image/")) {
      setStatus("Drop a picture (PNG, JPG, WEBP)");
      return;
    }
    const fd = new FormData();
    fd.append("file", file, file.name || "chat.jpg");
    const res = await fetch("/api/chat-background", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Could not set background");
    applyChatBackground();
    setStatus("Chat background set");
  }

  async function resumeChatSession(sid) {
    if (!sid) return;
    try {
      const sess = await api(`/api/sessions/${encodeURIComponent(sid)}`);
      state.sessionId = sess.id || sid; persistSessionId(state.sessionId);
      const log = $("#chat-log"); if (log) { log.innerHTML=""; for (const m of (sess.messages || [])) appendMsg(m.role, m.content, {matrixAgent:m.matrix_agent,messageId:m.message_id,feedback:m.feedback,stats:m.stats,ragHits:m.rag_hits,sessionId:sess.id||sid,silent:true}); }
      updateStudioChatSnapshot();
      $("#modal-resume-chat")?.close();
      setStatus(`Resumed chat · ${sess.title || sid}`);
    } catch (e) { alert(e.message || "Could not resume chat"); }
  }

  async function renameChatSession(s) {
    const next = prompt("Rename chat", s.title || "Chat");
    if (!next || !next.trim()) return;
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/update`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:next.trim()})}); await loadResumeChats(); } catch(e){ alert(e.message || "Could not rename chat"); }
  }

  async function toggleSessionFlag(s, key, value) {
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/update`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:s.title||"Chat", favorite:key==='favorite'?!!value:!!s.favorite, archived:key==='archived'?!!value:!!s.archived, tags:s.tags||[]})}); await loadResumeChats(); }
    catch(e){ alert(e.message || "Could not update chat session"); }
  }
  async function tagSession(s) {
    const raw=prompt("Tags (comma separated)", (s.tags||[]).join(", "));
    if(raw===null) return;
    const tags=raw.split(",").map(x=>x.trim()).filter(Boolean).slice(0,12);
    try { await api(`/api/sessions/${encodeURIComponent(s.id)}/update`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:s.title||"Chat", favorite:!!s.favorite, archived:!!s.archived, tags})}); await loadResumeChats(); }
    catch(e){ alert(e.message || "Could not update tags"); }
  }

  async function deleteChatSession(sid) {
    if (!confirm("Delete this saved chat? This removes only that session.")) return;
    try { await api(`/api/sessions/${encodeURIComponent(sid)}`, {method:"DELETE"}); if (state.sessionId===sid) { persistSessionId(null); state.sessionId=null; } await loadResumeChats(); } catch(e){ alert(e.message || "Could not delete chat"); }
  }

  function formatBytes(v) { const n=Number(v||0); if(!Number.isFinite(n)||n<=0) return "LOCAL"; const units=["B","KB","MB","GB"]; let x=n,i=0; while(x>=1024&&i<units.length-1){x/=1024;i++;} return `${x>=100?Math.round(x):x.toFixed(1)} ${units[i]}`; }

  
  // ── chat ─────────────────────────────────────────────────────────

  function updateGenerationDiagnostics(phase, stats = {}, error = '') {
    const badge = document.getElementById('studio-stream-health');
    const panel = document.getElementById('studio-live-line') || document.getElementById('studio-generation-panel');
    const phaseText = String(phase || 'IDLE').toUpperCase();
    if (badge) {
      badge.textContent = `STREAM ${phaseText}`;
      badge.classList.toggle('is-live', /THINKING|STREAMING|FLUSHING/.test(phaseText));
    }
    const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
    if (/THINKING|STREAMING/.test(phaseText) && !window.__cypraGenerationStartedAt) window.__cypraGenerationStartedAt = performance.now();
    const elapsedMs = window.__cypraGenerationStartedAt ? performance.now() - window.__cypraGenerationStartedAt : 0;
    const evalCount = Number(stats?.eval_count || 0);
    const evalSeconds = Number(stats?.eval_duration || 0) / 1e9;
    const rate = Number(stats?.tokens_per_sec || (evalCount && evalSeconds ? evalCount / evalSeconds : 0));
    set('studio-generation-phase', phaseText === 'ERROR' ? 'ERROR' : phaseText);
    set('studio-generation-tokens', evalCount ? `${evalCount} TOKENS` : '— TOKENS');
    set('studio-generation-rate', rate ? `${rate.toFixed(1)} TOK/S` : '— TOK/S');
    const thinkMode = String(stats?.think_mode || state.thinkPlan?.think_mode || '').toUpperCase();
    const thinkRequested = String(stats?.think_requested || state.thinkPlan?.think_requested || '').toUpperCase();
    const thinkTokens = Number(stats?.thinking_tokens_estimate || 0);
    const thinkLabel = thinkMode ? `THINK ${thinkRequested === 'AUTO' && thinkMode !== 'AUTO' ? `AUTO→${thinkMode}` : thinkMode}${thinkTokens > 0 ? ` · ≈${Math.round(thinkTokens)} TOK` : ''}` : '';
    set('studio-generation-think', thinkLabel);
    panel?.classList.toggle('is-live', /THINKING|STREAMING|FLUSHING/.test(phaseText));
    panel?.classList.toggle('is-error', phaseText === 'ERROR');
    if (/COMPLETE|ERROR|CANCELED/.test(phaseText)) window.__cypraGenerationStartedAt = 0;
  }

  function studioRequiredContextForResponse(tokens) {
    return normalizeStudioContext(state.settings?.ollama_num_ctx ?? 8192);
  }

  function formatStudioResponseLength(tokens) {
    return Number(tokens) < 0 ? 'UNLIMITED' : `${Number(tokens)} TOKENS`;
  }

  function estimateMessageTokens(text) {
    const value = String(text || "").trim();
    if (!value) return 0;
    // A provider-independent fallback for user messages and older saved replies.
    // Prefer the model's exact eval_count whenever it is available.
    const pieces = value.match(/[\p{L}\p{N}]+(?:['’][\p{L}\p{N}]+)?|[^\s]/gu);
    return Math.max(1, Math.round((pieces?.length || 0) * 1.12));
  }

  function setMessageTokenMeta(bubble, role, text, stats = null, agent = "", showDetails = true) {
    const meta = bubble?.querySelector(".msg-meta");
    if (!meta || role === "system") return;

    const exactOutput = Number(stats?.eval_count);
    const hasExactOutput = role === "assistant" && stats?.eval_count != null && Number.isFinite(exactOutput) && exactOutput > 0;
    const visibleTokens = hasExactOutput ? Math.max(0, Math.round(exactOutput)) : estimateMessageTokens(text);
    const parts = [];
    if (role === "assistant") parts.push(String(agent || "assistant"));
    if (visibleTokens > 0) {
      parts.push(hasExactOutput
        ? `${visibleTokens.toLocaleString()} output tokens`
        : `≈${visibleTokens.toLocaleString()} tokens`);
    }

    if (showDetails && role === "assistant" && stats && Object.keys(stats).length) {
      const promptTokens = Number(stats.prompt_eval_count);
      const outputRate = Number(stats.tokens_per_sec);
      const totalSeconds = Number(stats.total_duration) / 1e9;
      if (Number.isFinite(outputRate) && outputRate > 0) parts.push(`${outputRate.toFixed(1)} tok/s`);
      if (stats.prompt_eval_count != null && Number.isFinite(promptTokens)) parts.push(`${Math.round(promptTokens).toLocaleString()} prompt tokens`);
      if (stats.total_duration != null && Number.isFinite(totalSeconds) && totalSeconds >= .1) parts.push(`${totalSeconds.toFixed(1)}s total`);
      const reasoningEstimate = Number(stats.thinking_tokens_estimate);
      if (Number.isFinite(reasoningEstimate) && reasoningEstimate > 0) parts.push(`≈${Math.round(reasoningEstimate).toLocaleString()} reasoning`);
      if (stats.think_mode) parts.push(`think ${String(stats.think_mode).toLowerCase()}`);
      if (stats.done_reason && !/^(stop|eos)$/i.test(String(stats.done_reason))) parts.push(`ended ${stats.done_reason}`);
      if (stats.truncated) parts.push("response limit reached");
    }
    meta.textContent = parts.join(" · ");
  }

  function attachRagSources(bubble, hits) {
    const meta = bubble?.querySelector(".msg-meta");
    if (!meta) return;
    meta.querySelector(".rag-source-meta")?.remove();
    const rows = Array.isArray(hits) ? hits.filter((hit) => hit && hit.source && hit.source_id) : [];
    if (!rows.length) return;
    const wrap = document.createElement("span");
    wrap.className = "rag-source-meta";
    wrap.appendChild(document.createTextNode(" · RAG "));
    const seen = new Set();
    for (let i = 0; i < rows.length && seen.size < 4; i += 1) {
      const hit = rows[i];
      const sid = String(hit.source_id || "");
      if (!sid || seen.has(sid)) continue;
      seen.add(sid);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rag-citation-chip";
      btn.textContent = `${hit.ref || `RAG ${i + 1}`} · ${hit.source}`;
      const coverage = Math.round(Number(hit.coverage || 0) * 100);
      btn.title = `${hit.source} · chunk ${hit.chunk || "?"} · score ${Number(hit.score || 0).toFixed(3)}${coverage ? ` · coverage ${coverage}%` : ""}${hit.pinned ? " · pinned" : ""}`;
      btn.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); openRagSourceManager(sid); });
      wrap.appendChild(btn);
    }
    meta.appendChild(wrap);
  }

  function renderTurnFileChip() {
    const el = $("#studio-turn-file");
    if (!el) return;
    const tf = state.turnFile;
    if (!tf) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    el.hidden = false;
    const label = tf.kind === "review"
      ? `NEXT SEND · REVIEW CONTEXT · ${esc(tf.name)}`
      : `NEXT SEND · ${esc(tf.name)}${tf.trimmed ? " · excerpt" : ""}`;
    el.innerHTML = `<span>${label}</span><button type="button" class="studio-turn-file-clear" title="Don't attach this context">×</button>`;
  }

  function setTurnFile(name, text, path, kind = "file") {
    const cap = kind === "review" ? 5000 : 12000;
    let body = String(text || "").trim();
    const p = String(path || "").trim();
    if (!body && !p) return false;
    const trimmed = !p && body.length > cap;
    if (!p && trimmed) body = body.slice(0, cap - 24).trimEnd() + "\n[CONTEXT TRIMMED]";
    state.turnFile = { name: name || "file", text: body, path: p, trimmed, kind };
    renderTurnFileChip();
    setStatus(kind === "review"
      ? `Next send includes reviewed context from ${state.turnFile.name} (saved in chat history)`
      : `Next send includes ${state.turnFile.name} (one turn, not saved in history)`);
    return true;
  }

  function clearTurnFile() {
    if (!state.turnFile) {
      renderTurnFileChip();
      return;
    }
    state.turnFile = null;
    renderTurnFileChip();
  }

  window.__cypraSetTurnFile = (name, text, path) => setTurnFile(name, text, path);

    function clearFollowups() {
      const el = $("#followups");
      if (!el) return;
      el.hidden = true;
      el.classList.remove("followups-visible");
      el.innerHTML = "";
    }

    async function sendChat() {
    if (state.busy) return;
    if (state.honesty?.warming || state.honesty?.too_heavy) setStatus(state.honesty.line);

    const selectedAgent = $("#matrix-agent-quick")?.value || $("#set-matrix-agent")?.value || state.settings?.matrix_agent || "";
    // Agent selection is persisted by the selector/save handlers. Never gate a
    // chat send on a second Matrix request; the user message goes straight to
    // /api/chat and the server resolves the currently persisted agent.
    const input = $("#chat-input");
    const turnFile = state.turnFile;
    const reviewMode = !!turnFile && turnFile.kind === "review";
    const reviewContext = reviewMode ? String(turnFile.text || "").trim() : "";
    const attachedFile = turnFile && !reviewMode ? turnFile : null;
    let text = String(input?.value || "").trim();
    if (!text && (attachedFile || reviewContext)) text = reviewMode ? "Use the reviewed file context below." : "Use the attached one-turn file context.";
    if (!text) return;

    // Explicit persistence command. Ordinary chat is never auto-indexed, but
    // "Remember: ..." / "Save to knowledge: ..." writes the fact to RAG before
    // generation so it survives a Studio restart immediately.
    const rememberMatch = text.match(/^\s*(?:remember(?:\s+that)?|save\s+to\s+knowledge)\s*:\s*([\s\S]+)$/i);
    if (rememberMatch?.[1]?.trim()) {
      try {
        const remembered = await api("/api/rag/chat-knowledge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: rememberMatch[1].trim(), role: "user" }),
        });
        renderRagStatus(remembered);
        setStatus(remembered.duplicate ? "Knowledge already remembered" : "Knowledge remembered · persistent RAG updated");
        if (typeof window.showStudioToast === "function") window.showStudioToast("RAG KNOWLEDGE", remembered.duplicate ? "Already indexed" : "Remembered · survives restart", "ok");
      } catch (e) {
        setStatus(`Remember failed · ${e.message || e}`);
      }
    }

    input.value = "";
    input.dispatchEvent(new Event("input"));
    appendMsg("user", reviewMode ? `${text}\n\n[reviewed context: ${turnFile.name}]` : attachedFile ? `${text}\n\n[one-turn file: ${attachedFile.name}]` : text);
    clearTurnFile();

    state.busy = true;
    state.generationCanceled = false;
    state.abortController = new AbortController();
    setChatBusy(true);
    setStatus("Thinking…");
    const bubble = appendMsg("assistant", "", { matrixAgent: selectedAgent, sessionId: state.sessionId });
    bubble.classList.add("streaming");
    const bodyEl = bubble.querySelector(".body");
    resetThinkTty();

    const thinkOverride = currentThinkOverride();
    let full = "";
    let qualityInfo = null;
    let ragHits = [];
    let generationStats = {};
    let firstDelta = true;
    let streamRaf = 0;
    const logEl = $("#chat-log");
    const paint = () => {
      streamRaf = 0;
      if (bodyEl) bodyEl.textContent = full;
      if (logEl && logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 120) logEl.scrollTop = logEl.scrollHeight;
    };
    const queuePaint = () => { if (!streamRaf) streamRaf = requestAnimationFrame(paint); };

    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          session_id: state.sessionId,
          use_memory: false,
          use_rag: $("#rag-enabled-quick")?.checked !== false,
          auto_extract: false,
          think_mode: thinkOverride,
          plain: !!$("#plain-chat-quick")?.checked,
          talk: !!$("#talk-mode-quick")?.checked,
          files: !!$("#files-mode-quick")?.checked,
          stream: true,
          pinned: [],
          review_context: reviewContext,
          review_context_name: reviewMode ? (turnFile?.name || "") : "",
          turn_file_name: attachedFile?.name || "",
          turn_file_text: attachedFile?.path ? "" : (attachedFile?.text || ""),
          turn_file_path: attachedFile?.path || "",
        }),
        signal: state.abortController.signal,
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || response.statusText);
      }

      clearFollowups();
      updateGenerationDiagnostics("THINKING", generationStats);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamDone = false;
      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          if (streamDone) break;
          streamDone = true;
          buffer += decoder.decode() + "\n";
        } else {
          buffer += decoder.decode(value, { stream: true });
        }
        const parts = buffer.split(/\r?\n/);
        buffer = parts.pop() || "";
        for (const block of parts) {
          if (!block.startsWith("data:")) continue;
          let msg;
          try { msg = JSON.parse(block.slice(5).trim()); } catch (_) { continue; }
          if (msg.type === "started") {
            setStatus("Preparing model…");
          } else if (msg.type === "session") {
            state.sessionId = msg.session_id || state.sessionId;
            persistSessionId(state.sessionId);
            if (Array.isArray(msg.rag_hits)) ragHits = msg.rag_hits;
            state.thinkPlan = {
              think_mode: msg.think_mode || "", think_requested: msg.think_requested || "",
              think_reason: msg.think_reason || "", think_budget_tokens: Number(msg.think_budget_tokens || 0),
              think_native_supported: !!msg.think_native_supported, think_native_detected: !!msg.think_native_detected,
            };
            generationStats = { ...generationStats, ...state.thinkPlan };
            updateGenerationDiagnostics("THINKING", generationStats);
            if (Number.isFinite(Number(msg.response_tokens))) {
              const raw = Number(msg.response_tokens);
              const applied = raw < 0 ? -1 : Math.min(8192, Math.max(256, Math.round(raw)));
              state.settings = { ...(state.settings || {}), ollama_chat_tokens: applied };
              setVal("#set-ollama-chat-tok", String(applied));
              if (Number.isFinite(Number(msg.effective_context_tokens))) {
                const effective = Math.max(8192, Number(msg.effective_context_tokens));
                if ($("#studio-live-context")) $("#studio-live-context").textContent = String(effective);
              }
              
            }
            if (msg.matrix_agent) {
              bubble.dataset.matrixAgent = msg.matrix_agent;
              const role = bubble.querySelector(".role");
              if (role) role.textContent = `CYPRA · ${msg.matrix_agent}`;
              const agentEl = $("#think-tty-agent");
              if (agentEl) agentEl.textContent = msg.matrix_agent;
            }
            if (msg.model) {
              const title = $("#think-tty-title");
              if (title) title.textContent = "BASE THINK · " + String(msg.model).split(":")[0];
            }
          } else if (msg.type === "think") {
            if (state.settings?.show_model_thinking !== false) appendThinkTty(msg.text || "");
          } else if (msg.type === "files") {
            logWorkplace(msg.results || []);
            refreshWorkplace();
          } else if (msg.type === "generation_stats") {
            generationStats = { ...generationStats, ...(msg.stats || {}) };
            updateGenerationDiagnostics("STREAMING", generationStats);
          } else if (msg.type === "delta") {
            if (firstDelta) {
              firstDelta = false;
              bubble.classList.add("streaming-active");
              setStatus("Streaming reply…");
              updateGenerationDiagnostics("STREAMING", generationStats);
            }
            full += msg.text || "";
            queuePaint();
          } else if (msg.type === "polish") {
            if (String(msg.reply || "").trim()) full = msg.reply;
            if (!String(full || "").trim() && msg.files?.length) full = msg.files.map((item) => item.path ? `${item.op} ${item.path}` : item.op).join("\n");
            if (bodyEl) bodyEl.innerHTML = linkify(full || " ");
            qualityInfo = msg.quality || qualityInfo;
            if (msg.summary) setStatus(msg.summary);
            attachAssistantChrome(bubble, full, qualityInfo);
          } else if (msg.type === "done") {
            full = msg.reply || full;
            generationStats = { ...generationStats, ...(msg.stats || {}) };
            if (bodyEl) bodyEl.innerHTML = linkify(full);
            qualityInfo = msg.quality || qualityInfo;
            if (msg.message_id) bubble.dataset.messageId = msg.message_id;
            if (msg.session_id) { state.sessionId = msg.session_id; bubble.dataset.sessionId = msg.session_id; persistSessionId(state.sessionId); }
            if (msg.matrix_agent) bubble.dataset.matrixAgent = msg.matrix_agent;
            if (Array.isArray(msg.rag_hits)) ragHits = msg.rag_hits;
            bubble.dataset.feedback = String(Number(msg.feedback) || 0);
            attachAssistantChrome(bubble, full, qualityInfo);
            const agentSlug = String(bubble.dataset.matrixAgent || state.settings?.matrix_agent || "agent");
            const agentLabel = agentSlug.split(/[-_]+/).filter(Boolean).map((part) => part[0]?.toUpperCase() + part.slice(1)).join(" ");
            setMessageTokenMeta(bubble, "assistant", full, generationStats, agentLabel, state.settings?.show_generation_stats !== false);
            attachRagSources(bubble, ragHits);
            clearFollowups();
            setStatus("Done");
            updateGenerationDiagnostics("COMPLETE", generationStats);
            const talkMode = !!$("#talk-mode-quick")?.checked;
            const isDirectorReply = String(msg.matrix_agent || bubble.dataset.matrixAgent || "").toLowerCase() === "nexus-prime";
            const voiceAllowed = !!state.settings?.voice_output_enabled && (talkMode || (isDirectorReply ? state.settings?.tts_speak_director !== false : state.speakReplies));
            if (voiceAllowed && full) {
              CypraVoice.speak(full).then(() => { if (talkMode) scheduleTalkListen(); }).catch((error) => { if (error?.name !== "AbortError") setStatus("TTS: " + (error.message || error)); if (talkMode) setTimeout(scheduleTalkListen, 500); });
            } else if (talkMode) setTimeout(scheduleTalkListen, 500);
          } else if (msg.type === "error") {
            throw new Error(msg.error || "stream error");
          }
        }
        if (streamDone) break;
      }
      if (streamRaf) cancelAnimationFrame(streamRaf);
      if (full && bodyEl && bodyEl.textContent !== full && !bodyEl.innerHTML.includes("wikilink")) bodyEl.textContent = full;
      bubble.classList.add("chat-finalized");
      setTimeout(() => bubble.classList.remove("chat-finalized"), 420);
    } catch (error) {
      if (error?.name === "AbortError" || state.generationCanceled) {
        const partial = bodyEl?.textContent || full || "";
        bubble.classList.add("chat-canceled");
        setMessageTokenMeta(bubble, "assistant", partial, generationStats, `GENERATION STOPPED · ${bubble.dataset.matrixAgent || state.settings?.matrix_agent || "agent"}`);
        if (partial) attachAssistantChrome(bubble, partial, null);
        updateGenerationDiagnostics("CANCELED", generationStats);
        setStatus("Generation stopped · current partial reply kept");
      } else {
        updateGenerationDiagnostics("ERROR", generationStats, error?.message || error);
        if (bodyEl) bodyEl.textContent = "Error: " + (error.message || error);
        setStatus(error.message || error);
      }
    } finally {
      syncThinkQuickMode();
      state.busy = false;
      state.abortController = null;
      state.generationCanceled = false;
      setChatBusy(false);
      bubble.classList.remove("streaming", "extracting", "streaming-active");
    }
  }

  function appendMsg(role, text, options = {}) {
    const log = $("#chat-log");
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    if (role === "system") {
      el.textContent = text;
      if (!options.silent && state.settings?.voice_output_enabled && state.settings?.tts_speak_system) {
        CypraVoice.speak(text).catch((error) => {
          if (error?.name !== "AbortError") setStatus("TTS: " + (error.message || error));
        });
      }
    } else {
      const activeAgent = options.matrixAgent || state.settings?.matrix_agent || state.matrix?.agent?.slug || "assistant";
      const roleLabel = role === "assistant" ? `CYPRA · ${activeAgent}` : "USER";
      el.innerHTML = `<div class="msg-head"><div class="role">${roleLabel}</div><div class="msg-actions"></div></div><div class="body"></div><div class="msg-meta"></div>`;
      el.querySelector(".body").textContent = text;
      if (role === "assistant") {
        el.dataset.matrixAgent = activeAgent;
        if (options.messageId) el.dataset.messageId = options.messageId;
        if (options.sessionId) el.dataset.sessionId = options.sessionId;
        el.dataset.feedback = String(Number(options.feedback) || 0);
      }
      if (role === "assistant") {
        setMessageTokenMeta(el, role, text, options.stats, activeAgent, state.settings?.show_generation_stats !== false);
        attachRagSources(el, options.ragHits || []);
      } else if (role === "user") {
        attachKnowledgeAction(el, "user");
      }
      if (role === "assistant" && text) {
        el.querySelector(".body").innerHTML = formatMsg(text);
        attachAssistantChrome(el, text, null);
      }
    }
    log.appendChild(el);
    // Auto-scroll is a Studio presentation preference; keep manual scroll position when disabled.
    try {
      const pref = JSON.parse(localStorage.getItem("cypra.studio.ui.polish.v1") || "{}");
      if (pref.autoScroll !== false) log.scrollTop = log.scrollHeight;
    } catch (_) { log.scrollTop = log.scrollHeight; }
    // Force a fresh, deterministic chat-entry animation. CSS animations alone can be
    // skipped when the same DOM subtree is recycled or restored from session state.
    el.classList.add("chat-just-added");
    requestAnimationFrame(() => {
      requestAnimationFrame(() => el.classList.remove("chat-just-added"));
    });
    updateChatEmpty();
    const countNow = log.querySelectorAll(".msg").length;
    const mc = $("#studio-live-message-count"); if (mc) mc.textContent = String(countNow);
    updateStudioChatSnapshot();
    return el;
  }

  function syncAgentFeedbackButtons(bubble) {
    const vote=Number(bubble?.dataset?.feedback)||0;
    bubble?.querySelectorAll('.btn-agent-feedback').forEach(btn=>{
      const value=Number(btn.dataset.sentiment);
      btn.classList.toggle('is-active',value===vote);
      btn.setAttribute('aria-pressed',value===vote?'true':'false');
    });
  }

  async function submitAgentFeedback(bubble, sentiment) {
    const sid=bubble?.dataset?.sessionId||state.sessionId;
    const messageId=bubble?.dataset?.messageId;
    if(!sid||!messageId){setStatus('Finish the response before rating this agent');return;}
    const current=Number(bubble.dataset.feedback)||0;
    const next=current===sentiment?0:sentiment;
    try{
      const result=await api(`/api/sessions/${encodeURIComponent(sid)}/feedback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message_id:messageId,sentiment:next})});
      bubble.dataset.feedback=String(next);syncAgentFeedbackButtons(bubble);
      const score=Number(result.metrics?.score),agent=result.agent||bubble.dataset.matrixAgent||'agent';
      setStatus(`${agent} feedback ${next>0?'positive':next<0?'negative':'cleared'}${Number.isFinite(score)?` · score ${score.toFixed(1)}`:''}`);
    }catch(e){setStatus('Agent feedback: '+(e.message||e));}
  }

  function attachAssistantChrome(bubble, text, quality) {
    if (!bubble || bubble.classList.contains("system")) return;
    const actions = bubble.querySelector(".msg-actions");
    const meta = bubble.querySelector(".msg-meta");
    if (actions) {
      const copies = [...actions.querySelectorAll(".btn-copy-msg")];
      copies.slice(1).forEach((b) => b.remove());
      if (!actions.querySelector(".btn-copy-msg")) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn ghost sm btn-copy-msg";
        btn.title = "Copy this reply";
        btn.textContent = "COPY";
        btn.addEventListener("click", async () => {
          const body = bubble.querySelector(".body");
          const t = body?.innerText || body?.textContent || text || "";
          try {
            await navigator.clipboard.writeText(t);
            btn.textContent = "COPIED";
            setStatus("Reply copied to clipboard");
            setTimeout(() => { btn.textContent = "COPY"; }, 1100);
          } catch (_) {
            setStatus("Clipboard unavailable");
          }
        });
        actions.appendChild(btn);
      }
    }
    if (actions && !actions.querySelector(".btn-speak-msg")) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn ghost sm btn-speak-msg";
      btn.title =
        "Speak this reply aloud (browser Speech Synthesis or local provider TTS from Settings → AI → Speech)";
      btn.textContent = "🔊";
      btn.addEventListener("click", async () => {
        const body = bubble.querySelector(".body");
        const t = body?.innerText || body?.textContent || text || "";
        try {
          setSpeakStopVisible(true);
          await CypraVoice.speak(t);
        } catch (e) {
          setStatus("TTS: " + (e.message || e));
        } finally {
          setSpeakStopVisible(false);
        }
      });
      actions.appendChild(btn);
    }
    if (actions && !actions.querySelector('.btn-agent-feedback')) {
      for (const [sentiment,label,title] of [[1,'+','Positive — this agent response was useful'],[-1,'−','Negative — this agent response needs improvement']]) {
        const btn=document.createElement('button');btn.type='button';btn.className='btn ghost sm btn-agent-feedback';
        btn.dataset.sentiment=String(sentiment);btn.textContent=label;btn.title=title;btn.setAttribute('aria-label',title);
        btn.addEventListener('click',()=>submitAgentFeedback(bubble,sentiment));actions.appendChild(btn);
      }
    }
    syncAgentFeedbackButtons(bubble);
    attachKnowledgeAction(bubble, "assistant");
    if (meta && quality) {
      const conf = quality.confidence || "high";
      const fixed = quality.fixed_citations || 0;
      const issues = (quality.issues || []).length;
      meta.innerHTML = "";
      const badge = document.createElement("span");
      badge.className = `quality-badge conf-${conf}`;
      badge.title = (quality.issues || [])
        .map((i) => i.detail || i.code)
        .join("\n");
      badge.textContent =
        conf === "high" && !fixed && !issues
          ? "✓ verified"
          : fixed
            ? `✓ ${fixed} fix(es) · ${conf}`
            : `quality · ${conf}`;
      meta.appendChild(badge);
    }

  }

  
  function renderFollowups(_items) { /* Follow-up chips are intentionally not part of Studio. */ }

  function setSpeakStopVisible(on) {
    const btn = $("#btn-speak-stop");
    if (btn) btn.hidden = !on;
  }

  function updateChatEmpty() {
    const empty = $("#chat-empty");
    const log = $("#chat-log");
    if (!empty || !log) return;
    empty.hidden = !!log.querySelector(".msg");
  }

  function linkify(text) {
    return formatMsg(text);
  }

  function formatMsg(text) {
    let h = esc(text);
    h = h.replace(/\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]/g, (_, t) => {
      const id = t.trim();
      return `<span class="wikilink" data-id="${esc(id)}">[[${esc(id)}]]</span>`;
    });
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return h;
  }

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(t) {
    $("#chat-status").textContent = t || "";
  }

  // ── notes ────────────────────────────────────────────────────────

  function renderMarkdownLite(md) {
    let html = esc(md);
    html = html.replace(/^### (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^## (.+)$/gm, "<h2>$1</h2>");
    html = html.replace(/^# (.+)$/gm, "<h1>$1</h1>");
    html = html.replace(/^\s*[-*] (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, (m) => `<ul>${m}</ul>`);
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]/g, (_, t) => {
      const id = t.trim();
      return `<span class="wikilink" data-id="${esc(id)}">[[${esc(id)}]]</span>`;
    });
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\n\n/g, "</p><p>");
    return `<p>${html}</p>`;
  }

  // ── RAG knowledge ────────────────────────────────────────────────

  function setRagToggleState(enabled) {
    enabled = !!enabled;
    if (state.settings) state.settings.rag_enabled = enabled;
    if ($("#set-rag-enabled")) $("#set-rag-enabled").checked = enabled;
    if ($("#rag-enabled-quick")) $("#rag-enabled-quick").checked = enabled;
  }

  function ragKindLabel(doc) {
    const kind = String(doc?.kind || "file").toLowerCase();
    if (kind === "chat") return doc?.origin_role === "assistant" ? "CHAT · ASSISTANT" : "CHAT · USER";
    if (kind === "manual") return "MANUAL";
    return "FILE";
  }

  function ragDisplayName(doc) {
    return String(doc?.display_name || doc?.label || doc?.name || doc?.id || "knowledge");
  }

  function rebuildRagGroupFilter(documents) {
    const select = $("#rag-group-filter");
    if (!select) return;
    const prior = String(select.value || "all");
    const groups = [...new Set((documents || []).map((doc) => String(doc?.group || "").trim()).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    select.innerHTML = '<option value="all">All groups</option>';
    for (const group of groups) {
      const option = document.createElement("option");
      option.value = group;
      option.textContent = group;
      select.appendChild(option);
    }
    select.value = groups.includes(prior) ? prior : "all";
  }

  function renderRagStatus(payload) {
    const stats = payload?.stats || {};
    const settings = payload?.settings || {};
    const docs = Array.isArray(payload?.documents) ? payload.documents : [];
    state.ragDocuments = docs;
    if ($("#rag-status-engine")) $("#rag-status-engine").textContent = String(stats.retrieval || "bm25-cpu-v2").replace(/-cpu(?:-v2)?$/i, " · CPU").toUpperCase();
    if ($("#rag-status-sources")) $("#rag-status-sources").textContent = String(stats.sources ?? docs.length ?? 0);
    if ($("#rag-status-active")) $("#rag-status-active").textContent = String(stats.enabled_sources ?? docs.filter((d) => d?.enabled !== false).length);
    if ($("#rag-status-pinned")) $("#rag-status-pinned").textContent = String(stats.pinned_sources ?? docs.filter((d) => !!d?.pinned).length);
    if ($("#rag-status-chunks")) $("#rag-status-chunks").textContent = String(stats.chunks ?? 0);
    if ($("#rag-status-groups")) $("#rag-status-groups").textContent = String(stats.groups ?? new Set(docs.map((d) => d?.group).filter(Boolean)).size);
    if ($("#rag-status-chat")) $("#rag-status-chat").textContent = String(stats.chat_sources ?? docs.filter((d) => d?.kind === "chat").length);
    if ($("#rag-status-gpu")) $("#rag-status-gpu").textContent = stats.gpu_required ? "REQUIRED" : "NONE";
    const status = $("#rag-transfer-status");
    if (status) {
      const count = Number(stats.sources ?? docs.length ?? 0);
      const active = Number(stats.enabled_sources ?? docs.filter((d) => d?.enabled !== false).length);
      const pinned = Number(stats.pinned_sources ?? docs.filter((d) => !!d?.pinned).length);
      const chunks = Number(stats.chunks ?? 0);
      status.textContent = `${count} source${count === 1 ? "" : "s"} · ${active} active · ${pinned} pinned · ${chunks} chunks · ${formatBytes(stats.disk_bytes || 0)} · local only`;
    }
    if (settings.enabled != null) setRagToggleState(settings.enabled);
    if (settings.min_score != null && $("#set-rag-min-score")) $("#set-rag-min-score").value = String(settings.min_score);
    rebuildRagGroupFilter(docs);
    renderRagDocuments(docs);
  }

  function renderRagDocuments(documents) {
    const list = $("#rag-document-list");
    if (!list) return;
    list.innerHTML = "";
    const docs = Array.isArray(documents) ? documents : [];
    const filter = String($("#rag-source-filter")?.value || "all");
    const group = String($("#rag-group-filter")?.value || "all");
    const search = String($("#rag-source-search")?.value || "").trim().toLowerCase();
    const visible = docs.filter((doc) => {
      const kind = String(doc?.kind || "file");
      const enabled = doc?.enabled !== false;
      const pinned = !!doc?.pinned;
      if (filter === "file" || filter === "chat" || filter === "manual") {
        if (kind !== filter) return false;
      } else if (filter === "enabled" && !enabled) return false;
      else if (filter === "disabled" && enabled) return false;
      else if (filter === "pinned" && !pinned) return false;
      if (group !== "all" && String(doc?.group || "") !== group) return false;
      if (search) {
        const hay = [ragDisplayName(doc), doc?.name, doc?.label, doc?.group, ...(doc?.tags || []), doc?.preview]
          .map((v) => String(v || "").toLowerCase()).join(" ");
        if (!hay.includes(search)) return false;
      }
      return true;
    });
    if (!visible.length) {
      const row = document.createElement("div");
      row.className = "base-model-row";
      const info = document.createElement("div");
      const name = document.createElement("span");
      name.className = "bm-name";
      name.textContent = docs.length ? "No sources match the current filters" : "No knowledge sources indexed";
      const note = document.createElement("span");
      note.className = "bm-note";
      note.textContent = docs.length
        ? "Change type/group/search filters to view other indexed knowledge."
        : "Add a file, paste a durable fact, or use KNOW+ on a chat message.";
      info.append(name, note);
      row.appendChild(info);
      list.appendChild(row);
      return;
    }
    for (const doc of visible) {
      const row = document.createElement("div");
      row.className = `base-model-row rag-source-row${doc?.enabled === false ? " is-disabled" : ""}${doc?.pinned ? " is-pinned" : ""}`;
      row.dataset.sourceId = doc.id || "";
      const info = document.createElement("button");
      info.type = "button";
      info.className = "rag-source-info-button";
      info.title = "Preview and manage this source";
      info.addEventListener("click", () => openRagSourceManager(doc.id));
      const name = document.createElement("span");
      name.className = "bm-name";
      name.textContent = ragDisplayName(doc);
      const note = document.createElement("span");
      note.className = "bm-note";
      const flags = [ragKindLabel(doc)];
      if (doc?.pinned) flags.push("PINNED");
      if (doc?.enabled === false) flags.push("DISABLED");
      if (doc?.group) flags.push(`GROUP ${doc.group}`);
      flags.push(`${Number(doc.chunks || 0)} chunks`);
      flags.push(formatBytes(doc.bytes || 0));
      if (doc.created_at) flags.push(String(doc.created_at));
      note.textContent = flags.join(" · ");
      info.append(name, note);
      if (Array.isArray(doc.tags) && doc.tags.length) {
        const tags = document.createElement("span");
        tags.className = "bm-note rag-source-tags";
        tags.textContent = `#${doc.tags.join("  #")}`;
        info.appendChild(tags);
      }
      if (doc.preview) {
        const preview = document.createElement("span");
        preview.className = "bm-note rag-source-preview";
        preview.textContent = String(doc.preview);
        info.appendChild(preview);
      }
      const fit = document.createElement("span");
      fit.className = `bm-fit ${doc?.enabled === false ? "warn" : "good"}`;
      fit.textContent = doc?.enabled === false ? "OFF" : (doc?.pinned ? "PIN" : ragKindLabel(doc));
      const actions = document.createElement("div");
      actions.className = "rag-source-row-actions";
      const enable = document.createElement("button");
      enable.type = "button";
      enable.className = "btn ghost sm";
      enable.textContent = doc?.enabled === false ? "ENABLE" : "DISABLE";
      enable.title = doc?.enabled === false ? "Allow this source in retrieval" : "Keep the source but exclude it from retrieval";
      enable.addEventListener("click", () => quickPatchRagSource(doc.id, { enabled: doc?.enabled === false }, doc?.enabled === false ? "Source enabled" : "Source disabled"));
      const pin = document.createElement("button");
      pin.type = "button";
      pin.className = `btn ghost sm${doc?.pinned ? " is-active" : ""}`;
      pin.textContent = doc?.pinned ? "UNPIN" : "PIN";
      pin.title = doc?.pinned ? "Remove retrieval priority" : "Boost this source when it matches";
      pin.addEventListener("click", () => quickPatchRagSource(doc.id, { pinned: !doc?.pinned }, doc?.pinned ? "Source unpinned" : "Source pinned"));
      const manage = document.createElement("button");
      manage.type = "button";
      manage.className = "btn ghost sm";
      manage.textContent = "MANAGE";
      manage.addEventListener("click", () => openRagSourceManager(doc.id));
      actions.append(enable, pin, manage);
      row.append(info, fit, actions);
      list.appendChild(row);
    }
  }

  async function refreshRagStatus() {
    const payload = await api("/api/rag/status");
    renderRagStatus(payload);
    return payload;
  }

  window.__cypraRenderRagStatus = renderRagStatus;
  window.__cypraRefreshRagStatus = refreshRagStatus;

  async function saveRagPatch(patch, message = "RAG settings saved") {
    const result = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (result.settings) {
      state.settings = { ...(state.settings || {}), ...result.settings };
      fillSettingsForm(state.settings);
    }
    setStatus(message);
    return result;
  }

  async function setRagEnabled(enabled) {
    const before = state.settings?.rag_enabled !== false;
    setRagToggleState(enabled);
    try {
      await saveRagPatch({ rag_enabled: !!enabled }, enabled ? "RAG on · persistent local retrieval enabled" : "RAG off");
    } catch (e) {
      setRagToggleState(before);
      setStatus(`RAG toggle failed · ${e.message || e}`);
    }
  }

  async function quickPatchRagSource(sourceId, patch, message) {
    if (!sourceId) return;
    try {
      const data = await api(`/api/rag/documents/${encodeURIComponent(sourceId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      renderRagStatus(data);
      setStatus(message || "RAG source updated");
    } catch (e) {
      setStatus(`RAG source update failed · ${e.message || e}`);
    }
  }

  async function uploadRagFile(file) {
    if (!file) return;
    const status = $("#rag-transfer-status");
    if (status) status.textContent = `Indexing ${file.name}…`;
    const fd = new FormData();
    fd.append("file", file, file.name || "knowledge.txt");
    const response = await fetch("/api/rag/upload", { method: "POST", body: fd });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Knowledge upload failed");
    renderRagStatus(data);
    const sourceName = data.source?.display_name || data.source?.name || file.name;
    setStatus(data.duplicate ? `RAG duplicate content · already indexed as ${sourceName}` : `RAG indexed · ${sourceName}`);
    return data;
  }

  async function saveManualRagKnowledge() {
    const textEl = $("#rag-manual-text");
    const labelEl = $("#rag-manual-label");
    const status = $("#rag-manual-status");
    const text = String(textEl?.value || "").trim();
    const label = String(labelEl?.value || "").trim();
    if (!text) {
      if (status) status.textContent = "Enter knowledge to save.";
      textEl?.focus();
      return;
    }
    if (status) status.textContent = "Saving and indexing…";
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const name = label ? (/\.[a-z0-9]{1,8}$/i.test(label) ? label : `${label}.txt`) : `manual-knowledge-${stamp}.txt`;
    try {
      const data = await api("/api/rag/text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, text }),
      });
      renderRagStatus(data);
      if (textEl) textEl.value = "";
      if (labelEl) labelEl.value = "";
      if (status) status.textContent = data.duplicate ? "Duplicate content already present." : "Saved · persistent across restarts.";
      setStatus(data.duplicate ? "Knowledge content already exists in RAG" : "Knowledge saved to persistent RAG");
    } catch (e) {
      if (status) status.textContent = `Save failed · ${e.message || e}`;
      setStatus(`RAG save failed · ${e.message || e}`);
    }
  }

  async function saveMessageToKnowledge(bubble, role, button) {
    const text = String(bubble?.querySelector(".body")?.innerText || bubble?.querySelector(".body")?.textContent || "").trim();
    if (!text) {
      setStatus("Nothing in this message to save");
      return;
    }
    const original = button?.textContent || "KNOW+";
    if (button) { button.disabled = true; button.textContent = "SAVING"; }
    try {
      const data = await api("/api/rag/chat-knowledge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, role: role === "assistant" ? "assistant" : "user" }),
      });
      bubble.dataset.ragKnowledgeSaved = "1";
      if (button) { button.textContent = "IN RAG"; button.classList.add("is-active"); }
      renderRagStatus(data);
      setStatus(data.duplicate ? "Message content already exists in persistent RAG" : "Message saved to persistent RAG knowledge");
      if (typeof window.showStudioToast === "function") window.showStudioToast("RAG KNOWLEDGE", data.duplicate ? "Duplicate content already indexed" : "Saved · survives restart", "ok");
    } catch (e) {
      if (button) button.textContent = original;
      setStatus(`Save to knowledge failed · ${e.message || e}`);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function attachKnowledgeAction(bubble, role) {
    if (!bubble || !["user", "assistant"].includes(role)) return;
    const actions = bubble.querySelector(".msg-actions");
    if (!actions || actions.querySelector(".btn-save-knowledge")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn ghost sm btn-save-knowledge";
    btn.textContent = bubble.dataset.ragKnowledgeSaved === "1" ? "IN RAG" : "KNOW+";
    btn.title = "Save this message to persistent RAG knowledge. It will survive Studio restarts.";
    btn.setAttribute("aria-label", "Save message to persistent RAG knowledge");
    btn.addEventListener("click", () => saveMessageToKnowledge(bubble, role, btn));
    actions.appendChild(btn);
  }

  async function openRagSourceManager(sourceId) {
    if (!sourceId) return;
    const dialog = $("#modal-rag-source");
    const status = $("#rag-source-modal-status");
    if (!dialog) return;
    state.ragActiveSourceId = sourceId;
    if (status) status.textContent = "Loading source…";
    try {
      const data = await api(`/api/rag/documents/${encodeURIComponent(sourceId)}`);
      const source = data.source || {};
      state.ragActiveSource = source;
      if ($("#rag-source-modal-title")) $("#rag-source-modal-title").textContent = ragDisplayName(source);
      if ($("#rag-source-name")) $("#rag-source-name").value = source.name || "";
      if ($("#rag-source-label")) $("#rag-source-label").value = source.label || "";
      if ($("#rag-source-group")) $("#rag-source-group").value = source.group || "";
      if ($("#rag-source-tags")) $("#rag-source-tags").value = (source.tags || []).join(", ");
      if ($("#rag-source-enabled")) $("#rag-source-enabled").checked = source.enabled !== false;
      if ($("#rag-source-pinned")) $("#rag-source-pinned").checked = !!source.pinned;
      if ($("#rag-source-text-preview")) $("#rag-source-text-preview").value = data.text || "";
      if ($("#rag-source-modal-meta")) {
        $("#rag-source-modal-meta").textContent = `${ragKindLabel(source)} · ${Number(source.chunks || 0)} chunks · ${formatBytes(source.bytes || 0)} · added ${source.created_at || "unknown"} · updated ${source.updated_at || "unknown"}${data.trimmed ? " · preview trimmed" : ""}`;
      }
      if (status) status.textContent = source.enabled === false ? "Disabled · retained locally but excluded from retrieval." : "Ready.";
      if (!dialog.open) dialog.showModal();
    } catch (e) {
      if (status) status.textContent = `Load failed · ${e.message || e}`;
      setStatus(`RAG source preview failed · ${e.message || e}`);
    }
  }

  async function saveRagSourceManager() {
    const sid = state.ragActiveSourceId;
    if (!sid) return;
    const status = $("#rag-source-modal-status");
    if (status) status.textContent = "Saving source metadata…";
    const tags = String($("#rag-source-tags")?.value || "").split(/[,;\n]/).map((v) => v.trim()).filter(Boolean);
    try {
      const data = await api(`/api/rag/documents/${encodeURIComponent(sid)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: String($("#rag-source-name")?.value || "").trim(),
          label: String($("#rag-source-label")?.value || "").trim(),
          group: String($("#rag-source-group")?.value || "").trim(),
          tags,
          enabled: !!$("#rag-source-enabled")?.checked,
          pinned: !!$("#rag-source-pinned")?.checked,
        }),
      });
      renderRagStatus(data);
      if (status) status.textContent = "Saved.";
      await openRagSourceManager(sid);
      setStatus("RAG source metadata saved");
    } catch (e) {
      if (status) status.textContent = `Save failed · ${e.message || e}`;
    }
  }

  async function reindexActiveRagSource() {
    const sid = state.ragActiveSourceId;
    if (!sid) return;
    const status = $("#rag-source-modal-status");
    if (status) status.textContent = "Reindexing this source…";
    try {
      const data = await api(`/api/rag/documents/${encodeURIComponent(sid)}/reindex`, { method: "POST" });
      renderRagStatus(data);
      if (status) status.textContent = "Reindexed with current chunk settings.";
      await openRagSourceManager(sid);
      setStatus("RAG source reindexed");
    } catch (e) {
      if (status) status.textContent = `Reindex failed · ${e.message || e}`;
    }
  }

  async function removeActiveRagSource() {
    const sid = state.ragActiveSourceId;
    const doc = state.ragActiveSource || state.ragDocuments?.find((d) => d.id === sid);
    if (!sid) return;
    const display = ragDisplayName(doc || { id: sid });
    if (state.settings?.confirm_destructive !== false && !confirm(`Remove ${display} from persistent RAG knowledge?`)) return;
    try {
      const data = await api(`/api/rag/documents/${encodeURIComponent(sid)}`, { method: "DELETE" });
      renderRagStatus(data);
      $("#modal-rag-source")?.close();
      state.ragActiveSourceId = null;
      state.ragActiveSource = null;
      setStatus(`RAG source removed · ${display}`);
    } catch (e) {
      setStatus(`RAG remove failed · ${e.message || e}`);
    }
  }

  async function testRagSearch() {
    const query = String($("#rag-test-query")?.value || "").trim();
    const out = $("#rag-test-results");
    if (!query) {
      if (out) out.textContent = "Enter a retrieval query first.";
      return;
    }
    if (out) out.textContent = "Searching local index…";
    try {
      const minScore = Number($("#set-rag-min-score")?.value ?? state.settings?.rag_min_score ?? 0.25);
      const data = await api("/api/rag/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: Number($("#set-rag-top-k")?.value || 4), min_score: Number.isFinite(minScore) ? minScore : 0.25 }),
      });
      const hits = Array.isArray(data.hits) ? data.hits : [];
      if (!hits.length) {
        if (out) out.textContent = `No indexed excerpt cleared the relevance threshold (${Number(data.min_score || 0).toFixed(2)}).`;
        return;
      }
      if (out) out.textContent = hits.map((hit, i) => {
        const coverage = Math.round(Number(hit.coverage || 0) * 100);
        const terms = (hit.matched_terms || []).join(", ") || "none";
        const flags = [hit.pinned ? "PINNED" : "", hit.group ? `GROUP ${hit.group}` : ""].filter(Boolean).join(" · ");
        return `[RAG ${i + 1}] ${hit.source} · chunk ${hit.chunk}\nscore ${Number(hit.score || 0).toFixed(3)} · base ${Number(hit.base_score || 0).toFixed(3)} · coverage ${coverage}%${flags ? ` · ${flags}` : ""}\nmatched: ${terms}\n${hit.snippet || ""}`;
      }).join("\n\n");
    } catch (e) {
      if (out) out.textContent = `Search failed · ${e.message || e}`;
    }
  }

  function downloadRagBundleCopy(bundle, filename) {
    try {
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json;charset=utf-8" });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = filename || "cypra-rag-knowledge.json";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
      return true;
    } catch (_) { return false; }
  }

  async function exportRagBundle() {
    const button = $("#btn-rag-export");
    const prior = button?.textContent || "EXPORT KNOWLEDGE";
    if (button) { button.disabled = true; button.textContent = "EXPORTING…"; }
    try {
      const data = await api("/api/rag/bundle/export-file", { method: "POST" });
      downloadRagBundleCopy(data.bundle, data.filename || "cypra-rag-knowledge.json");
      if ($("#rag-transfer-status")) $("#rag-transfer-status").textContent = `Knowledge bundle exported · ${data.path || "MatrixFiles/Exports"}`;
      setStatus(`RAG knowledge exported · ${Number(data.bundle?.source_count || 0)} sources`);
    } catch (e) {
      setStatus(`RAG export failed · ${e.message || e}`);
    } finally {
      if (button) { button.disabled = false; button.textContent = prior; }
    }
  }

  async function importRagBundleFile(file) {
    if (!file) return;
    if (file.size > 64 * 1024 * 1024) throw new Error("RAG knowledge bundle is larger than 64 MB");
    let payload;
    try { payload = JSON.parse(await file.text()); }
    catch (_) { throw new Error("RAG knowledge bundle is not valid JSON"); }
    const data = await api("/api/rag/bundle/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderRagStatus(data);
    setStatus(`RAG knowledge imported · ${Number(data.imported || 0)} new · ${Number(data.duplicates || 0)} duplicates${data.failed ? ` · ${data.failed} failed` : ""}`);
    if ($("#rag-transfer-status")) $("#rag-transfer-status").textContent = `Import complete · ${Number(data.imported || 0)} new · ${Number(data.duplicates || 0)} duplicates${data.failed ? ` · ${data.failed} failed` : ""}`;
  }

  function bindRagUi() {
    const root = $("#panel-rag") || document.body;
    if (root.dataset.ragBound === "1") return;
    root.dataset.ragBound = "1";
    $("#set-rag-enabled")?.addEventListener("change", () => setRagEnabled(!!$("#set-rag-enabled").checked));
    $("#rag-enabled-quick")?.addEventListener("change", () => setRagEnabled(!!$("#rag-enabled-quick").checked));
    for (const [id, key, note] of [
      ["#set-rag-top-k", "rag_top_k", "RAG retrieval depth saved"],
      ["#set-rag-context-chars", "rag_context_chars", "RAG prompt budget saved"],
      ["#set-rag-chunk-chars", "rag_chunk_chars", "RAG chunk size saved · reindex existing sources to apply"],
      ["#set-rag-chunk-overlap", "rag_chunk_overlap", "RAG overlap saved · reindex existing sources to apply"],
      ["#set-rag-min-score", "rag_min_score", "RAG minimum relevance saved"],
    ]) {
      $(id)?.addEventListener("change", async () => {
        const value = Number($(id).value);
        if (!Number.isFinite(value)) return;
        try { await saveRagPatch({ [key]: key === "rag_min_score" ? value : Math.round(value) }, note); }
        catch (e) { setStatus(`RAG setting failed · ${e.message || e}`); }
      });
    }
    $("#btn-rag-add")?.addEventListener("click", () => $("#rag-file-input")?.click());
    $("#rag-file-input")?.addEventListener("change", async (event) => {
      const files = [...(event.target.files || [])];
      let indexed = 0;
      let duplicates = 0;
      try {
        for (const file of files) {
          const result = await uploadRagFile(file);
          if (result?.duplicate) duplicates += 1;
          else if (result?.ok) indexed += 1;
        }
        if (files.length > 1) setStatus(`RAG files processed · ${indexed} indexed${duplicates ? ` · ${duplicates} duplicate-content matches` : ""}`);
      } catch (e) {
        setStatus(`RAG upload failed · ${e.message || e}`);
        if ($("#rag-transfer-status")) $("#rag-transfer-status").textContent = e.message || String(e);
      } finally { event.target.value = ""; }
    });
    $("#btn-rag-reindex")?.addEventListener("click", async () => {
      const status = $("#rag-transfer-status");
      if (status) status.textContent = "Rebuilding local RAG index…";
      try {
        const data = await api("/api/rag/reindex", { method: "POST" });
        renderRagStatus(data);
        setStatus(`RAG reindexed · ${data.sources || 0} sources · ${data.chunks || 0} chunks`);
      } catch (e) { setStatus(`RAG reindex failed · ${e.message || e}`); }
    });
    $("#btn-rag-export")?.addEventListener("click", exportRagBundle);
    $("#btn-rag-import")?.addEventListener("click", () => $("#rag-bundle-input")?.click());
    $("#rag-bundle-input")?.addEventListener("change", async (event) => {
      const file = event.target.files?.[0];
      try { await importRagBundleFile(file); }
      catch (e) { setStatus(`RAG import failed · ${e.message || e}`); }
      finally { event.target.value = ""; }
    });
    $("#btn-rag-open-folder")?.addEventListener("click", async () => {
      try { await api("/api/rag/folder/open", { method: "POST" }); setStatus("RAG folder opened"); }
      catch (e) { setStatus(e.message || "Could not open RAG folder"); }
    });
    $("#btn-rag-clear")?.addEventListener("click", async () => {
      if (!confirm("Clear ALL persistent RAG knowledge? This removes indexed files, manual notes, and saved chat facts.")) return;
      try {
        const data = await api("/api/rag/clear", { method: "POST" });
        renderRagStatus(data);
        setStatus(`RAG cleared · ${Number(data.sources_removed || 0)} sources removed`);
      } catch (e) { setStatus(`RAG clear failed · ${e.message || e}`); }
    });
    $("#btn-rag-save-manual")?.addEventListener("click", saveManualRagKnowledge);
    $("#rag-manual-text")?.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); saveManualRagKnowledge(); }
    });
    for (const id of ["#rag-source-filter", "#rag-group-filter"]) {
      $(id)?.addEventListener("change", () => renderRagDocuments(state.ragDocuments || []));
    }
    $("#rag-source-search")?.addEventListener("input", () => renderRagDocuments(state.ragDocuments || []));
    $("#btn-rag-test")?.addEventListener("click", testRagSearch);
    $("#rag-test-query")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); testRagSearch(); }
    });
    $("#btn-rag-source-save")?.addEventListener("click", saveRagSourceManager);
    $("#btn-rag-source-reindex")?.addEventListener("click", reindexActiveRagSource);
    $("#btn-rag-source-remove")?.addEventListener("click", removeActiveRagSource);
    $("#modal-rag-source")?.addEventListener("close", () => {
      state.ragActiveSourceId = null;
      state.ragActiveSource = null;
    });
  }

  // ── ingest / query / settings ───────────────────────────────────

  function enableSettingsDrag() {
    const dialog = $("#modal-settings");
    const handle = $("#settings-drag-handle");
    if (!dialog || !handle || handle.dataset.dragBound) return;
    handle.dataset.dragBound = "1";
    let dragging = false;
    let ox = 0;
    let oy = 0;

    const clampPosition = (left, top) => {
      const w = dialog.offsetWidth || 0;
      const h = dialog.offsetHeight || 0;
      return {
        left: Math.max(8, Math.min(Math.max(8, window.innerWidth - w - 8), left)),
        top: Math.max(8, Math.min(Math.max(8, window.innerHeight - h - 8), top)),
      };
    };

    const move = (e) => {
      if (!dragging) return;
      const pos = clampPosition(e.clientX - ox, e.clientY - oy);
      dialog.style.left = pos.left + "px";
      dialog.style.top = pos.top + "px";
      dialog.classList.add("settings-dragging");
      e.preventDefault();
    };

    const end = () => {
      if (!dragging) return;
      dragging = false;
      dialog.classList.remove("settings-dragging");
      document.removeEventListener("pointermove", move, true);
      document.removeEventListener("pointerup", end, true);
      document.removeEventListener("pointercancel", end, true);
    };

    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      if (e.target.closest("button, a, input, select, textarea")) return;
      const rect = dialog.getBoundingClientRect();
      dragging = true;
      ox = e.clientX - rect.left;
      oy = e.clientY - rect.top;
      dialog.style.margin = "0";
      dialog.style.position = "fixed";
      dialog.style.left = rect.left + "px";
      dialog.style.top = rect.top + "px";
      dialog.style.transform = "none";
      dialog.classList.add("settings-dragging");
      document.addEventListener("pointermove", move, true);
      document.addEventListener("pointerup", end, true);
      document.addEventListener("pointercancel", end, true);
      try { handle.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
      e.stopPropagation();
    });
  }

  /** Filter settings fields/cards by search query for readability. */
  function filterSettingsSearch(q) {
    const query = String(q || "")
      .trim()
      .toLowerCase();
    const emptyEl = $("#settings-search-empty");
    const panels = $$(".stab-panel");
    const cards = $$(".settings-card, .settings-grid label, .settings-checks .check, .stab-panel > label, .stab-panel > .check");

    // reset
    $$(".settings-search-hide").forEach((el) => el.classList.remove("settings-search-hide"));
    panels.forEach((p) => p.classList.remove("settings-search-hide"));

    if (!query) {
      if (emptyEl) emptyEl.classList.add("hidden");
      return;
    }

    let any = false;
    const matchPanel = new Set();

    // Match cards and top-level fields
    $$(".settings-card").forEach((card) => {
      const text = (card.textContent || "").toLowerCase();
      const hit = text.includes(query);
      if (!hit) card.classList.add("settings-search-hide");
      else {
        any = true;
        const panel = card.closest(".stab-panel");
        if (panel) matchPanel.add(panel.id);
      }
    });

    // Loose labels not in cards
    $$(".stab-panel > label, .stab-panel .settings-grid > label, .stab-panel .settings-checks > .check").forEach(
      (lab) => {
        if (lab.closest(".settings-card")) return;
        const text = (lab.textContent || "").toLowerCase();
        if (!text.includes(query)) lab.classList.add("settings-search-hide");
        else {
          any = true;
          const panel = lab.closest(".stab-panel");
          if (panel) matchPanel.add(panel.id);
        }
      }
    );

    // Hide panels with no matches; activate first match
    panels.forEach((p) => {
      if (!matchPanel.has(p.id)) p.classList.add("settings-search-hide");
    });
    if (matchPanel.size) {
      const firstId = [...matchPanel][0];
      const stab = firstId.replace(/^stab-/, "");
      $$(".stab").forEach((t) => t.classList.toggle("active", t.dataset.stab === stab));
      panels.forEach((p) => {
        p.classList.toggle("active", p.id === firstId);
        if (p.id === firstId) p.classList.remove("settings-search-hide");
      });
    }

    if (emptyEl) emptyEl.classList.toggle("hidden", any);
  }

  async function openSettings({ asTab = true } = {}) {
    // Settings is a first-class Studio workspace. Do not make the UI wait on
    // health/model/plugin calls, and do not touch chat context or keep-alive.
    const dialog = $("#modal-settings");
    if (dialog && !dialog.open) {
      try {
        if (asTab && typeof dialog.show === "function") dialog.show();
        else if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      } catch (_) { dialog.setAttribute("open", ""); }
    }
    try {
      const s = await api("/api/settings");
      state.settings = s;
      fillSettingsForm(s);
      syncStudioSettingsSummary();
      
    } catch (_) {
      fillSettingsForm(state.settings || {});
      syncStudioSettingsSummary();
      
    }

    // Heavy/optional settings data is deliberately deferred until after the
    // window is visible so the button never causes a long blank/blocking pause.
    window.setTimeout(() => {
      refreshPlugins().catch(() => {});
      refreshRagStatus().catch(() => {});
    }, 250);
  }

  
  let edgeVoicesLoaded = false;
  function syncTTSProviderUI() {
    const provider = $("#set-tts-provider")?.value || "local";
    const edge = provider === "edge";
    if ($("#tts-local-voice-field")) $("#tts-local-voice-field").hidden = edge;
    if ($("#tts-edge-controls")) $("#tts-edge-controls").hidden = !edge;
  }

  async function loadEdgeVoices() {
    if (edgeVoicesLoaded) return;
    if (!$("#set-voice-output")?.checked || $("#set-tts-provider")?.value !== "edge" || !$("#set-tts-allow-online")?.checked) {
      setStatus("Enable Voice Output, select Edge Online, and allow online TTS first");
      return;
    }
    try {
      await api("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice_output_enabled: true, tts_provider: "edge", tts_allow_online: true }),
      });
      const result = await api("/api/tts/voices/edge");
      const voices = Array.isArray(result.voices) ? result.voices : [];
      const select = $("#set-tts-edge-voice");
      if (select && voices.length) {
        const selected = state.settings?.tts_edge_voice || select.value || "en-US-AvaNeural";
        select.innerHTML = "";
        voices.forEach((voice) => {
          const option = document.createElement("option");
          option.value = voice.short_name;
          option.textContent = `${voice.friendly_name || voice.short_name}${voice.gender ? ` · ${voice.gender}` : ""}`;
          select.appendChild(option);
        });
        select.value = voices.some((voice) => voice.short_name === selected) ? selected : voices[0].short_name;
        edgeVoicesLoaded = true;
      }
    } catch (error) {
      setStatus(`Edge voices unavailable · ${error.message || error}`);
    }
  }

  async function refreshLocalTTSStatus() {
    const line = $("#local-tts-status");
    try {
      const status = await api("/api/tts/status");
      const local = status.local || {};
      const select = $("#set-tts-local-voice");
      const voices = Array.isArray(local.voices) ? local.voices : [];
      const voiceLabel = (voice) => {
        const match = String(voice).match(/^en_(US|GB)-(.+)-(low|medium|high)$/i);
        if (!match) return voice;
        const accent = match[1].toUpperCase() === "GB" ? "English UK" : "English US";
        const name = match[2].split("_").map((part) => part ? part[0].toUpperCase() + part.slice(1) : part).join(" ");
        const quality = match[3][0].toUpperCase() + match[3].slice(1).toLowerCase();
        return `${name} · ${accent} · ${quality}`;
      };
      if (select && voices.length) {
        const selected = state.settings?.tts_local_voice || select.value || voices[0];
        select.innerHTML = "";
        voices.forEach((voice) => {
          const option = document.createElement("option");
          option.value = voice;
          option.textContent = voiceLabel(voice);
          select.appendChild(option);
        });
        select.value = voices.includes(selected) ? selected : voices[0];
      }
      if (line) {
        line.textContent = local.ready
          ? `PIPER · CPU · ${voices.length} local voice(s) · lazy ${local.lazy ? "YES" : "NO"}`
          : `PIPER · CPU · NOT INSTALLED · expected in ${local.voices_dir || "MatrixFiles/Voice/Piper"}`;
      }
    } catch (_) {
      if (line) line.textContent = "PIPER · CPU · STATUS UNAVAILABLE";
    }
  }

  function syncStudioSettingsSummary() {
    const s = state.settings || {};
    if ($("#studio-settings-think")) $("#studio-settings-think").value = normalizeThinkMode(s.think_mode || "auto");
    syncThinkQuickMode();
    const configured = Number(s.ollama_num_ctx ?? 8192);
    if ($("#set-ollama-ctx")) $("#set-ollama-ctx").value = String(normalizeStudioContext(configured));
    if ($("#studio-live-context")) $("#studio-live-context").textContent = normalizeStudioContext(configured).toLocaleString();
    refreshLocalTTSStatus();

    const runtimeModel = state.matrixRuntime?.active_model || "";
    const activeModel = runtimeModel || state.local?.active_model || s.chat_model_active || s.ollama_chat_model || s.chat_model || "";
    if (activeModel) {
      updateStudioPullProgress({
        running: false,
        ok: true,
        status: `READY · ${activeModel}`,
        stage: "ready",
        percent: 100,
        bytes_done: 0,
        bytes_total: 0,
        current: activeModel,
      });
    }
  }

  

  async function refreshStudioRuntime(trace=false) {
    try {
      const rt = await api("/api/matrix/runtime");
      state.matrixRuntime = rt;
      const h = state.honesty || {};
      const loadedModel = rt.loaded_model || h.resident_model || (h.warming ? `LOADING · ${h.warm_model || h.chat_model || rt.active_model || "MODEL"}` : "NO MODEL LOADED");
      const values = {
        "#studio-live-model": rt.active_model || rt.agent_base_model || rt.configured_model || "NO MODEL",
        "#studio-live-loaded-model": loadedModel,
        "#studio-live-endpoint": rt.endpoint || "—",
        "#studio-live-store": rt.model_store || "—",
        "#studio-live-agent": rt.agent_label || rt.active_agent || "NO AGENT",
        "#studio-live-history-scope": rt.history_scope || "CURRENT CHAT ONLY",
        "#studio-live-history-turns": String(rt.history_turns || 24),
        "#studio-live-handoff": rt.handoff ? "ON" : "OFF",
      };
      Object.entries(values).forEach(([selector, value]) => {
        const el = $(selector);
        if (el) el.textContent = value;
      });
      const gpu = rt.gpu || {};
      if ($("#studio-live-gpu")) $("#studio-live-gpu").textContent = gpu.name || "—";
      if ($("#studio-live-vram")) $("#studio-live-vram").textContent = (gpu.vram_used != null && gpu.vram_total != null) ? `${gpu.vram_used} / ${gpu.vram_total} MB` : "—";
      if ($("#studio-live-util")) $("#studio-live-util").textContent = gpu.util != null ? `${gpu.util}%` : "—";
      if ($("#studio-live-temp")) $("#studio-live-temp").textContent = gpu.temp != null ? `${gpu.temp}°C` : "—";
      syncStudioSettingsSummary();
      return rt;
    } catch (_) {
      return null;
    }
  }

  function bindKillLocalhost() {
    const btn = $("#btn-kill-localhost");
    if (!btn || btn.dataset.bound === "1") return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      btn.disabled = true;
      btn.textContent = "OPENING…";
      setStatus("Opening Kill Localhost…");
      try {
        const r = await fetch("/api/runtime/kill", { method: "POST" });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
        setStatus("Kill Localhost launched · confirm in the Windows dialog");
      } catch (err) {
        setStatus(err.message || "Kill localhost failed");
      } finally {
        btn.disabled = false;
        btn.textContent = "KILL LOCALHOST";
      }
    });
    $("#runtime-warm")?.addEventListener("click", async (event) => {
      const warm = event.currentTarget;
      warm.disabled = true;
      warm.textContent = "WARMING…";
      try { await doWarmModel(); } finally {
        warm.disabled = false;
        warm.textContent = "WARM MODEL";
        $("#runtime-refresh")?.click();
      }
    });
    $("#runtime-refresh")?.addEventListener("click", async () => {
      const refresh = $("#runtime-refresh");
      if (refresh) { refresh.disabled = true; refresh.textContent = "CHECKING…"; }
      try {
        await refreshStudioRuntime(false);
        const st = await api("/api/llm/status");
        const h = st.honesty || {};
        if ($("#runtime-control-quant")) $("#runtime-control-quant").textContent = String(h.quantization || "UNKNOWN").toUpperCase();
        if ($("#runtime-control-plan-b")) $("#runtime-control-plan-b").textContent = `${h.tuning_mode === "plan-b-auto" ? "AUTO" : "MANUAL"} · ${h.num_batch || "—"}`;
        if ($("#runtime-control-kv")) $("#runtime-control-kv").textContent = String(h.kv_cache_quantization || "q8_0").toUpperCase();
        if ($("#runtime-control-flash")) $("#runtime-control-flash").textContent = h.flash_attention === false ? "OFF" : "ON";
        if ($("#runtime-control-concurrency")) $("#runtime-control-concurrency").textContent = `${h.max_loaded_models || 1} MODEL · ${h.num_parallel || 1} REQUEST`;
        if ($("#runtime-control-note")) $("#runtime-control-note").textContent = h.line || "Runtime status refreshed.";
      } catch (err) {
        setStatus(err.message || "Runtime status failed");
      } finally {
        if (refresh) { refresh.disabled = false; refresh.textContent = "CHECK STATUS"; }
      }
    });
    $("#runtime-unload")?.addEventListener("click", async (event) => {
      const unload = event.currentTarget;
      unload.disabled = true;
      unload.textContent = "UNLOADING…";
      setStatus("Unloading resident Ollama model…");
      try {
        const result = await api("/api/llm/unload", { method: "POST" });
        setStatus(`VRAM released · ${(result.unloaded || []).length} model(s) unloaded`);
        $("#runtime-refresh")?.click();
      } catch (err) { setStatus(err.message || "VRAM unload failed"); }
      finally { unload.disabled = false; unload.textContent = "UNLOAD VRAM"; }
    });
    $("#runtime-refresh")?.click();
  }



  
  function setConfigTransferStatus(message, level = "") {
    const el = $("#config-transfer-status");
    if (!el) return;
    el.textContent = String(message || "");
    if (level) el.dataset.level = level;
    else el.removeAttribute("data-level");
  }

  function downloadStudioConfigCopy(config, filename) {
    if (!config || typeof config !== "object") return false;
    try {
      const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json;charset=utf-8" });
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = filename || "cypra-matrix-studio-config.json";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
      return true;
    } catch (_) {
      return false;
    }
  }

  async function exportStudioConfig() {
    const button = $("#btn-export-config");
    const prior = button?.textContent || "EXPORT CONFIG";
    if (button) { button.disabled = true; button.textContent = "EXPORTING…"; }
    setConfigTransferStatus("Exporting current Studio configuration…");
    try {
      // The server builds the export from live settings and atomically saves a
      // durable copy under MatrixFiles/Exports. The browser download is a
      // convenience copy only; WebView2 download behavior cannot lose the backup.
      const result = await api("/api/studio/config/export-file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format: "cypra-matrix-studio-config" }),
      });
      const config = result.config || await api("/api/studio/config/export");
      downloadStudioConfigCopy(config, result.filename || "cypra-matrix-studio-config.json");
      const path = result.path || "MatrixFiles/Exports";
      setConfigTransferStatus(`Exported · ${path}`, "ok");
      setStatus("Configuration exported");
      if (typeof window.showStudioToast === "function") {
        window.showStudioToast("CONFIG EXPORTED", "Portable settings backup written to MatrixFiles/Exports.", "ok");
      }
    } catch (error) {
      setConfigTransferStatus(error.message || "Configuration export failed", "bad");
      setStatus(error.message || "Configuration export failed");
    } finally {
      if (button) { button.disabled = false; button.textContent = prior; }
    }
  }

  async function importStudioConfigFile(file) {
    const input = $("#config-import-file");
    const button = $("#btn-import-config");
    const prior = button?.textContent || "IMPORT CONFIG";
    try {
      if (!file) return;
      if (file.size > 25 * 1024 * 1024) throw new Error("Configuration file is larger than 25 MB");
      setConfigTransferStatus(`Reading ${file.name || "configuration"}…`);
      let payload;
      try {
        payload = JSON.parse(await file.text());
      } catch (_) {
        throw new Error("Configuration file is not valid JSON");
      }
      if (!payload || payload.format !== "cypra-matrix-studio-config" || typeof payload.settings !== "object" || Array.isArray(payload.settings)) {
        throw new Error("Not a valid Cypra Matrix Studio configuration file");
      }
      const version = Number(payload.version ?? 1);
      if (!Number.isInteger(version) || version < 1) throw new Error("Configuration version is invalid");
      const count = Object.keys(payload.settings || {}).length;
      const agents = Array.isArray(payload.custom_agents) ? payload.custom_agents.length : 0;
      if (state.settings?.confirm_destructive !== false) {
        const summary = `Import ${count} settings${agents ? ` and ${agents} custom agent file(s)` : ""}?\n\nCurrent chats, models, and memory are not replaced.`;
        if (!confirm(summary)) {
          setConfigTransferStatus("Import cancelled");
          return;
        }
      }
      if (button) { button.disabled = true; button.textContent = "IMPORTING…"; }
      setConfigTransferStatus("Validating and importing configuration…");
      const result = await api("/api/studio/config/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      state.settings = result.settings || await api("/api/settings");
      cachePersistedAppearance(state.settings);
      fillSettingsForm(state.settings);
      applyUiTheme(state.settings);
      livePreviewSettings();
      state.speakReplies = !!state.settings.speak_replies;
      updateStudioChatSnapshot();
      await refreshState().catch(() => {});
      const importedAgents = Number(result.custom_agents_imported || 0);
      const skippedAgents = Number(result.custom_agents_skipped || 0);
      const context = Number(result.context_tokens || state.settings?.ollama_num_ctx || 8192).toLocaleString();
      const agentText = importedAgents ? ` · ${importedAgents} custom agent${importedAgents === 1 ? "" : "s"}` : "";
      const skippedText = skippedAgents ? ` · ${skippedAgents} skipped` : "";
      setConfigTransferStatus(`Imported · context ${context}${agentText}${skippedText}`, skippedAgents ? "warn" : "ok");
      setStatus("Configuration imported");
      if (typeof window.showStudioToast === "function") {
        window.showStudioToast("CONFIG IMPORTED", `Settings restored · context ${context}.`, skippedAgents ? "warn" : "ok");
      }
    } catch (error) {
      setConfigTransferStatus(error.message || "Configuration import failed", "bad");
      setStatus(error.message || "Configuration import failed");
    } finally {
      if (button) { button.disabled = false; button.textContent = prior; }
      if (input) input.value = "";
    }
  }

  function exportSessionById(sid, title) {
    if (!sid) return exportCurrentSession();
    const a = document.createElement("a");
    a.href = `/api/session/export?session_id=${encodeURIComponent(sid)}`;
    a.download = `${String(title || "chat").replace(/[^\w\-]+/g, "_").slice(0, 40) || "chat"}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function exportCurrentSession() {
    const sid = state.sessionId || "";
    const url = sid ? `/api/session/export?session_id=${encodeURIComponent(sid)}` : "/api/session/export";
  }

  function updateStudioChatSnapshot() {
    const log = $("#chat-log");
    const count = log ? log.querySelectorAll(".msg").length : 0;
    const agent = state.settings?.matrix_agent || state.matrix?.agent?.slug || $("#matrix-agent-quick")?.value || "NO AGENT";
    const model = state.local?.active_model || state.settings?.ollama_chat_model || state.settings?.chat_model || "NO MODEL";
    const handoff = !!state.settings?.matrix_handoff;
    const values = {
      "#studio-status-agent": agent,
      "#studio-status-model": model,
      "#studio-compose-messages": `${count} msgs`,
      "#studio-live-message-count": String(count),
      "#studio-live-agent": agent,
      "#studio-live-handoff": handoff ? "ON" : "OFF",
    };
    Object.entries(values).forEach(([selector, value]) => {
      const el = $(selector);
      if (el) el.textContent = value;
    });
    const toggle = $("#studio-chat-snapshot-handoff-toggle");
    if (toggle) toggle.checked = handoff;
  }


  function bindStudioInlineSettings() {
    $("#studio-settings-think")?.addEventListener("change", async () => {
      const mode = normalizeThinkMode($("#studio-settings-think").value);
      await persistThinkMode(mode, "Settings");
    });
    $("#set-think-budget")?.addEventListener("change", async () => {
      const el = $("#set-think-budget");
      const value = Math.max(128, Math.min(8192, Math.round(Number(el?.value || 768))));
      if (el) el.value = String(value);
      try {
        const data = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ think_budget_tokens: value }) });
        state.settings = { ...(state.settings || {}), ...(data.settings || {}), think_budget_tokens: value };
        setStatus(`Reasoning budget target saved · ${value.toLocaleString()} tokens`);
      } catch (e) { setStatus(`Reasoning budget save failed · ${e.message || e}`); }
    });

    $("#set-ollama-ctx")?.addEventListener("change", async () => {
      const control = $("#set-ollama-ctx");
      if (!control) return;
      const previous = normalizeStudioContext(state.settings?.ollama_num_ctx ?? 8192);
      const value = normalizeStudioContext(control.value);
      control.value = String(value);
      control.disabled = true;
      setStatus(`Applying context · ${value.toLocaleString()} tokens`);
      try {
        const data = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ollama_num_ctx: value }),
        });
        const saved = normalizeStudioContext(data.settings?.ollama_num_ctx ?? value);
        state.settings = { ...(state.settings || {}), ...(data.settings || {}), ollama_num_ctx: saved };
        control.value = String(saved);
        if ($("#studio-live-context")) $("#studio-live-context").textContent = saved.toLocaleString();
        setStatus(`Context saved · ${saved.toLocaleString()} tokens`);
        if (typeof window.showStudioToast === "function") {
          window.showStudioToast("CONTEXT", `${saved.toLocaleString()} tokens · applied to Ollama chat + Matrix agents`, "ok");
        }
      } catch (error) {
        control.value = String(previous);
        if ($("#studio-live-context")) $("#studio-live-context").textContent = previous.toLocaleString();
        setStatus(`Context save failed · ${error.message || error}`);
      } finally {
        control.disabled = false;
      }
    });

    const testVoice = async () => {
      const sample = "Matrix voice playback test. Local CPU speech is operational.";
      setStatus("Voice test…");
      try {
        setSpeakStopVisible(true);
        CypraVoice?.invalidateProviderCache?.();
        const provider = $("#set-tts-provider")?.value || "local";
        await CypraVoice.speak(sample, {
          provider,
          voiceId: provider === "edge"
            ? ($("#set-tts-edge-voice")?.value || "en-US-AvaNeural")
            : ($("#set-tts-local-voice")?.value || "en_US-lessac-medium"),
          preview: true,
        });
        setStatus("Voice test finished");
      } catch (error) {
        setStatus("Voice test: " + (error.message || error));
      } finally {
        setSpeakStopVisible(false);
      }
    };
    $("#btn-tts-test-legacy")?.addEventListener("click", testVoice);
    $("#btn-tts-test")?.addEventListener("click", () => {
      const talk = $("#talk-mode-quick");
      if (!talk) return;
      talk.checked = !talk.checked;
      talk.dispatchEvent(new Event("change", { bubbles: true }));
      $("#btn-tts-test").textContent = talk.checked ? "Stop talk" : "Start talk";
      $("#btn-tts-test").classList.toggle("primary", talk.checked);
    });
    $("#set-voice-output")?.addEventListener("change", () => {
      CypraVoice?.stopSpeak?.({ release: !$("#set-voice-output").checked });
      syncTTSProviderUI();
    });
    $("#set-tts-provider")?.addEventListener("change", () => {
      CypraVoice?.stopSpeak?.({ release: $("#set-tts-provider").value !== "local" });
      CypraVoice?.invalidateProviderCache?.();
      syncTTSProviderUI();
    });
    $("#set-tts-allow-online")?.addEventListener("change", () => {
      CypraVoice?.stopSpeak?.();
      syncTTSProviderUI();
    });
    $("#btn-edge-voices")?.addEventListener("click", loadEdgeVoices);

    $("#studio-chat-snapshot-handoff-toggle")?.addEventListener("change", async (event) => {
      const on = !!event.target.checked;
      if ($("#set-matrix-handoff")) $("#set-matrix-handoff").checked = on;
      if (state.settings) state.settings.matrix_handoff = on;
      try {
        await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ matrix_handoff: on }) });
        setStatus(on ? "Agent handoff enabled for this chat" : "Agent handoff disabled");
      } catch (error) {
        event.target.checked = !on;
        if ($("#set-matrix-handoff")) $("#set-matrix-handoff").checked = !on;
        alert(error.message);
      }
      updateStudioChatSnapshot();
    });
    $("#btn-export-session")?.addEventListener("click", () => exportCurrentSession());
  }

  function bindWizardControls() {
    const go = $("#wiz-go");
    if (go && go.dataset.wizardBound !== "1") {
      go.dataset.wizardBound = "1";
      go.addEventListener("click", (event) => {
        event.preventDefault();
        finishWizard();
      });
    }
    const skip = $("#wiz-skip");
    if (skip && skip.dataset.wizardBound !== "1") {
      skip.dataset.wizardBound = "1";
      skip.addEventListener("click", (event) => {
        event.preventDefault();
        skipWizard();
      });
    }
  }

  async function maybeShowWizard() {
    try {
      const o = await api("/api/onboarding");
      if (o.needs_onboarding) $("#wizard")?.classList.remove("hidden");
    } catch (_) {}
  }

  async function finishWizard() {
    const prov = state.settings?.llm_provider || "ollama";
    try {
      await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ llm_provider: prov, onboarding_done: true }),
      });
      await api("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ done: true }),
      });
      $("#wizard")?.classList.add("hidden");
      await refreshState();
      setStatus("Setup saved");
    } catch (e) {
      alert(e.message);
    }
  }

  async function skipWizard() {
    try {
      await api("/api/onboarding", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ done: true }),
      });
    } catch (_) {}
    $("#wizard")?.classList.add("hidden");
  }

  function closeSettingsModal() {
    const dlg = $("#modal-settings");
    if (!dlg) return;
    try {
      if (typeof dlg.close === "function" && dlg.open) dlg.close();
    } catch (_) {}
    dlg.removeAttribute("open");
    dlg.classList.remove("open", "visible", "show");
    document.body.classList.remove("modal-open", "settings-open", "settings-tab-active", "studio-primary-view-settings");
    setPrimaryStudioView("chat");
  }

  async function saveSettings(e) {
    if (e && e.preventDefault) e.preventDefault();
    const body = collectSettingsFromForm();
    if (!body.theme_preset || body.theme_preset === "null") body.theme_preset = "ember";
    const key = $("#set-key")?.value?.trim?.() || "";
    if (key) body.legacy_cloud_key = key;
    try {
      const data = await api("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.settings = { ...(state.settings || {}), ...(data.settings || body) };
      cachePersistedAppearance(state.settings);
      fillSettingsForm(state.settings);
      if (window.CypraVoice?.invalidateProviderCache) CypraVoice.invalidateProviderCache();
      state.speakReplies = !!state.settings.speak_replies;
      applyUiTheme(state.settings);
      updateStudioChatSnapshot();
      setStatus("Settings saved");
      if (typeof window.showStudioToast === "function") window.showStudioToast("SAVED", "All Studio settings written to disk.", "ok");
    } catch (err) {
      alert(err.message);
    }
  }

  let _talkListen = null;
  let _talkListenToken = 0;
  function stopTalkListen() {
    _talkListenToken += 1;
    try { _talkListen?.stop?.(); } catch (_) {}
    _talkListen = null;
  }
  async function scheduleTalkListen() {
    if (!$("#talk-mode-quick")?.checked || state.busy) return;
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Rec) {
      if (_talkListen) return;
      const nativeApi = window.pywebview?.api;
      if (typeof nativeApi?.listen_for_speech !== "function") {
        setStatus("Talk mode: Windows speech recognition is unavailable.");
        return;
      }
      const token = ++_talkListenToken;
      _talkListen = { kind: "windows", token };
      setStatus("Talk · listening as " + ($("#matrix-agent-quick")?.value || "agent") + "…");
      try {
        const result = await nativeApi.listen_for_speech();
        if (token !== _talkListenToken || !$("#talk-mode-quick")?.checked) return;
        _talkListen = null;
        const text = String(result?.text || "").trim();
        if (text) {
          const box = $("#chat-input");
          if (box) box.value = text;
          setStatus("Talk · sending…");
          sendChat();
        } else {
          setStatus("Talk · no speech detected; listening again…");
          setTimeout(scheduleTalkListen, 650);
        }
      } catch (e) {
        if (token !== _talkListenToken) return;
        _talkListen = null;
        setStatus("Talk: " + (e.message || "speech recognition ended"));
        if ($("#talk-mode-quick")?.checked) setTimeout(scheduleTalkListen, 900);
      }
      return;
    }
    stopTalkListen();
    const rec = new Rec();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    rec.onresult = (ev) => {
      const text = ev.results?.[0]?.[0]?.transcript?.trim();
      if (!text) return;
      const box = $("#chat-input");
      if (box) box.value = text;
      sendChat();
    };
    rec.onerror = () => setStatus("Talk listen ended");
    rec.onend = () => { if (_talkListen === rec) _talkListen = null; };
    _talkListen = rec;
    setStatus("Talk · listening as " + ($("#matrix-agent-quick")?.value || "agent") + "…");
    try { rec.start(); } catch (e) { setStatus(e.message || "Could not start talk listen"); }
  }

  // ── push-to-talk ─────────────────────────────────────────────────
  async function startPtt() {
    try {
      $("#btn-mic").classList.add("recording");
      setStatus("Listening…");
      await CypraVoice.pushToTalkStart();
    } catch (e) {
      $("#btn-mic").classList.remove("recording");
      setStatus(e.message);
    }
  }

  async function stopPtt() {
    $("#btn-mic").classList.remove("recording");
    if (!CypraVoice.isRecording()) return;
    try {
      setStatus("Transcribing…");
      const text = await CypraVoice.pushToTalkStop();
      if (text) {
        $("#chat-input").value = ($("#chat-input").value + " " + text).trim();
        setStatus("Transcribed — send when ready");
      } else setStatus("No speech detected");
    } catch (e) {
      setStatus(e.message);
    }
  }

  // ── voice overlay ────────────────────────────────────────────────
  let voiceLive = false;

  window.setInterval(() => { if (document.visibilityState !== "hidden") refreshStudioRuntime(false); }, 5000);

})();


/* LOCAL FILE REVIEW — isolated from chat/session context */
(function setupLocalFileReview(){
  const init = () => {
    if (window.__cypraLocalFileReviewBound) return;

    const $ = (sel) => document.querySelector(sel);
    const input = $("#file-review-input");
    const btn = $("#btn-review-file");
    const dlg = $("#modal-file-review");
    const nameEl = $("#file-review-name");
    const typeEl = $("#file-review-type");
    const promptEl = $("#file-review-prompt");
    const outputEl = $("#file-review-output");
    const statusEl = $("#file-review-status");
    const runBtn = $("#file-review-run");
    const cancelBtn = $("#file-review-cancel");
    const insertBtn = $("#file-review-insert");
    const chooseBtn = $("#file-review-choose");
    const stopBtn = $("#file-review-stop");
    const ragBtn = $("#file-review-add-rag");
    const ragStatusEl = $("#file-review-rag-status");
    if (!input || !btn || !dlg || !runBtn) return;
    window.__cypraLocalFileReviewBound = true;
    $("#file-review-think-hide")?.addEventListener("click", () => {
      const tty = $("#file-review-think");
      if (tty) tty.hidden = true;
    });

    let selectedFile = null;
    let selectedPath = "";
    let lastReview = "";
    let reviewSourceText = "";
    let reviewSourcePayload = null;
    let reviewAbort = null;
    let pendingRagPick = false;
    let ragBusy = false;

    const setReviewBusy = (on) => {
      window.__cypraReviewBusy = !!on;
      runBtn.disabled = !!on;
      if (stopBtn) {
        stopBtn.hidden = !on;
        stopBtn.disabled = !on;
      }
    };
    window.__cypraStopReview = () => {
      try { reviewAbort?.abort(); } catch (_) {}
    };

    const selectedName = () =>
      selectedFile?.name || (selectedPath && selectedPath.split(/[/\\]/).pop()) || "file";

    const outputText = () => String(outputEl?.textContent || "").trim();

    const insertableText = () => {
      if (lastReview) return { label: "review", body: lastReview };
      const out = outputText();
      if (out && !/^Review failed:/i.test(out)) return { label: "review", body: out };
      return null;
    };

    const detailOf = (data, fallback) => {
      const d = data && data.detail;
      if (!d) return fallback;
      if (typeof d === "string") return d;
      if (Array.isArray(d)) return d.map((x) => x.msg || x).join("; ");
      return String(d);
    };

    const setStatus = (t, kind="") => {
      if (!statusEl) return;
      statusEl.textContent = t;
      statusEl.dataset.kind = kind;
    };

    const setRagStatus = (t, kind="") => {
      if (!ragStatusEl) return;
      ragStatusEl.textContent = `RAG: ${t}`;
      ragStatusEl.dataset.kind = kind;
    };

    const setRagBusy = (on) => {
      ragBusy = !!on;
      if (ragBtn) {
        ragBtn.disabled = ragBusy;
        ragBtn.textContent = ragBusy ? "INDEXING…" : "ADD TO RAG";
      }
    };

    const closeDialog = () => {
      try { if (dlg.open) dlg.close(); } catch (_) {}
      dlg.removeAttribute("open");
    };

    const showReviewDialog = () => {
      try {
        if (typeof dlg.showModal === "function") {
          if (!dlg.open) dlg.showModal();
          return;
        }
      } catch (_) {}
      dlg.setAttribute("open", "");
    };

    const acceptFile = (file, path="", sizeHint) => {
      if (!file && !path) return;
      selectedFile = file || null;
      selectedPath = path || "";
      const name = (file && file.name) || (path && path.split(/[/\\]/).pop()) || "file";
      const size = Number.isFinite(sizeHint)
        ? sizeHint
        : (file && Number.isFinite(file.size) ? file.size : 0);
      if (nameEl) nameEl.textContent = name;
      if (typeEl) {
        const kind = (file && file.type) || (name.includes(".") ? name.split(".").pop().toUpperCase() : "file");
        typeEl.textContent = `${kind} · ${size} bytes`;
      }
      if (outputEl) outputEl.textContent = "";
      lastReview = "";
      reviewSourceText = "";
      setRagStatus("not indexed from this dialog.");
      setStatus(size === 0 && !path
        ? "Selected, but the desktop window reported 0 bytes. Use Choose File."
        : "File ready · choose REVIEW FILE or ADD TO RAG.");
    };

    const openHtmlPicker = () => {
      try { input.value = ""; } catch (_) {}
      try {
        if (typeof input.showPicker === "function") {
          input.showPicker();
          return;
        }
      } catch (_) {}
      try { input.click(); } catch (err) {
        setStatus(`Unable to open file picker: ${err?.message || err}`, "bad");
      }
    };

    const nativePicker = async () => {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.pick_review_file !== "function") return null;
      try {
        return await api.pick_review_file();
      } catch (_) {
        return null;
      }
    };

    const choose = async (event) => {
      event?.preventDefault?.();
      event?.stopPropagation?.();
      const result = await nativePicker();
      if (result) {
        if (result.cancelled) return;
        if (result.error) { setStatus(result.error, "bad"); return; }
        if (result.ok && result.path) {
          acceptFile(null, result.path, result.size);
          return;
        }
      }
      openHtmlPicker();
    };

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      showReviewDialog();
      setStatus(selectedFile || selectedPath ? "File ready · review it or add it directly to RAG." : "Choose a local file, then review it or add it directly to RAG.");
    });
    chooseBtn?.addEventListener("click", choose);

    input.addEventListener("change", () => {
      const file = input.files?.[0] || null;
      if (!file) {
        pendingRagPick = false;
        return;
      }
      acceptFile(file);
      if (pendingRagPick) {
        pendingRagPick = false;
        void ingestSelectedToRag();
      }
    });

    const onDrop = (e) => {
      e.preventDefault();
      e.stopPropagation();
      const file = e.dataTransfer?.files?.[0];
      if (file) acceptFile(file);
    };
    dlg.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
    dlg.addEventListener("drop", onDrop);

    cancelBtn?.addEventListener("click", () => {
      try { reviewAbort?.abort(); } catch (_) {}
      closeDialog();
    });
    dlg.addEventListener("cancel", (e) => {
      e.preventDefault();
      try { reviewAbort?.abort(); } catch (_) {}
      closeDialog();
    });

    const bytesToB64 = (buf) => {
      const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
      let bin = "";
      const step = 0x8000;
      for (let i = 0; i < bytes.length; i += step) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
      }
      return btoa(bin);
    };

    const readFilePayload = async (file) => {
      const name = file.name || "file";
      const ext = (name.split(".").pop() || "").toLowerCase();
      const binary = new Set(["pdf", "docx", "xlsx"]);
      if (binary.has(ext)) {
        const buf = await file.arrayBuffer();
        return { name, content_b64: bytesToB64(buf), size: buf.byteLength };
      }
      let text = "";
      try { text = await file.text(); } catch (_) { text = ""; }
      if (!text && file.size) {
        const buf = await file.arrayBuffer();
        return { name, content_b64: bytesToB64(buf), size: buf.byteLength };
      }
      return { name, text, size: (file.size || text.length) };
    };

    const ingestSelectedToRag = async () => {
      if (ragBusy) return;
      if (!selectedFile && !selectedPath) {
        // Native desktop picker can index immediately. Browser fallback marks
        // the next file-input change for immediate indexing, so REVIEW is not
        // required in either path.
        const result = await nativePicker();
        if (result) {
          if (result.cancelled) return;
          if (result.error) { setRagStatus(result.error, "bad"); return; }
          if (result.ok && result.path) {
            acceptFile(null, result.path, result.size);
          }
        }
        if (!selectedFile && !selectedPath) {
          pendingRagPick = true;
          openHtmlPicker();
          return;
        }
      }

      setRagBusy(true);
      setRagStatus(`indexing ${selectedName()}…`);
      try {
        let payload;
        if (selectedPath) {
          payload = { name: selectedName(), path: selectedPath };
        } else {
          if (Number(selectedFile?.size || 0) > 10 * 1024 * 1024) {
            throw new Error("Knowledge file is too large (10 MB maximum).");
          }
          const filePayload = await readFilePayload(selectedFile);
          if (!filePayload.size && !(filePayload.text || filePayload.content_b64)) {
            throw new Error("This window could not read the file (0 bytes). Use Choose File.");
          }
          payload = filePayload;
        }

        const response = await fetch("/api/rag/content", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(detailOf(data, response.statusText || "RAG indexing failed"));

        if (typeof window.__cypraRenderRagStatus === "function") {
          window.__cypraRenderRagStatus(data);
        } else if (typeof window.__cypraRefreshRagStatus === "function") {
          await window.__cypraRefreshRagStatus();
        }

        const sourceName = data.source?.name || selectedName();
        setRagStatus(data.duplicate ? `already indexed · ${sourceName}` : `indexed · ${sourceName} · persistent`, "ok");
        if (typeof window.showStudioToast === "function") {
          window.showStudioToast(
            data.duplicate ? "RAG ALREADY HAS FILE" : "ADDED TO RAG",
            `${sourceName} · no review required`,
            "ok"
          );
        }
      } catch (e) {
        setRagStatus(e?.message || String(e), "bad");
      } finally {
        setRagBusy(false);
      }
    };

    ragBtn?.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      void ingestSelectedToRag();
    });

    const streamReview = async (payload) => {
      reviewAbort = new AbortController();
      const reviewThinkOverride = String(document.querySelector("#think-mode-quick")?.value || "default").trim().toLowerCase();
      payload.think_mode = reviewThinkOverride !== "default" ? reviewThinkOverride : null;
      const res = await fetch("/api/review-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: reviewAbort.signal,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(detailOf(data, res.statusText || "File review failed"));
      }
      if (!res.body) throw new Error("No review stream from the server.");
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      lastReview = "";
      if (outputEl) outputEl.textContent = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split(/\r?\n/);
        buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          let msg;
          try { msg = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (msg.type === "started") {
            const src = msg.turn_context || msg.source_excerpt || msg.review_source || "";
            if (src) reviewSourceText = String(src).trim();
            setStatus(`Reading ${msg.file_name || selectedName()}…`);
          } else if (msg.type === "session") {
            const src = msg.turn_context || msg.source_excerpt || msg.review_source || "";
            if (src) reviewSourceText = String(src).trim();
            setStatus(`Reviewing ${msg.file_name || selectedName()} · ${msg.model || "model"}…`);
          } else if (msg.type === "think") {
            if (document.querySelector("#set-show-think")?.checked === false) continue;
            const tty = $("#file-review-think");
            const body = $("#file-review-think-body");
            if (tty) tty.hidden = false;
            if (body) {
              body.textContent += msg.text || "";
              body.scrollTop = body.scrollHeight;
            }
          } else if (msg.type === "delta") {
            lastReview += msg.text || "";
            if (outputEl) outputEl.textContent = lastReview;
            outputEl.scrollTop = outputEl.scrollHeight;
          } else if (msg.type === "done") {
            lastReview = String(msg.review || lastReview || "").trim();
            reviewSourceText = String(msg.turn_context || msg.source_excerpt || msg.review_source || reviewSourceText || "").trim();
            if (outputEl) outputEl.textContent = lastReview || "No review text returned.";
            setStatus(`Review complete · ${msg.file_name || selectedName()} · INSERT is ready`, "ok");
          } else if (msg.type === "error") {
            throw new Error(msg.error || "File review failed");
          }
        }
      }
      if (!lastReview) throw new Error("No review text returned.");
    };

    stopBtn?.addEventListener("click", () => {
      try { reviewAbort?.abort(); } catch (_) {}
    });

    runBtn.addEventListener("click", async () => {
      if (!selectedFile && !selectedPath) { setStatus("Choose a file first.", "bad"); return; }
      setReviewBusy(true);
      if (outputEl) outputEl.textContent = "";
      const thinkBody = $("#file-review-think-body");
      if (thinkBody) thinkBody.textContent = "";
      const thinkTty = $("#file-review-think");
      if (thinkTty) thinkTty.hidden = true;
      lastReview = "";
      reviewSourceText = "";
      setStatus("Reading file and reviewing locally…");
      const instruction = (promptEl?.value || "Review this file for important content, structure, issues, and actionable findings.").trim();
      try {
        let payload = { instruction, path: selectedPath || "", name: selectedName() };
        if (!selectedPath) {
          const filePayload = await readFilePayload(selectedFile);
          if (!filePayload.size && !(filePayload.text || filePayload.content_b64)) {
            const native = await nativePicker();
            if (native && native.ok && native.path) {
              acceptFile(null, native.path, native.size);
              payload = { instruction, path: native.path, name: native.name || selectedName() };
            } else {
              throw new Error("This window could not read the file (0 bytes). Use Choose File.");
            }
          } else {
            payload = { instruction, ...filePayload };
            reviewSourcePayload = { ...filePayload };
            if (filePayload.text) reviewSourceText = String(filePayload.text).trim();
          }
        } else {
          reviewSourcePayload = { path: selectedPath, name: selectedName() };
        }
        if (payload.text && !reviewSourceText) reviewSourceText = String(payload.text).trim();
        await streamReview(payload);
      } catch (e) {
        if (e?.name === "AbortError") {
          lastReview = String(lastReview || outputText() || "").trim();
          if (outputEl && lastReview) outputEl.textContent = lastReview;
          setStatus(lastReview ? "Review stopped · partial kept · INSERT is ready" : "Review stopped", "ok");
        } else {
          setStatus(e?.message || String(e), "bad");
          if (outputEl) outputEl.textContent = "Review failed: " + (e?.message || e);
        }
      } finally {
        setReviewBusy(false);
        reviewAbort = null;
      }
    });

    $("#file-review-next-send")?.addEventListener("click", async (event) => {
      event?.preventDefault?.();
      event?.stopPropagation?.();

      const name = selectedName();
      const reviewed = String(lastReview || outputText() || "").trim();
      if (!reviewed) {
        setStatus("Complete the file review before NEXT SEND.", "bad");
        return;
      }

      // NEXT SEND is a review-context handoff, not a raw-file attachment.
      // The review result is compacted, queued, sent on the next chat turn, and
      // persisted in session history. No hidden memory context is added by this handoff.
      const cap = 5000;
      let context = `[REVIEWED FILE CONTEXT]\nFile: ${name}\n\n${reviewed}`;
      if (context.length > cap) {
        context = context.slice(0, cap - 24).trimEnd() + "\n[REVIEW CONTEXT TRIMMED]";
      }

      const attach = window.__cypraSetTurnFile;
      if (typeof attach !== "function") {
        setStatus("Chat is not ready for reviewed-context handoff.", "bad");
        return;
      }

      const queued = attach(name, context, "", "review");
      if (!queued) {
        setStatus("NEXT SEND could not queue the reviewed context.", "bad");
        return;
      }

      closeDialog();
      setStatus(`NEXT SEND queued · reviewed context from ${name}`, "ok");
    });

    $("#file-review-copy")?.addEventListener("click", async () => {
      const t = lastReview || outputText();
      if (!t) { setStatus("Nothing to copy yet.", "bad"); return; }
      try {
        await navigator.clipboard.writeText(t);
        setStatus("Review copied.", "ok");
      } catch (_) {
        setStatus("Clipboard unavailable", "bad");
      }
    });

    insertBtn?.addEventListener("click", async () => {
      const inputBox = $("#chat-input");
      if (!inputBox) return;
      let payload = insertableText();
      if (!payload && selectedFile) {
        try {
          const filePayload = await readFilePayload(selectedFile);
          const body = (filePayload.text || "").trim();
          if (body) payload = { label: "file", body };
        } catch (_) {}
      }
      if (!payload) {
        setStatus("Nothing to insert yet. Review a file, or choose a readable text file.", "bad");
        return;
      }
      let body = payload.body;
      if (payload.label === "file" && body.length > 32000) {
        body = body.slice(0, 32000) + "\n\n[FILE INSERT TRIMMED — use Review File for the full document so chat context stays intact.]";
      }
      const prefix = payload.label === "file"
        ? `[FILE: ${selectedName()}]\n\n`
        : `[FILE REVIEW: ${selectedName()}]\n\n`;
      inputBox.value = `${prefix}${body}`;
      inputBox.dispatchEvent(new Event("input", { bubbles: true }));
      closeDialog();
      inputBox.focus();
      if (typeof window.showStudioToast === "function") {
        window.showStudioToast(
          payload.label === "file" ? "FILE INSERTED" : "REVIEW INSERTED",
          "Placed in the chat composer; it has not been sent.",
          "ok"
        );
      }
    });
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, {once:true});
  else init();
})();
