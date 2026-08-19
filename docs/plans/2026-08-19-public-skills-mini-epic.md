# Public Scholialang skills mini-epic

Approved 2026-08-19. This is the public-repository half of the DFL agent-skills assessment and remains separate from OpenTalon's proprietary operator-skills track.

1. `skills-scholia-01-doctor` — read-only version, capability, and configuration diagnosis.
2. `skills-scholia-02-verify` — positive, negative, composition, and clean installed-artifact verification.
3. `skills-scholia-03-cross-host-release-gate` — deterministic host materialization and a no-publish release gate.

The plan runs through an OpenTalon GUI instance rooted at `scholialang-mcp`, uses Claude Fable 5, preserves `master`, and branches from a dedicated `stage` integration branch. No PRD authorizes publication, tagging, PyPI, marketplace updates, global installation, or default-branch writes.
