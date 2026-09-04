"use strict";

const assert = require("node:assert/strict");
global.window = global;
global.CustomEvent = global.CustomEvent || class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
global.dispatchEvent = () => true;
let delegatedStop = null;
global.document = {
  addEventListener(type, handler) {
    if (type === "click") delegatedStop = handler;
  },
};
global.__cypraSettings = { voice_output_enabled: true, tts_provider: "local" };
global.speechSynthesis = { cancel() {}, speaking: false, getVoices() { return []; }, speak() {} };
global.SpeechSynthesisUtterance = class SpeechSynthesisUtterance {};
global.URL.createObjectURL = () => "blob:test";
global.URL.revokeObjectURL = () => {};

let stopRequests = 0;
let paused = false;
global.fetch = async (url) => {
  if (url === "/api/tts/stop") {
    stopRequests += 1;
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return {
    ok: true,
    headers: { get: () => "audio/wav" },
    arrayBuffer: async () => new ArrayBuffer(16),
    json: async () => ({}),
  };
};

global.Audio = class Audio {
  pause() { paused = true; }
  play() { return Promise.resolve(); }
};

require("../static/js/voice.js");

(async () => {
  const speaking = global.CypraVoice.speak("Stop button integration test.", { provider: "local", preview: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(typeof delegatedStop, "function", "delegated Stop Voice handler must be installed");
  delegatedStop({
    target: { closest: (selector) => selector === "[data-tts-stop]" ? {} : null },
    preventDefault() {},
    stopPropagation() {},
  });
  await speaking;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(paused, true, "active HTML audio must be paused");
  assert.equal(stopRequests, 1, "server cancellation endpoint must be called once");
  assert.equal(global.CypraVoice.isSpeaking(), false);
  console.log("voice-stop-ok");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
