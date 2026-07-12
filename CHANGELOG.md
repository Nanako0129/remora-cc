# Changelog

All notable changes to Remora are documented here.

## 0.1.0 — 2026-07-12

Initial release. It includes the isolated launcher, a six-role GPT-5.6 Sol/Luna map with Terra as the default Sonnet alias, TOML configuration, environment or credential-command authentication, offline/online doctor checks, and isolation-focused tests.

The public installation path is approval-gated and release-pinned. Release archives carry SHA-256 checksums and GitHub build-provenance attestations; the bootstrap requires both unless the user explicitly accepts checksum-only verification. The installer performs collision checks, preserves existing configuration, updates the payload atomically, and never writes native Claude state.
