# MCP 0.7.3 cumulative acceptance and merge package

The cumulative candidate is ready for the consolidated merge decision, with
the caveats below. It preserves the viewer/provenance baseline and integrity
repairs, consumes the accepted core 0.7.3 artifact, and adds opt-in durable
state plus independently gated modern stdio Events, Tasks, and Heartbeat.

The tested **product candidate** is
`d253d120f4062bd7be041d2be32f2316a7a82451` on
`feat/sch073-08-integrated-acceptance`. The subsequent acceptance-package
commit adds only this report and its machine receipt; it does not change the
tested runtime, tests, dependencies, or build inputs. Resolve and pin that
package commit before the merge decision. The run closeout records its full
SHA and live remote parity separately, avoiding a circular self-hash.

## Lineage and preserved receipts

Every row below is ancestral to the tested candidate. Historical reports are
preserved; this report adds cumulative evidence rather than rewriting them.

| Input | Exact accepted or integrated tip |
| --- | --- |
| Stage / rollback | `f08062fb9f3ea56645ab1ba1c75a177e21c29e9f` |
| Viewer timestamps/model provenance | `76785bcc074ce7f984885659882ff193a1b61cb1` |
| Orchestrator provenance | `9767917514c7c2a5437af3a62c82949fd719a1c4` |
| Integrity repair ancestry | `b39c2b84e4e213a572eb96e6320bff7feb6cbe69` |
| Recovered core-binding candidate | `3d08ff5acf586975d84b54f8938e7c4e7de015a0` |
| Recovered wave-one integration | `b7b44b62cc5c79a69f51ea2bbf7c987b090a20b4` |
| Durable implementation | `6de4d12462c8fae444e0c6f237092e45f703be31` |
| Durable lifecycle / accepted branch tip | `413490ff45da2bd2c2cd6d86391774a17f898cce` |
| Durable cumulative integration | `ecb17e2c52d4d5c8a6894e17cdc8e8e9a059e1d4` |
| Real adapter implementation | `e52fc6ab82be9df7771a148023f0ac9451951061` |
| Adapter lifecycle / accepted branch tip | `a4d220d988b2a0938d4c408321a8892ac66240d9` |
| Combined input to this review | `db34b6e418f36086d12ac9047060cb35f5a75dc4` |

The durable and adapter feature remotes retain their implementation tips;
their later lifecycle commits are preserved in the cumulative integration.
The old preservation ledger's held-integrity status is historical: subsequent
integration made the integrity tip ancestral, as independently verified here.
See the [preservation ledger](../plans/2026-09-05-sch073-01-preservation-ledger.md),
[core receipt](core-0.7.3.md), [store contract](../durable-capability-store.md),
and [adapter contract](../real-adapters.md).

## Repair and independent evidence

Independent review of `db34b6e` found that object/array routing keys could raise
`TypeError` before validation and terminate stdio. The repaired candidate
validates IDs, methods, and tool names before hash-based routing. Fourteen
regressions exercise off/enforce policies and prove that a valid discovery
request still succeeds after each error. An independent subprocess probe
also covered null, scalar, boolean and fractional variants, validated error
schemas, and observed clean EOF exits with empty stderr.

The independent verdict and hashes are recorded in the adjacent
[machine receipt](mcp-0.7.3-integrated-acceptance.json). This is candidate
acceptance; it does not certify a production embedding host.

| Check on repaired candidate | Result |
| --- | --- |
| Full repository and all three plugin test trees | 627 passed; zero failures/errors/skips |
| Installed wheel: all integration tests plus core artifact contract | 213 passed; zero failures/errors/skips |
| Repair subsystem before freezing candidate | 205 passed |
| Independently rerun malformed-routing regressions | 14 passed |
| Independent additional malformed-envelope subprocess cases | 14 passed |
| Two clean wheel builds | Identical SHA256 |
| Desktop bundle | Staged and hashed; not packed or distributed |
| Skill materialization | Fresh and deterministic |

Full tests used a clean `git archive` export with an isolated clone's Git
metadata at the exact candidate SHA. Git-dependent release-gate tests require
that metadata. A first archive-only attempt had 602 passes and 11 missing-Git
failures; the corrected harness ran every test again. No test was skipped to
obtain the final verdict. Test/build dependencies were installed in a private
Python 3.12 environment created from system Python. Core and PyYAML came from
the hash-checked retained wheelhouse; `pip check` passed with ambient import
paths removed. Installed tests asserted their private installation origins.

The full scope was `tests/` and `plugins/{codex,claude-code,ollama}/scholialang/tests/`.
The installed scope was `tests/integration/` and
`tests/test_core073_artifact_contract.py`. Commands used `--timeout=10` and
the launcher's required heavy-test ignores. Existing test-specific timeout
markers were retained. Fingerprint fixtures were required from a clean export
of spec `4e19eae3e55566dac450c0dbf7bcba03d12afc50`.

Wheel SHA256:
`c9a1fcaf7d36f5798f9a54b9a77cdf9f0e53e03b3affee0b55d2a466c35b5f1e`.
The receipt also binds the source archive, wire pins and independent evidence.
Captures contain disposable synthetic workloads; private runtime records and
raw model transcripts are excluded from this published package.

## Caveats and downstream handoff

- The original recovered-core run retained a failed lifecycle record. Its
  corrected candidate and remote receipt were subsequently integrated. Fresh
  cumulative tests above establish the current candidate's behavior; they do
  not relabel that historical run as successful.
- Trace validation remains report-only. This run's terminal process, final
  POST and parser-owned trace adjudication must occur after agent handoff.
  Trace presence alone is not terminal acceptance.
- Adapter certification in the workload fixture is synthetic. Production
  hosts must independently bind the exact installed implementation digest,
  schema pins, authorization, project/principal scope, and execution evidence.
  The receipt grants no activation authority; default launchers remain inert.
- Workers, identity authentication, heartbeat scheduling/consumer lineage,
  filesystem durability, and retention maintenance remain host responsibilities.
  Notifications may duplicate; retention overflow requires authoritative refetch.
- Local validation used Python 3.12. No new browser visual pass, hardware
  power-loss test, hosted transport acceptance, or wider Python matrix is claimed.
- The global test-floor gate was skipped because `OT_SKIP_TEST_FLOOR=1` was
  already set. The complete cumulative repository suite still ran above.

The consumer handoff is **prepared**, with receipt-bound candidate evidence.
It is not a delivery or acknowledgement claim. Downstream implementation stays
receipt-gated until the operator accepts this candidate. PyPI release,
production activation, and Cloud propagation remain separate gates.

## One consolidated merge decision and rollback

Proposed target: `stage`, expected at
`f08062fb9f3ea56645ab1ba1c75a177e21c29e9f`. Proposed method: a reviewed no-ff
merge of the pinned package commit from `feat/sch073-08-integrated-acceptance`.
Before merging, re-resolve remote stage and candidate parity; if stage moved,
reconcile and retest the new combined tree. This run creates no PR and performs
no merge or release.

Remote rollback branch:
`backup/sch073-08-stage-before-acceptance-20260906`, at the exact stage SHA above.
Before merge, rollback means leaving stage unchanged. After an authorized
no-ff merge, use `git revert -m 1 <recorded-merge-sha>` on a repair branch and
review it through the normal process. Do not reset or force-push stage.
For an embedding host, remove its adapter opt-in and restore its previous
application receipt; preserve capability databases and historical evidence.

All three acceptance stories are satisfied: SCH073-08-S1 cumulative regression,
SCH073-08-S2 independent adjudication, and SCH073-08-S3 consolidated merge,
rollback, and consumer package. The operator decision is whether to accept
this exact candidate for the proposed merge; release and activation are separate.
