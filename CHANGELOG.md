# Changelog

Notable changes to terum-capture. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow the tags in this repo. Entries before this file existed were
reconstructed from git history.

## [Unreleased]

### Added
- Project-scoped capture by default, with an interactive project picker in
  `setup` — the hook is written into each selected repo's git-ignored
  `.claude/settings.local.json` instead of globally (#6)

### Changed
- `install.sh` now announces install phases and reports failures as a one-line
  reason instead of a raw pipx/uv error dump (#19, #20)
- README leads with the `install.sh` one-liner; pipx is the alternative (#21)

### Fixed
- `install.sh` falls back to `uv` with a managed Python when pipx can't work
  (broken Homebrew Pythons on macOS 26.1/26.2), and `terum-capture update` is
  uv-aware afterwards (#19)

## [0.6.1] — 2026-08-04

### Added
- The update nag is now user-visible: surfaced as a `systemMessage` from the
  Stop hook instead of being silently swallowed (#18)

## [0.6.0] — 2026-08-04

### Added
- Delivery gate: decision guidance and a hook-performed conflict check on
  UserPromptSubmit (#16, #17)
- `AGENTS.md` so Codex-style agents load the repo conventions (#16)

### Changed
- The delivery hook's two retrieval lanes run in parallel with a 15s timeout
  (bug-592)

### Fixed
- The delivery hook's fail-open guarantee is structural — a gate failure can no
  longer block the user's prompt (bugs 593, 594)

## [0.5.0] — 2026-07-31

### Added
- Version signaling baseline: the CLI reports its version with events and the
  backend can request an update nag, gated by a cross-repository sync check

[Unreleased]: https://github.com/ryanliu-terum/terum-capture/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/ryanliu-terum/terum-capture/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/ryanliu-terum/terum-capture/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/ryanliu-terum/terum-capture/releases/tag/v0.5.0
