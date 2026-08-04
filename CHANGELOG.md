# Changelog

## v0.7.2

- Repair the public DAG-to-grammar contract: XML exports now use canonical
  Scholia structure, preserve atom attributes, translate result edges into
  nested closure, and never truncate JSON/XML into invalid documents.
- Replace the plugin-only `Summary` pseudo-atom with canonical atoms, reject
  unknown atom kinds, and expose the complete canonical atom catalog through
  `scholia_catalog` / `scholia_lookup`.
- Make session finish emit a goal-closing, premise-backed `Concluding` and add
  behavioral tests for every public DAG/export/search composition identified
  during the 0.7.1 damage-control audit.
- Vendor the synchronized scholialang 0.7.2 constraint fix. The language
  grammar remains Scholia v0.6.2; package/plugin release and grammar versions
  are now labeled separately in public copy.

## v0.7.1

- **Fingerprint parity — synchronized suite release.** First synchronized
  `scholialang` + `scholialang-mcp` release: both advance to 0.7.1 together
  (`scholialang` skipped 0.7.0 intentionally; the mcp's prior release was
  0.7.0). Re-vendored the offline validator snapshot from the `scholialang`
  0.7.1 release commit `fb4de98`, which adds additive `<algo>:<hex>`
  `fingerprint=` support on `<Observation>` (`fingerprint_well_formed` rule).
  The bundled offline validator now recognizes `fingerprint=`; a fingerprint-less
  trace validates byte-identically to prior behavior. `UPSTREAM.json` cites the
  0.7.1 provenance and `SCHOLIA_VALIDATOR_VERSION` reads `0.7.1` across all three
  host plugins.
- **Dependency advance.** Bumped the `scholialang` dependency to
  `>=0.7.1,<0.8` and raised the plugin's installed-package acceptance floor
  (`MIN_VALIDATOR_VERSION`) to `(0, 7, 1)`. **Coordinated publish order:**
  `scholialang` 0.7.1 must publish to PyPI *before* this release is published,
  or the dependency will not resolve for anyone installing from PyPI. This
  repo's CI does not `pip install` the package (it runs `PYTHONPATH=src
  pytest`), so the ordering is **not** enforced by a red check — it has to be
  respected at publish time. `publish.yml` builds and `twine check`s the
  artifact but does not resolve dependencies either.
- **Shared contract parity.** The additive `fingerprint=` contract the plugin
  validates against is `scholialang-spec` `9c1fcfa` (the merged contract) —
  referenced, not forked. `scholialang-spec-ref.txt` pins that SHA and the
  `spec-parity` workflow drives the vendored engine against the spec's own
  fixture manifest at it, fail-closed, so the parity claim is mechanically
  checked rather than asserted. The shared `action_recorded` conformance corpus
  is unchanged at spec v0.6.2; the 0.7.1 vendored engine still passes it
  byte-for-byte (the additive-revision parity proof).
- **Version and host parity.** Bumped package, MCP/LSP server identities, Claude
  Code / Codex plugin manifests, and marketplace metadata to 0.7.1; the three
  plugin servers and vendored snapshots remain byte-identical.

## v0.7.0

- **Final-stable MCP 2026-07-28 adapter.** Added the stateless, per-request
  protocol path alongside the existing legacy handshake path. Discovery now
  returns `supportedVersions`, cache metadata, and server identity in result
  `_meta`; modern requests require their protocol version and client
  capabilities.
- **Strict dual-era lifecycle.** Requests declaring MCP 2026-07-28 reject
  removed `initialize`, `ping`, `logging/setLevel`, and resource subscription
  methods with `-32601 MethodNotFound`. Pre-2026 clients retain their supported
  `initialize` and `ping` behavior throughout the 0.7.x line.
- **Auditable version failures.** Unsupported versions fail closed with the
  final `-32022` error shape, including the requested version and the server's
  supported-version list. The generated support matrix probes both wheel and
  vendored-plugin servers over real stdio.
- **Package and host parity.** Bumped the integration package, MCP/LSP server
  identities, Claude Code and Codex plugin manifests, and marketplace metadata
  to 0.7.0. The Scholia language, validator, conformance corpus, and dependency
  remain intentionally pinned to the separately versioned 0.6.2 release.
- **Release reliability.** The Python test matrix covers 3.11, 3.12, and 3.13;
  Codex exhaust watcher tests no longer depend on ambient rollout state.

## v0.6.2

- **Cross-step Action result closure.** Vendored the merged and tagged
  `scholialang` v0.6.2 parser and validator into all three host plugins.
  `action_recorded` now accepts nested results at any descendant depth,
  immediate same-Step result siblings, explicitly linked later
  Finding/Observation/Evidence chains, goal-closing Concludings, and optional
  `records_result` graph edges. Unrelated, order-only, next-Step-unlinked, and
  unclosed Actions remain invalid.
- **Shared conformance contract.** Imported the exact tagged
  `scholialang-spec` v0.6.2 13-case positive/negative corpus and run it directly
  against the bundled validator. Commit-addressed vendor tools record source
  and rendered hashes, while release tests enforce byte identity across the
  Claude Code, Codex, and Ollama plugin copies.
- **Release and runtime parity.** Bumped package, MCP/LSP servers, plugin
  manifests, marketplace metadata, and validator reporting to `0.6.2`; fixed
  the stale `scholialang_mcp.__version__` value. The Python dependency is
  bounded to `scholialang>=0.6.2,<0.7`, and plugins reject an older installed
  validator before falling back to their bundled v0.6.2 engine.
- **Publication hardening.** PyPI publication now runs the complete test suite,
  checks release-tag/version parity, builds both distributions, and validates
  their metadata before trusted publishing. The PyPI wheel remains limited to
  the MCP/LSP core; host plugins and Scholia Live remain separate marketplace
  artifacts.

## v0.6.1

- **Scholia v0.6.1 propagation — `status=` on `<Concluding>`.** Re-synced the
  three bundled `_scholia_vendored/{atoms,parser,validator}.py` copies from the
  status-bearing `scholialang` v0.6.1 source. The vendored parser now reads the
  optional `status` attribute on `<Concluding>` and the vendored validator
  enforces the closed `met|unmet|partially_met` enum (absent stays valid for
  v0.5/v0.6.0 back-compat). `SCHOLIA_VALIDATOR_VERSION` now reads `0.6.1`.
  Bumped the package `version` to `0.6.1` and the `scholialang` dependency pin
  to `>=0.6.1`. Propagated byte-identically to all three plugin trees via
  `scripts/sync_plugins.sh`. Added a public-hygiene leak guard
  (`tests/test_public_hygiene.py` + `.github/workflows/hygiene.yml`) that fails
  on internal-only references, with a planted-reference self-test.
- **Canonical Scholia positioning** (#22). README opening, plugin
  manifest descriptions (Claude Code + Codex), marketplace metadata,
  `pyproject.toml` description, and the GitHub repo description now use
  the canonical framing: Scholia helps agentic systems preserve
  reasoning state, use tools without quality loss, and reduce context
  cost across long-horizon work. MCP/plugin surfaces emphasize the
  integration story (MCP, LSP, Codex/Claude Code/Ollama host recipes,
  local DAG access). Copy-only; no tool or version changes.
- **Live Claude Code exhaust capture** (#26). Default-on capture of Claude Code
  session exhaust into the SQLite-backed Scholialang store (opt-out via
  `SCHOLIA_EXHAUST=0`).
- **Live Codex exhaust capture** (#27). Default-on Codex rollout exhaust capture,
  matching the Claude Code behaviour.
- **Scholia Live: multi-project viewer** (#25). Web viewer gains a multi-project
  switcher and scope toggle, plus an atom feed-order toggle (newest/oldest first).

## v0.6.0

- **Scholia v0.6 vendored substrate catchup.** Refreshed the bundled
  `_scholia_vendored/{atoms,parser,validator}.py` from the published
  `scholialang` v0.6 (relative imports), so `scholia_lint_snippet` /
  `scholia_lint_trace` now run the v0.6 grammar with content-addressable
  `canonical_id` well-formedness. `SCHOLIA_VALIDATOR_VERSION` reads
  `0.6.0`; the vendored `compute_canonical_id` matches the scholialang v0.6
  golden vectors byte-for-byte. Propagated byte-identically to the Codex and
  Ollama plugin trees via `scripts/sync_plugins.sh`.
- Fixed stale grammar-version copy (v0.4/v0.5 → v0.6) in plugin
  descriptions, READMEs, the skill, and the `scholia_lint_snippet` tool
  description. Plugin package versions stay on their own tracks
  (marketplace `0.3.2`, Claude Code plugin `0.3.2`, Codex
  `0.2.1+codex.<ts>`, `scholialang-mcp` package `0.6.0`), distinct from the
  v0.6 grammar the plugins validate.
- **Claude Code plugin 0.3.1:** remove the redundant `"hooks"` key from
  `plugin.json`. Claude Code auto-loads `hooks/hooks.json` by convention, so
  also declaring it in the manifest caused a "Duplicate hooks file detected"
  error on `/reload-plugins`. The hook file is unchanged and still auto-loads.
- **Default per-project auto-emit (Approach A: smart server + thin host
  adapters).** Out of the box, the plugin now keeps a Scholialang trace per
  project, one DAG per session, tagged by host.
  - **Server core (shared by all three variants):** new idempotent
    `scholia_dag_ensure_session` tool (find-or-create the current session's
    per-project DAG, keyed by `host:session_id`, with a partial unique index +
    `IntegrityError` retry for concurrent hosts) and `scholia_dag_finish_session`
    (append the closing `Summary`/`Concluding`). Adds a `session_key` column with
    a guarded migration for pre-0.3.0 databases, a `PRAGMA busy_timeout` so two
    hosts writing the shared SQLite store wait instead of erroring, and `host` /
    `session_key` surfaced in DAG metadata.
  - **Opt-out (one switch, enforced server-side so every host honors it):**
    `SCHOLIA_AUTOEMIT=0` (or `false`/`off`) disables globally; a `.scholia-off`
    file in the project root disables that repo. Explicit, user-requested tracing
    (`auto=false`) still works.
  - **Claude Code adapter (guaranteed boundaries):** `SessionStart` hook opens/
    resumes the session DAG and injects its `dag_id` + emit guidance; `SessionEnd`
    hook appends the closing summary. Hooks are import-safe, fail closed, and
    never break the session. Skill documents the auto-emit convention.
  - **Codex + Ollama adapters (best-effort, no lifecycle hooks):** Codex skill
    instructs the model to call `ensure_session` (host `codex`) at task start;
    Ollama ships `recipes/autoemit-system-prompt.md` for generic MCP hosts.
  - **Anti-drift:** `scripts/sync_plugins.sh` propagates the canonical Claude Code
    server + vendored validator to the Codex and Ollama variants, keeping the
    three byte-identical (already enforced by the test suite).
- Add the local Codex Scholialang plugin under `plugins/codex/scholialang`.
- Add a repo-local Codex marketplace entry for `scholialang@scholialang-mcp`.
- Add full Codex rollout exhaust import into SQLite-backed Scholialang DAGs,
  including raw rollout atoms and internal agent harness canonical event atoms.
- Document the plugin storage model, safety policy, install flow, and validation
  commands.
- **Port the full scholialang v0.5 grammar validator into the bundled MCP
  server.** `scholia_lint_snippet` now runs the complete 17-rule check
  (well-formed, reference complete, decision closed, action recorded,
  hypothesis evaluated, retract consistent, constraint respected, goal
  declared, unknown operator, location/edge shape, v0.3.1 optional
  fields, Concluding goal resolution, Concluding citations, criticality
  non-decrease, action-modal warnings, duplicate active Concluding warnings,
  and confidence-ceiling warnings). The previous tag-balance behaviour is
  preserved via `mode='tag_balance'`. Validator prefers the installed
  `scholialang` package; falls back to a vendored snapshot at
  `scripts/_scholia_vendored/` when the package isn't available.
- Add `scholia_lint_trace` returning per-rule structured errors, warnings,
  and counts for CI gates and dashboards.
- Expand `scholia_catalog` to expose the full v0.5 closed-set
  vocabulary (32 atom kinds, canonical operators, edge types, effect
  kinds, ref types, and criticality rank) plus `lint_engine` and
  `validator_version`.
- **Add the Claude Code plugin under `plugins/claude-code/scholialang/`.**
  Same MCP server, same vendored validator, same skill semantics.
  Marketplace + Claude Code plugin manifest included.
- **Add the Ollama / local-model integration tree under
  `plugins/ollama/scholialang/`.** Same MCP server plus drop-in recipes
  for Continue.dev, Cline, open-webui, and a generic stdio MCP host
  spec. Single shared local SQLite store across all three plugins.
- Cross-plugin parity test guards against server-script drift between
  the three plugin trees.
- Bump bundled server version to `0.2.0`.

## v0.4.0

- Initial standalone protocol-tooling release for Scholia.
- Includes the MCP atlas server, provider adapter stubs, and the MVP LSP server.
