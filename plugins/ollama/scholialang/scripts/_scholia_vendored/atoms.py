"""Scholia atom catalog — v0.6 shared contract.

This module is the canonical set of types for the Scholia notation.
It is pure: no I/O, no parsing logic, no validation logic, no network.

v0.6 adds content-addressable ``canonical_id`` — a SHA-256 hash of the
atom's structural identity (``{kind, content, attrs}``, provenance
excluded). The catalog stays the v0.5 closed set (32 kinds); only the
base ``Atom`` grows a ``canonical_id`` field plus the
``compute_canonical_id`` hasher. The hash is defined to be byte-identical
across Scholia implementations so the same structural atom emitted from
different sessions/hosts addresses to the same id.

Spec source: ``docs/notation/NOTATION_REFERENCE.md`` §3 (atoms),
§4 (operators), §5 (primitives), §6 (Step container). Any change here
that diverges from the reference doc is a bug — update the reference
doc first.

Why plain dataclasses rather than Pydantic: the core is meant to embed
in every process that consumes or emits a trace (launcher, Monitor,
Adjudicator). Keeping it stdlib-only removes a transitive dep from
embedders and makes the atoms cheap to instantiate during hot-path
parsing.

Why ``kind`` is a ``ClassVar`` on each subclass: the dataclass fields
themselves are the payload; ``kind`` is a per-type discriminator that
the serializer reads when emitting JSON/YAML. It does not vary per
instance, so keeping it off the instance dict avoids repeating the
literal on every atom.
"""
from __future__ import annotations

import hashlib
import json
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar, Optional, Union


# ── §4 — logical operators ────────────────────────────────────────────

class Operator(str, Enum):
    """The 11 inline operators from NOTATION_REFERENCE.md §4.

    Written UPPERCASE inside element content, not as tags. Examples
    appear in the spec table; the parser extracts them out of content
    text into the containing atom's ``operators`` list.
    """

    AND = "AND"
    OR = "OR"
    XOR = "XOR"
    NOT = "NOT"
    IMPLIES = "IMPLIES"
    REFER = "REFER"
    FORALL = "FORALL"
    EXISTS = "EXISTS"
    BEFORE = "BEFORE"
    AFTER = "AFTER"
    EQUALS = "EQUALS"


OPERATORS: tuple[str, ...] = tuple(op.value for op in Operator)

# Module-private mirror of OPERATORS as a tuple of values, used by the
# canonical-set definition below. Kept separate so the validator-ratified
# set is defined directly from the spec enum — bump the enum, the
# validator follows.
_OPERATOR_VALUES: tuple[str, ...] = OPERATORS


# ── §5 — data primitives (type aliases) ───────────────────────────────

# Primitives are inline tokens: ``LIST(string): [a, b, c]`` etc. We
# expose them as Python type aliases so consumers can type-annotate a
# field that receives primitive-shaped content. Runtime code does not
# narrow on them — the parser leaves primitive literals as strings in
# the containing atom's ``content`` and callers deserialize as needed.

LIST = list
SET = set
MAP = dict
STRING = str
NUMBER = Union[int, float]
BOOL = bool

PRIMITIVES: tuple[str, ...] = ("LIST", "SET", "MAP", "STRING", "NUMBER", "BOOL")


# ── §3 — the atom catalog ─────────────────────────────────────────────

@dataclass
class Atom:
    """Base Scholia atom — per NOTATION_REFERENCE.md §3.

    Every atom carries the same three common fields: an optional
    ``id`` (used for intra-trace ``REFER`` resolution), ``content``
    (free-form text captured between the opening and closing tags),
    and ``children`` (nested atoms — the notation composes).

    Subclasses override ``kind`` via ``ClassVar`` and may add
    kind-specific fields (e.g. ``Evidence.polarity``). The serializer
    uses ``kind`` as the discriminator when emitting JSON/YAML.

    v0.6 adds ``canonical_id`` — a content-addressable SHA-256 hash of
    the atom's structural identity. The parser populates it lazily;
    ``Atom`` construction does not (so hand-built atoms stay cheap and
    the hash is computed once, at parse time, by ``compute_canonical_id``).
    """

    id: Optional[str] = None
    content: str = ""
    children: list["Atom"] = field(default_factory=list)
    operators: list[str] = field(default_factory=list)
    canonical_id: Optional[str] = None
    kind: ClassVar[str] = "Atom"


# ── 3a — reasoning atoms ─────────────────────────────────────────────

@dataclass
class Thinking(Atom):
    """§3a — internal deliberation; chain-of-thought, not observation."""

    kind: ClassVar[str] = "Thinking"


@dataclass
class Observation(Atom):
    """§3a — external input: bash / read / query result captured.

    v0.3.1 reserves two optional attributes that v0.4 emitters will
    populate but v0.3 emitters must not: ``location`` (``file:start:end``
    line span) and ``confidence`` (float string in ``[0.0, 1.0]``). The
    validator accepts absence as the v0.3 shape and validates closed-
    set values on presence. See ``docs/scholia/SCHOLIA_v0.3.1_SPEC.md``.

    v0.4-B (PRD rsi-scholia-v0.4-code-graph-metadata) populates
    ``location`` with a ``<path>:<start>:<end>`` line span pointing at
    the symbol the Observation describes. The shape is AST-derived
    ground truth from the rewriter's Phase 2 enrichment; callers MUST
    NOT guess line numbers. The validator enforces the format regex
    via ``check_location_edge_shape``.
    """

    timestamp: Optional[str] = None
    location: Optional[str] = None
    confidence: Optional[str] = None
    kind: ClassVar[str] = "Observation"


@dataclass
class Action(Atom):
    """§3a — external state change; must produce a ``<Finding>``."""

    timestamp: Optional[str] = None
    kind: ClassVar[str] = "Action"


# ── 3b — evidence atoms ──────────────────────────────────────────────

@dataclass
class Hypothesis(Atom):
    """§3b — an explicit conjecture the agent will test."""

    kind: ClassVar[str] = "Hypothesis"


@dataclass
class Evidence(Atom):
    """§3b — observation bearing on one or more hypotheses.

    ``for_ref`` is spelled with a trailing underscore to avoid Python's
    reserved ``for``; serializers emit/read the wire attribute as
    ``for``. ``polarity`` is one of ``"supports"``, ``"refutes"``, or
    ``"neutral"``.
    """

    for_ref: Optional[str] = None
    polarity: Optional[str] = None
    kind: ClassVar[str] = "Evidence"


_FOR_GOAL_DEPRECATION_MSG = (
    "Finding.for_goal is deprecated in Scholia v0.5; use for_hyp instead. "
    "A Finding evaluates a Hypothesis, not a Goal. for_goal is preserved "
    "for v0.4 compatibility and will be removed in v0.6."
)


@dataclass
class Finding(Atom):
    """§3b — a conclusion drawn from available evidence.

    v0.5 makes ``for_hyp`` the canonical reference attribute because a
    Finding evaluates a Hypothesis. ``for_goal`` is retained as a
    deprecated v0.4 compatibility alias.
    """

    for_hyp: Optional[str] = None
    for_goal: Optional[str] = None
    status: Optional[str] = None
    kind: ClassVar[str] = "Finding"

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "for_goal" and value is not None:
            if not object.__getattribute__(self, "__dict__").get(
                "_warned_for_goal", False
            ):
                warnings.warn(
                    _FOR_GOAL_DEPRECATION_MSG,
                    DeprecationWarning,
                    stacklevel=2,
                )
                object.__setattr__(self, "_warned_for_goal", True)
        object.__setattr__(self, name, value)

    @classmethod
    def from_legacy(cls, data: dict[str, Any]) -> "Finding":
        """Build a v0.5 Finding from v0.4-shaped attributes."""
        kwargs = {k: v for k, v in data.items() if k != "for_goal"}
        legacy_for_goal = data.get("for_goal")
        if legacy_for_goal is not None and "for_hyp" not in kwargs:
            kwargs["for_hyp"] = legacy_for_goal
        return cls(**kwargs)


@dataclass
class Contradiction(Atom):
    """§3b — detected inconsistency; forces a ``<Deciding>`` to retract."""

    kind: ClassVar[str] = "Contradiction"


@dataclass
class Uncertainty(Atom):
    """§3b — confidence below 1 attached to a target atom.

    ``on`` is the id of the target. ``confidence`` is a numeric in
    ``[0, 1]`` or a qualitative bucket (``"low"``, ``"medium"``,
    ``"high"``); kept as a string for roundtrip fidelity.
    """

    on: Optional[str] = None
    confidence: Optional[str] = None
    kind: ClassVar[str] = "Uncertainty"


@dataclass
class Retract(Atom):
    """§3b — revoke a prior finding; preserves history, never deletes.

    ``target`` names the atom being retracted. ``reason`` is the
    short rationale. ``replacement`` is an optional id of the new
    finding that supersedes the retracted one.
    """

    target: Optional[str] = None
    reason: Optional[str] = None
    replacement: Optional[str] = None
    kind: ClassVar[str] = "Retract"


@dataclass
class Concluding(Atom):
    """§3b — chain-level epistemic close.

    A ``<Concluding>`` asserts that cited Findings, Observations, or
    Evidence together resolve a stated Goal into a closing proposition.
    It is distinct from ``<Finding>`` (one hypothesis) and
    ``<Deciding>`` (action commitment).
    """

    for_goal: Optional[str] = None
    confidence: Optional[float] = None
    criticality: Optional[str] = None
    kind: ClassVar[str] = "Concluding"

    def __post_init__(self) -> None:
        if self.for_goal is None:
            raise ValueError(
                "Concluding requires for_goal — the closing claim must "
                "name the Goal it resolves."
            )


# ── 3c — control atoms ───────────────────────────────────────────────

@dataclass
class Deciding(Atom):
    """§3c — a branch point; must enumerate options + produce a Finding."""

    options: list[str] = field(default_factory=list)
    kind: ClassVar[str] = "Deciding"


@dataclass
class Alternative(Atom):
    """§3c — an explicitly rejected option inside a ``<Deciding>``."""

    label: Optional[str] = None
    rejected_because: Optional[str] = None
    kind: ClassVar[str] = "Alternative"


@dataclass
class Branch(Atom):
    """§3c — a legal transition out of a ``<Deciding>``.

    ``of`` names the parent Deciding's id. ``label`` is the option
    name (matches one of the parent's ``options`` entries).
    """

    of: Optional[str] = None
    label: Optional[str] = None
    kind: ClassVar[str] = "Branch"


@dataclass
class Loop(Atom):
    """§3c — iteration over a collection.

    ``over`` references the collection (``REFER:...``). ``as_var`` is
    the per-iteration binding; stored with a trailing underscore to
    avoid Python's reserved ``as``.
    """

    over: Optional[str] = None
    as_var: Optional[str] = None
    kind: ClassVar[str] = "Loop"


@dataclass
class Parallel(Atom):
    """§3c — concurrent independent steps; children have no ordering."""

    kind: ClassVar[str] = "Parallel"


# ── 3d — reference atoms ─────────────────────────────────────────────

@dataclass
class Storing(Atom):
    """§3d — self-closing; persists a value to trace-local memory.

    The stored name/value is parsed out of the tag's paren-argument
    (e.g. ``<Storing(main_head="a7173208")/>``). Kept as a
    ``name: Optional[str]`` + ``value: Optional[str]`` pair on the
    atom so the validator can reason about stored refs.
    """

    name: Optional[str] = None
    value: Optional[str] = None
    kind: ClassVar[str] = "Storing"


@dataclass
class Print(Atom):
    """§3d — self-closing; surfaces a one-line summary to the reader."""

    kind: ClassVar[str] = "Print"


@dataclass
class Reference(Atom):
    """§3d — explicit back-link (``REFER:`` inline is the shorthand)."""

    to: Optional[str] = None
    kind: ClassVar[str] = "Reference"


@dataclass
class Implication(Atom):
    """§3d — explicit forward-link (``IMPLIES:`` inline is the shorthand)."""

    next: Optional[str] = None
    kind: ClassVar[str] = "Implication"


# ── 3e — social atoms ────────────────────────────────────────────────

@dataclass
class Handoff(Atom):
    """§3e — pass work to another agent.

    ``to`` is the receiving agent's name. ``package`` is the work
    package identifier. Constraint strings on the handoff live in
    ``constraints``.
    """

    to: Optional[str] = None
    package: Optional[str] = None
    constraints: list[str] = field(default_factory=list)
    kind: ClassVar[str] = "Handoff"


@dataclass
class Question(Atom):
    """§3e — request for input from another agent or human."""

    to: Optional[str] = None
    scope: Optional[str] = None
    default: Optional[str] = None
    kind: ClassVar[str] = "Question"


@dataclass
class Review(Atom):
    """§3e — an audit of another agent's atom; must produce a Finding."""

    of: Optional[str] = None
    reviewer: Optional[str] = None
    kind: ClassVar[str] = "Review"


# ── 3f — meta atoms ──────────────────────────────────────────────────

@dataclass
class Constraint(Atom):
    """§3f — a hard rule in effect during this trace.

    Binds subsequent atoms: a ``<Deciding>`` must respect the rule
    and an ``<Action>`` must not violate it. ``scope`` defaults to
    ``"trace"``.
    """

    scope: Optional[str] = None
    kind: ClassVar[str] = "Constraint"


@dataclass
class Goal(Atom):
    """§3f — target proposition the agent is pursuing."""

    scope: Optional[str] = None
    priority: Optional[str] = None
    success_criteria: list[str] = field(default_factory=list)
    related_constraints: list[str] = field(default_factory=list)
    deadline: Optional[str] = None
    failure_modes: list[str] = field(default_factory=list)
    criticality: Optional[str] = None
    kind: ClassVar[str] = "Goal"


@dataclass
class Confidence(Atom):
    """§3f — numeric or qualitative score on another atom (dual of Uncertainty)."""

    on: Optional[str] = None
    level: Optional[str] = None
    basis: Optional[str] = None
    kind: ClassVar[str] = "Confidence"


@dataclass
class EventRef(Atom):
    """§3f — sidecar link to a host application's event-stream row."""

    instance: Optional[str] = None
    run_id: Optional[str] = None
    sequence: Optional[int] = None
    for_ref: Optional[str] = None
    wall_clock: Optional[str] = None
    kind: ClassVar[str] = "EventRef"


@dataclass
class Budget(Atom):
    """§3f — declared resource cap for a Goal, Step, or atom."""

    for_ref: Optional[str] = None
    tokens: Optional[int] = None
    actions: Optional[int] = None
    wall_clock_ms: Optional[int] = None
    kind: ClassVar[str] = "Budget"


@dataclass
class Cost(Atom):
    """§3f — measured resource consumption for an atom."""

    for_ref: Optional[str] = None
    tokens: Optional[int] = None
    wall_clock_ms: Optional[int] = None
    dollars: Optional[float] = None
    kind: ClassVar[str] = "Cost"


# ── v0.3.1 — primitive hooks reserved for v0.4 ───────────────────────
#
# These four atoms are SCHEMA-RESERVED in v0.3.1: the validator accepts
# them with closed-set attribute values, but the rewriter MUST NOT
# emit them. v0.4 PRDs populate them. See
# ``docs/scholia/SCHOLIA_v0.3.1_SPEC.md`` for the full contract.

@dataclass
class Edge(Atom):
    """v0.3.1 schema-reserved; v0.4-B populates — dependency-graph
    sub-element on ``<Observation>``.

    Wire form: ``<Edge type="depends_on" target="src/foo.py"/>``. The
    wire ``type`` attribute is carried on the Python field
    ``edge_type`` to avoid shadowing the Python builtin; the parser's
    per-class wire-alias map handles the mapping. ``target`` is a
    file path or import-path string — NOT an in-trace atom id, so
    the reference-completeness validator skips it.

    Closed set of ``type`` values is enforced by the validator's
    location/edge-shape rule (V031_EDGE_TYPES below): ``depends_on``,
    ``referenced_by``, ``imports``, ``references``. v0.4-B's
    rewriter populates ``depends_on``; the orchestrator's reverse-
    index pass populates ``referenced_by``.
    """

    edge_type: Optional[str] = None
    target: Optional[str] = None
    kind: ClassVar[str] = "Edge"


@dataclass
class Effect(Atom):
    """v0.3.1 — side-effect annotation on ``<Observation>``.

    Wire form: ``<Effect kind="io_write"/>``. The wire ``kind``
    attribute is carried on the Python field ``effect_kind`` because
    ``kind`` is the ClassVar discriminator; the parser's per-class
    wire-alias map handles the mapping. Closed set: ``io_write``,
    ``network``, ``subprocess``, ``mutates_state``, ``pure``.
    """

    effect_kind: Optional[str] = None
    kind: ClassVar[str] = "Effect"


@dataclass
class Ref(Atom):
    """v0.3.1 — typed external reference on ``<Observation>``.

    Wire form: ``<Ref type="test_owner" target="tests/foo.py"/>``.
    Distinct from ``<Reference>``: ``<Reference to="...">`` is an
    intra-trace back-link to an atom id; ``<Ref type="..." target="...">``
    is a typed external pointer to a file/test/doc artifact. The
    reference-completeness validator skips ``Ref.target`` because the
    target is not an in-trace atom id.
    """

    ref_type: Optional[str] = None
    target: Optional[str] = None
    kind: ClassVar[str] = "Ref"


@dataclass
class Meta(Atom):
    """v0.3.1 — risk-flag annotation on ``<Step>``.

    Wire form: ``<Meta criticality="kernel"/>``. Distinct from the
    ``Meta:research-mode`` pseudo-atom (which is parser-normalised
    via ``_normalise_pseudo_atoms`` and lives in
    ``PSEUDO_ATOM_KINDS``); the bare ``Meta`` tag is a closed-set
    atom registered in ``ATOM_KINDS``. Closed set: ``kernel``,
    ``verifier``, ``ledger``, ``bridge``, ``incidental``. Absence is
    semantically equivalent to ``incidental``.
    """

    criticality: Optional[str] = None
    kind: ClassVar[str] = "Meta"


# ── §6 — the ``<Step>`` container ────────────────────────────────────

@dataclass
class Step:
    """§6 — top-level container for one coherent atomic advance.

    A trace is ``list[Step]``. Each Step has a unique-within-trace
    ``id``, a human-readable ``name``, and 1..N child atoms.
    """

    id: Optional[str] = None
    name: Optional[str] = None
    atoms: list[Atom] = field(default_factory=list)
    kind: ClassVar[str] = "Step"


# ── Kind registry ─────────────────────────────────────────────────────

# The parser uses this to reject unknown tag names. v0.1 locks the
# vocabulary — extensibility lives in an ``<Ext:TagName>`` namespace
# that is a separate PRD. Adding a new atom requires: new dataclass
# above, entry in ``_ATOM_CLASSES``, and a spec update.
_ATOM_CLASSES: dict[str, type[Atom]] = {
    "Thinking": Thinking,
    "Observation": Observation,
    "Action": Action,
    "Hypothesis": Hypothesis,
    "Evidence": Evidence,
    "Finding": Finding,
    "Contradiction": Contradiction,
    "Uncertainty": Uncertainty,
    "Retract": Retract,
    "Concluding": Concluding,
    "Deciding": Deciding,
    "Alternative": Alternative,
    "Branch": Branch,
    "Loop": Loop,
    "Parallel": Parallel,
    "Storing": Storing,
    "Print": Print,
    "Reference": Reference,
    "Implication": Implication,
    "Handoff": Handoff,
    "Question": Question,
    "Review": Review,
    "Constraint": Constraint,
    "Goal": Goal,
    "Confidence": Confidence,
    "EventRef": EventRef,
    "Budget": Budget,
    "Cost": Cost,
    "Edge": Edge,
    "Effect": Effect,
    "Ref": Ref,
    "Meta": Meta,
}

ATOM_KINDS: tuple[str, ...] = tuple(_ATOM_CLASSES.keys())
PSEUDO_ATOM_KINDS: tuple[str, ...] = ("Meta:research-mode",)


# ── Closed-set canonical helpers (grammar-emergence detector) ────────
#
# ``OPERATORS`` above is the *spec-listed* operator vocabulary — what the
# notation reference doc says exists. ``CANONICAL_OPERATORS`` is the
# *validator-ratified* subset — what existing parser/validator/prompt
# infrastructure currently understands and enforces. The two diverge on
# purpose: a token in OPERATORS but absent from CANONICAL_OPERATORS is
# spec-future, not spec-present.
#
# The grammar-emergence detector treats anything outside
# CANONICAL_OPERATORS as a novel-operator finding so emergent extensions
# the verbalizer attempts (e.g. NOT:Obs_22 on 2026-05-03 ~05:30 UTC) get
# logged as evidence for downstream spec-extension review. Bumping this
# set is the contract for promoting a token from "candidate" to "ratified."
#
# v0.3 (2026-05-03) ratified ``NOT`` after empirical emergence during the
# rsi-uvicorn-teardown-quiet run; the validator's closed-set check now
# rejects any reference operator outside this set with rule
# ``unknown_operator`` (NOTATION_REFERENCE.md §9 rule 8).
#
# v0.4 (2026-05-11) operator-driven mass-promotion of the remaining 8
# spec-listed operators ahead of the pre-MS-Co-Pilot benchmark window.
# Before this bump, AND/OR/XOR/FORALL/EXISTS/BEFORE/AFTER/EQUALS were
# in the ``Operator`` enum + NOTATION_REFERENCE.md §4 but rejected by
# the validator — agents that emitted them got an ``unknown_operator``
# error and the trace lost the operator. Promotion brings the validator
# in line with the spec; future spec extensions still flow through the
# grammar-emergence detector → review → ratify path.
CANONICAL_OPERATORS: frozenset[str] = frozenset(_OPERATOR_VALUES)

# Frozen mirror of ATOM_KINDS for closed-set membership checks. Kept as
# its own constant so the detector + future spec-extension PRDs have a
# single named handle to amend when a new atom kind is ratified.
KNOWN_KINDS: frozenset[str] = frozenset(_ATOM_CLASSES.keys())


# ── v0.5 — criticality ordering ──────────────────────────────────────

CRITICALITY_RANK: dict[str, int] = {
    "incidental": 0,
    "bridge": 1,
    "ledger": 2,
    "verifier": 3,
    "kernel": 4,
}


# ── v0.6 — content-addressable canonical IDs ─────────────────────────
#
# Ported byte-for-byte from the scholialang v0.6 reference so that the
# same structural atom hashes to the same canonical_id across every
# Scholia implementation (standalone package, the bundled MCP validator,
# and OpenTalon's vendored copy). The cross-impl identity is load-bearing
# for the DAG registry — two emits of the same atom from different
# sessions MUST address to the same node. Do not "improve" this hasher
# without bumping every implementation in lockstep.


class CanonicalIdMismatch(ValueError):
    """Raised when a claimed canonical_id does not match the recomputed hash.

    Carries ``atom_id`` (the local id, may be ``None``), ``claimed`` (the
    canonical_id on the wire), and ``computed`` (the hash recomputed from
    the parsed atom's structural content). The validator's
    ``canonical_id_well_formed`` rule surfaces this as a structured
    violation; strict-mode parsers may re-raise directly.
    """

    def __init__(
        self,
        atom_id: Optional[str],
        claimed: str,
        computed: str,
    ) -> None:
        self.atom_id = atom_id
        self.claimed = claimed
        self.computed = computed
        super().__init__(
            f"canonical_id mismatch on atom id={atom_id!r}: "
            f"claimed={claimed!r} computed={computed!r}"
        )


# Provenance / non-identity fields excluded from the canonical_id hash.
# Reason: timestamp / wall_clock / run_id / sequence / instance carry
# session-level metadata; two emits of the same structural atom from
# different sessions must produce the same canonical_id.
_PROVENANCE_FIELDS: frozenset[str] = frozenset({
    "timestamp",
    "wall_clock",
    "run_id",
    "sequence",
    "instance",
})

# Base-Atom bookkeeping fields — never part of the content hash.
# ``id`` is local-scope; ``canonical_id`` is the output we're computing;
# ``children`` are hashed independently (v0.7 Merkle-DAG extension would
# fold them in); ``operators`` are derived from content text.
_NON_HASH_FIELDS: frozenset[str] = frozenset({
    "id",
    "canonical_id",
    "content",
    "children",
    "operators",
    "kind",
})


def _hash_value(value: Any) -> Any:
    """Coerce a field value to a JSON-serializable shape for hashing."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_hash_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _hash_value(v) for k, v in value.items()}
    return value


def compute_canonical_id(atom: "Atom") -> str:
    """Compute the content-addressable canonical_id for ``atom``.

    Hash input is the canonical-JSON serialization of
    ``{"kind", "content", "attrs"}`` where:

    - ``content`` is the body text with leading/trailing whitespace
      stripped.
    - ``attrs`` is a sorted dict of the atom's kind-specific fields,
      excluding provenance (``timestamp``, ``run_id``, ``wall_clock``,
      ``sequence``, ``instance``) and the base bookkeeping fields
      (``id``, ``canonical_id``, ``children``, ``operators``).
    - Empty / ``None`` attrs are dropped so absence and explicit-None
      hash identically.

    Output: ``"sha256:" + first 12 hex chars`` of the SHA-256 digest
    of the canonical JSON. Multihash-compatible prefix.
    """
    attrs: dict[str, Any] = {}
    for f in fields(atom):
        if f.name in _NON_HASH_FIELDS or f.name in _PROVENANCE_FIELDS:
            continue
        value = getattr(atom, f.name, None)
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        attrs[f.name] = _hash_value(value)

    payload = {
        "kind": atom.kind,
        "content": (atom.content or "").strip(),
        "attrs": attrs,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


# Regex matching ``UPPERCASE_TOKEN:atom_id`` operator-target pairs in
# free-form atom content. Token is ASCII uppercase + underscore, length
# ≥ 2 to avoid matching single-letter abbreviations. Atom id is the
# ``Tag_NN`` id minted by the translator (alphanumerics + underscore).
import re as _re  # local alias keeps the public namespace tidy
_OPERATOR_TARGET_RE: _re.Pattern[str] = _re.compile(
    r"\b([A-Z][A-Z_]{1,})\s*:\s*([A-Za-z][A-Za-z0-9_]*)"
)


def is_canonical_operator(token: str) -> bool:
    """Return ``True`` when ``token`` is in the validator-ratified set.

    Used by the grammar-emergence detector to gate novel-operator
    findings — a ``False`` return for a token regex-extracted from atom
    content means the verbalizer has emitted something outside the
    currently ratified vocabulary.
    """
    return token in CANONICAL_OPERATORS


def is_known_kind(kind: str) -> bool:
    """Return ``True`` when ``kind`` is one of the ratified atom kinds."""
    return kind in KNOWN_KINDS


def parse_operators_from_content(content: str) -> list[tuple[str, str]]:
    """Extract ``(operator_name, target_atom_id)`` pairs from ``content``.

    Returns the list in source order — duplicates are preserved so
    callers can count occurrences. Empty / non-string input returns
    an empty list. The regex is tolerant of surrounding whitespace
    (``REFER : Obs_01``) and embeds (``…and IMPLIES:Fin_03 follows.``).
    """
    if not content or not isinstance(content, str):
        return []
    return [(m.group(1), m.group(2)) for m in _OPERATOR_TARGET_RE.finditer(content)]


def atom_class_for_kind(kind: str) -> Optional[type[Atom]]:
    """Return the dataclass for an atom kind; ``None`` when unknown.

    Used by the parser + serializer to dispatch by wire kind name.
    """
    return _ATOM_CLASSES.get(kind)


def atom_kinds() -> tuple[str, ...]:
    """Return the tuple of all known atom kind names."""
    return ATOM_KINDS


# Expose atom class mapping for sibling modules that need to iterate.
def iter_atom_classes() -> list[tuple[str, type[Atom]]]:
    """Return ``(kind, cls)`` pairs for every known atom kind."""
    return list(_ATOM_CLASSES.items())


# Kind-specific field names — serializer uses this so each atom emits
# its distinctive fields without a second inspection pass. Keeping the
# map here (next to the atoms) means adding a new atom is a single-
# file change.
KIND_SPECIFIC_FIELDS: dict[str, tuple[str, ...]] = {
    "Observation": ("timestamp", "location", "confidence"),
    "Action": ("timestamp",),
    "Evidence": ("for_ref", "polarity"),
    "Finding": ("for_hyp", "for_goal", "status"),
    "Concluding": ("for_goal", "confidence", "criticality"),
    "Uncertainty": ("on", "confidence"),
    "Retract": ("target", "reason", "replacement"),
    "Deciding": ("options",),
    "Alternative": ("label", "rejected_because"),
    "Branch": ("of", "label"),
    "Loop": ("over", "as_var"),
    "Storing": ("name", "value"),
    "Reference": ("to",),
    "Implication": ("next",),
    "Handoff": ("to", "package", "constraints"),
    "Question": ("to", "scope", "default"),
    "Review": ("of", "reviewer"),
    "Constraint": ("scope",),
    "Goal": (
        "scope",
        "priority",
        "success_criteria",
        "related_constraints",
        "deadline",
        "failure_modes",
        "criticality",
    ),
    "Confidence": ("on", "level", "basis"),
    "EventRef": ("instance", "run_id", "sequence", "for_ref", "wall_clock"),
    "Budget": ("for_ref", "tokens", "actions", "wall_clock_ms"),
    "Cost": ("for_ref", "tokens", "wall_clock_ms", "dollars"),
    "Edge": ("edge_type", "target"),
    "Effect": ("effect_kind",),
    "Ref": ("ref_type", "target"),
    "Meta": ("criticality",),
}


# Attribute-name aliases between Python field names and wire-format
# attribute names. ``for`` / ``as`` are reserved Python words, so
# internally we carry them as ``for_ref`` / ``as_var``; on the wire
# they stay ``for`` / ``as`` to match the spec exactly.
WIRE_ATTR_ALIASES: dict[str, str] = {
    "for_ref": "for",
    "for_goal": "for_goal",
    "as_var": "as",
}

# Reverse — wire → field name. Built once at module import.
FIELD_ATTR_ALIASES: dict[str, str] = {v: k for k, v in WIRE_ATTR_ALIASES.items()}


# v0.3.1 — per-kind wire→field aliases. The global ``FIELD_ATTR_ALIASES``
# is symmetric across all atoms, but v0.3.1 introduced wire attributes
# (``kind`` on ``<Effect>``, ``type`` on ``<Edge>`` and ``<Ref>``) that
# collide with the ClassVar discriminator (``kind``) or the Python
# builtin (``type``) only on those specific atoms. Per-class mapping
# keeps the parser's setattr call from shadowing the discriminator.
KIND_SPECIFIC_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "Effect": {"kind": "effect_kind"},
    "Edge": {"type": "edge_type"},
    "Ref": {"type": "ref_type"},
}


def wire_name(field_name: str) -> str:
    """Return the wire attribute name for a Python field name."""
    return WIRE_ATTR_ALIASES.get(field_name, field_name)


def field_name(wire_attr: str) -> str:
    """Return the Python field name for a wire attribute name."""
    return FIELD_ATTR_ALIASES.get(wire_attr, wire_attr)


def field_name_for(kind: str, wire_attr: str) -> str:
    """Return the Python field name for ``wire_attr`` on ``kind``.

    Consults the per-kind alias map first (v0.3.1 hook), then falls
    back to the global ``FIELD_ATTR_ALIASES``. Used by the parser when
    applying parsed XML attributes onto an atom dataclass.
    """
    kind_aliases = KIND_SPECIFIC_FIELD_ALIASES.get(kind)
    if kind_aliases and wire_attr in kind_aliases:
        return kind_aliases[wire_attr]
    return FIELD_ATTR_ALIASES.get(wire_attr, wire_attr)


def wire_name_for(kind: str, py_field: str) -> str:
    """Inverse of :func:`field_name_for` — Python field name → wire attr.

    The parser uses this to compute the *closed set of wire attributes*
    each atom kind permits. v0.3.1 strict-closed-set posture: anything
    not in this set is rejected at parse time (closes the gap where
    ``<Observation foo="bar">`` silently passed pre-v0.3.1-fix).
    """
    kind_aliases = KIND_SPECIFIC_FIELD_ALIASES.get(kind)
    if kind_aliases:
        for wire, py in kind_aliases.items():
            if py == py_field:
                return wire
    return WIRE_ATTR_ALIASES.get(py_field, py_field)


# ── v0.3.1 — reserved closed-sets (consumed by validator/parser) ─────
#
# Every v0.3.1 primitive hook has a closed-set value contract. These
# constants are imported by parser/validator/tests so the canonical
# set lives in one place. Bumping a set requires a spec doc update.

SCHOLIA_VALIDATOR_VERSION: str = "0.6.0"

V031_EDGE_TYPES: frozenset[str] = frozenset({
    "depends_on",
    "referenced_by",
    "imports",
    "references",
})

V031_EFFECT_KINDS: frozenset[str] = frozenset({
    "io_write",
    "network",
    "subprocess",
    "mutates_state",
    "pure",
})

V031_REF_TYPES: frozenset[str] = frozenset({
    "test_owner",
    "doc_owner",
})

V031_META_CRITICALITIES: frozenset[str] = frozenset({
    "kernel",
    "verifier",
    "ledger",
    "bridge",
    "incidental",
})

# ``file:start:end`` line-span format. Used by parser/validator to check
# ``<Observation location="...">`` values when present.
import re as _v031_re  # local alias keeps the public namespace tidy
V031_LOCATION_RE: _v031_re.Pattern[str] = _v031_re.compile(r"^[^:]+:\d+:\d+$")

# v0.4-B back-compat aliases — referenced by atlas/code_graph callers.
V04B_EDGE_TYPES = V031_EDGE_TYPES


def is_valid_location(value: str | None) -> bool:
    """Return ``True`` when ``value`` matches the v0.3.1/v0.4-B location regex."""
    if not value or not isinstance(value, str):
        return False
    return bool(V031_LOCATION_RE.match(value))


def is_valid_edge_type(value: str | None) -> bool:
    """Return ``True`` when ``value`` is in :data:`V031_EDGE_TYPES`."""
    if not value or not isinstance(value, str):
        return False
    return value in V031_EDGE_TYPES


# ── v0.6 — minimal single-atom XML serialization ─────────────────────
#
# The full trace serializers (JSON/YAML) live in :mod:`scholialang.serializer`
# and the human Markdown renderer in :mod:`scholialang.renderer`. This is a
# small single-atom → XML helper that emits the v0.6 ``canonical_id``
# attribute; the canonical-prelude renderer (:mod:`scholialang.prelude`)
# consumes it for its ``inline`` transcript mode. It mirrors the wire shape
# the parser accepts, so ``parse_atom(atom_to_xml(a))`` round-trips.


def _atom_to_element(atom: "Atom") -> "ET.Element":
    elem = ET.Element(atom.kind)
    if atom.id is not None:
        elem.set("id", atom.id)
    if atom.canonical_id is not None:
        elem.set("canonical_id", atom.canonical_id)
    for fname in KIND_SPECIFIC_FIELDS.get(atom.kind, ()):
        value = getattr(atom, fname, None)
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
            elem.set(wire_name_for(atom.kind, fname), ",".join(str(v) for v in value))
        else:
            elem.set(wire_name_for(atom.kind, fname), str(value))
    if atom.content:
        elem.text = atom.content
    for child in atom.children:
        elem.append(_atom_to_element(child))
    return elem


def atom_to_xml(atom: "Atom") -> str:
    """Serialize a single atom (and its children) to an XML string.

    Emits the v0.6 ``canonical_id`` attribute when present. Used by the
    canonical-prelude renderer's ``inline`` mode; full-trace serialization
    lives in :mod:`scholialang.serializer`.
    """
    return ET.tostring(_atom_to_element(atom), encoding="unicode")


# Convenience type alias for a trace — an ordered list of Steps.
Trace = list[Step]


# Backwards-safety: expose Any so downstream ``isinstance`` checks
# can narrow a nested ``children`` list without importing every atom
# kind by name.
AnyAtom = Atom
_: Any = None  # noqa: E501 — suppress unused var linter in some configs
