# Accepted core 0.7.3 input

MCP 0.7.3 consumes Scholialang commit
`9a86a4645c49074c4a415ade01093bff0e2ca70c`, accepted for consumer integration.
Its validator/package version is **0.7.3** and its language grammar is
**0.7.0**, with the legacy 0.6.2 profile and fixtures preserved. This is an
unpublished candidate; this receipt grants no publication or activation authority.

The accepted artifacts are retained in `vendor/core`, not rebuilt substitutes:

| Artifact | SHA256 |
| --- | --- |
| `scholialang-0.7.3-py3-none-any.whl` | `76c9e9a15cb3039bcf59a63b4727271cc9933745eecd4280712c5df65616317a` |
| `scholialang-0.7.3.tar.gz` | `45c710fe24f1713ae54a57740b991b506dbb124af36c8ca0698a70f54915e88d` |

`vendor/core/RECEIPT.json` binds these files to the original acceptance
receipt's hash, the source commit, accepted spec commit, dependency closure,
and rollback commit. Later rebuilds of the same version have different archive
hashes and are not the inputs used here. `scripts/verify_core_input.py` checks
the retained hashes, every Python module's wheel/sdist equality, and licenses.

## Offline installation

Create a private Python 3.11+ virtual environment. With pip and the project's
build requirements (`setuptools>=77`, `wheel`) available in that environment,
run from a checkout or unpacked source distribution:

```sh
python scripts/verify_core_input.py
python -m pip install --no-index --find-links vendor/core --require-hashes -r vendor/core/requirements.txt
python -m pip install --no-deps --no-build-isolation .
python -m pip check
```

For a prebuilt MCP wheel, replace `.` in the second install with its path.
The two runtime distributions install entirely from the retained wheelhouse.
The core dependency is pinned to `scholialang==0.7.3`; the hash-checked install
also prevents a different same-version artifact being substituted.

## Vendoring and dependency closure

`scripts/vendor_scholialang.py <core-checkout> <accepted-commit>` refuses an
unaccepted commit, changed artifact hashes, and any source/wheel/sdist module
difference. It copies `atoms`, `parser`, `validator`, and `serializer`, changing
only package-relative imports. Each host's `UPSTREAM.json` records the source
and rewritten hashes. Run `bash scripts/sync_plugins.sh` afterward to propagate
the canonical copy and license files. Core semantics are unchanged.

The standalone server prefers an installed engine only when its version and
all four source hashes match the receipt; otherwise it uses the retained
snapshot. Ordinary XML validation has no third-party dependency. Importing
the vendored serializer's YAML interface requires PyYAML; Desktop declares it.

Core is dual licensed **MIT OR Apache-2.0**. Both original license files ship
with each vendor snapshot and the retained artifacts. PyYAML **6.0.3** is the
only runtime dependency and is **MIT** licensed, with no transitive runtime
dependencies. Its original sdist, MIT license, and a portable pure-Python wheel
are retained. The wheel was built from that sdist with libyaml disabled; build
command/tool versions and all hashes are in the receipt. No core artifact was
uploaded or fetched from a package index.

## Parity contract

`tests/test_core073_artifact_contract.py` checks retained inputs, tamper
rejection, installed module bytes and origins, and the accepted 204-case
semantic corpus against the installed engine and each isolated host snapshot.
The upstream runner and semantic fixtures are retained unchanged with hashes.
The corpus tests include XML/JSON/YAML roundtrips and phase-specific negative
expectations. Missing required inputs fail, rather than skip.

The spec pin is `4e19eae3e55566dac450c0dbf7bcba03d12afc50`. The legacy
action-recorded corpus remains at 0.6.2; fingerprint fixtures are read from
that pinned spec checkout with `MCP_REQUIRE_FINGERPRINT_FIXTURES=1`.
Installed test runs set `MCP_EXPECT_IMPORT_PREFIX` to their private environment
so the origin checks and protocol subprocesses cannot use the source package.

Upstream historical comments mentioning older releases are preserved for
source parity; all active package, validator, plugin and grammar identities
reflect the accepted version axes.

## Rollback

Revert this candidate to restore the prior 0.7.2 receipt at MCP commit
`dbe66e9` (full hash in the machine receipt), whose core source was
`c6ff32a5b028ac40fd1c12303c3eb37700f51dca`. The prior snapshot remains in Git
history and the accepted 0.7.3 artifacts remain recoverable from this commit.
