# Contributing

## Scope

remora stays intentionally small. Contributions should strengthen session-scoped routing, portability, diagnostics, documentation, or tests without turning the launcher into a proxy, account manager, GUI, or persistent Claude configuration manager.

| In scope | Out of scope |
|---|---|
| Dynamic agent rendering | Editing `~/.claude` |
| Child-process environment isolation | Bundling OAuth tokens |
| Gateway compatibility diagnostics | Reimplementing gateway translation |
| Portable credential helpers | Shell-evaluated secret commands |

## Development

```bash
git clone https://github.com/Nanako0129/remora-cc.git
cd remora-cc
make check
```

Tests must not require a real gateway, modify the developer's Claude configuration, or print secrets. An integration that needs a live provider belongs behind an explicit opt-in flag and must redact authentication and response bodies by default.

## Pull requests

Describe the isolation boundary affected by the change, show the command used to verify it, and include a regression test for launcher or installer behavior. Documentation changes should keep the English and Traditional Chinese setup instructions aligned when they alter user-visible behavior.
