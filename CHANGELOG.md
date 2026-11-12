# Changelog

All notable changes to Document Intelligence MCP are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the app uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

**`REQUIRES_CORE` bumped to `>=0.4.0,<0.5.0`.**

The core shipped 0.4.0 with four bug fixes and no contract change, and this
extension still declared `>=0.3.0,<0.4.0` — so updating the core would have
refused its activation. The gate was right and the declaration was stale.

The new bound is earned rather than asserted: this extension's tests import the
real core and run its manifest through the core's own validator, and they pass
against 0.4.0.
## [0.1.0] — 2026-11-12

### Added

**The reference extension for the four primitives.**

Seven tools, one agent, three doctypes, and no flow — the absence being the
deliberate half.

- `tools/manifest.py` — the contract. Imports no Frappe, so the core can
  validate it on a machine with none of this app's dependencies installed.
  `REQUIRES_CORE = ">=0.3.0,<0.4.0"`, the first core version carrying the four
  primitives.
- `tools/parsing.py` — turning a model's answer into fields, deliberately
  literal. `NOT_FOUND` and `AMBIGUOUS` are *values*, not absences: a field the
  model could not read is a field the reader must be told about. A parser that
  "cleaned up" the output would be a parser that invents data.
- `tools/extraction.py` — the Frappe handlers, and the job the worker runs.
  Reads bytes through the core's binary port, renders the prompt through the
  core's restricted template engine, asks the core's provider, writes a draft.
- `agent.py` — persona, four boundaries, three prompts (one always-on, two
  selected by situation), and a deterministic `system_prompt()`.
- `hooks.py` — `frappe_mcp_tools` points at the manifest; `frappe_mcp_agent`
  points at the agent.
- Doctypes: `Extraction Draft`, `Extraction Field`, `Document Intelligence MCP
  Settings`.
- `install.py` — creates the role and the Single settings row, idempotently,
  so a restored site repairs itself on the next migrate. The settings ship
  **restrictive**: no provider, no auto-accept, `data_leaves_site` off.
- `tests/test_manifest.py` — 41 tests that import the **real core** and run
  this manifest through the core's own validator.

### The decisions it demonstrates

- **No flow, because a human decides.** Extraction, review, promote — the
  middle step is a person reading a draft. A flow is several calls with a
  transaction of its own, and there is no transaction that spans a human
  decision. `healthcare-mcp` ships one flow because admission is three
  documents that must all succeed; this is the opposite shape.
- **Every write declares its compensation**, and a test asserts the tool it
  names actually exists in the registry.
- **The document text is fenced.** It is a field a *user* edited, and an
  unfenced interpolation is a cross-user injection primitive. A test asserts a
  hostile value cannot end the fence early.
- **The snapshot is taken at call time and never refreshed.** It is what the
  caller could do when they asked — the only thing they authorised.
- **A draft is not an authorisation.** `promote_extraction` checks the caller's
  permission on the target through Frappe, rather than inheriting it from
  whoever queued the extraction.

