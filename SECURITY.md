# Security Policy

## Scope

Cypra Matrix Studio is a local-first desktop AI workspace. It is not a security sandbox, and local models/plugins should not be treated as untrusted-code containment boundaries.

## Network boundary

The Studio server binds to loopback (`127.0.0.1`). The HTTP layer rejects non-loopback Host/Origin requests. New configuration should use `CYPRA_*` environment variables; selected historical `BRAIN_*` names are accepted only as launcher compatibility fallbacks.

Do not expose the Studio port through router forwarding, public tunnels, reverse proxies, or firewall exceptions unless you separately add appropriate authentication, transport security, and access controls.

## Ollama and model traffic

Local inference is intended to use the project-local Ollama runtime/model store. Model installation/update actions can create outbound network traffic when the user explicitly starts a pull.

Treat model files as executable-adjacent supply-chain inputs: obtain them from sources you trust and keep hashes/version information when reproducibility matters.

## Voice / TTS privacy

Local Piper TTS runs locally when its assets are installed.

Edge TTS is optional and online. When enabled, text submitted for Edge synthesis leaves the local machine and is sent to the external TTS provider. Keep online TTS disabled when content must remain entirely local.

Provider changes and `STOP VOICE` should terminate active playback/synthesis so text is not unintentionally carried into a different provider session.

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
