# Input receipt — accepted scholialang 0.7.3 core artifact

This receipt binds scholialang-mcp v0.7.3 to the **exact accepted**
scholialang 0.7.3 core artifact. Vendoring consumed this accepted source
directly; no package index (PyPI or otherwise) was required or used to
materialize the engine.

## Accepted artifact identities

| Artifact | sha256 |
| --- | --- |
| `scholialang-0.7.3-py3-none-any.whl` | `bbe08813bb0431824fa82db6b086ff2aafca5f6024e0b377dcfa7d37c25c1831` |
| `scholialang-0.7.3.tar.gz` | `457fe675175adf2c3166eeb55ffe86f8e9e0fb72b5acea54615ac3401c2557b2` |

- **Source commit:** `9a86a4645c49074c4a415ade01093bff0e2ca70c`
  (scholialang release 0.7.3 — canonical reference-target preservation and
  exact collision identity in the registry/serializer).
- **Acceptance channel:** operator adjudication archive
  `sch073-preflight-20260904.VKrsy3/j04-adjudication/9a86a4645c49074c4a415ade01093bff0e2ca70c-astra-resume-20260905/artifacts/`
  (independent review record `ASTRA-RESUME-INDEPENDENT-REVIEW.md` and
  per-environment wheel/sdist/source verification logs live beside the
  artifacts in that archive).

## Vendoring parity (verified during this rebind)

- `src/scholialang/atoms.py`, `parser.py`, `validator.py`, `serializer.py`,
  and `registry.py` at commit `9a86a464…` are **byte-identical** (sha256) to
  the same files inside the accepted sdist, and the wheel's
  `scholialang/atoms.py` / `parser.py` / `validator.py` are byte-identical to
  the sdist copies. Vendoring from the commit therefore vendors the accepted
  artifact's source exactly.
- `scripts/vendor_scholialang.py` re-vendored the engine from that commit;
  per-file source and post-rewrite hashes are recorded in each plugin's
  `_scholia_vendored/UPSTREAM.json`, with `validator_version` reading
  `0.7.3`.

## Dependency and license closure

- Runtime dependency closure of scholialang 0.7.3: `pyyaml>=6.0` (used by the
  serializer's YAML surface via `yaml.safe_dump` / `yaml.safe_load`). No other
  runtime dependencies.
- License: `MIT OR Apache-2.0`; `LICENSE-MIT` and `LICENSE-APACHE` are
  shipped in both the accepted sdist and wheel (`license-files` metadata),
  matching this repository's own dual license.

## Known upstream cosmetic nit (deliberately preserved)

- Upstream 0.7.3's `validator.py` module docstring still says
  ``SCHOLIA_VALIDATOR_VERSION`` "reads ``0.7.2``" (and a comment references
  "0.7.2 behavior"); the **actual constant** in `atoms.py` reads `0.7.3`, and
  every reporting surface (catalog, `UPSTREAM.json`, dist metadata) says
  `0.7.3`. The vendored snapshot must stay byte-identical to the accepted
  artifact, so this stale upstream comment is preserved, not patched. Fix
  belongs upstream in a future scholialang release.

## Rollback

- Prior engine receipt: scholialang 0.7.2, vendored from commit
  `c6ff32a5b028ac40fd1c12303c3eb37700f51dca`. Reverting the v0.7.3 rebind
  commit restores that receipt; both artifact identities remain recorded
  here and in the Git history.
