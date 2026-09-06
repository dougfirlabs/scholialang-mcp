# ScholiaLang MCP 0.7.3 manual acceptance smoke — codex launch route (2026-09-06)

Report-only acceptance smoke for PRD
`sch073-manual-acceptance-smoke-codex-20260906` (RSI run
`a53df8b581584fa99fd6d016049a75b8`). This receipt and its evidence files are
the only artifacts the run produces; no tracked product source was modified
anywhere.

## Verdict

**pass_with_caveats** — every executed check passed with exit code 0 and real
subprocess stdio evidence; the caveats below are environmental, not product
failures.

## Exact code under test

| Field | Value |
| --- | --- |
| Target repo | `scholialang-mcp` staged at `/tmp/sch073-mcp-stage-base-20260906` |
| Branch | `integration/sch073-mcp-stage-base-20260906` |
| Tip at test time | `75319cc8ac9b12dbfb76458c5c882fd6577f1a11` |
| PRD-recorded source tip | `db8f700` |
| Spec corpus | `scholialang-spec` clone at pinned ref `4e19eae3e55566dac450c0dbf7bcba03d12afc50` (per `scholialang-spec-ref.txt`) |

The tip is one commit ahead of the PRD-recorded `db8f700`; that commit
(`prd: add fresh codex acceptance smoke identity`) adds only
`.ralph/rsi/sch073-manual-acceptance-smoke-codex-20260906.json` (+19 lines).
The tested runtime, tests, dependencies, and build inputs are identical to
`db8f700`.

## Environment

Fresh untracked venv in the run worktree (Python 3.12.3, pytest 9.1.1,
pytest-timeout, jsonschema 4.23.0). `scholialang-mcp` was installed
**non-editable** from the staging tree with `scholialang==0.7.3` resolved from
the repo's own `vendor/core/scholialang-0.7.3-py3-none-any.whl`
(0.7.3 is not on PyPI — latest published is 0.7.2), keeping the smoke hermetic
to the pinned tip and leaving the staging tree untouched.

## SMOKE-CODEX-S1 — grammar/spec verification: PASS

All commands ran from `/tmp/sch073-mcp-stage-base-20260906` at the tip above.

| Command | Result | Exit | Log |
| --- | --- | --- | --- |
| `python -m pytest tests/test_core073_artifact_contract.py tests/test_spec_fingerprint_fixtures.py -q` | 8 passed, 5 skipped | 0 | `s1-grammar-spec.log` |
| same, `-rs` (skip reasons: fingerprint corpus not found) | 8 passed, 5 skipped | 0 | `s1-skip-reasons.log` |
| same, with `SCHOLIALANG_SPEC_DIR=/tmp/sch073-spec-pin-20260906` | 12 passed, 1 skipped | 0 | `s1-grammar-spec-pinned.log` |
| same, plus `MCP_REQUIRE_FINGERPRINT_FIXTURES=1` (fail-closed gate) | **13 passed, 0 skipped** | 0 | `s1-grammar-spec-final.log` |

The initial skips were solely the spec-fingerprint fixtures requiring a
`scholialang-spec` checkout. A throwaway clone of the local
`~/projects/scholialang-spec` repo was checked out at the pinned ref and
supplied via `SCHOLIALANG_SPEC_DIR`; with the fail-closed
`MCP_REQUIRE_FINGERPRINT_FIXTURES=1` gate engaged, the full 13-test set
passes with zero skips.

## SMOKE-CODEX-S2 — real MCP stdio features: PASS

One run, exit 0, **195/195 tests passed** (`s2-real-stdio.log`; count
confirmed by `--collect-only`):

```
python -m pytest tests/integration/test_real_adapters.py \
    tests/integration/test_mcp_protocol.py \
    tests/integration/test_mcp_2026_conformance.py \
    tests/integration/test_durable_store.py \
    -q --basetemp=<evidence>/pytest-basetemp
```

- `test_real_adapters.py` (151): real subprocess stdio peers, including
  malformed routing keys ×30 (server survives, correct JSON-RPC error codes),
  capability default-off combinations ×8, policy combinations ×27,
  task states and keyed-input idempotency, cancellation races, reconnect
  creation-retry/refetch/identity, heartbeat hints + new-epoch-on-restart,
  heartbeat invalid/expired/future/wrong-identity fail-closed, portable
  heartbeat lineage replay, revocation closing subscriptions and blocking
  refetch, and an independently prepared schema oracle (~70 wire cases).
- `test_mcp_protocol.py` (3) and `test_mcp_2026_conformance.py` (11):
  targeted protocol regressions — version negotiation, legacy initialize,
  removed-method and unknown-method fail-closed behavior.
- `test_durable_store.py` (30): default-off has no disk effect, abrupt
  process-exit/restart matrix, expiry/reconnect cursor and clock rollback,
  CAS concurrency, fail-closed policy drift.

### Wire evidence

76 wire captures were written by the real stdio harness (one JSON per spawned
server subprocess). Method histogram across all captures:
`subscriptions/listen` ×118, `notifications/subscriptions/acknowledged` ×80,
`tools/call` ×78, `server/discover` ×67, `resources/read` ×40, `tasks/get`
×39, `tasks/cancel` ×38, `tasks/update` ×18, `tools/list` ×12,
`notifications/cancelled` ×6, plus `tasks/result`, `tasks/provide_input`,
`tasks/list`, `notifications/tools/list_changed`, `notifications/tasks`.
Heartbeat traffic appears as the `com.dougfirlabs/heartbeat` capability
(×420) and `heartbeat://participants/...` resource reads.

Durable copies: five representative captures under `wire-samples/`
(malformed routing, heartbeat hints/epoch, task states + idempotency,
revocation, reconnect) and `wire-captures.sha256` fingerprinting all 76.
The full basetemp lived under the ephemeral run worktree and is regenerable
by re-running the command above.

## Changed-file check

`git status --porcelain` (tracked entries) in the staging repo was captured
before S1 and after S2: both show exactly one entry, the **pre-existing**
`.gitignore` modification (+2 local ignore lines for launcher directories,
present before this run started — see `s1-grammar-spec.log` header). No
tracked file was created, modified, or deleted by the smoke; untracked
launcher run-plan files under `.ralph/run-plans/` predate the run and were
not touched.

## Caveats and boundaries

1. **Report-only.** No merge, deploy, PyPI publication, or Cloud propagation
   occurred, and this receipt makes no claim about them. `scholialang 0.7.3`
   is **not** on PyPI; the vendored wheel was used deliberately.
2. **Driver.** The PRD's launch contract routed a codex driver; this
   execution ran under the Claude RSI driver in the launcher-provisioned run
   worktree. Commands, code, and evidence are driver-independent.
3. **Tip drift.** Tested tip `75319cc` vs PRD-recorded `db8f700`: PRD
   identity file only, runtime-identical (verified via `git show --stat`).
4. **Spec corpus.** Fingerprint fixtures came from a local clone of
   `scholialang-spec` checked out at the pinned ref, not a fresh remote
   fetch.
5. The staging repo's own `.venv` was broken (activate scripts, no
   interpreter); the smoke used a fresh venv in the run worktree instead.
