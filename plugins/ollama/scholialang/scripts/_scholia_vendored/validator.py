"""Scholia validator — the eight rules from NOTATION_REFERENCE.md §9.

Each rule is its own pure function for unit-testability. They all
take the trace + a pre-built reference index (id → atom) and return
a list of ``ValidationError`` — empty when the rule passes. The
public entry point :func:`validate` stitches them together and
returns a single ``ValidationResult`` with the breakdown.

Why rule-1 (well-formedness) is cheap here: if a trace got this far
it came from :mod:`scholialang.parser`, which raises on
malformed input. The rule still runs a structural pass (every atom
has a known kind, every Step has an atoms list) so a trace that
arrived via ``from_json`` / ``from_yaml`` is also covered without
re-invoking the XML-ish parser.

Performance target from the PRD: a 100-step trace validates in
< 50ms. The index is O(n) and every rule is O(n) or O(n log n).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .atoms import (
    ATOM_KINDS,
    CANONICAL_OPERATORS,
    PSEUDO_ATOM_KINDS,
    SCHOLIA_VALIDATOR_VERSION,
    V031_EDGE_TYPES,
    V031_EFFECT_KINDS,
    V031_LOCATION_RE,
    V031_META_CRITICALITIES,
    V031_REF_TYPES,
    V04B_EDGE_TYPES,
    Action,
    Atom,
    Concluding,
    Constraint,
    Deciding,
    Edge,
    Effect,
    Evidence,
    Finding,
    Goal,
    Hypothesis,
    Meta,
    Observation,
    Ref,
    Retract,
    Review,
    Step,
    Storing,
    Uncertainty,
    is_valid_location,
    parse_operators_from_content,
)

CONCLUSION_TYPES = (Finding, Concluding)


RULE_WELL_FORMED = "well_formed"
RULE_REFERENCE_COMPLETE = "reference_complete"
RULE_DECISION_CLOSED = "decision_closed"
RULE_ACTION_RECORDED = "action_recorded"
RULE_HYPOTHESIS_EVALUATED = "hypothesis_evaluated"
RULE_RETRACT_CONSISTENT = "retract_consistent"
RULE_CONSTRAINT_RESPECTED = "constraint_respected"
RULE_GOAL_DECLARED = "goal_declared"
RULE_UNKNOWN_OPERATOR = "unknown_operator"
RULE_LOCATION_EDGE_SHAPE = "location_edge_shape"
RULE_V031_OPTIONAL_FIELDS = "v031_optional_fields"

RULE_NAMES: tuple[str, ...] = (
    RULE_WELL_FORMED,
    RULE_REFERENCE_COMPLETE,
    RULE_DECISION_CLOSED,
    RULE_ACTION_RECORDED,
    RULE_HYPOTHESIS_EVALUATED,
    RULE_RETRACT_CONSISTENT,
    RULE_CONSTRAINT_RESPECTED,
    RULE_GOAL_DECLARED,
    RULE_UNKNOWN_OPERATOR,
    RULE_LOCATION_EDGE_SHAPE,
    RULE_V031_OPTIONAL_FIELDS,
)


@dataclass(frozen=True)
class ValidationError:
    """One rule violation.

    ``rule`` is one of ``RULE_NAMES``. ``atom_id`` points at the
    offending atom (empty when the rule applies to a Step or to the
    trace as a whole). ``message`` is a one-line human string.
    """

    rule: str
    atom_id: str
    message: str


@dataclass
class ValidationResult:
    """Outcome of a full :func:`validate` call.

    ``ok`` is ``True`` iff every rule produced zero errors. The
    per-rule breakdown on ``errors_by_rule`` preserves the ordering
    from ``RULE_NAMES`` so a caller rendering the output can surface
    rules in canonical order.

    ``scholia_validator_version`` records the validator semantic
    version (``SCHOLIA_VALIDATOR_VERSION`` in :mod:`scholialang.atoms`),
    so downstream tools can branch on the field-set the validator
    accepted. Introduced in v0.3.1 as part of the primitive-hooks
    reservation contract; before v0.3.1 the field was implicit.
    """

    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    errors_by_rule: dict[str, list[ValidationError]] = field(default_factory=dict)
    scholia_validator_version: str = SCHOLIA_VALIDATOR_VERSION

    def summary(self) -> str:
        """One-line human-readable summary of the validation outcome."""
        if self.ok:
            return "Scholia trace: valid."
        return (
            f"Scholia trace: {len(self.errors)} violation(s) across "
            f"{len([r for r, es in self.errors_by_rule.items() if es])} rule(s)."
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _walk_atoms(trace: list[Step]):
    """Yield every atom in the trace, depth-first, including nested children."""
    for step in trace:
        for atom in step.atoms:
            yield from _descend(atom)


def _descend(atom: Atom):
    """Yield ``atom`` and all descendant atoms, depth-first."""
    yield atom
    for child in atom.children:
        yield from _descend(child)


def _build_id_index(trace: list[Step]) -> dict[str, Atom]:
    """Map every declared atom id to its atom. Also indexes Step ids."""
    index: dict[str, Atom] = {}
    for atom in _walk_atoms(trace):
        if atom.id:
            index[atom.id] = atom
        if isinstance(atom, Storing) and atom.name:
            index[atom.name] = atom
        for match in re.finditer(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", atom.content, re.MULTILINE):
            index.setdefault(match.group(1), atom)
    return index


def _step_ids(trace: list[Step]) -> set[str]:
    """Set of Step ids; Steps aren't atoms but Implication/Reference
    can point at them, so references resolve when either an atom or a
    Step carries the id.
    """
    return {s.id for s in trace if s.id}


def _iter_operator_refs(atom: Atom):
    """Yield every ``OP:target`` pair declared on an atom's ``operators``."""
    for token in atom.operators:
        if ":" in token:
            op, target = token.split(":", 1)
            yield op, target


# ── Rule 1 — well-formedness ─────────────────────────────────────────


def check_well_formed(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 1 — every atom has a known kind + every Step has atoms.

    The parser raises on malformed XML before we get here, but AST
    reconstitution from JSON/YAML can hand us a structurally-invalid
    shape. This rule surfaces that class of bug loudly.
    """
    errors: list[ValidationError] = []
    for step in trace:
        if step.atoms is None:
            errors.append(
                ValidationError(
                    rule=RULE_WELL_FORMED,
                    atom_id=step.id or "",
                    message=(
                        f"Step '{step.id or '?'}' has no atoms list."
                    ),
                )
            )
    for atom in _walk_atoms(trace):
        if atom.kind not in ATOM_KINDS and atom.kind not in PSEUDO_ATOM_KINDS:
            errors.append(
                ValidationError(
                    rule=RULE_WELL_FORMED,
                    atom_id=atom.id or "",
                    message=(
                        f"Atom kind '{atom.kind}' is not in v0.2 catalog."
                    ),
                )
            )
    return errors


# ── Rule 2 — reference completeness ──────────────────────────────────


def check_reference_complete(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 2 — every ``REFER:id`` / attribute reference resolves."""
    errors: list[ValidationError] = []
    step_ids = _step_ids(trace)

    def _resolves(target: str) -> bool:
        return target in index or target in step_ids

    for atom in _walk_atoms(trace):
        for op, target in _iter_operator_refs(atom):
            if op in CANONICAL_OPERATORS and target and not _resolves(target):
                errors.append(
                    ValidationError(
                        rule=RULE_REFERENCE_COMPLETE,
                        atom_id=atom.id or "",
                        message=(
                            f"{op}:{target} does not resolve to any declared id."
                        ),
                    )
                )
        # v0.3.1: ``<Edge target="...">`` and ``<Ref target="...">``
        # carry repo-relative paths / test selectors / doc anchors,
        # NOT in-trace atom ids. Skip them here so they don't trip
        # reference-completeness.
        if isinstance(atom, (Edge, Ref)):
            continue
        # Structured reference attrs on specific atoms.
        for attr in ("to", "next", "for_ref", "for_goal", "target", "on", "of"):
            value = getattr(atom, attr, None)
            if isinstance(value, str) and value and not _resolves(value):
                if attr == "of" and isinstance(atom, Review):
                    # Reviews can reference cross-trace ids like
                    # "SubjectAgent:Finding_02"; accept those even
                    # though we can't resolve them in-trace.
                    if ":" in value:
                        continue
                if attr == "to":
                    # Handoff/Question ``to`` is a role or agent name,
                    # not a trace-local id — skip the resolve check.
                    continue
                if attr == "target" and isinstance(atom, Edge):
                    # v0.4-B — Edge.target is a file path / import
                    # path, not an in-trace atom id. The
                    # location/edge-shape rule validates its shape;
                    # reference-completeness has nothing to enforce.
                    continue
                errors.append(
                    ValidationError(
                        rule=RULE_REFERENCE_COMPLETE,
                        atom_id=atom.id or "",
                        message=(
                            f"{atom.kind}.{attr}='{value}' does not resolve "
                            "to any declared id."
                        ),
                    )
                )
        for attr in ("related_constraints",):
            values = getattr(atom, attr, None)
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, str):
                    continue
                target = value.removeprefix("REFER:")
                if target and not _resolves(target):
                    errors.append(
                        ValidationError(
                            rule=RULE_REFERENCE_COMPLETE,
                            atom_id=atom.id or "",
                            message=(
                                f"{atom.kind}.{attr} contains '{value}', "
                                "which does not resolve to any declared id."
                            ),
                        )
                    )
    return errors


# ── Rule 8 — operator-known (closed-set check) ───────────────────────


def check_unknown_operator(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 8 — every ``OP:atom_id`` token uses a canonical operator.

    Added in v0.3 (2026-05-03) following empirical emergence of
    ``NOT:atom_id`` during the rsi-uvicorn-teardown-quiet run. The
    check scans atom content via ``parse_operators_from_content``
    rather than ``atom.operators`` because the parser's operator
    extraction is alternation-bound to the spec-listed ``OPERATORS``
    tuple — a fully novel operator name (e.g. ``MAYBE``, ``PERHAPS``)
    would not survive into ``atom.operators`` and would slip through
    silently. Scanning content with the broader detector regex
    (any ``[A-Z][A-Z_]+:atom_id`` shape) closes that gap.

    Validator-reject + grammar-emergence-log are not mutually
    exclusive: this rule fails the trace, the detector still appends
    a finding under a host-managed grammar-emergence sidecar
    so the spec-extension promotion pipeline keeps the corpus.

    Targets must match the Scholia atom_id shape (CapitalizedWord with
    at least one ``_``, e.g. ``Hyp_01`` / ``GatherInput_04``) — prose
    with colons (``BUT: git's …``, ``VERDICT: READY``) does not trip
    the rule.
    """
    errors: list[ValidationError] = []
    seen: set[tuple[str, str]] = set()
    for atom in _walk_atoms(trace):
        if not atom.content:
            continue
        for op, target in parse_operators_from_content(atom.content):
            if op in CANONICAL_OPERATORS:
                continue
            if "_" not in target:
                continue
            key = (atom.id or "", op)
            if key in seen:
                continue
            seen.add(key)
            errors.append(
                ValidationError(
                    rule=RULE_UNKNOWN_OPERATOR,
                    atom_id=atom.id or "",
                    message=(
                        f"Unknown operator {op!r}; canonical set is "
                        f"{sorted(CANONICAL_OPERATORS)}."
                    ),
                )
            )
    return errors


# ── Rule 3 — decision closure ────────────────────────────────────────


def check_decision_closed(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 3 — every ``<Deciding>`` produces a conclusion atom."""
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if not isinstance(atom, Deciding):
            continue
        if not any(
            isinstance(descendant, CONCLUSION_TYPES)
            for child in atom.children
            for descendant in _descend(child)
        ) and "decision =" not in atom.content:
            errors.append(
                ValidationError(
                    rule=RULE_DECISION_CLOSED,
                    atom_id=atom.id or "",
                    message=(
                        "Deciding block has no child Finding or Concluding — branch "
                        "choice not recorded."
                    ),
                )
            )
    return errors


# ── Rule 4 — action recorded ─────────────────────────────────────────


def check_action_recorded(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 4 — every ``<Action>`` is followed by or contains a conclusion.

    The §8 composition rule says an Action must produce a conclusion. We
    accept either a direct child conclusion or a sibling conclusion that
    appears later in the same Step — agents often write the Finding
    as a peer atom rather than nesting it.
    """
    errors: list[ValidationError] = []
    for step in trace:
        # Build pre-order list of (index, atom) for siblings.
        for i, atom in enumerate(step.atoms):
            if not isinstance(atom, Action):
                continue
            has_nested = any(isinstance(c, CONCLUSION_TYPES) for c in atom.children)
            has_sibling = any(
                isinstance(sib, CONCLUSION_TYPES) for sib in step.atoms[i + 1 :]
            )
            if not (has_nested or has_sibling):
                errors.append(
                    ValidationError(
                        rule=RULE_ACTION_RECORDED,
                        atom_id=atom.id or "",
                        message=(
                            "Action has no recording Finding or Concluding (neither "
                            "nested nor sibling)."
                        ),
                    )
                )
    return errors


# ── Rule 5 — hypothesis evaluated ────────────────────────────────────


def check_hypothesis_evaluated(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 5 — every Hypothesis has Evidence OR explicit Uncertainty."""
    errors: list[ValidationError] = []

    # Gather evidence/uncertainty references up front so the per-
    # hypothesis lookup is O(1). Evidence.for_ref points at the
    # hypothesis id; Uncertainty.on points at the same.
    evidence_by_target: dict[str, list[Evidence]] = {}
    uncertainty_by_target: dict[str, list[Uncertainty]] = {}
    for atom in _walk_atoms(trace):
        if isinstance(atom, Evidence) and atom.for_ref:
            evidence_by_target.setdefault(atom.for_ref, []).append(atom)
        elif isinstance(atom, Uncertainty) and atom.on:
            uncertainty_by_target.setdefault(atom.on, []).append(atom)

    for atom in _walk_atoms(trace):
        if not isinstance(atom, Hypothesis):
            continue
        hid = atom.id or ""
        if not hid:
            errors.append(
                ValidationError(
                    rule=RULE_HYPOTHESIS_EVALUATED,
                    atom_id="",
                    message=(
                        "Hypothesis without an id cannot be linked to "
                        "Evidence or Uncertainty."
                    ),
                )
            )
            continue
        if hid in evidence_by_target or hid in uncertainty_by_target:
            continue
        errors.append(
            ValidationError(
                rule=RULE_HYPOTHESIS_EVALUATED,
                atom_id=hid,
                message=(
                    f"Hypothesis '{hid}' has no Evidence and no "
                    "open Uncertainty — reasoning dangling."
                ),
            )
        )
    return errors


# ── Rule 6 — retract consistent ──────────────────────────────────────


def check_retract_consistent(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 6 — every Retract names an existing conclusion id."""
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if not isinstance(atom, Retract):
            continue
        target = atom.target or ""
        if not target:
            errors.append(
                ValidationError(
                    rule=RULE_RETRACT_CONSISTENT,
                    atom_id=atom.id or "",
                    message="Retract is missing a target attribute.",
                )
            )
            continue
        referenced = index.get(target)
        if referenced is None:
            errors.append(
                ValidationError(
                    rule=RULE_RETRACT_CONSISTENT,
                    atom_id=atom.id or "",
                    message=(
                        f"Retract target '{target}' does not resolve to "
                        "any declared id."
                    ),
                )
            )
        elif not isinstance(referenced, CONCLUSION_TYPES):
            errors.append(
                ValidationError(
                    rule=RULE_RETRACT_CONSISTENT,
                    atom_id=atom.id or "",
                    message=(
                        f"Retract target '{target}' is a "
                        f"{referenced.kind}, not a Finding or Concluding."
                    ),
                )
            )
    return errors


# ── Rule 7 — constraint respected ────────────────────────────────────


def check_constraint_respected(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 7 — no Action violates an active ``<Constraint>``.

    v0.1 scoping: a Constraint is active from the moment it appears
    to the end of the trace. Violation detection is a keyword test —
    if the constraint text says ``Never <verb>`` or ``must not
    <verb>``, a later Action whose content contains that verb is
    flagged. v0.1 intentionally errs on the side of false negatives
    (a linter is not a theorem prover); explicit audit via ``<Review>``
    is the backstop for constraint interpretation.
    """
    errors: list[ValidationError] = []
    active_constraints: list[Constraint] = []

    # Flatten step-order so "active from appearance" is a simple scan.
    ordered: list[Atom] = []
    for step in trace:
        for top in step.atoms:
            for atom in _descend(top):
                ordered.append(atom)

    for atom in ordered:
        if isinstance(atom, Constraint):
            active_constraints.append(atom)
            continue
        if not isinstance(atom, Action):
            continue
        action_content = atom.content.lower()
        for constraint in active_constraints:
            verbs = _extract_forbidden_verbs(constraint.content)
            for verb in verbs:
                if verb and verb in action_content:
                    errors.append(
                        ValidationError(
                            rule=RULE_CONSTRAINT_RESPECTED,
                            atom_id=atom.id or "",
                            message=(
                                f"Action appears to violate constraint "
                                f"'{constraint.id or '?'}': forbidden verb "
                                f"'{verb}' in Action content."
                            ),
                        )
                    )
    return errors


_FORBIDDEN_RE_PARTS = (
    r"[Nn]ever\s+(?P<verb1>[a-z][a-z_\- ]{1,40}?)\b",
    r"must\s+not\s+(?P<verb2>[a-z][a-z_\- ]{1,40}?)\b",
    r"do\s+not\s+(?P<verb3>[a-z][a-z_\- ]{1,40}?)\b",
)


def _extract_forbidden_verbs(constraint_text: str) -> list[str]:
    """Pull the verb phrase following ``Never`` / ``must not`` / ``do not``.

    Each verb phrase is normalised to lowercase + whitespace-stripped
    so the keyword test against action content is case-insensitive.
    Returns an empty list when the constraint doesn't match any of
    the three forbidden-pattern templates.
    """
    import re as _re

    verbs: list[str] = []
    for pattern in _FORBIDDEN_RE_PARTS:
        for match in _re.finditer(pattern, constraint_text):
            for key in ("verb1", "verb2", "verb3"):
                try:
                    phrase = match.group(key)
                except (IndexError, LookupError):
                    continue
                if phrase:
                    verbs.append(phrase.strip().lower())
    return verbs


# ── Rule 8 — goal declaration ────────────────────────────────────────


_GOAL_STATUSES = {"met", "unmet", "partially_met", "met_late"}


def check_goal_declared(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 8 — every required Goal has a status-declaring conclusion."""
    if any(atom.kind == "Meta:research-mode" for atom in _walk_atoms(trace)):
        return []

    errors: list[ValidationError] = []
    findings_by_goal: dict[str, list[Atom]] = {}
    for atom in _walk_atoms(trace):
        if isinstance(atom, CONCLUSION_TYPES) and getattr(atom, "for_goal", None):
            findings_by_goal.setdefault(atom.for_goal, []).append(atom)

    for atom in _walk_atoms(trace):
        if not isinstance(atom, Goal):
            continue
        if (atom.priority or "optional") != "required":
            continue
        goal_id = atom.id or ""
        if not goal_id:
            errors.append(
                ValidationError(
                    rule=RULE_GOAL_DECLARED,
                    atom_id="",
                    message="Required Goal must carry an id.",
                )
            )
            continue
        status_findings = [
            finding
            for finding in findings_by_goal.get(goal_id, [])
            if finding.status in _GOAL_STATUSES
        ]
        if status_findings:
            continue
        errors.append(
            ValidationError(
                rule=RULE_GOAL_DECLARED,
                atom_id=goal_id,
                message=(
                    f"Required Goal '{goal_id}' has no Finding or Concluding with "
                    "for_goal and status in met/unmet/partially_met."
                ),
            )
        )
    return errors


# ── Rule 10 — v0.3.1 optional-field closed-set check (defensive) ─────


def check_v031_optional_fields(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 10 — v0.3.1 primitive-hook closed-set values are well-formed.

    The parser raises ``ScholiaParseError`` on malformed v0.3.1 values
    when input comes through the XML-ish parse path. This rule mirrors
    those checks at the validator layer so AST-reconstituted traces
    (e.g. loaded from JSON/YAML, or constructed in tests) get the
    same strict-closed-set enforcement.

    Absence of every reserved field is the v0.3 shape and validates
    trivially. Presence triggers the closed-set rule per
    ``docs/scholia/SCHOLIA_v0.3.1_SPEC.md``.
    """
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if isinstance(atom, Observation):
            location = atom.location
            if location is not None and not V031_LOCATION_RE.match(location):
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<Observation> location must match "
                            f"'file:start:end'; got {location!r}."
                        ),
                    )
                )
            confidence = atom.confidence
            if confidence is not None:
                bad = False
                try:
                    value = float(confidence)
                except (TypeError, ValueError):
                    bad = True
                    value = None
                if bad or value is None or not 0.0 <= value <= 1.0:
                    errors.append(
                        ValidationError(
                            rule=RULE_V031_OPTIONAL_FIELDS,
                            atom_id=atom.id or "",
                            message=(
                                f"<Observation> confidence must be a float "
                                f"in [0.0, 1.0]; got {confidence!r}."
                            ),
                        )
                    )
        elif isinstance(atom, Edge):
            if atom.edge_type is not None and atom.edge_type not in V031_EDGE_TYPES:
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<Edge> type must be one of "
                            f"{sorted(V031_EDGE_TYPES)}; got "
                            f"{atom.edge_type!r}."
                        ),
                    )
                )
        elif isinstance(atom, Effect):
            if (
                atom.effect_kind is not None
                and atom.effect_kind not in V031_EFFECT_KINDS
            ):
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<Effect> kind must be one of "
                            f"{sorted(V031_EFFECT_KINDS)}; got "
                            f"{atom.effect_kind!r}."
                        ),
                    )
                )
        elif isinstance(atom, Ref):
            if atom.ref_type is not None and atom.ref_type not in V031_REF_TYPES:
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<Ref> type must be one of "
                            f"{sorted(V031_REF_TYPES)}; got "
                            f"{atom.ref_type!r}."
                        ),
                    )
                )
        elif isinstance(atom, Meta):
            if (
                atom.criticality is not None
                and atom.criticality not in V031_META_CRITICALITIES
            ):
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<Meta> criticality must be one of "
                            f"{sorted(V031_META_CRITICALITIES)}; got "
                            f"{atom.criticality!r}."
                        ),
                    )
                )
    return errors


# ── Rule 9 — v0.4-B location + edge shape ────────────────────────────


def check_location_edge_shape(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 9 (v0.4-B) — strict shape enforcement on Edge.target presence.

    Per PRD rsi-scholia-v0.4-code-graph-metadata story V04B-02:
    ``<Edge target=...>`` MUST be a non-empty string when the atom is
    present. ``location`` regex and ``Edge.type`` closed-set are also
    covered by :func:`check_v031_optional_fields`; this rule adds the
    target-presence check that v0.3.1 didn't cover.

    The rule is intentionally narrow: it does NOT check that the line
    span resolves to an actual symbol in the referenced file (that
    drift is the "best-at-sweep-time" semantic the PRD documents).
    """
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if isinstance(atom, Observation):
            loc = atom.location
            if loc and not is_valid_location(loc):
                errors.append(
                    ValidationError(
                        rule=RULE_LOCATION_EDGE_SHAPE,
                        atom_id=atom.id or "",
                        message=(
                            f"Observation.location={loc!r} does not "
                            "match shape <path>:<start>:<end>."
                        ),
                    )
                )
        if isinstance(atom, Edge):
            if atom.edge_type is not None and atom.edge_type not in V04B_EDGE_TYPES:
                errors.append(
                    ValidationError(
                        rule=RULE_LOCATION_EDGE_SHAPE,
                        atom_id=atom.id or "",
                        message=(
                            f"Edge.type={atom.edge_type!r} is not in "
                            f"closed set {sorted(V04B_EDGE_TYPES)}."
                        ),
                    )
                )
            if atom.target is not None and not atom.target.strip():
                errors.append(
                    ValidationError(
                        rule=RULE_LOCATION_EDGE_SHAPE,
                        atom_id=atom.id or "",
                        message="Edge.target must be a non-empty string.",
                    )
                )
    return errors


# ── Orchestration ────────────────────────────────────────────────────


_RULES: tuple[tuple[str, Callable[[list[Step], dict[str, Atom]], list[ValidationError]]], ...] = (
    (RULE_WELL_FORMED, check_well_formed),
    (RULE_REFERENCE_COMPLETE, check_reference_complete),
    (RULE_DECISION_CLOSED, check_decision_closed),
    (RULE_ACTION_RECORDED, check_action_recorded),
    (RULE_HYPOTHESIS_EVALUATED, check_hypothesis_evaluated),
    (RULE_RETRACT_CONSISTENT, check_retract_consistent),
    (RULE_CONSTRAINT_RESPECTED, check_constraint_respected),
    (RULE_GOAL_DECLARED, check_goal_declared),
    (RULE_UNKNOWN_OPERATOR, check_unknown_operator),
    (RULE_LOCATION_EDGE_SHAPE, check_location_edge_shape),
    (RULE_V031_OPTIONAL_FIELDS, check_v031_optional_fields),
)


def validate(trace: list[Step]) -> ValidationResult:
    """Run all ten rules against ``trace`` and return a ``ValidationResult``.

    Rule 10 is the v0.3.1 primitive-hook closed-set check; it is a
    defensive mirror of the parser-side rejection so AST-reconstituted
    traces (JSON/YAML/in-test) get the same enforcement.
    """
    index = _build_id_index(trace)
    errors: list[ValidationError] = []
    errors_by_rule: dict[str, list[ValidationError]] = {
        name: [] for name in RULE_NAMES
    }
    for name, rule in _RULES:
        rule_errors = rule(trace, index)
        errors.extend(rule_errors)
        errors_by_rule[name] = rule_errors
    return ValidationResult(
        ok=not errors,
        errors=errors,
        errors_by_rule=errors_by_rule,
        scholia_validator_version=SCHOLIA_VALIDATOR_VERSION,
    )
