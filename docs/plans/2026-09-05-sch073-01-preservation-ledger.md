# SCH073-01 preservation ledger — reconciled viewer/provenance baseline

Date: 2026-09-05. Author: automated reconciliation run for PRD
`sch073-01-reconcile-baseline-scholialang-mcp` (control-plane records under
`.ralph/` / the run's KB post carry the run identity).

## 1. Source pins (re-resolved against the live remote before execution)

Remote: `git@github.com:dougfirlabs/scholialang-mcp.git` (`origin`).

| Ref | Branch | SHA (pinned == live) |
|---|---|---|
| master | `origin/master` | `d3bde04bc18d80ee9f504adbf4279343d59e513e` |
| stage | local `stage` (shared checkout), parent of this branch | `f08062fb9f3ea56645ab1ba1c75a177e21c29e9f` |
| PR #42 | `origin/feat/viewer-timestamps-model-meta` | `76785bcc074ce7f984885659882ff193a1b61cb1` |
| PR #43 | `origin/feat/dag-orchestrator-provenance` | `9767917514c7c2a5437af3a62c82949fd719a1c4` |
| PR #46 (HELD) | `origin/codex/fix-072-integrity-hardening` | `b39c2b84e4e213a572eb96e6320bff7feb6cbe69` |
| spec fingerprint | `scholialang-spec` | `9c1fcfa46059c8b461a92922bb65519b2b6bd5fe` |

Merge bases: #42↔master = master tip `d3bde04`; #43↔#42 = #42 tip `76785bc`
(#43 stacks directly on #42); stage↔master = master tip `d3bde04`;
#42↔stage = `d3bde04`. The shared checkout at
`~/projects/scholialang-mcp` (branch `stage`, `f08062f`) was not moved or
touched; all work happened in this candidate branch's isolated worktree.

## 2. Integration record

Cumulative branch: `feat/sch073-01-reconcile-baseline-scholialang-mcp`.

- `4fb9c40` — no-ff merge of PR #42 tip `76785bc` (4 author commits preserved
  in ancestry: `7c8f1e0`, `1273bbd`, `f21bfca`, `76785bc`).
- `c8d5aaf` — no-ff merge of PR #43 tip `9767917` (author commit preserved).

**Conflict decisions: none required.** Both merges were conflict-free; the
file sets are disjoint (stage touched skills/gate/materializer surfaces, the
PR stack touched plugin MCP/webview server scripts and added three test
modules). Verified post-merge that every file from the PR stack is
byte-identical to the #43 tip (`git diff 9767917 HEAD -- plugins/ tests/…`
shows only stage-side additions). No ours/theirs blanket selection occurred.

**Acceptance SHA: `c8d5aaf1e4878b96163e219e888aebdbb3aad353`.** Every
verification below ran on this tree. Commits after it are records only (this
ledger, run bookkeeping); they touch no shipped surface.

## 3. Verification environment

- Disposable venv `.venv-sch073` (uv, Python 3.12.3) inside the worktree;
  `pip install -e ".[dev]"`; `scholialang 0.7.2` from the index. No shared
  host or runner venv was modified.
- Pinned spec exported to a clean scratch dir via
  `git archive 9c1fcfa…` → `/tmp/sch073-spec-9c1fcfa`
  (`SCHOLIALANG_SPEC_DIR`), fixtures present, `MCP_REQUIRE_FINGERPRINT_FIXTURES=1`.
- Host contamination scrubbed for all acceptance runs: the runner exports
  `SCHOLIA_AUTOEMIT=0`, which flips the server's auto-emit opt-out and makes
  `tests/test_dag_orchestrator.py::test_session_dag_records_orchestrator_alongside_harness`
  fail (its fixture delenv's `SCHOLIA_ORCHESTRATOR`/`SCHOLIA_HOST` but not
  `SCHOLIA_AUTOEMIT`). Gates ran under
  `env -u SCHOLIA_AUTOEMIT -u SCHOLIA_HOST -u SCHOLIA_ORCHESTRATOR`.
  The missing delenv is a test-hygiene gap in #42/#43 as reviewed; proposed
  for the #46 corrective slice, NOT patched here (reviewed content preserved
  exactly).

## 4. Preservation rows (feature → regression command → result)

All commands ran at `c8d5aaf` with
`PYTHONPATH=src …/.venv-sch073/bin/python -m pytest` in the scrubbed env.

### PR #42 — viewer timestamps + model provenance (`76785bc`)

| Feature | Regression command | Result |
|---|---|---|
| Readable timestamps + relative-age refresh | `pytest tests/test_viewer_snapshot_revision.py` + browser evidence (§6) | pass |
| Model provenance, stamp + tag fallback, first-writer policy | `pytest tests/test_dag_model_provenance.py` (28 tests) | pass |
| Harness/stream distinction, placeholder filtering, session-end backfill | `pytest tests/test_dag_model_provenance.py` (transcript/backfill cases) | pass |
| Revision-based polling; unchanged poll cannot hydrate the graph | `pytest tests/test_viewer_snapshot_revision.py::test_revision_does_not_materialize_dags` + browser idle probe (§6) | pass |
| Schema migration (model column) on legacy DBs, idempotent | `pytest tests/test_dag_model_provenance.py::test_migration_*` + real legacy-DB exercise (§5) | pass |

### PR #43 — orchestrator provenance (`9767917`)

| Feature | Regression command | Result |
|---|---|---|
| Declared orchestrator recorded alongside harness | `pytest tests/test_dag_orchestrator.py` (16 tests) | pass |
| Stored-column-wins + tag fallback | same module (`test_stored_column_wins`, `test_falls_back_to_tag`) | pass |
| Hostname guard (machine names rejected for orchestrator/host) | same module (guard cases) | pass |
| Orchestrator schema migration, first-writer | same module + §5 | pass |

### Stage-only public-skills surfaces (`f08062f`)

| Feature | Regression command | Result |
|---|---|---|
| Doctor read-only | `pytest tests/test_scholia_doctor.py` | pass |
| Verify isolated (clean-wheel arm) | `pytest tests/test_scholia_verify.py` | pass |
| Deterministic skill materialization | `pytest tests/test_skill_materialization.py` + double-materialization (§7) | pass |
| No-publish release gate | `pytest tests/test_release_gate.py` + full gate run (§7) | pass |
| Desktop-host capability honesty | `pytest tests/test_claude_desktop_mcpb.py` | pass |
| Public hygiene (leak guard) | `pytest tests/test_public_hygiene.py` + raw CI grep on clean export | pass (zero matches) |
| Spec fingerprint parity (fail-closed, all six fixtures) | `pytest tests/test_spec_fingerprint_fixtures.py` with `MCP_REQUIRE_FINGERPRINT_FIXTURES=1` | pass |

### Full gates at the acceptance SHA

| Gate | Command | Result |
|---|---|---|
| Full suite | `PYTHONPATH=src python -m pytest -ra` (fixtures required, scrubbed env) | **328 passed, 0 failed, 0 skipped**, exit 0 |
| Codex plugin unittest | `python -m unittest plugins.codex.scholialang.tests.test_scholialang_mcp_server` | 28 tests OK, exit 0 |

## 5. Legacy-database migration evidence

A consistent read-only snapshot of the live operator database
(`~/.scholialang/scholialang.sqlite3`, 230 MB, pre-#42 schema — no `model`
or `orchestrator` columns) was taken with the sqlite backup API into a
disposable copy (`/tmp/sch073-legacy-db/`). Opening it with the merged
server's `connect()`:

- both columns added; second connect idempotent;
- row counts unchanged: 35,950 dags / 168,109 nodes / 122,873 edges;
- a real legacy dag reads through the new metadata path with null
  model/orchestrator and correct harness fallback (`codex`).

The live operator database itself was opened read-only and never written.

## 6. Browser evidence (real viewer)

`scholialang_webview_server.py` served a seeded disposable home; Chromium
via Playwright, desktop (1440×900) and mobile (390×844) viewports.
Artifacts: `/tmp/sch073-viewer-evidence/` (JSON results + screenshots).

- zero console errors, zero failed requests, zero HTTP ≥400 on both viewports;
- atoms/frontier/DAG list show readable timestamps and live relative ages;
- provenance strip renders `model claude-opus-4-7 via nightly-sweep`;
- 4-second unchanged idle window: **zero** `/api/snapshot` hydrations
  (revision-gated SSE push, matching `test_revision_does_not_materialize_dags`);
- an atom appended during viewing rendered **0.69 s** after the write —
  within the same clock second, confirming same-second update detection.

## 7. Skill materialization + release gate evidence

- `scripts/materialize_skills.py --check --json` → `ok: true`, no drift.
- Two independent materializations from two clean `git archive HEAD` exports:
  `diff -r` over `plugins/` → **byte-identical**.
- `scripts/release_gate.py --output-dir /tmp/sch073-release-gate --json` →
  `overall.verdict: "pass"`, zero reasons; wheel + mcpb artifacts built;
  every publication step recorded `performed: false`, blocked on explicit
  operator release approval; version recommendation `bump_before_publish`
  correctly left unapplied/non-binding. (`dirty_working_tree: true` in its
  source stanza reflects only untracked local venvs; a clean-export hygiene
  scan of the same HEAD was zero-match.)

## 8. Historical branch inventory (not promoted)

| Branch | Class | Rationale |
|---|---|---|
| `backup/…`, `stable/…` (4 refs) | rollback anchors | keep; never merge |
| `feat/skills-scholia-01/02/03`, `gated-stack/*skills*`, `repair/public-skills-wave1-hygiene`, `ops/public-skills-recovery-plan` | superseded | content already in stage `f08062f` |
| `feat/viewer-timestamps-model-meta`, `feat/dag-orchestrator-provenance` | integrated here | #42/#43, ancestry preserved in this branch |
| `codex/fix-072-integrity-hardening` | HELD | #46 — explicitly pending, see §9 |
| `feat/ext-scholialang-live-exhaust`, `feat/ext-scholialang-codex-live-exhaust`, `feat/live-viewer-feed-order`, `feat/ext-scholialang-multi-project-viewer` | reusable | viewer/exhaust follow-on candidates; unreviewed against this baseline |
| `feat/default-autoemit`, `feat/52x-b3d-mcp-fingerprint-parity`, `feat/jlens-v07-integration-gate`, `feat/mcp-2026-07-28-dual-version-adapter`, `chore/*`, `fix/*` (remaining) | stale/superseded | predate 0.7.2 baseline or already merged via master |

## 9. Disposition and rollback

- **#46 is explicitly pending.** Not merged, not cherry-picked, no behavior
  from `b39c2b8` entered this branch. Proposed next base for #46: the
  cumulative tip of this branch (acceptance SHA `c8d5aaf`, or the branch tip
  containing this ledger), after the PRD02 repairs land.
- **Proposed PR disposition (no action taken):** retarget #43's PR onto the
  cumulative branch or close #42/#43 as "merged via reconciliation" once the
  operator accepts this baseline; both source branches' tips are fully
  contained in this branch's ancestry, so either disposition loses nothing.
- **Rollback:** abandon branch `feat/sch073-01-reconcile-baseline-scholialang-mcp`;
  `stage` (`f08062f`), `master` (`d3bde04`), #42/#43/#46 tips, and the live
  operator database are all unchanged. Scratch artifacts live only under
  `/tmp/sch073-*` and are disposable.
