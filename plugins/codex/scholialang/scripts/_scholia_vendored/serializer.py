"""Scholia serializers — AST ↔ JSON ↔ YAML.

JSON is the canonical machine format; YAML is the config-friendly
twin. Both are lossless round-trips per NOTATION_REFERENCE.md §10b/c
— a trace serialised and parsed back is bit-identical under canonical
key ordering (see :func:`to_canonical_json`).

Why a separate canonical form: the enforcement layer hashes trace
bytes via ``content hashing`` to produce a content
address. Hashing arbitrary JSON whitespace + key order is brittle;
``to_canonical_json`` pins both so the hash is stable across agent
runs and across serializer versions.

YAML support leans on PyYAML's ``safe_dump`` / ``safe_load`` so no
arbitrary Python objects reconstitute. Every atom kind roundtrips
through both paths — exercised end-to-end in the unit tests.
"""
from __future__ import annotations

import json
from typing import Any

import yaml

from .atoms import (
    KIND_SPECIFIC_FIELDS,
    PSEUDO_ATOM_KINDS,
    SEMANTIC_KINDS,
    Atom,
    SemanticShapeError,
    Step,
    atom_class_for_kind,
    compute_canonical_id,
    normalize_semantic_atom,
    wire_name,
)


# ── Atom → dict ──────────────────────────────────────────────────────


def _atom_to_dict(atom: Atom) -> dict[str, Any]:
    """Convert an atom dataclass into a plain dict for serialization.

    Key order is stable: ``kind`` first (so the dispatch is cheap on
    read), then common fields, then kind-specific fields. Empty
    collections are emitted as empty — a parsed-then-serialized atom
    should equal its source for the roundtrip invariant, and that
    means not dropping fields based on content.
    """
    if atom.kind in SEMANTIC_KINDS:
        # v0.7 — encode the validated normalized copy: a missing
        # canonical_id is computed on the copy (the caller's object is
        # NEVER mutated) and the canonical_id is retained on the wire.
        atom = normalize_semantic_atom(atom)
    out: dict[str, Any] = {"kind": atom.kind}
    if atom.id is not None:
        out["id"] = atom.id
    canonical_id = atom.canonical_id
    if canonical_id is None and atom.kind not in PSEUDO_ATOM_KINDS:
        # Compute for the wire only; a caller-owned legacy atom is not stamped.
        canonical_id = compute_canonical_id(atom)
    if canonical_id is not None:
        out["canonical_id"] = canonical_id
    out["content"] = atom.content
    out["operators"] = list(atom.operators)
    for field_name in KIND_SPECIFIC_FIELDS.get(atom.kind, ()):
        value = getattr(atom, field_name)
        if isinstance(value, list):
            out[wire_name(field_name)] = list(value)
        elif isinstance(value, dict):
            # v0.7 Map.entries — a real mapping on the JSON/YAML wire,
            # never a Python repr or a JSON-encoded string.
            out[wire_name(field_name)] = dict(value)
        else:
            out[wire_name(field_name)] = value
    if atom.children:
        out["children"] = [_atom_to_dict(c) for c in atom.children]
    else:
        out["children"] = []
    return out


def _step_to_dict(step: Step) -> dict[str, Any]:
    """Convert a ``Step`` into a dict with the §10b shape."""
    out: dict[str, Any] = {}
    if step.id is not None:
        out["id"] = step.id
    if step.name is not None:
        out["name"] = step.name
    out["atoms"] = [_atom_to_dict(a) for a in step.atoms]
    return out


def trace_to_dict(
    trace: list[Step], *, trace_id: str | None = None
) -> dict[str, Any]:
    """Convert a full trace into the §10b JSON-shaped dict."""
    out: dict[str, Any] = {}
    if trace_id is not None:
        out["trace_id"] = trace_id
    out["steps"] = [_step_to_dict(s) for s in trace]
    return out


# ── dict → Atom ──────────────────────────────────────────────────────


# Structural keys legal on every semantic-kind payload in JSON/YAML/dict
# input, alongside the kind-specific wire fields. Anything else is an
# unknown field and is rejected at decode (parse phase).
_SEMANTIC_COMMON_KEYS: frozenset[str] = frozenset({
    "kind",
    "id",
    "canonical_id",
    "content",
    "operators",
    "children",
})


def _semantic_atom_from_dict(kind: str, payload: dict[str, Any]) -> Atom:
    """Strict v0.7 decode for Map/Event/Task dict payloads (parse phase).

    Rejects unknown fields, non-list-of-string ``operators``, nonempty
    ``children``, and non-mapping / nonstring-key ``entries`` — malformed
    field shapes are rejected, never silently discarded. Field semantics
    (required fields, enums, typed entries, references) are validate-
    phase and left to :func:`scholialang.validator.validate`.
    """
    cls = atom_class_for_kind(kind)
    assert cls is not None  # caller dispatches only known semantic kinds
    allowed = _SEMANTIC_COMMON_KEYS | {
        wire_name(f) for f in KIND_SPECIFIC_FIELDS[kind]
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SemanticShapeError(
            f"<{kind}> has unknown field(s) {unknown!r}; the closed "
            f"per-kind field set is {sorted(allowed)!r} (unknown fields "
            "on the v0.7 semantic kinds are rejected across XML, JSON, "
            "YAML, and dictionary input).",
            rule="semantic_unknown_field",
            kind=kind,
        )
    atom = cls()
    atom.id = payload.get("id")
    canonical_id = payload.get("canonical_id")
    if canonical_id is not None:
        # An explicit claim is preserved verbatim — matching or not — so
        # canonical_id_well_formed can report tampering at validate.
        atom.canonical_id = canonical_id
    atom.content = payload.get("content", "")
    operators = payload.get("operators", [])
    if not isinstance(operators, list) or not all(
        isinstance(op, str) for op in operators
    ):
        raise SemanticShapeError(
            f"<{kind}> operators must be a list of strings; got "
            f"{operators!r} (no coercion from arbitrary objects).",
            rule="semantic_operators_list",
            kind=kind,
        )
    atom.operators = list(operators)
    children = payload.get("children", [])
    if children:
        raise SemanticShapeError(
            f"<{kind}> children must be empty — new kinds carry no "
            "nested child atoms in this revision.",
            rule="semantic_children_empty",
            kind=kind,
        )
    for field_name in KIND_SPECIFIC_FIELDS[kind]:
        wire_key = wire_name(field_name)
        if wire_key not in payload:
            continue
        value = payload[wire_key]
        if kind == "Map" and field_name == "entries" and value is not None:
            if not isinstance(value, dict):
                raise SemanticShapeError(
                    f"<Map> entries must be a real mapping, never "
                    f"{type(value).__name__!r} — a JSON-encoded string "
                    "or other non-mapping value is rejected.",
                    rule="map_entries_shape",
                    kind=kind,
                )
            for key in value:
                if not isinstance(key, str):
                    raise SemanticShapeError(
                        f"<Map> entries keys must be strings; got {key!r} "
                        f"({type(key).__name__}).",
                        rule="map_entries_shape",
                        kind=kind,
                    )
            value = dict(value)
        setattr(atom, field_name, value)
    # Normalize: structural re-check plus canonical_id computed when the
    # payload carried none (claimed values, even mismatched, survive).
    return normalize_semantic_atom(atom)


def _atom_from_dict(payload: dict[str, Any]) -> Atom:
    """Reconstruct the right atom dataclass from a dict payload."""
    kind = payload.get("kind")
    if not isinstance(kind, str):
        raise ValueError(
            "Scholia atom dict missing 'kind' discriminator."
        )
    if kind in SEMANTIC_KINDS:
        return _semantic_atom_from_dict(kind, payload)
    cls = atom_class_for_kind(kind)
    if cls is None:
        if kind not in PSEUDO_ATOM_KINDS:
            raise SemanticShapeError(
                f"Unknown Scholia atom kind: {kind!r}",
                rule="unknown_kind",
                kind=kind,
            )
        atom = Atom()
        atom.kind = kind
    elif kind == "Concluding":
        atom = cls(for_goal=payload.get("for_goal"))
    else:
        atom = cls()
    atom.id = payload.get("id")
    # Legacy kinds are legal canonical-reference targets of Map/Event/Task.
    # Preserve explicit claims (including bad claims for validation) instead
    # of silently losing their identity across machine-format round trips.
    atom.canonical_id = payload.get("canonical_id")
    atom.content = str(payload.get("content", ""))
    atom.operators = list(payload.get("operators", []))
    for field_name in KIND_SPECIFIC_FIELDS.get(kind, ()):
        wire_key = wire_name(field_name)
        if wire_key in payload:
            if (
                kind == "Finding"
                and field_name == "for_goal"
                and "for_hyp" not in payload
            ):
                setattr(atom, "for_hyp", payload[wire_key])
            else:
                setattr(atom, field_name, payload[wire_key])
    atom.children = [_atom_from_dict(c) for c in payload.get("children", [])]
    if atom.canonical_id is None and kind not in PSEUDO_ATOM_KINDS:
        atom.canonical_id = compute_canonical_id(atom)
    return atom


def _step_from_dict(payload: dict[str, Any]) -> Step:
    return Step(
        id=payload.get("id"),
        name=payload.get("name"),
        atoms=[_atom_from_dict(a) for a in payload.get("atoms", [])],
    )


def trace_from_dict(payload: dict[str, Any]) -> list[Step]:
    """Reconstruct a trace from the §10b JSON-shaped dict."""
    steps_raw = payload.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ValueError("Scholia trace dict must carry a list 'steps'.")
    return [_step_from_dict(s) for s in steps_raw]


# ── JSON ─────────────────────────────────────────────────────────────


def to_json(
    trace: list[Step],
    *,
    trace_id: str | None = None,
    indent: int | None = 2,
) -> str:
    """Serialize a trace to JSON (non-canonical — readable)."""
    payload = trace_to_dict(trace, trace_id=trace_id)
    return json.dumps(payload, indent=indent, ensure_ascii=False)


def to_canonical_json(
    trace: list[Step], *, trace_id: str | None = None
) -> str:
    """Serialize with sorted keys + compact separators — hashing input.

    Canonical form is deterministic across Python versions: same
    trace always produces byte-identical output. The enforcement
    layer feeds this string to ``goat_hash`` so a trace's
    ``content_hash`` field is reproducible by an auditor who only has
    the AST.
    """
    payload = trace_to_dict(trace, trace_id=trace_id)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# v0.7 — narrow malformed-input hardening on the full-trace JSON loader.
# Duplicate mapping keys have already destroyed information by the time a
# consumer sees the decoded object, and nonfinite tokens are not
# interoperable JSON; both are rejected while decoding. This is
# documented as malformed-input rejection (valid legacy corpus parity is
# proven by the golden/corpus suites), not a semantic change to any
# well-formed legacy document.


def _json_pairs_reject_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise SemanticShapeError(
                f"JSON mapping contains duplicate key {key!r}; duplicate "
                "keys are rejected while decoding rather than silently "
                "overwritten.",
                rule="map_entries_shape",
            )
        obj[key] = value
    return obj


def _json_reject_nonfinite(token: str) -> Any:
    raise SemanticShapeError(
        f"JSON document contains the nonfinite token {token!r}; NaN and "
        "infinities are not interoperable JSON values and are rejected "
        "while decoding (they cannot appear in Map entries or any other "
        "mapping).",
        rule="map_entries_shape",
    )


def from_json(text: str) -> list[Step]:
    """Parse a JSON trace string back into Steps."""
    payload = json.loads(
        text,
        object_pairs_hook=_json_pairs_reject_duplicates,
        parse_constant=_json_reject_nonfinite,
    )
    if not isinstance(payload, dict):
        raise ValueError("Scholia JSON trace must be a top-level object.")
    return trace_from_dict(payload)


# ── YAML ─────────────────────────────────────────────────────────────


def to_yaml(
    trace: list[Step], *, trace_id: str | None = None
) -> str:
    """Serialize a trace to YAML via ``safe_dump``."""
    payload = trace_to_dict(trace, trace_id=trace_id)
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


class _DuplicateKeyRejectingLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys while decoding.

    v0.7 narrow malformed-input hardening, mirroring the JSON loader:
    a duplicate YAML key silently drops data under vanilla safe_load.
    """


def _yaml_construct_mapping_strict(
    loader: _DuplicateKeyRejectingLoader, node: Any, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise SemanticShapeError(
                f"YAML mapping key {key!r} is not hashable.",
                rule="map_entries_shape",
            ) from exc
        if duplicate:
            raise SemanticShapeError(
                f"YAML mapping contains duplicate key {key!r}; duplicate "
                "keys are rejected while decoding rather than silently "
                "overwritten.",
                rule="map_entries_shape",
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_DuplicateKeyRejectingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _yaml_construct_mapping_strict,
)


def from_yaml(text: str) -> list[Step]:
    """Parse a YAML trace string back into Steps."""
    payload = yaml.load(text, Loader=_DuplicateKeyRejectingLoader)  # noqa: S506
    if not isinstance(payload, dict):
        raise ValueError("Scholia YAML trace must be a top-level mapping.")
    return trace_from_dict(payload)
