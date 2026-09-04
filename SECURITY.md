# Security Policy

## Scope

Cypra Matrix Studio is a local-first desktop AI workspace. It is not a security sandbox, and local models/plugins should not be treated as untrusted-code containment boundaries.

## Network boundary

The Studio server binds to loopback (`127.0.0.1`). The HTTP layer rejects non-loopback Host/Origin requests. New configuration should use `CYPRA_*` environment variables; selected historical `BRAIN_*` names are accepted only as launcher compatibility fallbacks.

Do not expose the Studio port through router forwarding, public tunnels, reverse proxies, or firewall exceptions unless you separately add appropriate authentication, transport security, and access controls.

Optional Edge TTS is outbound-only. It does not require Matrix Studio to expose a second TTS listener or inbound speech API.

## Ollama and model traffic

Local inference is intended to use the project-local Ollama runtime/model store. Model installation/update actions can create outbound network traffic when the user explicitly starts a pull.

Treat model files as executable-adjacent supply-chain inputs: obtain them from sources you trust and keep hashes/version information when reproducibility matters.

## Voice / TTS privacy

Matrix Studio supports two primary speech paths with different privacy boundaries.

### Piper Local

Piper is the local/offline speech provider. When its project-local assets are installed, speech inference stays on the host and does not intentionally send spoken text to an external service.

Use Piper for credentials, private configuration, security logs, sensitive file contents, or any material that should remain entirely local.

### Edge Online

Edge TTS is optional, online, and is not treated as equivalent to local speech. Edge synthesis is permitted only when all three controls are enabled:

1. **Voice Output** is enabled.
2. **Provider** is set to **Edge Online**.
3. **Allow Online TTS** is enabled.

Fresh/default settings keep Voice Output disabled, Piper selected, and online TTS permission disabled.

Matrix Studio calls the `edge-tts` Python package directly. It does not require a second local TTS web server or expose an additional inbound TTS API. When Edge is used, the final sanitized speech text leaves the host and is sent to Microsoft's speech service.

### Edge sanitization process

Edge receives a narrow plain-text speech payload rather than a raw Matrix response object, agent object, request context, environment dictionary, tool-call structure, or other application state.

Before an Edge request is allowed to reach `edge_tts.Communicate(...)`, the text passes through the following outbound pipeline:

```text
visible Matrix response
        |
        v
online secret redaction
        |
        v
general speech sanitization
        |
        v
final online secret redaction
        |
        v
EdgeEngine.synthesize(text: str, ...)
        |
        v
edge_tts.Communicate(...)
```

The online-only redaction layer is deterministic and is designed to remove or redact common credential-bearing material, including:

- API-key assignments and common API-key prefixes,
- `Bearer` and `Authorization` credentials,
- password, token, access-token, refresh-token, secret, and client-secret assignments,
- private-key blocks,
- credential-style JSON fields,
- URLs containing token/key/authentication query parameters,
- internal-looking labels such as `SYSTEM_PROMPT`, `DEVELOPER_PROMPT`, `BRAIN_CONTEXT`, `TOOL_OUTPUT`, `TOOL_CALL`, `AGENT_CONTEXT`, `INTERNAL_CONTEXT`, and `HIDDEN_CONTEXT` when they carry values.

The normal speech sanitizer also removes or suppresses material that should not be spoken as ordinary prose, such as code blocks, URLs, structured dumps, internal metadata, and private path material according to the active voice settings.

The online sanitizer is separate from Piper. Local Piper speech can therefore remain available for content the user intentionally wants spoken locally without sending that content to an external speech provider.

### Fail-closed behavior

Edge sanitization is fail-closed.

If sanitization raises an exception, Matrix Studio must not send the original unsanitized text to Edge. The required behavior is:

1. Block the Edge request.
2. Record only a concise sanitizer failure message.
3. Fall back to Piper when the configured fallback permits it.
4. Otherwise skip speech.

If sanitization produces no safe text, the Edge request is also blocked.

Production TTS logs should contain only operational metadata such as provider, voice, character count, status, timing, or error class. They should not intentionally record full spoken text, detected secret values, Authorization headers, API keys, private keys, or sanitizer input/output.

### Sanitizer limitations

The Edge sanitizer is a defense-in-depth control, not a semantic data-classification system. Deterministic pattern matching cannot identify every form of sensitive prose or every custom secret format.

Ordinary private information can still be sensitive even when it does not resemble a credential. If that text is intentionally spoken through Edge, the sanitized speech payload may still be transmitted to the remote service.

For sensitive sessions, use **Piper Local** or disable voice output.

Provider changes and `STOP VOICE` terminate active playback/synthesis so stale speech is not intentionally carried into another provider session.

## File review

Local file review handles user-selected files. Review results may be inserted into chat or queued for one-turn reviewed context.

The clean baseline does not silently persist reviewed file contents into the legacy Memory v1 path.

Do not review files containing secrets unless the active model/provider path is appropriate for that data.

## Plugins

Plugins are executable code. Installing a local or GitHub plugin is equivalent to trusting code that can run with the Studio process's user permissions.

Before installing a plugin:

- inspect its source and dependencies,
- prefer pinned/reproducible versions,
- verify the repository/source,
- avoid plugins that require unnecessary filesystem/network privileges.

## Configuration and secrets

Configuration exports must not expose provider API keys or other secrets. Keep `.env` files, credentials, private sessions, local logs, and private project workspaces out of public repositories.

Never commit model-provider credentials to source control.

## Local filesystem

Run Studio as a normal user. Avoid Administrator elevation unless a specific installation action genuinely requires it.

Project-relative paths are preferred for portability and to avoid accidentally operating on unrelated user directories.

## Destructive actions

Keep confirmation enabled for destructive/reset operations. A settings reset should affect only the requested settings group and should not delete unrelated project files, model weights, or unknown future settings.

The settings migration layer uses an explicit retired-key allowlist rather than broad key deletion.

## Legacy Memory v1

Legacy Memory v1 remains in the backend as a dormant migration/reference source while the replacement Memory/RAG architecture is developed. Current chat does not automatically inject that legacy memory path.

When Memory/RAG v2 is introduced, migration should be explicit, reversible until validated, provenance-preserving, and bounded so a failed migration cannot destroy the original store.

## Reporting a vulnerability

When reporting a security issue, include:

- affected version/build,
- operating system and Python version,
- exact reproduction steps,
- expected vs. observed behavior,
- relevant logs with secrets removed,
- whether the issue requires local access or can cross the loopback boundary.

Do not include real credentials, private conversation contents, or sensitive user files in a report.
