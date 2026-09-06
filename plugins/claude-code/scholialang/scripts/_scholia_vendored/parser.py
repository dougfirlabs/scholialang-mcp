"""Scholia parser — XML-ish text → AST.

Per NOTATION_REFERENCE.md §2, Scholia is "XML-ish, but intentionally
degenerate — a subset parseable with a ~100-line parser." We lean on
stdlib ``xml.etree.ElementTree`` for the tree walk and do a small
post-pass to extract inline operators, stored-value args on
self-closing tags, and structured attributes.

Why not strict XML: the content produced by agents interleaves
natural prose with angle-bracketed pseudo-tags (``<bash>``, ``<git>``)
inside ``<Observation>`` / ``<Action>`` content. ElementTree accepts
these as child elements; we capture them as raw text on the parent
since they are not part of the v0.1 atom catalog. Other XML-ish
quirks handled here:

* Self-closing ``<Storing(name="v")/>`` — ElementTree rejects the
  parenthesised argument on a tag name. The pre-pass rewrites these
  into attribute form before parsing.
* Inline operators (``REFER:x``, ``IMPLIES:y``) inside text content
  — extracted with a regex pass and attached to the containing atom.
* Unknown top-level tag names — rejected with a helpful line/column
  message. v0.1 is a closed vocabulary (§11).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.etree.ElementTree import Element

from .atoms import (
    ATOM_KINDS,
    PSEUDO_ATOM_KINDS,
    OPERATORS,
    SEMANTIC_KINDS,
    V031_EDGE_TYPES,
    V031_EFFECT_KINDS,
    V031_LOCATION_RE,
    V031_META_CRITICALITIES,
    V031_REF_TYPES,
    Atom,
    Alternative,
    Branch,
    Concluding,
    Deciding,
    Finding,
    Goal,
    Handoff,
    Map,
    Step,
    Storing,
    atom_class_for_kind,
    compute_canonical_id,
    field_name,
    field_name_for,
    wire_name_for,
)


# ── v0.3.1 — strict closed-set of wire attributes per atom kind ───────
#
# Closes the gap where ``<Observation foo="bar">`` silently passed
# pre-v0.3.1-fix (parser ignored unknown attrs, validator never
# checked). Builds the allowed-set programmatically from each kind's
# dataclass fields so v0.4 additions to the schema automatically widen
# the allowed set when their fields land on the dataclass.
#
# Two opt-out mechanisms preserve existing parser features that don't
# fit a strict-closed-set posture:
#
# * ``_OPEN_NAMESPACE_KINDS`` — atoms whose paren-arg form accepts
#   arbitrary operator-supplied keys (``<Storing(main_head="...")/>``
#   stores ``main_head`` as the key). These atoms skip the strict
#   check entirely.
# * ``_EXTRA_WIRE_ATTRS_BY_KIND`` — per-kind wire attrs that exist
#   only in the parser's desugaring pre-pass and don't appear on the
#   dataclass (e.g. ``<Deciding chose="A"/>`` short-form desugars to
#   child atoms; ``chose`` itself is never set as a field).

import dataclasses
from . import atoms as _atoms_module


# Fields on the base Atom that are NOT XML attributes (they're content
# and structural). Subclasses inherit these via @dataclass but they
# must NOT appear in the allowed-wire-attrs set.
_NON_ATTR_BASE_FIELDS: frozenset[str] = frozenset({
    "content",
    "children",
    "operators",
})


# Atoms whose wire-attribute namespace is operator-defined (the paren-
# arg form accepts arbitrary key=value pairs). The strict closed-set
# check skips these kinds by design — see Storing/Print docstrings.
_OPEN_NAMESPACE_KINDS: frozenset[str] = frozenset({
    "Storing",
    "Print",
})


# Per-kind extra wire attrs not derived from dataclass fields. These
# exist as parser desugar inputs (e.g. ``<Deciding chose="A"/>`` short-
# form expands into ``<Branch/>`` + ``<Finding/>`` children; ``chose``
# is consumed during desugar and never lands on the Deciding instance).
_EXTRA_WIRE_ATTRS_BY_KIND: dict[str, frozenset[str]] = {
    "Deciding": frozenset({"chose"}),
}

# Universal parser sidecars. ``value`` is produced by
# ``_normalise_paren_args`` whenever an atom is written in paren-arg
# shorthand without an explicit key (``<Print("hi")/>`` →
# ``<Print value="hi"/>``). It can appear on any kind, so the strict
# closed-set check must always permit it.
_UNIVERSAL_WIRE_ATTRS: frozenset[str] = frozenset({"value"})


def _build_allowed_attrs_by_kind() -> dict[str, frozenset[str]]:
    """One-time at module import: enumerate allowed wire attrs per kind.

    Reads each Atom subclass's dataclass fields, filters out
    structural-non-attribute fields, translates Python field names to
    wire names via :func:`atoms.wire_name_for`. Adds per-kind desugar-
    only extras from ``_EXTRA_WIRE_ATTRS_BY_KIND``. The resulting map
    is the canonical closed set the parser enforces.
    """
    result: dict[str, frozenset[str]] = {}
    for name in dir(_atoms_module):
        cls = getattr(_atoms_module, name)
        if not isinstance(cls, type):
            continue
        if not dataclasses.is_dataclass(cls):
            continue
        kind = getattr(cls, "kind", None)
        if not isinstance(kind, str) or kind == "Atom":
            continue
        wire_attrs: set[str] = set()
        for fld in dataclasses.fields(cls):
            if fld.name in _NON_ATTR_BASE_FIELDS:
                continue
            wire_attrs.add(wire_name_for(kind, fld.name))
        wire_attrs |= _EXTRA_WIRE_ATTRS_BY_KIND.get(kind, frozenset())
        wire_attrs |= _UNIVERSAL_WIRE_ATTRS
        result[kind] = frozenset(wire_attrs)
    return result


# Built once. Module-level constant. Any future v0.4 additions to a
# dataclass automatically widen the allowed set on the next import.
_ALLOWED_ATTRS_BY_KIND: dict[str, frozenset[str]] = _build_allowed_attrs_by_kind()


class ScholiaParseError(ValueError):
    """Raised when input is not a parseable Scholia trace.

    ``rule`` (v0.7, optional) carries the conformance rule code for the
    parse-phase failure when one applies (e.g. ``unknown_kind``,
    ``semantic_unknown_field``) so conformance harnesses can dispatch
    without message-pattern matching. Older call sites that pass only a
    message are unchanged.
    """

    def __init__(self, message: str, *, rule: str | None = None):
        super().__init__(message)
        self.rule = rule


# ── Pre-pass — normalise XML-ish quirks ──────────────────────────────

# <Storing(name="value")/> → <Storing name="value"/> — ElementTree
# cannot tolerate parens in a tag name, so we rewrite to standard
# attribute form before handing it over.
_PAREN_ARG_RE = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9]*)\((?P<args>[^)]*)\)\s*(?P<close>/?)>"
)
_PAREN_CLOSE_RE = re.compile(r"</(?P<tag>[A-Za-z][A-Za-z0-9]*)\([^)]*\)>")


def _normalise_paren_args(text: str) -> str:
    def _rewrite(match: re.Match[str]) -> str:
        tag = match.group("tag")
        args = match.group("args").strip()
        close = match.group("close")
        if not args:
            return f"<{tag}{close}>"
        # Split "name=value" pairs. Fallback: treat the whole arg as a
        # raw ``value`` string so ``<Print("hello")/>`` still round-
        # trips with its content visible on the atom.
        if "=" in args:
            normalized = args
        else:
            # Strip surrounding quotes on a lone literal.
            lit = args.strip()
            if (
                len(lit) >= 2
                and lit[0] == lit[-1]
                and lit[0] in ('"', "'")
            ):
                lit = lit[1:-1]
            normalized = f'value="{_xml_escape(lit)}"'
        return f"<{tag} {normalized}{close}>"

    text = _PAREN_ARG_RE.sub(_rewrite, text)
    return _PAREN_CLOSE_RE.sub(lambda match: f"</{match.group('tag')}>", text)


# <!-- comment --> — stripped before parsing so ElementTree doesn't
# trip on embedded dashes inside reasoning prose. Comments are
# discarded per §2 (no semantic role).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_XML_FENCE_RE = re.compile(r"```xml\s*(?P<body>.*?)```", re.DOTALL)


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def _extract_xml_fences(text: str) -> str:
    if "```xml" not in text:
        return text
    # If the input is already raw XML (e.g. straight from the rewriter
    # after _strip_codeblock), bypass markdown extraction. Otherwise an
    # inline mention like ``parser extracts XML from Markdown ```xml```
    # blocks`` inside an Observation's content would trigger fence
    # parsing, find no actual fences, and return empty — silently
    # nuking the trace. (Observed 2026-05-24 on scholia/cli.py.)
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return text
    parts: list[str] = []
    current_step_id: str | None = None
    current_step_name: str | None = None
    current_blocks: list[str] = []

    def _flush() -> None:
        nonlocal current_blocks
        if not current_blocks:
            return
        body = "\n".join(current_blocks)
        if current_step_id:
            name = _xml_escape(current_step_name or current_step_id)
            parts.append(f'<Step id="{current_step_id}" name="{name}">\n{body}\n</Step>')
        else:
            parts.append(body)
        current_blocks = []

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        heading = re.match(r"^##\s+Step\s+(\d+)\s+[—-]\s+(.+?)\s*$", lines[i])
        if heading:
            _flush()
            current_step_id = f"Step_{int(heading.group(1)):02d}"
            current_step_name = heading.group(2).strip()
            i += 1
            continue
        if lines[i].strip() == "```xml":
            i += 1
            block: list[str] = []
            while i < len(lines) and lines[i].strip() != "```":
                block.append(lines[i])
                i += 1
            current_blocks.append("\n".join(block).strip())
        i += 1
    _flush()
    extracted = "\n".join(parts)
    # Defensive fallback: if the markdown extraction yielded nothing
    # (no actual fences or headings, just an incidental ``` mention),
    # return the original text rather than blanking the trace.
    return extracted if extracted.strip() else text


_LEGACY_GATHER_OPEN_RE = re.compile(r"<GatherInput(?:_(?P<id>[A-Za-z0-9_]+))?>")
_LEGACY_GATHER_CLOSE_RE = re.compile(r"</GatherInput(?:_[A-Za-z0-9_]+)?>")


def _normalise_legacy_v01_tags(text: str) -> str:
    def _open(match: re.Match[str]) -> str:
        suffix = match.group("id")
        if suffix:
            return f'<Observation id="GatherInput_{suffix}">'
        return "<Observation>"

    text = _LEGACY_GATHER_OPEN_RE.sub(_open, text)
    return _LEGACY_GATHER_CLOSE_RE.sub("</Observation>", text)


_RESEARCH_MODE_TAG = "Meta_research_mode"


def _normalise_pseudo_atoms(text: str) -> str:
    return (
        text.replace("<Meta:research-mode/>", f"<{_RESEARCH_MODE_TAG}/>")
        .replace("<Meta:research-mode />", f"<{_RESEARCH_MODE_TAG}/>")
    )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ── Inline operator extraction ───────────────────────────────────────

# Match UPPERCASE operator tokens that optionally carry a ``:target``
# payload — e.g. ``REFER:Finding_03`` or a bare ``FORALL``. We do this
# after the tree parse so prose preserves the operator string in
# ``content`` and the list form appears on ``atom.operators``.
_OPERATOR_ALTERNATION = "|".join(re.escape(op) for op in OPERATORS)
_OPERATOR_RE = re.compile(
    r"\b(?P<op>" + _OPERATOR_ALTERNATION + r")\b(?::(?P<target>[A-Za-z0-9_]+))?"
)


def _extract_operators(content: str) -> list[str]:
    """Return the list of ``OP`` or ``OP:target`` tokens in ``content``."""
    found: list[str] = []
    for match in _OPERATOR_RE.finditer(content):
        tok = match.group("op")
        tgt = match.group("target")
        found.append(f"{tok}:{tgt}" if tgt else tok)
    return found


# ── Tree → AST ───────────────────────────────────────────────────────


def _text_of(elem: Element) -> str:
    """Return text + serialized child fragments for an atom body.

    ElementTree splits mixed content into ``text`` + ``tail`` on each
    child; for atoms whose body is prose with embedded XML-ish
    non-atom tags (``<bash>``, ``<git>``), we re-stringify them so
    the original content is preserved verbatim on the atom.
    """
    chunks: list[str] = [elem.text or ""]
    for child in list(elem):
        if child.tag in ATOM_KINDS:
            # Atom children are handled by the caller; their text is
            # skipped here. We keep the ``tail`` though — it is the
            # prose between atom children.
            chunks.append(child.tail or "")
            continue
        # Non-atom XML-ish embed — re-render as a string on the parent.
        chunks.append(ET.tostring(child, encoding="unicode", method="xml"))
        chunks.append(child.tail or "")
    return "".join(chunks).strip()


def _is_iso8601(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _parse_int_attr(kind: str, key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ScholiaParseError(
            f"<{kind}> attribute {key} must be an integer; got {value!r}."
        ) from exc


def _parse_float_attr(kind: str, key: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ScholiaParseError(
            f"<{kind}> attribute {key} must be a number; got {value!r}."
        ) from exc


_INT_ATTRS = {"sequence", "tokens", "actions", "wall_clock_ms"}
_FLOAT_ATTRS = {"dollars"}


def _coerce_attr(kind: str, key: str, value: str):
    if key in _INT_ATTRS:
        return _parse_int_attr(kind, key, value)
    if kind == "Concluding" and key == "confidence":
        return _parse_float_attr(kind, key, value)
    if key in _FLOAT_ATTRS:
        return _parse_float_attr(kind, key, value)
    return value


def _apply_attrs(atom: Atom, attrs: dict[str, str]) -> None:
    """Copy parsed element attributes onto ``atom`` fields.

    Unknown attributes are silently ignored; the v0.1 atom dataclasses
    define exactly the attribute set the spec describes, and strict
    rejection would make the parser hostile to future minor spec
    tweaks. The validator is the strict layer.

    v0.3.1: per-kind wire→field aliasing routes wire ``kind`` on
    ``<Effect>`` to the ``effect_kind`` field (since ``kind`` on the
    instance would shadow the ClassVar discriminator) and similarly
    routes wire ``type`` on ``<Edge>``/``<Ref>``. See
    ``KIND_SPECIFIC_FIELD_ALIASES`` in ``atoms.py``.
    """
    for wire_key, wire_val in attrs.items():
        if (
            atom.kind == "Finding"
            and wire_key == "for_goal"
            and "for_hyp" not in attrs
        ):
            py_key = "for_hyp"
        else:
            py_key = field_name_for(atom.kind, wire_key)
        if py_key == "kind":
            # Guard against accidentally shadowing the ClassVar
            # discriminator on any atom kind we haven't aliased yet.
            continue
        if hasattr(atom, py_key):
            setattr(atom, py_key, _coerce_attr(atom.kind, py_key, wire_val))


def _validate_attrs(kind: str, attrs: dict[str, str]) -> None:
    # Specific error messages fire FIRST so their precision is preserved
    # for callers/tests that key on them. The generic closed-set check
    # at the end catches everything else.
    if "timestamp" in attrs:
        # v0.7 — <Event> carries its own optional RFC3339 timestamp; its
        # timezone/possibility contract is a VALIDATE-phase rule
        # (event_timestamp_shape), so the parser accepts the raw string.
        if kind not in {"Observation", "Action", "Event"}:
            raise ScholiaParseError(
                "timestamp is only valid on <Observation>, <Action>, and <Event>."
            )
        if kind != "Event" and not _is_iso8601(attrs["timestamp"]):
            raise ScholiaParseError(
                f"<{kind}> timestamp must be ISO-8601; got {attrs['timestamp']!r}."
            )
    if "deadline" in attrs and not _is_iso8601(attrs["deadline"]):
        raise ScholiaParseError(
            f"<{kind}> deadline must be ISO-8601; got {attrs['deadline']!r}."
        )
    if "wall_clock" in attrs and not _is_iso8601(attrs["wall_clock"]):
        raise ScholiaParseError(
            f"<{kind}> wall_clock must be ISO-8601; got {attrs['wall_clock']!r}."
        )
    if kind == "Concluding" and not attrs.get("for_goal"):
        raise ScholiaParseError("<Concluding> requires for_goal.")
    _validate_v031_attrs(kind, attrs)
    # v0.3.1 — strict closed-set rejection of unknown wire attributes.
    # PRD V031-01 acceptance criterion #3: ``<Observation foo='bar'>``
    # must be invalid. Pre-fix, the parser silently ignored unknowns
    # and the validator never checked — the standards-body strict-
    # closed-set posture had a hole. Fixed here. Atoms whose paren-arg
    # form accepts arbitrary operator-supplied keys (Storing, Print)
    # skip this check; see ``_OPEN_NAMESPACE_KINDS`` above.
    if kind in SEMANTIC_KINDS:
        # v0.7 — the new kinds close their field set even harder than the
        # legacy posture: the parser-sidecar ``value`` attribute (a
        # paren-arg desugar artifact) is NOT legal on them.
        allowed = _ALLOWED_ATTRS_BY_KIND.get(kind, frozenset()) - _UNIVERSAL_WIRE_ATTRS
        unknown = set(attrs.keys()) - allowed
        if unknown:
            raise ScholiaParseError(
                f"<{kind}> has unknown field(s) {sorted(unknown)!r}; "
                f"the closed per-kind field set is {sorted(allowed)!r} "
                "(unknown fields on the v0.7 semantic kinds are rejected "
                "across XML, JSON, YAML, and dictionary input).",
                rule="semantic_unknown_field",
            )
    elif kind not in _OPEN_NAMESPACE_KINDS:
        allowed = _ALLOWED_ATTRS_BY_KIND.get(kind, frozenset())
        unknown = set(attrs.keys()) - allowed
        if unknown:
            raise ScholiaParseError(
                f"<{kind}> has unknown attribute(s) {sorted(unknown)!r}; "
                f"allowed wire attributes for this kind: {sorted(allowed)!r}. "
                f"v0.3.1 strict-closed-set posture — see "
                f"docs/scholia/SCHOLIA_v0.3.1_SPEC.md."
            )


def _validate_v031_attrs(kind: str, attrs: dict[str, str]) -> None:
    """v0.3.1 — closed-set validation for the reserved primitive hooks.

    Parser-side rejects malformed closed-set values immediately. The
    validator carries a defensive mirror for traces reconstituted from
    JSON/YAML (see ``check_v031_optional_fields`` in ``validator.py``).
    """
    if kind == "Observation":
        location = attrs.get("location")
        if location is not None and not V031_LOCATION_RE.match(location):
            raise ScholiaParseError(
                f"<Observation> location must match 'file:start:end'; "
                f"got {location!r}."
            )
        confidence = attrs.get("confidence")
        if confidence is not None:
            try:
                value = float(confidence)
            except ValueError as exc:
                raise ScholiaParseError(
                    f"<Observation> confidence must be a float in [0.0, 1.0]; "
                    f"got {confidence!r}."
                ) from exc
            if not 0.0 <= value <= 1.0:
                raise ScholiaParseError(
                    f"<Observation> confidence must be in [0.0, 1.0]; "
                    f"got {value}."
                )
    elif kind == "Edge":
        edge_type = attrs.get("type")
        if edge_type is not None and edge_type not in V031_EDGE_TYPES:
            raise ScholiaParseError(
                f"<Edge> type must be one of {sorted(V031_EDGE_TYPES)}; "
                f"got {edge_type!r}."
            )
    elif kind == "Effect":
        effect_kind = attrs.get("kind")
        if effect_kind is not None and effect_kind not in V031_EFFECT_KINDS:
            raise ScholiaParseError(
                f"<Effect> kind must be one of {sorted(V031_EFFECT_KINDS)}; "
                f"got {effect_kind!r}."
            )
    elif kind == "Ref":
        ref_type = attrs.get("type")
        if ref_type is not None and ref_type not in V031_REF_TYPES:
            raise ScholiaParseError(
                f"<Ref> type must be one of {sorted(V031_REF_TYPES)}; "
                f"got {ref_type!r}."
            )
    elif kind == "Meta":
        criticality = attrs.get("criticality")
        if (
            criticality is not None
            and criticality not in V031_META_CRITICALITIES
        ):
            raise ScholiaParseError(
                f"<Meta> criticality must be one of "
                f"{sorted(V031_META_CRITICALITIES)}; got {criticality!r}."
            )
    elif kind in {"Goal", "Concluding"}:
        criticality = attrs.get("criticality")
        if (
            criticality is not None
            and criticality not in V031_META_CRITICALITIES
        ):
            raise ScholiaParseError(
                f"<{kind}> criticality must be one of "
                f"{sorted(V031_META_CRITICALITIES)}; got {criticality!r}."
            )
        if kind == "Concluding":
            confidence = attrs.get("confidence")
            if confidence is not None:
                try:
                    value = float(confidence)
                except ValueError as exc:
                    raise ScholiaParseError(
                        "<Concluding> confidence must be a float in [0.0, 1.0]; "
                        f"got {confidence!r}."
                    ) from exc
                if not 0.0 <= value <= 1.0:
                    raise ScholiaParseError(
                        "<Concluding> confidence must be in [0.0, 1.0]; "
                        f"got {value}."
                    )


def _decode_map_entries_attr(raw: str) -> dict[str, object]:
    """Decode the XML ``entries`` attribute — an embedded JSON object.

    v0.7 parse-phase contract (rule ``map_entries_shape``): the value
    must decode to a JSON object; duplicate keys and nonfinite tokens
    (``NaN`` / ``Infinity``) are rejected while decoding, before they
    can silently destroy information. Typed-value violations (floats,
    nulls, arrays, …) are validate-phase and pass through here.
    """

    def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, value in pairs:
            if key in decoded:
                raise ScholiaParseError(
                    f"<Map> entries contains duplicate key {key!r}; keys "
                    "must be unique — duplicates are rejected while "
                    "decoding rather than silently overwritten.",
                    rule="map_entries_shape",
                )
            decoded[key] = value
        return decoded

    def _reject_nonfinite(token: str) -> object:
        raise ScholiaParseError(
            f"<Map> entries contains the nonfinite JSON token {token!r}; "
            "NaN and infinities are not interoperable JSON values.",
            rule="map_entries_shape",
        )

    try:
        decoded = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_reject_nonfinite
        )
    except ScholiaParseError:
        raise
    except ValueError as exc:
        raise ScholiaParseError(
            f"<Map> entries must be a JSON object in the XML attribute; "
            f"decoding failed: {exc}",
            rule="map_entries_shape",
        ) from exc
    if not isinstance(decoded, dict):
        raise ScholiaParseError(
            f"<Map> entries must be a JSON object mapping string keys to "
            f"values; got {type(decoded).__name__}.",
            rule="map_entries_shape",
        )
    return decoded


def _build_pseudo_atom(kind: str) -> Atom:
    atom = Atom()
    atom.kind = kind
    return atom


def _build_atom(elem: Element, *, parent_kind: str | None = None) -> Atom:
    """Instantiate the right atom dataclass for ``elem``."""
    kind = "Meta:research-mode" if elem.tag == _RESEARCH_MODE_TAG else elem.tag
    if kind in PSEUDO_ATOM_KINDS:
        return _build_pseudo_atom(kind)
    cls = atom_class_for_kind(kind)
    if cls is None:
        raise ScholiaParseError(
            f"Unknown Scholia atom: <{kind}> (not in v0.2 catalog).",
            rule="unknown_kind",
        )
    if kind == "Alternative" and parent_kind != "Deciding":
        raise ScholiaParseError("<Alternative> is only valid inside <Deciding>.")
    _validate_attrs(kind, dict(elem.attrib))
    if kind in SEMANTIC_KINDS and len(list(elem)):
        # v0.7 — new kinds carry no nested child atoms (or any child
        # elements) in this revision; relationships use explicit
        # reference fields instead.
        raise ScholiaParseError(
            f"<{kind}> must have empty children — the v0.7 semantic kinds "
            "carry no nested child atoms; use explicit reference fields.",
            rule="semantic_children_empty",
        )

    if cls is Concluding:
        try:
            atom = Concluding(
                for_goal=elem.attrib.get("for_goal"),
                confidence=(
                    _parse_float_attr(kind, "confidence", elem.attrib["confidence"])
                    if "confidence" in elem.attrib
                    else None
                ),
                criticality=elem.attrib.get("criticality"),
                status=elem.attrib.get("status"),
            )
        except ValueError as exc:
            raise ScholiaParseError(str(exc)) from exc
        atom.id = elem.attrib.get("id")
    else:
        atom: Atom = cls()  # every atom dataclass has all-default fields
        _apply_attrs(atom, dict(elem.attrib))
        atom.id = elem.attrib.get("id")
    if isinstance(atom, Storing) and atom.value and not atom.name:
        atom.name = atom.value
    if isinstance(atom, Map) and isinstance(atom.entries, str):
        # v0.7 — the XML wire carries entries as an embedded JSON object;
        # decode (rejecting duplicates/nonfinite tokens) before hashing.
        atom.entries = _decode_map_entries_attr(atom.entries)
    atom.content = _text_of(elem)
    atom.operators = _extract_operators(atom.content)

    # Recurse into atom children (non-atom tags were absorbed as text).
    for child in list(elem):
        child_kind = (
            "Meta:research-mode" if child.tag == _RESEARCH_MODE_TAG else child.tag
        )
        if child_kind not in ATOM_KINDS and child_kind not in PSEUDO_ATOM_KINDS:
            continue
        atom.children.append(_build_atom(child, parent_kind=kind))

    # Post-process kind-specific content that doesn't live in
    # attributes — e.g. ``<Deciding>`` options list embedded as
    # ``options = LIST:\n  - ...`` in the prose body. v0.1 keeps this
    # best-effort: structured options on the atom if we can find them,
    # otherwise callers still have the raw content string.
    if isinstance(atom, Deciding):
        atom = _desugar_deciding(atom, elem)
    elif isinstance(atom, Goal):
        _populate_goal_fields(atom)
    elif isinstance(atom, Handoff):
        atom.constraints = (
            _extract_list_options(atom.content) or atom.constraints
        )

    # v0.6 — stamp the content-addressable canonical_id now that content,
    # structured attributes, and children are all populated. The hash is
    # computed over {kind, content, attrs} (children excluded — see
    # ``compute_canonical_id``), so it is stable even after Deciding
    # short-form desugaring, which only adds children. A claimed
    # canonical_id on the wire that does NOT match the recomputed hash is
    # preserved verbatim so the validator's ``canonical_id_well_formed``
    # rule can surface the tamper; otherwise the computed hash is stamped.
    _claimed_cid = elem.attrib.get("canonical_id")
    _computed_cid = compute_canonical_id(atom)
    atom.canonical_id = (
        _claimed_cid
        if (_claimed_cid and _claimed_cid != _computed_cid)
        else _computed_cid
    )
    return atom


def _desugar_deciding(atom: Deciding, elem: Element) -> Deciding:
    options_attr = elem.attrib.get("options")
    chose = elem.attrib.get("chose")
    if options_attr is None and chose is None:
        atom.options = _extract_list_options(atom.content) or atom.options
        return atom
    if not options_attr or not chose:
        raise ScholiaParseError(
            "<Deciding> short form requires both options and chose attributes."
        )
    options = [part.strip() for part in options_attr.split(",") if part.strip()]
    if chose not in options:
        raise ScholiaParseError(
            f"<Deciding> chose={chose!r} must match one of options={options!r}."
        )
    rationale = atom.content.strip()
    atom.options = options
    atom.content = ""
    for option in options:
        atom.children.append(Branch(of=atom.id, label=option))
    finding_text = f"chose {chose}"
    if rationale:
        finding_text = f"{finding_text}: {rationale}"
    atom.children.append(Finding(content=finding_text))
    return atom


_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)


def _extract_list_options(content: str) -> list[str]:
    """Pull ``- item`` lines from ``content`` as a flat option list."""
    items: list[str] = []
    for match in _LIST_ITEM_RE.finditer(content):
        raw = match.group(1).strip()
        if (
            len(raw) >= 2
            and raw[0] == raw[-1]
            and raw[0] in ('"', "'")
        ):
            raw = raw[1:-1]
        items.append(raw)
    return items


def _extract_named_list(content: str, name: str) -> list[str]:
    inline_re = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*LIST:\[(?P<body>[^\]]*)\]\s*$",
        re.MULTILINE,
    )
    inline = inline_re.search(content)
    if inline:
        items: list[str] = []
        for raw in inline.group("body").split(","):
            item = raw.strip()
            if item.startswith("REFER:"):
                item = item.removeprefix("REFER:")
            if item:
                items.append(item)
        return items

    items: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if not re.match(rf"^\s*{re.escape(name)}\s*=\s*LIST:\s*$", line):
            continue
        for item_line in lines[i + 1 :]:
            match = re.match(r"^\s*-\s+(.+?)\s*$", item_line)
            if not match:
                break
            item = match.group(1).strip()
            if (
                len(item) >= 2
                and item[0] == item[-1]
                and item[0] in ('"', "'")
            ):
                item = item[1:-1]
            items.append(item)
        break
    return items


def _extract_named_scalar(content: str, name: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(name)}\s*=\s*\"?(?P<value>[^\"\n]+)\"?\s*$",
        content,
        re.MULTILINE,
    )
    if not match:
        return None
    return match.group("value").strip()


def _populate_goal_fields(atom: Goal) -> None:
    atom.success_criteria = (
        _extract_named_list(atom.content, "success_criteria")
        or atom.success_criteria
    )
    atom.related_constraints = (
        _extract_named_list(atom.content, "related_constraints")
        or atom.related_constraints
    )
    atom.failure_modes = (
        _extract_named_list(atom.content, "failure_modes")
        or atom.failure_modes
    )
    deadline = _extract_named_scalar(atom.content, "deadline")
    if deadline and not atom.deadline:
        if not _is_iso8601(deadline):
            raise ScholiaParseError(
                f"<Goal> deadline must be ISO-8601; got {deadline!r}."
            )
        atom.deadline = deadline


def _build_step(elem: Element) -> Step:
    """Instantiate a ``Step`` from an ``<Step>`` element.

    Top-level children of a ``<Step>`` must be valid atoms. Unknown
    tags at this level are the common agent-author mistake we want
    to surface loudly, so they raise here; the tree walk below
    (``_build_atom``) already rejects unknown kinds.
    """
    if elem.tag != "Step":
        raise ScholiaParseError(
            f"Expected <Step> at top level; got <{elem.tag}>."
        )
    step = Step(
        id=elem.attrib.get("id"),
        name=elem.attrib.get("name"),
        atoms=[],
    )
    for child in list(elem):
        if child.tag.startswith(_ELEM_TREE_NAMESPACE):
            continue
        child_kind = (
            "Meta:research-mode" if child.tag == _RESEARCH_MODE_TAG else child.tag
        )
        if child_kind not in ATOM_KINDS and child_kind not in PSEUDO_ATOM_KINDS:
            raise ScholiaParseError(
                f"Unknown Scholia atom: <{child.tag}> "
                "(not in v0.2 catalog).",
                rule="unknown_kind",
            )
        step.atoms.append(_build_atom(child))
    return step


# ElementTree prefixes namespaced tags with ``{ns}``; we don't use
# namespaces in v0.1 but the token exists so we can skip prefixed
# tags without false-positive-rejecting them.
_ELEM_TREE_NAMESPACE = "{"


# ── Public API ───────────────────────────────────────────────────────


def parse(text: str) -> list[Step]:
    """Parse a Scholia XML-ish trace string into a list of Steps.

    The input may be either a single ``<Scholia>`` root wrapping many
    steps, or a bare sequence of ``<Step>`` elements. The parser
    tolerates both forms because agent output rarely carries a
    redundant outer wrapper but spec-fixture files often do.
    """
    if not isinstance(text, str):
        raise ScholiaParseError("Scholia input must be a string.")
    cleaned = _extract_xml_fences(text)
    cleaned = _strip_comments(cleaned)
    cleaned = _normalise_pseudo_atoms(cleaned)
    cleaned = _normalise_legacy_v01_tags(cleaned)
    cleaned = _normalise_paren_args(cleaned)
    wrapped = f"<__scholia_root__>{cleaned}</__scholia_root__>"
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError as exc:
        raise ScholiaParseError(
            f"Malformed Scholia XML-ish input: {exc}"
        ) from exc

    steps: list[Step] = []
    for child in list(root):
        tag = child.tag
        if tag == "Step":
            steps.append(_build_step(child))
            continue
        if tag == "Scholia":
            for grandchild in list(child):
                if grandchild.tag != "Step":
                    continue
                steps.append(_build_step(grandchild))
            continue
        if tag == _RESEARCH_MODE_TAG:
            implicit = Step(id=None, name=None, atoms=[_build_atom(child)])
            steps.append(implicit)
            continue
        if tag in ATOM_KINDS:
            # Bare atoms outside a Step — wrap in an implicit Step so
            # downstream code always sees ``list[Step]``.
            implicit = Step(id=None, name=None, atoms=[_build_atom(child)])
            steps.append(implicit)
            continue
        raise ScholiaParseError(
            f"Unknown Scholia atom: <{tag}> (not in v0.2 catalog).",
            rule="unknown_kind",
        )
    return steps


def parse_atom(text: str) -> Atom:
    """Parse a single atom fragment (no ``<Step>`` wrapper).

    Useful for tests + for callers that want to embed an atom inside
    other structures. Raises ``ScholiaParseError`` if the input
    contains more than one top-level atom.
    """
    steps = parse(text)
    if len(steps) != 1 or len(steps[0].atoms) != 1:
        raise ScholiaParseError(
            "parse_atom expects exactly one top-level atom."
        )
    return steps[0].atoms[0]
