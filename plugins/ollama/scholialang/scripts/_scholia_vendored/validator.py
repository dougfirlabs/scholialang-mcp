"""Scholia validator — v0.2-v0.6 rules from the canonical spec.

Covers the cumulative rule set: the v0.2-v0.4 structural/reference rules,
the v0.5 Concluding-scoped rules (for_goal_resolves, refer_at_least_one,
criticality_non_decreasing + the three warnings), and the v0.6
content-addressable additions — ``canonical_id_well_formed`` (a universal
recompute-and-compare hard-fail) and the canonical-id-aware
``reference_complete`` rule fed by the 4-path :func:`resolve_refer`
resolver. v0.6.2 extends ``action_recorded`` to accept later, explicitly
REFER-linked results across Step boundaries. v0.7.1 adds the additive
``fingerprint_well_formed`` rule (see ``FINGERPRINT.md`` in scholialang-spec):
hard-fail, vacuous when the attribute is absent, and purely structural -- it
never recomputes a digest against source.

``SCHOLIA_VALIDATOR_VERSION`` tracks the package version and reads ``0.7.2``.
It was versioned separately through 0.6.2; as of the 0.7.1 synchronized suite
release the two move together, so a reader can map a validation result straight
onto an installed package. The shared spec conformance corpus is a separate
axis and is still cut at v0.6.2 -- an additive rule revision must keep passing
it byte-for-byte. ``tests/unit/scholia/test_release_versions.py`` enforces the
package-side parity.

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
from typing import Any, Callable, Optional

from .atoms import (
    ATOM_KINDS,
    CANONICAL_OPERATORS,
    CRITICALITY_RANK,
    EVENT_TYPE_RE,
    GRAMMAR_PROFILES,
    JSON_SAFE_INT_MAX,
    JSON_SAFE_INT_MIN,
    MAP_VALUE_TYPES,
    compute_canonical_id,
    PSEUDO_ATOM_KINDS,
    SCHOLIA_GRAMMAR_VERSION,
    SCHOLIA_VALIDATOR_VERSION,
    SEMANTIC_BODY_REQUIRED,
    SEMANTIC_ID_PATTERNS,
    SEMANTIC_KINDS,
    SEMANTIC_OPAQUE_FIELDS,
    SEMANTIC_REF_FIELDS,
    TASK_STATUSES,
    V031_EDGE_TYPES,
    V031_EFFECT_KINDS,
    V031_LOCATION_RE,
    V031_META_CRITICALITIES,
    V031_REF_TYPES,
    V04B_EDGE_TYPES,
    Action,
    Atom,
    Confidence,
    Constraint,
    Concluding,
    Deciding,
    Edge,
    Effect,
    Evidence,
    Event,
    Finding,
    Goal,
    Hypothesis,
    Map,
    Meta,
    Observation,
    Ref,
    Retract,
    Review,
    Step,
    Storing,
    Task,
    Uncertainty,
    is_valid_fingerprint,
    is_valid_location,
    parse_operators_from_content,
    semantic_structural_violations,
)


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
RULE_FOR_GOAL_RESOLVES = "for_goal_resolves"
RULE_REFER_AT_LEAST_ONE = "refer_at_least_one"
RULE_CRITICALITY_NON_DECREASING = "criticality_non_decreasing"
RULE_NO_ACTION_IN_CONCLUDING = "no_action_in_concluding"
RULE_SINGLE_ACTIVE_CONCLUDING_PER_GOAL = "single_active_concluding_per_goal"
RULE_MIN_CONFIDENCE_CEILING = "min_confidence_ceiling"
# v0.6 — content-addressable canonical_id integrity (hard-fail).
RULE_CANONICAL_ID_WELL_FORMED = "canonical_id_well_formed"
# v0.6.x-proposed — fingerprint= well-formedness (hard-fail; vacuous when
# absent). See docs/scholia/FINGERPRINT.md §3.
RULE_FINGERPRINT_WELL_FORMED = "fingerprint_well_formed"

# v0.7.3-candidate — proposed Map/Event/Task semantic rules (hard-fail,
# vacuous when no semantic atom is present). The first three mirror the
# parse-phase structural strictness for Python-constructed atoms.
RULE_SEMANTIC_CHILDREN_EMPTY = "semantic_children_empty"
RULE_SEMANTIC_OPERATORS_LIST = "semantic_operators_list"
RULE_MAP_ENTRIES_SHAPE = "map_entries_shape"
RULE_SEMANTIC_ID_SHAPE = "semantic_id_shape"
RULE_SEMANTIC_ID_UNIQUE = "semantic_id_unique"
RULE_MAP_REQUIRED_FIELDS = "map_required_fields"
RULE_MAP_VALUE_TYPE = "map_value_type"
RULE_MAP_ENTRIES_TYPED = "map_entries_typed"
RULE_MAP_REF_RESOLVES = "map_ref_resolves"
RULE_EVENT_REQUIRED_FIELDS = "event_required_fields"
RULE_EVENT_TYPE_TOKEN = "event_type_token"
RULE_EVENT_TIMESTAMP_SHAPE = "event_timestamp_shape"
RULE_EVENT_OCCURRENCE_UNIQUE = "event_occurrence_unique"
RULE_TASK_REQUIRED_FIELDS = "task_required_fields"
RULE_TASK_STATUS_ENUM = "task_status_enum"
RULE_TASK_EVIDENCE_REQUIRED = "task_evidence_required"
RULE_SEMANTIC_REF_TARGET_KIND = "semantic_ref_target_kind"
# v0.7 — explicit grammar-profile negotiation outcome (hard-fail).
RULE_GRAMMAR_PROFILE_UNSUPPORTED = "grammar_profile_unsupported"

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
    RULE_FOR_GOAL_RESOLVES,
    RULE_REFER_AT_LEAST_ONE,
    RULE_CRITICALITY_NON_DECREASING,
    RULE_NO_ACTION_IN_CONCLUDING,
    RULE_SINGLE_ACTIVE_CONCLUDING_PER_GOAL,
    RULE_MIN_CONFIDENCE_CEILING,
    RULE_CANONICAL_ID_WELL_FORMED,
    RULE_FINGERPRINT_WELL_FORMED,
    RULE_SEMANTIC_CHILDREN_EMPTY,
    RULE_SEMANTIC_OPERATORS_LIST,
    RULE_MAP_ENTRIES_SHAPE,
    RULE_SEMANTIC_ID_SHAPE,
    RULE_SEMANTIC_ID_UNIQUE,
    RULE_MAP_REQUIRED_FIELDS,
    RULE_MAP_VALUE_TYPE,
    RULE_MAP_ENTRIES_TYPED,
    RULE_MAP_REF_RESOLVES,
    RULE_EVENT_REQUIRED_FIELDS,
    RULE_EVENT_TYPE_TOKEN,
    RULE_EVENT_TIMESTAMP_SHAPE,
    RULE_EVENT_OCCURRENCE_UNIQUE,
    RULE_TASK_REQUIRED_FIELDS,
    RULE_TASK_STATUS_ENUM,
    RULE_TASK_EVIDENCE_REQUIRED,
    RULE_SEMANTIC_REF_TARGET_KIND,
    RULE_GRAMMAR_PROFILE_UNSUPPORTED,
)

WARNING_RULE_NAMES: tuple[str, ...] = (
    RULE_NO_ACTION_IN_CONCLUDING,
    RULE_SINGLE_ACTIVE_CONCLUDING_PER_GOAL,
    RULE_MIN_CONFIDENCE_CEILING,
)

CONCLUSION_TYPES = (Finding, Concluding)


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


@dataclass(frozen=True)
class ValidationWarning:
    """One non-fatal validator warning."""

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
    warnings: list[ValidationWarning] = field(default_factory=list)
    warnings_by_rule: dict[str, list[ValidationWarning]] = field(default_factory=dict)
    scholia_validator_version: str = SCHOLIA_VALIDATOR_VERSION

    def summary(self) -> str:
        """One-line human-readable summary of the validation outcome."""
        if self.ok:
            if self.warnings:
                return f"Scholia trace: valid with {len(self.warnings)} warning(s)."
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
    """Yield every ``OP:target`` pair declared on an atom's ``operators``.

    Non-string tokens are skipped defensively — a malformed operators
    list on a v0.7 semantic atom is reported by the
    ``semantic_operators_list`` rule instead of crashing the walk.
    """
    for token in atom.operators:
        if isinstance(token, str) and ":" in token:
            op, target = token.split(":", 1)
            yield op, target


_ACTION_MODAL_RE: re.Pattern[str] = re.compile(
    r"\b(should|will|recommend|choose|propose|recommends?|proposes?|chooses?)\s+\w+",
    re.IGNORECASE,
)


def _parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _refer_targets(atom: Atom) -> set[str]:
    """Return exact ``REFER:`` targets declared on ``atom``.

    Parser-produced atoms populate ``operators``; hand-built tests and older
    persisted traces may carry only inline content. Scan both representations
    so semantic closure does not depend on which construction path was used.
    """
    targets = {
        target
        for op, target in _iter_operator_refs(atom)
        if op == "REFER" and target
    }
    targets.update(
        target
        for op, target in parse_operators_from_content(atom.content)
        if op == "REFER" and target
    )
    return targets


def _graph_has_edge(
    graph: Any,
    *,
    edge_type: str,
    source_id: str | None = None,
    target_id: str | None = None,
) -> bool:
    """Query the optional validator graph through its minimal protocol."""
    if graph is None:
        return False
    has_edge = getattr(graph, "has_edge", None)
    if not callable(has_edge):
        return False
    try:
        return bool(
            has_edge(
                edge_type=edge_type,
                source_id=source_id,
                target_id=target_id,
            )
        )
    except TypeError:
        return False


def _atom_criticality(atom: Atom) -> Optional[str]:
    direct = getattr(atom, "criticality", None)
    if isinstance(direct, str) and direct:
        return direct
    for child in atom.children:
        if isinstance(child, Meta) and child.criticality:
            return child.criticality
    return None


def _atom_confidence(atom: Atom, all_atoms: list[Atom]) -> Optional[float]:
    direct = getattr(atom, "confidence", None)
    parsed = _parse_float(direct)
    if parsed is not None:
        return parsed
    if not atom.id:
        return None
    for other in all_atoms:
        if isinstance(other, Uncertainty) and other.on == atom.id:
            value = _parse_float(other.confidence)
            if value is not None:
                return value
        if isinstance(other, Confidence) and other.on == atom.id:
            value = _parse_float(other.level)
            if value is not None:
                return value
    return None


def _retracted_ids(all_atoms: list[Atom]) -> set[str]:
    return {
        atom.target
        for atom in all_atoms
        if isinstance(atom, Retract) and atom.target
    }


def _effective_concluding_criticality(
    concluding: Concluding,
    index: dict[str, Atom],
) -> Optional[str]:
    declared = _atom_criticality(concluding)
    if declared and declared in CRITICALITY_RANK:
        return declared

    ranks: list[int] = []
    for target in _refer_targets(concluding):
        atom = index.get(target)
        if not isinstance(atom, (Finding, Observation)):
            continue
        crit = _atom_criticality(atom)
        if crit and crit in CRITICALITY_RANK:
            ranks.append(CRITICALITY_RANK[crit])

    if not ranks:
        return None
    max_rank = max(ranks)
    for name, rank in CRITICALITY_RANK.items():
        if rank == max_rank:
            return name
    return None


def _has_retract_for(target_id: str, all_atoms: list[Atom]) -> bool:
    return any(
        isinstance(atom, Retract) and atom.target == target_id
        for atom in all_atoms
    )


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
    # v0.6 — a REFER/attribute target may be a content-addressable
    # canonical_id (``sha256:<hex>``) rather than a local id. Resolve
    # those against the in-trace canonical_id index so v0.6 traces don't
    # false-positive. (Inline ``REFER:sha256:<hex>`` operator-token
    # extraction still splits on the second colon — that deeper
    # operator-regex change is deferred, matching the reference
    # implementation's own v0.6 Phase-3 boundary; attribute-form
    # canonical_id refs resolve
    # cleanly here.)
    canonical_index = _build_canonical_id_index(trace)

    def _resolves(target: str) -> bool:
        return (
            target in index
            or target in step_ids
            or target in canonical_index
        )

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
        for attr in (
            "to",
            "next",
            "for_ref",
            "for_hyp",
            "for_goal",
            "target",
            "on",
            "of",
        ):
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


# ── v0.6 — content-addressable canonical_id resolver + integrity rule ─


def _build_canonical_id_index(trace: list[Step]) -> dict[str, Atom]:
    """Map every populated ``canonical_id`` to its atom (depth-first).

    First-write-wins on collision (two atoms hashing to the same
    canonical_id are, by construction, structurally identical).
    """
    canonical_index: dict[str, Atom] = {}
    for atom in _walk_atoms(trace):
        if atom.canonical_id:
            canonical_index.setdefault(atom.canonical_id, atom)
    return canonical_index


def resolve_refer(
    trace: list[Step],
    target: str,
    *,
    registry: Optional[Any] = None,
    id_index: Optional[dict[str, Atom]] = None,
    canonical_index: Optional[dict[str, Atom]] = None,
) -> Optional[Any]:
    """v0.6 REFER resolver — 4-path lookup. First non-None wins.

    1. ``id_index[target]`` — local id match in this trace (v0.5 path).
    2. ``canonical_index[target]`` — canonical_id match in this trace.
    3. ``registry.get(target)`` — registry lookup by canonical_id when a
       :class:`scholialang.registry.Registry` instance is supplied.
    4. ``None`` — unresolved.

    Returns the resolved atom-like object (``Atom`` from in-trace lookup,
    ``dict`` from the registry) or ``None``. This is the lookup primitive;
    callers wanting a rule violation message use the reference-complete
    rule. The ``registry`` arg is duck-typed (anything with ``.get`` that
    returns ``None`` on miss) so the validator stays decoupled from the
    registry module.
    """
    if id_index is None:
        id_index = _build_id_index(trace)
    direct = id_index.get(target)
    if direct is not None:
        return direct

    if canonical_index is None:
        canonical_index = _build_canonical_id_index(trace)
    in_trace = canonical_index.get(target)
    if in_trace is not None:
        return in_trace

    if registry is not None:
        atom_dict = registry.get(target)
        if atom_dict is not None:
            return atom_dict

    return None


def check_canonical_id_well_formed(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.6 — every atom carrying a ``canonical_id`` matches the recomputed hash.

    When an atom's ``canonical_id`` is ``None`` the rule is vacuous
    (back-compat with v0.5 atoms that never carried one). When it is set,
    the rule recomputes the hash from the atom's structural content and
    hard-fails on mismatch — the canonical signal of tamper or stale
    storage.
    """
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if atom.canonical_id is None:
            continue
        recomputed = compute_canonical_id(atom)
        if atom.canonical_id != recomputed:
            errors.append(
                ValidationError(
                    rule=RULE_CANONICAL_ID_WELL_FORMED,
                    atom_id=atom.id or "",
                    message=(
                        f"canonical_id mismatch: claimed='{atom.canonical_id}' "
                        f"recomputed='{recomputed}'. The atom's content or attrs "
                        "have been mutated relative to the declared canonical_id; "
                        "re-emit with the recomputed value or treat the stored "
                        "value as tampered."
                    ),
                )
            )
    return errors


# ── v0.6.x-proposed — fingerprint= well-formedness (hard-fail) ───────


def check_fingerprint_well_formed(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.6.x-proposed — every atom carrying a ``fingerprint`` is well-formed.

    See ``docs/scholia/FINGERPRINT.md`` §3. The rule is purely structural
    and additive, mirroring ``canonical_id_well_formed``:

    1. When an atom carries no ``fingerprint`` the rule is **vacuous** —
       the ignore-if-absent guarantee (§4). A fingerprint-less trace
       validates byte-identically to pre-revision behavior.
    2. When present, the value must match ``^[a-z0-9]+:[0-9a-f]+$`` (an
       ``<algo>:<hex>`` pair). A malformed value (``sha256:NOTHEX``, a bare
       hash with no ``algo:`` prefix, an empty value) is a hard-fail.
    3. When present, the atom **must** also carry a ``location`` — a
       fingerprint binds a span; with no span to bind it is a hard-fail.

    The rule does **not** recompute the digest against source (a notation
    validator has no repo access); re-verification is a consumer-side
    operation (§5). ``fingerprint`` lives only on ``<Observation>`` today,
    but the walk is generic (``getattr`` default ``None``) so it follows
    ``location`` wherever a future revision makes it legal.
    """
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        fingerprint = getattr(atom, "fingerprint", None)
        if fingerprint is None:
            continue
        if not is_valid_fingerprint(fingerprint):
            errors.append(
                ValidationError(
                    rule=RULE_FINGERPRINT_WELL_FORMED,
                    atom_id=atom.id or "",
                    message=(
                        f"fingerprint {fingerprint!r} is not well-formed; "
                        "expected '<algo>:<hex>' (a lowercase algorithm label, "
                        "a colon, then lowercase hex) — see "
                        "docs/scholia/FINGERPRINT.md §3."
                    ),
                )
            )
            continue
        if not getattr(atom, "location", None):
            errors.append(
                ValidationError(
                    rule=RULE_FINGERPRINT_WELL_FORMED,
                    atom_id=atom.id or "",
                    message=(
                        f"fingerprint {fingerprint!r} present without a "
                        "location; a fingerprint binds a source span, so the "
                        "atom must also carry location — see "
                        "docs/scholia/FINGERPRINT.md §3."
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
    """Rule 3 — every ``<Deciding>`` produces a ``<Finding>``."""
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if not isinstance(atom, Deciding):
            continue
        if not any(
            isinstance(descendant, Finding)
            for child in atom.children
            for descendant in _descend(child)
        ) and "decision =" not in atom.content:
            errors.append(
                ValidationError(
                    rule=RULE_DECISION_CLOSED,
                    atom_id=atom.id or "",
                    message=(
                        "Deciding block has no child Finding — branch "
                        "choice not recorded."
                    ),
                )
            )
    return errors


# ── Rule 4 — action recorded ─────────────────────────────────────────


def check_action_recorded(
    trace: list[Step], _index: dict[str, Atom], graph: Any = None
) -> list[ValidationError]:
    """Rule 4 — every ``<Action>`` produces a recorded conclusion.

    The §8 composition rule accepts a nested Finding/Concluding, an immediate
    same-Step sibling Finding/Concluding, or a later same-trace result that
    explicitly links back. A later Finding may REFER the Action directly or
    REFER an Observation/Evidence that itself REFERs the Action. A later
    Concluding must REFER the Action directly and close a Goal. A
    ``records_result`` graph edge targeting the Action is also sufficient when
    callers provide a graph with a compatible ``has_edge`` method.
    Chronological order alone is deliberately insufficient for non-immediate
    siblings.
    """
    errors: list[ValidationError] = []
    ordered_atoms = [
        (step_index, atom_index, atom)
        for step_index, step in enumerate(trace)
        for atom_index, atom in enumerate(step.atoms)
    ]
    for i, (step_index, atom_index, atom) in enumerate(ordered_atoms):
        if not isinstance(atom, Action):
            continue
        action_id = atom.id or ""
        later_atoms = [entry[2] for entry in ordered_atoms[i + 1 :]]
        has_nested = any(
            isinstance(descendant, CONCLUSION_TYPES)
            for child in atom.children
            for descendant in _descend(child)
        )
        step_atoms = trace[step_index].atoms
        has_immediate_sibling = (
            atom_index + 1 < len(step_atoms)
            and isinstance(step_atoms[atom_index + 1], CONCLUSION_TYPES)
        )
        direct_result_sources = {
            sibling.id
            for sibling in later_atoms
            if isinstance(sibling, (Observation, Evidence))
            and sibling.id
            and action_id
            and action_id in _refer_targets(sibling)
        }
        has_linked_result = False
        if action_id:
            if _graph_has_edge(
                graph,
                edge_type="records_result",
                target_id=action_id,
            ):
                has_linked_result = True
            for sibling in later_atoms:
                if isinstance(sibling, Finding):
                    refs = _refer_targets(sibling)
                    if action_id in refs or refs.intersection(direct_result_sources):
                        has_linked_result = True
                        break
                if (
                    isinstance(sibling, Concluding)
                    and sibling.for_goal
                    and action_id in _refer_targets(sibling)
                ):
                    has_linked_result = True
                    break
        if not (has_nested or has_immediate_sibling or has_linked_result):
            errors.append(
                ValidationError(
                    rule=RULE_ACTION_RECORDED,
                    atom_id=action_id,
                    message=(
                        f"Action '{action_id or '?'}' has no recording "
                        "Finding/Concluding (neither nested, immediate "
                        "sibling, nor later linked result)."
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
    """Rule 6 — every Retract names an existing close/downgrade target."""
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
        elif not isinstance(referenced, (Finding, Concluding, Goal)):
            errors.append(
                ValidationError(
                    rule=RULE_RETRACT_CONSISTENT,
                    atom_id=atom.id or "",
                    message=(
                        f"Retract target '{target}' resolves to a "
                        f"<{referenced.kind}>; legal v0.5 targets are "
                        "Finding, Concluding, or Goal."
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
                # Match a complete action token, never a substring inside an
                # unrelated word (``delete`` must not match ``undeleted``).
                token_pattern = rf"(?<![a-z0-9_-]){re.escape(verb)}(?![a-z0-9_-])"
                if verb and re.search(token_pattern, action_content):
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


_FORBIDDEN_RE = r"\b(?:never|must\s+not|do\s+not)\s+(?P<verb>[a-z][a-z_-]*)\b"
_NON_VERB_OPENERS = {"a", "an", "any", "the"}


def _extract_forbidden_verbs(constraint_text: str) -> list[str]:
    """Pull the verb phrase following ``Never`` / ``must not`` / ``do not``.

    Each verb phrase is normalised to lowercase + whitespace-stripped
    so the keyword test against action content is case-insensitive.
    Returns an empty list when the constraint doesn't match any of
    the three forbidden-pattern templates.
    """
    import re as _re

    verbs: list[str] = []
    for match in _re.finditer(_FORBIDDEN_RE, constraint_text, flags=_re.IGNORECASE):
        token = match.group("verb").strip().lower()
        # A noun phrase such as ``Never a bare null`` is a data-shape rule,
        # not an imperative verb phrase. Semantic enforcement belongs in a
        # dedicated rule; treating the article ``a`` as a verb makes almost
        # every Action a false positive.
        if token not in _NON_VERB_OPENERS:
            verbs.append(token)
    return verbs


# ── Rule 8 — goal declaration ────────────────────────────────────────


_GOAL_STATUSES = {"met", "unmet", "partially_met", "met_late"}

# v0.6.1 — the closed enum for the OPTIONAL ``status`` attribute on a
# <Concluding>. Narrower than ``_GOAL_STATUSES`` (no ``met_late``): the
# ratified v0.6.1 spec scopes a Concluding's terminal disposition to
# met/unmet/partially_met. Absence is valid (back-compat); presence of
# any value outside this set is a hard validation error.
_CONCLUDING_STATUSES = {"met", "unmet", "partially_met"}


def check_goal_declared(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """Rule 8 — every required Goal has a status-declaring Finding."""
    if any(atom.kind == "Meta:research-mode" for atom in _walk_atoms(trace)):
        return []

    errors: list[ValidationError] = []
    findings_by_goal: dict[str, list[Finding]] = {}
    concludings_by_goal: dict[str, list[Concluding]] = {}
    for atom in _walk_atoms(trace):
        if isinstance(atom, Finding):
            target = atom.for_goal or atom.for_hyp
            if target:
                findings_by_goal.setdefault(target, []).append(atom)
        elif isinstance(atom, Concluding) and atom.for_goal:
            concludings_by_goal.setdefault(atom.for_goal, []).append(atom)

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
        # v0.6.1 — read Concluding.status when present. A Concluding closes
        # a required Goal when it carries no status (v0.5/v0.6.0 back-compat)
        # or a status in the ratified met/unmet/partially_met enum. An
        # out-of-enum status is rejected separately by the optional-field
        # rule, so it must not silently close the Goal here.
        status_concludings = [
            concluding
            for concluding in concludings_by_goal.get(goal_id, [])
            if concluding.status is None
            or concluding.status in _CONCLUDING_STATUSES
        ]
        if status_findings or status_concludings:
            continue
        errors.append(
            ValidationError(
                rule=RULE_GOAL_DECLARED,
                atom_id=goal_id,
                message=(
                    f"Required Goal '{goal_id}' has no Finding with "
                    "for_goal/for_hyp status in met/unmet/partially_met "
                    "and no Concluding for_goal close."
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
        elif isinstance(atom, (Goal, Concluding)):
            criticality = getattr(atom, "criticality", None)
            if (
                criticality is not None
                and criticality not in V031_META_CRITICALITIES
            ):
                errors.append(
                    ValidationError(
                        rule=RULE_V031_OPTIONAL_FIELDS,
                        atom_id=atom.id or "",
                        message=(
                            f"<{atom.kind}> criticality must be one of "
                            f"{sorted(V031_META_CRITICALITIES)}; got "
                            f"{criticality!r}."
                        ),
                    )
                )
            if isinstance(atom, Concluding) and atom.confidence is not None:
                value = _parse_float(atom.confidence)
                if value is None or not 0.0 <= value <= 1.0:
                    errors.append(
                        ValidationError(
                            rule=RULE_V031_OPTIONAL_FIELDS,
                            atom_id=atom.id or "",
                            message=(
                                "<Concluding> confidence must be a float "
                                f"in [0.0, 1.0]; got {atom.confidence!r}."
                            ),
                        )
                    )
            # v0.6.1 — OPTIONAL status enum on <Concluding>. Absent is the
            # v0.5/v0.6.0 shape and validates trivially; present must be one
            # of met/unmet/partially_met per the ratified v0.6.1 spec.
            if isinstance(atom, Concluding) and atom.status is not None:
                if atom.status not in _CONCLUDING_STATUSES:
                    errors.append(
                        ValidationError(
                            rule=RULE_V031_OPTIONAL_FIELDS,
                            atom_id=atom.id or "",
                            message=(
                                "<Concluding> status must be one of "
                                f"{sorted(_CONCLUDING_STATUSES)}; got "
                                f"{atom.status!r}."
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


# ── v0.5 Concluding rules ────────────────────────────────────────────


def _concludings(trace: list[Step]) -> tuple[list[Atom], list[Concluding]]:
    all_atoms = list(_walk_atoms(trace))
    return all_atoms, [a for a in all_atoms if isinstance(a, Concluding)]


def check_for_goal_resolves(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.5 hard-fail — every Concluding closes an in-trace Goal."""
    _all_atoms, concludings = _concludings(trace)
    errors: list[ValidationError] = []
    for atom in concludings:
        target = atom.for_goal
        if not target:
            errors.append(
                ValidationError(
                    rule=RULE_FOR_GOAL_RESOLVES,
                    atom_id=atom.id or "",
                    message="Concluding has no for_goal target.",
                )
            )
            continue
        referenced = index.get(target)
        if referenced is None:
            errors.append(
                ValidationError(
                    rule=RULE_FOR_GOAL_RESOLVES,
                    atom_id=atom.id or "",
                    message=(
                        f"Concluding.for_goal='{target}' does not resolve "
                        "to any declared id in this trace."
                    ),
                )
            )
        elif not isinstance(referenced, Goal):
            errors.append(
                ValidationError(
                    rule=RULE_FOR_GOAL_RESOLVES,
                    atom_id=atom.id or "",
                    message=(
                        f"Concluding.for_goal='{target}' resolves to a "
                        f"<{referenced.kind}>, not a <Goal>."
                    ),
                )
            )
    return errors


def check_refer_at_least_one(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.5 hard-fail — Concluding must cite supporting atoms."""
    _all_atoms, concludings = _concludings(trace)
    errors: list[ValidationError] = []
    for atom in concludings:
        valid_targets = [
            target
            for target in _refer_targets(atom)
            if isinstance(index.get(target), (Finding, Observation, Evidence))
        ]
        if not valid_targets:
            errors.append(
                ValidationError(
                    rule=RULE_REFER_AT_LEAST_ONE,
                    atom_id=atom.id or "",
                    message=(
                        "Concluding body has no REFER: pointing to a "
                        "Finding, Observation, or Evidence atom."
                    ),
                )
            )
    return errors


def check_criticality_non_decreasing(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.5 hard-fail — Concluding cannot silently downgrade Goal risk."""
    all_atoms, concludings = _concludings(trace)
    errors: list[ValidationError] = []
    for atom in concludings:
        if not atom.for_goal:
            continue
        goal = index.get(atom.for_goal)
        if not isinstance(goal, Goal):
            continue
        goal_crit = _atom_criticality(goal)
        if not goal_crit or goal_crit not in CRITICALITY_RANK:
            continue
        concl_crit = _effective_concluding_criticality(atom, index)
        if not concl_crit or concl_crit not in CRITICALITY_RANK:
            continue
        goal_rank = CRITICALITY_RANK[goal_crit]
        concl_rank = CRITICALITY_RANK[concl_crit]
        if concl_rank >= goal_rank:
            continue
        if _has_retract_for(atom.for_goal, all_atoms):
            continue
        errors.append(
            ValidationError(
                rule=RULE_CRITICALITY_NON_DECREASING,
                atom_id=atom.id or "",
                message=(
                    f"Concluding criticality '{concl_crit}' is lower than "
                    f"Goal '{atom.for_goal}' criticality '{goal_crit}'. "
                    f"Authorize the downgrade with <Retract target='{atom.for_goal}'/>."
                ),
            )
        )
    return errors


def check_no_action_in_concluding(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationWarning]:
    """v0.5 warning — Concluding states belief, not action commitment."""
    _all_atoms, concludings = _concludings(trace)
    warnings: list[ValidationWarning] = []
    for atom in concludings:
        match = _ACTION_MODAL_RE.search(atom.content or "")
        if match:
            warnings.append(
                ValidationWarning(
                    rule=RULE_NO_ACTION_IN_CONCLUDING,
                    atom_id=atom.id or "",
                    message=(
                        f"Concluding body contains action-modal phrase "
                        f"{match.group(0)!r}; route action commitment "
                        "through <Deciding>."
                    ),
                )
            )
    return warnings


def check_single_active_concluding_per_goal(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationWarning]:
    """v0.5 warning — a Goal should have one active Concluding."""
    all_atoms, concludings = _concludings(trace)
    retracted = _retracted_ids(all_atoms)
    active_by_goal: dict[str, list[Concluding]] = {}
    for atom in concludings:
        if not atom.for_goal:
            continue
        if atom.id and atom.id in retracted:
            continue
        active_by_goal.setdefault(atom.for_goal, []).append(atom)

    warnings: list[ValidationWarning] = []
    for goal_id, group in active_by_goal.items():
        if len(group) <= 1:
            continue
        for atom in group:
            warnings.append(
                ValidationWarning(
                    rule=RULE_SINGLE_ACTIVE_CONCLUDING_PER_GOAL,
                    atom_id=atom.id or "",
                    message=(
                        f"Goal '{goal_id}' has {len(group)} active "
                        "Concludings; retract superseded closes."
                    ),
                )
            )
    return warnings


def check_min_confidence_ceiling(
    trace: list[Step], index: dict[str, Atom]
) -> list[ValidationWarning]:
    """v0.5 warning — Concluding confidence should not exceed support."""
    all_atoms, concludings = _concludings(trace)
    warnings: list[ValidationWarning] = []
    for atom in concludings:
        if atom.confidence is None:
            continue
        cited_confidences: list[float] = []
        for target in _refer_targets(atom):
            cited = index.get(target)
            if not isinstance(cited, (Finding, Evidence)):
                continue
            confidence = _atom_confidence(cited, all_atoms)
            if confidence is not None:
                cited_confidences.append(confidence)
        if not cited_confidences:
            continue
        min_conf = min(cited_confidences)
        if atom.confidence > min_conf:
            warnings.append(
                ValidationWarning(
                    rule=RULE_MIN_CONFIDENCE_CEILING,
                    atom_id=atom.id or "",
                    message=(
                        f"Concluding.confidence={atom.confidence} exceeds "
                        f"the minimum confidence of cited Findings/Evidence "
                        f"({min_conf})."
                    ),
                )
            )
    return warnings


# ── v0.7.3-candidate — Map/Event/Task semantic rules ─────────────────
#
# All rules are vacuous when the trace carries no semantic atom, so
# legacy traces validate byte-identically to 0.7.2 behavior. Reference
# resolution is against the COMPLETE trace (forward references allowed)
# by declared local atom id or by canonical id; targets are atoms,
# never Steps. Nothing here executes work: ``runtime_ref`` /
# ``external_ref`` stay opaque strings.


def _semantic_atoms(trace: list[Step]) -> list[Atom]:
    return [a for a in _walk_atoms(trace) if a.kind in SEMANTIC_KINDS]


def _declared_atom_index(trace: list[Step]) -> dict[str, Atom]:
    """Declared-id → atom index for semantic reference resolution.

    Unlike :func:`_build_id_index` this maps ONLY explicitly declared
    atom ids (no Storing names, no content-derived assignment names) —
    a semantic reference must name a declared atom. First declaration
    wins; duplicates are reported by ``semantic_id_unique``.
    """
    index: dict[str, Atom] = {}
    for atom in _walk_atoms(trace):
        if atom.id and atom.id not in index:
            index[atom.id] = atom
    return index


def _resolve_semantic_target(
    value: str,
    atom_index: dict[str, Atom],
    canonical_index: dict[str, Atom],
) -> Optional[Atom]:
    """Resolve a semantic reference by declared local id or canonical id."""
    target = atom_index.get(value)
    if target is not None:
        return target
    return canonical_index.get(value)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def check_semantic_structure(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — decode-parity structural rules for constructed atoms.

    Python-constructed Map/Event/Task instances must fail ``validate()``
    under the same structural constraints the decoders enforce at parse:
    ``operators`` a list of strings, ``children`` empty, ``entries`` a
    real string-keyed mapping.
    """
    errors: list[ValidationError] = []
    for atom in _semantic_atoms(trace):
        for rule, message in semantic_structural_violations(atom):
            errors.append(
                ValidationError(rule=rule, atom_id=atom.id or "", message=message)
            )
    return errors


def check_semantic_id_shape(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — every semantic atom carries a kind-prefixed trace-local id."""
    errors: list[ValidationError] = []
    for atom in _semantic_atoms(trace):
        pattern = SEMANTIC_ID_PATTERNS[atom.kind]
        if not atom.id:
            errors.append(
                ValidationError(
                    rule=RULE_SEMANTIC_ID_SHAPE,
                    atom_id="",
                    message=(
                        f"<{atom.kind}> requires a trace-local id with "
                        f"prefix '{atom.kind}_' followed by ASCII letters, "
                        "digits, or underscores; the id is missing."
                    ),
                )
            )
            continue
        if not pattern.match(atom.id):
            errors.append(
                ValidationError(
                    rule=RULE_SEMANTIC_ID_SHAPE,
                    atom_id=atom.id,
                    message=(
                        f"<{atom.kind}> id {atom.id!r} must match "
                        f"'{atom.kind}_' followed by one or more ASCII "
                        "letters, digits, or underscores "
                        f"([A-Za-z0-9_]+)."
                    ),
                )
            )
    return errors


def check_semantic_id_unique(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — no id collision involving a semantic atom, trace-wide.

    A collision between a semantic atom and ANY other id holder — a
    second semantic atom, a legacy atom, or a Step — is an error.
    Legacy-only duplicate handling is deliberately unchanged.
    """
    holders: dict[str, list[Any]] = {}
    for step in trace:
        if step.id:
            holders.setdefault(step.id, []).append(step)
    for atom in _walk_atoms(trace):
        if atom.id:
            holders.setdefault(atom.id, []).append(atom)

    errors: list[ValidationError] = []
    for shared_id, owners in holders.items():
        if len(owners) < 2:
            continue
        semantic_owners = [
            o for o in owners if getattr(o, "kind", "Step") in SEMANTIC_KINDS
        ]
        if not semantic_owners:
            continue
        kinds = ", ".join(
            "Step" if isinstance(o, Step) else o.kind for o in owners
        )
        errors.append(
            ValidationError(
                rule=RULE_SEMANTIC_ID_UNIQUE,
                atom_id=shared_id,
                message=(
                    f"id {shared_id!r} is declared {len(owners)} times "
                    f"({kinds}); an id collision involving a v0.7 semantic "
                    "atom is an error anywhere in the trace, including "
                    "against Step and legacy atom ids."
                ),
            )
        )
    return errors


def _map_value_violation(value_type: str, value: Any) -> Optional[str]:
    """Return the reason ``value`` violates ``value_type``, or ``None``."""
    if value_type == "string":
        if not isinstance(value, str):
            return f"expected a string, got {value!r} ({type(value).__name__})"
        return None
    if value_type == "integer":
        if isinstance(value, bool):
            return f"expected a true integer, got the boolean {value!r} (bool is not integer)"
        if not isinstance(value, int):
            return f"expected a true integer, got {value!r} ({type(value).__name__})"
        if not JSON_SAFE_INT_MIN <= value <= JSON_SAFE_INT_MAX:
            return (
                f"integer {value!r} is outside the interoperable JSON "
                f"exact-integer range [{JSON_SAFE_INT_MIN}, {JSON_SAFE_INT_MAX}]"
            )
        return None
    if value_type == "boolean":
        if not isinstance(value, bool):
            return f"expected a bool exactly, got {value!r} ({type(value).__name__})"
        return None
    if value_type == "atom_ref":
        if not isinstance(value, str) or not value.strip():
            return (
                f"expected a nonempty atom-reference string, got {value!r} "
                f"({type(value).__name__})"
            )
        return None
    return None


def check_map_rules(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — Map required fields, value_type enum, and typed entries."""
    errors: list[ValidationError] = []
    for atom in _semantic_atoms(trace):
        if not isinstance(atom, Map):
            continue
        atom_id = atom.id or ""
        value_type = atom.value_type
        entries = atom.entries
        if value_type is None:
            errors.append(
                ValidationError(
                    rule=RULE_MAP_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message="<Map> requires value_type; it is missing.",
                )
            )
        elif not isinstance(value_type, str) or value_type not in MAP_VALUE_TYPES:
            errors.append(
                ValidationError(
                    rule=RULE_MAP_VALUE_TYPE,
                    atom_id=atom_id,
                    message=(
                        f"<Map> value_type must be exactly one of "
                        f"{sorted(MAP_VALUE_TYPES)}; got {value_type!r}."
                    ),
                )
            )
        if entries is None:
            errors.append(
                ValidationError(
                    rule=RULE_MAP_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message=(
                        "<Map> requires entries (a possibly-empty mapping); "
                        "it is missing."
                    ),
                )
            )
        if not isinstance(atom.content, str):
            errors.append(
                ValidationError(
                    rule=RULE_MAP_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message=(
                        f"<Map> body must be a string when present; got "
                        f"{atom.content!r}."
                    ),
                )
            )
        if not isinstance(entries, dict) or not isinstance(value_type, str):
            continue
        if value_type not in MAP_VALUE_TYPES:
            continue
        for key, value in entries.items():
            if not isinstance(key, str):
                # Reported structurally by map_entries_shape; skip typing.
                continue
            if not key.strip():
                errors.append(
                    ValidationError(
                        rule=RULE_MAP_ENTRIES_TYPED,
                        atom_id=atom_id,
                        message=(
                            f"<Map> entries key {key!r} is empty or "
                            "whitespace-only; keys must be nonempty strings."
                        ),
                    )
                )
            violation = _map_value_violation(value_type, value)
            if violation:
                errors.append(
                    ValidationError(
                        rule=RULE_MAP_ENTRIES_TYPED,
                        atom_id=atom_id,
                        message=(
                            f"<Map> entries[{key!r}] violates the declared "
                            f"homogeneous value_type {value_type!r}: {violation}. "
                            "Floats, NaN, infinities, nulls, arrays, nested "
                            "objects, and mixed types are rejected."
                        ),
                    )
                )
    return errors


def check_map_ref_resolves(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — Map atom_ref entries resolve to a declared non-Step atom.

    A Map must not refer to itself, and references are never executed,
    dereferenced externally, or treated as authority.
    """
    errors: list[ValidationError] = []
    atom_index = _declared_atom_index(trace)
    canonical_index = _build_canonical_id_index(trace)
    step_ids = _step_ids(trace)
    for atom in _semantic_atoms(trace):
        if not isinstance(atom, Map):
            continue
        if atom.value_type != "atom_ref" or not isinstance(atom.entries, dict):
            continue
        atom_id = atom.id or ""
        for key, value in atom.entries.items():
            if not _is_nonempty_str(value):
                continue  # typed rule reports the shape problem
            target = _resolve_semantic_target(value, atom_index, canonical_index)
            if target is None:
                if value in step_ids:
                    errors.append(
                        ValidationError(
                            rule=RULE_MAP_REF_RESOLVES,
                            atom_id=atom_id,
                            message=(
                                f"<Map> entries[{key!r}]={value!r} names a "
                                "Step; atom_ref values must resolve to a "
                                "declared atom of any kind other than Step."
                            ),
                        )
                    )
                else:
                    errors.append(
                        ValidationError(
                            rule=RULE_MAP_REF_RESOLVES,
                            atom_id=atom_id,
                            message=(
                                f"<Map> entries[{key!r}]={value!r} is "
                                "dangling — it does not resolve to any "
                                "declared atom id or canonical id in this "
                                "trace."
                            ),
                        )
                    )
                continue
            if target is atom or (
                atom.id and value == atom.id
            ) or (
                atom.canonical_id and value == atom.canonical_id
            ):
                errors.append(
                    ValidationError(
                        rule=RULE_MAP_REF_RESOLVES,
                        atom_id=atom_id,
                        message=(
                            f"<Map> entries[{key!r}]={value!r} refers to "
                            "this Map itself; a Map must not refer to itself."
                        ),
                    )
                )
    return errors


_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _rfc3339_violation(value: Any) -> Optional[str]:
    """Return why ``value`` is not an RFC3339 timestamp with timezone."""
    if not isinstance(value, str) or not value.strip():
        return f"expected an RFC3339 timestamp string, got {value!r}"
    if not _RFC3339_RE.match(value):
        return (
            f"{value!r} is not RFC3339-shaped with an explicit timezone "
            "offset (naive timestamps are rejected)"
        )
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return f"{value!r} names an impossible date/time"
    if parsed.tzinfo is None:
        return f"{value!r} carries no explicit timezone"
    return None


def check_event_rules(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — Event required fields, type token, timestamp, occurrence key."""
    errors: list[ValidationError] = []
    events: list[Event] = [
        a for a in _semantic_atoms(trace) if isinstance(a, Event)
    ]
    for atom in events:
        atom_id = atom.id or ""
        for field_name in ("source", "occurrence_id", "event_type"):
            value = getattr(atom, field_name)
            if not _is_nonempty_str(value):
                errors.append(
                    ValidationError(
                        rule=RULE_EVENT_REQUIRED_FIELDS,
                        atom_id=atom_id,
                        message=(
                            f"<Event> requires {field_name} as a nonempty "
                            f"string; got {value!r}."
                        ),
                    )
                )
        if not _is_nonempty_str(atom.content):
            errors.append(
                ValidationError(
                    rule=RULE_EVENT_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message=(
                        "<Event> requires nonempty content — the body "
                        "describes what was recorded."
                    ),
                )
            )
        event_type = atom.event_type
        if (
            isinstance(event_type, str)
            and event_type.strip()
            and not EVENT_TYPE_RE.match(event_type)
        ):
            errors.append(
                ValidationError(
                    rule=RULE_EVENT_TYPE_TOKEN,
                    atom_id=atom_id,
                    message=(
                        f"<Event> event_type {atom.event_type!r} must match "
                        "[A-Za-z][A-Za-z0-9_.-]* (a producer-defined "
                        "classification token, not a new atom kind)."
                    ),
                )
            )
        if atom.timestamp is not None:
            violation = _rfc3339_violation(atom.timestamp)
            if violation:
                errors.append(
                    ValidationError(
                        rule=RULE_EVENT_TIMESTAMP_SHAPE,
                        atom_id=atom_id,
                        message=(
                            f"<Event> timestamp: {violation}. RFC3339 with "
                            "an explicit timezone is required; the "
                            "timestamp is excluded from canonical identity."
                        ),
                    )
                )
        for field_name in SEMANTIC_OPAQUE_FIELDS["Event"]:
            value = getattr(atom, field_name)
            if value is not None and not _is_nonempty_str(value):
                errors.append(
                    ValidationError(
                        rule=RULE_EVENT_REQUIRED_FIELDS,
                        atom_id=atom_id,
                        message=(
                            f"<Event> {field_name}, when present, must be a "
                            f"nonempty opaque string; got {value!r}."
                        ),
                    )
                )

    seen_pairs: dict[tuple[str, str], Event] = {}
    for atom in events:
        source, occurrence_id = atom.source, atom.occurrence_id
        if not (
            isinstance(source, str)
            and source.strip()
            and isinstance(occurrence_id, str)
            and occurrence_id.strip()
        ):
            continue
        pair = (source, occurrence_id)
        first = seen_pairs.get(pair)
        if first is None:
            seen_pairs[pair] = atom
            continue
        errors.append(
            ValidationError(
                rule=RULE_EVENT_OCCURRENCE_UNIQUE,
                atom_id=atom.id or "",
                message=(
                    f"<Event> occurrence pair (source={atom.source!r}, "
                    f"occurrence_id={atom.occurrence_id!r}) duplicates "
                    f"{first.id or '?'} in this trace; the pair identifies "
                    "one occurrence even when bodies match — transport "
                    "deduplication belongs upstream of semantic insertion."
                ),
            )
        )
    return errors


def check_task_rules(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — Task required fields, status enum, evidence requirement.

    ``status`` values are descriptive claims only; runtime lifecycle
    states are rejected by the closed enum. Validating a satisfied Task
    checks the shape/resolution of its evidence reference — never the
    truth of the evidence, and never execution of anything.
    """
    errors: list[ValidationError] = []
    for atom in _semantic_atoms(trace):
        if not isinstance(atom, Task):
            continue
        atom_id = atom.id or ""
        status = atom.status
        if status is None:
            errors.append(
                ValidationError(
                    rule=RULE_TASK_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message="<Task> requires status; it is missing.",
                )
            )
        elif not isinstance(status, str) or status not in TASK_STATUSES:
            errors.append(
                ValidationError(
                    rule=RULE_TASK_STATUS_ENUM,
                    atom_id=atom_id,
                    message=(
                        f"<Task> status must be exactly one of "
                        f"{sorted(TASK_STATUSES)}; got {status!r}. Runtime "
                        "lifecycle states (queued, working, input_required, "
                        "cancelled, expired, paused) are never legal in the "
                        "semantic status enum."
                    ),
                )
            )
        if not _is_nonempty_str(atom.content):
            errors.append(
                ValidationError(
                    rule=RULE_TASK_REQUIRED_FIELDS,
                    atom_id=atom_id,
                    message=(
                        "<Task> requires nonempty content — the body "
                        "describes the work obligation."
                    ),
                )
            )
        if status in {"satisfied", "unsatisfied"} and not _is_nonempty_str(
            atom.evidence_ref
        ):
            errors.append(
                ValidationError(
                    rule=RULE_TASK_EVIDENCE_REQUIRED,
                    atom_id=atom_id,
                    message=(
                        f"<Task> status={status!r} requires evidence_ref so "
                        "the verdict has an explicit reference; it is "
                        "missing. (No evidence reference is required for "
                        "open or withdrawn.)"
                    ),
                )
            )
        for field_name in SEMANTIC_OPAQUE_FIELDS["Task"]:
            value = getattr(atom, field_name)
            if value is not None and not _is_nonempty_str(value):
                errors.append(
                    ValidationError(
                        rule=RULE_TASK_REQUIRED_FIELDS,
                        atom_id=atom_id,
                        message=(
                            f"<Task> {field_name}, when present, must be a "
                            f"nonempty opaque string; got {value!r}."
                        ),
                    )
                )
    return errors


def check_semantic_ref_target_kind(
    trace: list[Step], _index: dict[str, Atom]
) -> list[ValidationError]:
    """v0.7 — typed reference fields resolve to their permitted kinds.

    Dangling and wrong-kind targets are rejected. Opaque correlators
    (``external_ref`` / ``runtime_ref``) are NOT reference fields and
    are never resolved here.
    """
    errors: list[ValidationError] = []
    atom_index = _declared_atom_index(trace)
    canonical_index = _build_canonical_id_index(trace)
    for atom in _semantic_atoms(trace):
        ref_fields = SEMANTIC_REF_FIELDS.get(atom.kind)
        if not ref_fields:
            continue
        atom_id = atom.id or ""
        for field_name, allowed_kinds in ref_fields.items():
            value = getattr(atom, field_name)
            if value is None:
                continue
            if not _is_nonempty_str(value):
                errors.append(
                    ValidationError(
                        rule=RULE_SEMANTIC_REF_TARGET_KIND,
                        atom_id=atom_id,
                        message=(
                            f"<{atom.kind}> {field_name} must be a nonempty "
                            f"reference string; got {value!r}."
                        ),
                    )
                )
                continue
            target = _resolve_semantic_target(value, atom_index, canonical_index)
            if target is None:
                errors.append(
                    ValidationError(
                        rule=RULE_SEMANTIC_REF_TARGET_KIND,
                        atom_id=atom_id,
                        message=(
                            f"<{atom.kind}> {field_name}={value!r} is "
                            "dangling — it does not resolve to any declared "
                            "atom id or canonical id in this trace."
                        ),
                    )
                )
                continue
            if target.kind not in allowed_kinds:
                errors.append(
                    ValidationError(
                        rule=RULE_SEMANTIC_REF_TARGET_KIND,
                        atom_id=atom_id,
                        message=(
                            f"<{atom.kind}> {field_name}={value!r} resolves "
                            f"to a <{target.kind}>; the permitted target "
                            f"kind(s) for this field: "
                            f"{', '.join(allowed_kinds)}."
                        ),
                    )
                )
    return errors


def check_grammar_profile(
    trace: list[Step], profile: Optional[str]
) -> list[ValidationError]:
    """v0.7 — explicit grammar-profile validation entry point.

    ``profile=None`` selects the current grammar
    (``SCHOLIA_GRAMMAR_VERSION``) for unversioned local input. A caller
    crossing a negotiated boundary passes the profile explicitly: under
    the legacy ``0.6.2`` profile every v0.7 semantic kind is rejected
    with a structured unsupported-version diagnostic — never silently
    rewritten to a legacy kind — and an unknown profile is itself
    rejected the same way.
    """
    if profile is None:
        return []
    allowed = GRAMMAR_PROFILES.get(profile)
    if allowed is None:
        return [
            ValidationError(
                rule=RULE_GRAMMAR_PROFILE_UNSUPPORTED,
                atom_id="",
                message=(
                    f"unsupported grammar profile {profile!r}: this "
                    f"validator (package {SCHOLIA_VALIDATOR_VERSION}) "
                    f"supports profiles "
                    f"{sorted(GRAMMAR_PROFILES)} — negotiate a supported "
                    "grammar version instead of assuming forward "
                    "compatibility."
                ),
            )
        ]
    errors: list[ValidationError] = []
    for atom in _walk_atoms(trace):
        if atom.kind in PSEUDO_ATOM_KINDS:
            continue
        if atom.kind not in allowed:
            errors.append(
                ValidationError(
                    rule=RULE_GRAMMAR_PROFILE_UNSUPPORTED,
                    atom_id=atom.id or "",
                    message=(
                        f"<{atom.kind}> is not part of grammar profile "
                        f"{profile}; the {profile} catalog has "
                        f"{len(allowed)} kinds. Unsupported kind under the "
                        "selected profile — surface this diagnostic to the "
                        "producer (never down-convert to a legacy kind) or "
                        f"negotiate grammar {SCHOLIA_GRAMMAR_VERSION}."
                    ),
                )
            )
    return errors


# ── Orchestration ────────────────────────────────────────────────────


_RULES: tuple[
    tuple[str, Callable[[list[Step], dict[str, Atom]], list[ValidationError]]],
    ...,
] = (
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
    (RULE_FOR_GOAL_RESOLVES, check_for_goal_resolves),
    (RULE_REFER_AT_LEAST_ONE, check_refer_at_least_one),
    (RULE_CRITICALITY_NON_DECREASING, check_criticality_non_decreasing),
    (RULE_CANONICAL_ID_WELL_FORMED, check_canonical_id_well_formed),
    (RULE_FINGERPRINT_WELL_FORMED, check_fingerprint_well_formed),
    # v0.7.3-candidate — Map/Event/Task semantic rules. The structure
    # check can emit three rule names (children/operators/entries-shape
    # mirrors); it is registered under the children rule and its errors
    # are re-bucketed by their own rule names in validate().
    (RULE_SEMANTIC_CHILDREN_EMPTY, check_semantic_structure),
    (RULE_SEMANTIC_ID_SHAPE, check_semantic_id_shape),
    (RULE_SEMANTIC_ID_UNIQUE, check_semantic_id_unique),
    (RULE_MAP_REQUIRED_FIELDS, check_map_rules),
    (RULE_MAP_REF_RESOLVES, check_map_ref_resolves),
    (RULE_EVENT_REQUIRED_FIELDS, check_event_rules),
    (RULE_TASK_REQUIRED_FIELDS, check_task_rules),
    (RULE_SEMANTIC_REF_TARGET_KIND, check_semantic_ref_target_kind),
)

_WARNING_RULES: tuple[
    tuple[str, Callable[[list[Step], dict[str, Atom]], list[ValidationWarning]]],
    ...,
] = (
    (RULE_NO_ACTION_IN_CONCLUDING, check_no_action_in_concluding),
    (
        RULE_SINGLE_ACTIVE_CONCLUDING_PER_GOAL,
        check_single_active_concluding_per_goal,
    ),
    (RULE_MIN_CONFIDENCE_CEILING, check_min_confidence_ceiling),
)


def validate(
    trace: list[Step],
    *,
    graph: Any = None,
    profile: Optional[str] = None,
) -> ValidationResult:
    """Run all rules against ``trace`` and return a ``ValidationResult``.

    Warning rules are non-fatal: ``ok`` is true when there are no
    errors, even if warnings are present. ``graph`` is optional and
    duck-typed; when it exposes ``has_edge(...)``, Rule 4 can recognize
    a persisted ``records_result`` edge without coupling this package to
    a particular graph implementation.

    ``profile`` (v0.7) is the explicit grammar-profile entry point:
    ``None`` validates under the current grammar
    (``SCHOLIA_GRAMMAR_VERSION``); ``"0.6.2"`` additionally rejects
    every v0.7 semantic kind with a structured
    ``grammar_profile_unsupported`` diagnostic; an unknown profile is
    rejected outright the same way. Selecting a profile never rewrites
    or down-converts atoms.
    """
    index = _build_id_index(trace)
    errors: list[ValidationError] = []
    warnings: list[ValidationWarning] = []
    errors_by_rule: dict[str, list[ValidationError]] = {
        name: [] for name in RULE_NAMES
    }
    warnings_by_rule: dict[str, list[ValidationWarning]] = {
        name: [] for name in RULE_NAMES
    }
    for name, rule in _RULES:
        if name == RULE_ACTION_RECORDED:
            rule_errors = check_action_recorded(trace, index, graph)
        else:
            rule_errors = rule(trace, index)
        errors.extend(rule_errors)
        for error in rule_errors:
            # Semantic rule functions may emit more than one rule name
            # (e.g. the structure mirror); bucket each error by its own
            # declared rule so errors_by_rule stays exact.
            bucket = error.rule if error.rule in errors_by_rule else name
            errors_by_rule[bucket].append(error)
    profile_errors = check_grammar_profile(trace, profile)
    errors.extend(profile_errors)
    errors_by_rule[RULE_GRAMMAR_PROFILE_UNSUPPORTED].extend(profile_errors)
    for name, rule in _WARNING_RULES:
        rule_warnings = rule(trace, index)
        warnings.extend(rule_warnings)
        warnings_by_rule[name] = rule_warnings
    return ValidationResult(
        ok=not errors,
        errors=errors,
        errors_by_rule=errors_by_rule,
        warnings=warnings,
        warnings_by_rule=warnings_by_rule,
        scholia_validator_version=SCHOLIA_VALIDATOR_VERSION,
    )
