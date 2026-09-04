/**
 * Voice: push-to-talk speech recognition and multi-provider text-to-speech.
 * This module does not write chat memory or maintain a hidden realtime voice workspace.
 */
window.CypraVoice = (function () {
  let mediaRecorder = null;
  let chunks = [];
  let recording = false;
  let micStream = null;
  let playing = false;
  let currentAudio = null;
  let synthesisAbort = null;
  let speakToken = 0;
  let cachedProvider = null;
  let speechRecognition = null;
  let speechRecognitionText = "";
  let nativeRecognitionPromise = null;
  let recognitionMode = "";

  function _startBrowserRecognition() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    speechRecognition = null;
    speechRecognitionText = "";
    if (!Recognition) {
      const nativeApi = window.pywebview?.api;
      nativeRecognitionPromise = typeof nativeApi?.listen_for_speech === "function"
        ? nativeApi.listen_for_speech().catch(() => null)
        : null;
      recognitionMode = nativeRecognitionPromise ? "windows" : "provider";
      return;
    }
    try {
      const recognition = new Recognition();
      recognition.lang = document.documentElement.lang || navigator.language || "en-US";
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      recognition.onresult = (event) => {
        let heard = "";
        for (let i = 0; i < event.results.length; i += 1) {
          heard += `${event.results[i]?.[0]?.transcript || ""} `;
        }
        speechRecognitionText = heard.trim();
      };
      recognition.onerror = () => {};
      recognition.onend = () => {
        if (speechRecognition === recognition) speechRecognition = null;
      };
      recognition.start();
      speechRecognition = recognition;
      recognitionMode = "browser";
    } catch (_) {
      speechRecognition = null;
      recognitionMode = "provider";
    }
  }

  async function _stopBrowserRecognition() {
    const recognition = speechRecognition;
    if (recognition) {
      await new Promise((resolve) => {
        let settled = false;
        const finish = () => { if (!settled) { settled = true; resolve(); } };
        const previousEnd = recognition.onend;
        recognition.onend = (event) => {
          try { previousEnd?.(event); } catch (_) {}
          finish();
        };
        try { recognition.stop(); } catch (_) { finish(); }
        setTimeout(finish, 750);
      });
    }
    speechRecognition = null;
    if (speechRecognitionText.trim()) {
      nativeRecognitionPromise = null;
      return speechRecognitionText.trim();
    }
    if (nativeRecognitionPromise) {
      const nativeResult = await nativeRecognitionPromise;
      nativeRecognitionPromise = null;
      if (nativeResult?.ok && nativeResult.text) return String(nativeResult.text).trim();
    }
    return "";
  }

  async function pushToTalkStart() {
    if (recording) return;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const nativeApi = window.pywebview?.api;
    if (!Recognition && typeof nativeApi?.listen_for_speech === "function") {
      recording = true;
      mediaRecorder = null;
      chunks = [];
      _startBrowserRecognition();
      return;
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micStream = stream;
    chunks = [];
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : "audio/webm";
    mediaRecorder = new MediaRecorder(stream, { mimeType: mime });
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size) chunks.push(e.data);
    };
    mediaRecorder.start(100);
    recording = true;
    _startBrowserRecognition();
  }

  async function pushToTalkStop() {
    if (!recording) return "";
    recording = false;
    if (recognitionMode === "windows" && !mediaRecorder) {
      const windowsText = await _stopBrowserRecognition();
      if (windowsText) return windowsText;
      throw new Error("Windows speech recognition did not detect speech. Hold the mic, speak clearly, then release after finishing.");
    }
    if (!mediaRecorder) return "";
    await new Promise((resolve) => {
      mediaRecorder.onstop = resolve;
      mediaRecorder.stop();
    });
    (micStream?.getTracks() || []).forEach((t) => t.stop());
    micStream = null;
    const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
    chunks = [];
    const browserText = await _stopBrowserRecognition();
    if (browserText) return browserText;
    if (recognitionMode !== "provider") {
      throw new Error("No speech was detected. Hold the mic through the end of your sentence.");
    }
    const fd = new FormData();
    fd.append("file", blob, "speech.webm");
    const res = await fetch("/api/stt", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401) {
        throw new Error("Speech recognition is unavailable in this WebView. Enable Windows online speech recognition or add a provider key in Settings.");
      }
      throw new Error(data.detail || "STT failed");
    }
    return data.text || "";
  }

  function _settings() {
    return (window.__cypraSettings || {});
  }

  function _rate() {
    const r = Number(_settings().tts_rate);
    return Number.isFinite(r) ? Math.min(2, Math.max(0.5, r)) : 1;
  }

  function _pitch() {
    const p = Number(_settings().tts_pitch);
    return Number.isFinite(p) ? Math.min(2, Math.max(0.5, p)) : 1;
  }

  function _emitState(state) {
    try { window.dispatchEvent(new CustomEvent("cypra:tts-state", { detail: { state } })); } catch (_) {}
  }

  function stopSpeak(options = {}) {
    const notifyBackend = options.notifyBackend !== false;
    const release = options.release === true;
    speakToken += 1;
    playing = false;
    if (synthesisAbort) {
      try { synthesisAbort.abort(); } catch (_) {}
      synthesisAbort = null;
    }
    try {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
    } catch (_) {}
    if (currentAudio) {
      try {
        currentAudio.pause();
        if (typeof currentAudio.onended === "function") currentAudio.onended();
        currentAudio.src = "";
      } catch (_) {}
      currentAudio = null;
    }
    _emitState("idle");
    if (notifyBackend) {
      fetch("/api/tts/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ release }),
      }).catch(() => {});
    }
  }

  function _plainForSpeech(text) {
    let value = String(text || "");
    if (_settings().tts_skip_code !== false) {
      value = value.replace(/```[\s\S]*?```|~~~[\s\S]*?~~~/g, " Code block omitted. ");
    }
    if (_settings().tts_skip_urls !== false) {
      value = value.replace(/https?:\/\/\S+|www\.\S+/gi, " ");
    }
    return value
      .replace(/\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]/g, (_, t, d) => d || t)
      .replace(/[#*_`>]+/g, " ")
      .replace(/\s*\n+\s*/g, " ... ")
      .replace(/\.{4,}/g, "...")
      .replace(/[ \t]+/g, " ")
      .trim();
  }

  function _limitedPlain(text) {
    const plain = _plainForSpeech(text);
    const rawMaximum = Number(_settings().tts_max_chars);
    const maximum = Number.isFinite(rawMaximum) ? Math.min(10000, Math.max(100, rawMaximum)) : 1000;
    if (plain.length <= maximum) return plain;
    const clipped = plain.slice(0, maximum).trimEnd();
    const boundary = Math.max(clipped.lastIndexOf(". "), clipped.lastIndexOf("! "), clipped.lastIndexOf("? "));
    return (boundary > 0 ? clipped.slice(0, boundary + 1) : clipped.replace(/\s+\S*$/, "")).trim();
  }

  function _splitSentences(text) {
    const t = _limitedPlain(text);
    if (!t) return [];
    const byPause = t.split(/\.{3,}/).map((s) => s.trim()).filter(Boolean);
    const out = [];
    byPause.forEach((chunk, i) => {
      const parts = chunk.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [chunk];
      parts.map((s) => s.trim()).filter((s) => s.length > 1).forEach((s) => out.push(s));
      if (i < byPause.length - 1) out.push("__PAUSE__");
    });
    return out.length ? out : [t];
  }

  function _chunkPitch(text, base) {
    if (/!/.test(text) || /\b[A-Z]{3,}\b/.test(text)) return Math.min(1.8, base * 1.14);
    return base;
  }

  async function resolveProvider(force) {
    if (!force && cachedProvider) return cachedProvider;
    try {
      const res = await fetch("/api/tts/status");
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.provider) {
        cachedProvider = data.provider;
        return cachedProvider;
      }
    } catch (_) {}
    const conf = (_settings().tts_provider || "local").toLowerCase();
    if (["off", "local", "edge", "xai", "legacy_cloud", "browser"].includes(conf)) {
      cachedProvider = conf;
      return conf;
    }
    cachedProvider = "local";
    return cachedProvider;
  }

  function speakBrowser(text) {
    return new Promise((resolve, reject) => {
      if (!window.speechSynthesis) {
        reject(new Error("Browser Speech Synthesis not available"));
        return;
      }
      const token = speakToken;
      if (text === "__PAUSE__") {
        playing = true;
        setTimeout(() => resolve(), 380);
        return;
      }
      const utter = new SpeechSynthesisUtterance(_limitedPlain(text));
      utter.rate = _rate();
      utter.pitch = _chunkPitch(text, _pitch());
      // Prefer a local English voice when available
      try {
        const voices = speechSynthesis.getVoices() || [];
        const pref =
          voices.find((v) => /en(-|_)US/i.test(v.lang) && /natural|neural|premium/i.test(v.name)) ||
          voices.find((v) => /^en/i.test(v.lang)) ||
          voices[0];
        if (pref) utter.voice = pref;
      } catch (_) {}
      utter.onend = () => {
        if (token === speakToken) playing = false;
        _emitState("idle");
        resolve();
      };
      utter.onerror = (e) => {
        if (token === speakToken) playing = false;
        reject(e.error || new Error("Browser TTS error"));
      };
      playing = true;
      _emitState("playing");
      speechSynthesis.cancel();
      speechSynthesis.speak(utter);
    });
  }

  async function speakServer(text, voiceId, providerHint, options = {}) {
    synthesisAbort = new AbortController();
    _emitState("synthesizing");
    const res = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: synthesisAbort.signal,
      body: JSON.stringify({
        text: String(text || ""),
        voice_id: voiceId || (providerHint === "edge"
          ? (_settings().tts_edge_voice || "en-US-AvaNeural")
          : providerHint === "local" ? (_settings().tts_local_voice || "en_US-lessac-medium") : undefined),
        provider: providerHint || _settings().tts_provider || "local",
        replace: options.replace !== false,
        preview: options.preview === true,
      }),
    });
    synthesisAbort = null;
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const detail = err.detail;
      if (detail && detail.error === "browser_tts") {
        return speakBrowser(text);
      }
      const msg =
        typeof detail === "string"
          ? detail
          : detail?.message || err.message || "TTS failed";
      throw new Error(msg);
    }
    const buf = await res.arrayBuffer();
    const blob = new Blob([buf], { type: res.headers.get("content-type") || "audio/mpeg" });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    playing = true;
    const token = speakToken;
    console.info("[TTS] playback started");
    _emitState("playing");
    await new Promise((resolve, reject) => {
      audio.onended = () => {
        URL.revokeObjectURL(url);
        if (token === speakToken) {
          playing = false;
          currentAudio = null;
        }
        console.info("[TTS] playback complete");
        _emitState("idle");
        resolve();
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        playing = false;
        currentAudio = null;
        reject(new Error("Audio playback failed"));
      };
      audio.play().catch((error) => {
        URL.revokeObjectURL(url);
        playing = false;
        currentAudio = null;
        _emitState("idle");
        reject(error);
      });
    });
  }

  /**
   * Speak text using the active presentation provider.
   * options: { voiceId, provider, sentences }
   */
  async function speak(text, voiceIdOrOpts) {
    if (!text) return;
    const opts =
      typeof voiceIdOrOpts === "object" && voiceIdOrOpts
        ? voiceIdOrOpts
        : { voiceId: voiceIdOrOpts };
    stopSpeak({ notifyBackend: false });
    const token = ++speakToken;
    let provider = (opts.provider || "").toLowerCase();
    if (!provider || provider === "auto") {
      provider = await resolveProvider(false);
    }
    if (provider === "off") throw new Error("Voice Output is disabled");
    if (provider === "xai") provider = "legacy_cloud";
    if (provider === "local" || provider === "edge" || provider === "legacy_cloud") {
      try {
        await speakServer(text, opts.voiceId, provider, opts);
      } finally {
        if (token === speakToken) {
          playing = false;
          _emitState("idle");
        }
      }
      return;
    }
    const chunks =
      opts.sentences !== false ? _splitSentences(text) : [_limitedPlain(text)];
    const parts = chunks.length ? chunks : [_limitedPlain(text)];

    try {
      for (const part of parts) {
        if (token !== speakToken) return;
        if (part === "__PAUSE__") {
          await new Promise((r) => setTimeout(r, 380));
          continue;
        }
        await speakBrowser(part);
      }
    } finally {
      if (token === speakToken) playing = false;
    }
  }

  function isSpeaking() {
    return playing || !!(window.speechSynthesis && speechSynthesis.speaking);
  }

  function invalidateProviderCache() {
    cachedProvider = null;
  }


  // Capture-phase delegation keeps every Stop Voice control functional even if
  // Settings or the chat composer is rebuilt after initial event binding.
  if (typeof document !== "undefined") {
    document.addEventListener("click", (event) => {
      const button = event.target?.closest?.("[data-tts-stop]");
      if (!button) return;
      event.preventDefault();
      event.stopPropagation();
      stopSpeak();
      _emitState("idle");
    }, true);
  }

  return {
    pushToTalkStart,
    pushToTalkStop,
    speak,
    stopSpeak,
    isSpeaking,
    resolveProvider,
    invalidateProviderCache,
    isRecording: () => recording,
  };
})();
