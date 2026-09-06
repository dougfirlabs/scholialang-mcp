"""Run scholialang against scholialang-spec examples and rule fixtures.

v0.7.3 adds an explicit decoder for the self-describing semantic suite
(``suite_schema: semantic-atoms.v1`` under ``conformance/v0.7/``). It
dispatches parse-phase versus validate-phase negative expectations
correctly and FAILS CLOSED on: an unknown conformance JSON schema, a
missing required semantic suite (``--require-semantic-suite``),
duplicate case IDs, a mismatched declared count, an unsupported input
format, or an unknown expected rule. Parse failures are never
translated into generic successful negative outcomes — a negative
parse case must fail in the parse phase with its declared rule.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scholialang.atoms import (
    KIND_SPECIFIC_FIELDS,
    SEMANTIC_KINDS,
    SemanticShapeError,
    atom_to_xml,
)
from scholialang.parser import ScholiaParseError, parse
from scholialang.serializer import (
    from_json,
    from_yaml,
    to_canonical_json,
    to_json,
    to_yaml,
    trace_from_dict,
)
from scholialang.validator import RULE_NAMES, validate


@dataclass(frozen=True)
class _FixtureGraph:
    edges: tuple[dict[str, str], ...]

    def has_edge(
        self,
        *,
        edge_type: str,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> bool:
        return any(
            edge.get("relation") == edge_type
            and (source_id is None or edge.get("source_id") == source_id)
            and (target_id is None or edge.get("target_id") == target_id)
            for edge in self.edges
        )


def _run_rule_manifest(path: Path) -> tuple[int, list[str]]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, [f"{path}: raised {exc!r}"]

    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        return 0, [f"{path}: top-level 'cases' must be a list"]

    failures: list[str] = []
    checked = 0
    for case in cases:
        checked += 1
        if not isinstance(case, dict):
            failures.append(f"{path}: case #{checked} must be an object")
            continue
        case_id = str(case.get("id") or f"case-{checked}")
        expects = case.get("expects")
        if not isinstance(expects, dict):
            failures.append(f"{path}:{case_id}: missing expects object")
            continue
        rule = str(expects.get("rule") or "")
        expected_outcome = str(expects.get("outcome") or "")
        expected_count = expects.get("error_count")
        try:
            trace = parse(str(case.get("trace") or ""))
            raw_edges = case.get("graph_edges") or []
            if not isinstance(raw_edges, list) or not all(
                isinstance(edge, dict) for edge in raw_edges
            ):
                raise TypeError("graph_edges must be a list of objects")
            graph = _FixtureGraph(
                tuple(
                    {
                        "source_id": str(edge.get("source_id") or ""),
                        "target_id": str(edge.get("target_id") or ""),
                        "relation": str(edge.get("relation") or ""),
                    }
                    for edge in raw_edges
                )
            )
            result = validate(trace, graph=graph)
        except Exception as exc:  # pragma: no cover - surfaced as CLI output.
            failures.append(f"{path}:{case_id}: raised {exc!r}")
            continue

        if rule not in result.errors_by_rule:
            failures.append(f"{path}:{case_id}: unknown rule {rule!r}")
            continue
        rule_errors = result.errors_by_rule[rule]
        actual_outcome = "pass" if not rule_errors else "fail"
        if expected_outcome not in {"pass", "fail"}:
            failures.append(
                f"{path}:{case_id}: invalid expected outcome {expected_outcome!r}"
            )
        elif actual_outcome != expected_outcome:
            failures.append(
                f"{path}:{case_id}: expected {rule}={expected_outcome}, "
                f"got {actual_outcome}: {rule_errors}"
            )
        if not isinstance(expected_count, int):
            failures.append(
                f"{path}:{case_id}: expects.error_count must be an integer"
            )
        elif len(rule_errors) != expected_count:
            failures.append(
                f"{path}:{case_id}: expected {expected_count} {rule} errors, "
                f"got {len(rule_errors)}: {rule_errors}"
            )
        expected_atom_ids = expects.get("atom_ids")
        if expected_atom_ids is not None:
            actual_atom_ids = [error.atom_id for error in rule_errors]
            if actual_atom_ids != expected_atom_ids:
                failures.append(
                    f"{path}:{case_id}: expected atom_ids={expected_atom_ids!r}, "
                    f"got {actual_atom_ids!r}"
                )
    return checked, failures


# ── v0.7 — self-describing semantic-atoms suite decoder ──────────────

_SEMANTIC_SUITE_SCHEMA = "semantic-atoms.v1"
_COVERAGE_SCHEMA = "semantic-atoms.coverage.v1"
_SUPPORTED_CASE_FORMATS = ("xml", "json", "yaml", "dict")

# Rules the implementation can produce: every validator rule name plus
# the parse-phase-only codes raised by the parser/decoders.
_KNOWN_RULES: frozenset[str] = frozenset(RULE_NAMES) | {
    "unknown_kind",
    "semantic_unknown_field",
}


def _decode_case_payload(fmt: str, payload: Any):
    """Decode one case payload in its declared format (the parse phase)."""
    if fmt == "xml":
        return parse(str(payload))
    if fmt == "json":
        return from_json(payload if isinstance(payload, str) else json.dumps(payload))
    if fmt == "yaml":
        return from_yaml(str(payload))
    if fmt == "dict":
        # Deep-copy: decoding must never mutate the suite's shared object.
        return trace_from_dict(copy.deepcopy(payload))
    raise AssertionError(f"unsupported case format {fmt!r}")


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _trace_to_xml(trace) -> str:
    """Re-emit a parsed trace as XML for the round-trip chain."""
    parts: list[str] = []
    for step in trace:
        attrs = ""
        if step.id:
            attrs += f' id="{_xml_escape(step.id)}"'
        if step.name:
            attrs += f' name="{_xml_escape(step.name)}"'
        body = "\n".join(atom_to_xml(a) for a in step.atoms)
        parts.append(f"<Step{attrs}>\n{body}\n</Step>")
    return "\n".join(parts)


def _atom_semantic_view(atom) -> dict[str, Any]:
    """Comparable semantic projection of one atom.

    ``canonical_id`` is compared for the v0.7 semantic kinds (whose
    serializers retain it on every wire) — legacy kinds keep their
    existing lossy-on-JSON behavior and are compared without it.
    """
    view: dict[str, Any] = {
        "kind": atom.kind,
        "id": atom.id,
        "content": atom.content,
        "operators": list(atom.operators),
        "fields": {
            f: getattr(atom, f, None) for f in KIND_SPECIFIC_FIELDS.get(atom.kind, ())
        },
        "children": [_atom_semantic_view(c) for c in atom.children],
    }
    if atom.kind in SEMANTIC_KINDS:
        view["canonical_id"] = atom.canonical_id
    return view


def _trace_semantic_view(trace) -> list[dict[str, Any]]:
    return [
        {"id": s.id, "atoms": [_atom_semantic_view(a) for a in s.atoms]}
        for s in trace
    ]


def _roundtrip_failures(case_id: str, trace) -> list[str]:
    """XML→JSON→YAML→XML semantic-equality + canonical-JSON stability."""
    failures: list[str] = []
    baseline = _trace_semantic_view(trace)
    canonical = to_canonical_json(trace)
    stages = []
    trace_json = from_json(to_json(trace))
    stages.append(("json", trace_json))
    trace_yaml = from_yaml(to_yaml(trace_json))
    stages.append(("yaml", trace_yaml))
    trace_xml = parse(_trace_to_xml(trace_yaml))
    stages.append(("xml", trace_xml))
    for stage_name, stage_trace in stages:
        view = _trace_semantic_view(stage_trace)
        if view != baseline:
            failures.append(
                f"{case_id}: round-trip through {stage_name} lost semantic "
                f"equality"
            )
        if to_canonical_json(stage_trace) != canonical:
            failures.append(
                f"{case_id}: canonical JSON is not stable after the "
                f"{stage_name} round-trip"
            )
    return failures


def _find_atom(traces: list[Any], local_id: str):
    for trace in traces:
        for step in trace:
            stack = list(step.atoms)
            while stack:
                atom = stack.pop(0)
                if atom.id == local_id:
                    return atom
                stack.extend(atom.children)
    return None


def _run_semantic_case(case: dict[str, Any]) -> list[str]:
    """Execute one semantic-suite case; return failure strings."""
    case_id = str(case.get("id") or "?")
    fmt = str(case.get("format") or "")
    if fmt not in _SUPPORTED_CASE_FORMATS:
        return [f"{case_id}: unsupported input format {fmt!r}"]
    category = str(case.get("category") or "")
    expects = case.get("expects")
    if not isinstance(expects, dict):
        return [f"{case_id}: missing expects object"]

    if category == "negative":
        return _run_negative_case(case_id, fmt, case, expects)
    if category == "positive":
        return _run_positive_case(case_id, fmt, case, expects)
    return [f"{case_id}: unknown category {category!r}"]


def _run_negative_case(
    case_id: str, fmt: str, case: dict[str, Any], expects: dict[str, Any]
) -> list[str]:
    phase = str(expects.get("phase") or "")
    rule = str(expects.get("rule") or "")
    mentions = [str(m) for m in expects.get("diagnostic_must_mention", [])]
    if rule not in _KNOWN_RULES:
        return [f"{case_id}: unknown expected rule {rule!r} — failing closed"]
    if phase not in {"parse", "validate"}:
        return [f"{case_id}: unknown expected phase {phase!r} — failing closed"]

    if phase == "parse":
        try:
            _decode_case_payload(fmt, case.get("payload"))
        except (ScholiaParseError, SemanticShapeError, ValueError) as exc:
            actual_rule = getattr(exc, "rule", None)
            message = str(exc)
            failures: list[str] = []
            if actual_rule != rule:
                failures.append(
                    f"{case_id}: expected parse rule {rule!r}, got "
                    f"{actual_rule!r} ({message})"
                )
            for mention in mentions:
                if mention not in message:
                    failures.append(
                        f"{case_id}: parse diagnostic must mention "
                        f"{mention!r}; got: {message}"
                    )
            return failures
        return [f"{case_id}: expected a parse-phase failure ({rule}), but parsing succeeded"]

    # validate phase — the input must parse, then fail the declared rule.
    try:
        trace = _decode_case_payload(fmt, case.get("payload"))
    except Exception as exc:
        return [
            f"{case_id}: expected a validate-phase failure ({rule}) but the "
            f"input failed to parse: {exc!r}"
        ]
    result = validate(trace, profile=case.get("profile"))
    rule_errors = result.errors_by_rule.get(rule)
    if rule_errors is None:
        return [f"{case_id}: rule {rule!r} unknown to the validator — failing closed"]
    if not rule_errors:
        produced = sorted({e.rule for e in result.errors})
        return [
            f"{case_id}: expected validate rule {rule!r} to fire; produced "
            f"rules: {produced or 'none (trace valid)'}"
        ]
    failures = []
    if mentions and not any(
        all(m in error.message for m in mentions) for error in rule_errors
    ):
        failures.append(
            f"{case_id}: no {rule} diagnostic mentions all of {mentions!r}; "
            f"got: {[e.message for e in rule_errors]}"
        )
    return failures


def _run_positive_case(
    case_id: str, fmt: str, case: dict[str, Any], expects: dict[str, Any]
) -> list[str]:
    try:
        trace = _decode_case_payload(fmt, case.get("payload"))
    except Exception as exc:
        return [f"{case_id}: positive case failed to parse: {exc!r}"]
    traces = [trace]
    if case.get("payload_b") is not None:
        try:
            traces.append(_decode_case_payload(fmt, case.get("payload_b")))
        except Exception as exc:
            return [f"{case_id}: payload_b failed to parse: {exc!r}"]
    failures: list[str] = []
    for i, one_trace in enumerate(traces):
        result = validate(one_trace, profile=case.get("profile"))
        if not result.ok:
            failures.append(
                f"{case_id}: positive payload{'_b' if i else ''} failed "
                f"validation: {[(e.rule, e.message) for e in result.errors]}"
            )
    if failures:
        return failures
    if expects.get("roundtrip"):
        for one_trace in traces:
            failures.extend(_roundtrip_failures(case_id, one_trace))
    identity = case.get("identity")
    if isinstance(identity, dict):
        relation = identity.get("relation")
        cross_trace = identity.get("cross_trace_atom")
        if cross_trace is not None:
            # One local id compared ACROSS payload / payload_b.
            atoms = [_find_atom([t], str(cross_trace)) for t in traces]
            names = [str(cross_trace)] * len(traces)
        else:
            names = [str(n) for n in identity.get("atoms", [])]
            atoms = [_find_atom(traces, n) for n in names]
        found = [a for a in atoms if a is not None]
        if len(found) != len(atoms) or not found:
            failures.append(
                f"{case_id}: identity atoms {names!r} not all found"
            )
        else:
            cids = [a.canonical_id for a in found]
            if relation == "equal" and len(set(cids)) != 1:
                failures.append(
                    f"{case_id}: expected equal canonical ids, got {cids!r}"
                )
            if relation == "distinct" and len(set(cids)) != len(cids):
                failures.append(
                    f"{case_id}: expected distinct canonical ids, got {cids!r}"
                )
    return failures


def run_semantic_suite(
    suite_path: Path, inventory_path: Path | None = None
) -> tuple[int, list[str]]:
    """Execute every case of a ``semantic-atoms.v1`` suite. Fail closed."""
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, [f"{suite_path}: raised {exc!r}"]
    if not isinstance(suite, dict) or suite.get("suite_schema") != _SEMANTIC_SUITE_SCHEMA:
        return 0, [
            f"{suite_path}: unknown suite schema "
            f"{suite.get('suite_schema') if isinstance(suite, dict) else None!r}"
        ]
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        return 0, [f"{suite_path}: suite carries no cases"]

    failures: list[str] = []
    ids = [str(c.get("id") or "") for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        failures.append(f"{suite_path}: duplicate case IDs {duplicates!r}")

    coverage = suite.get("coverage") or {}
    declared_total = coverage.get("total_cases")
    if declared_total != len(cases):
        failures.append(
            f"{suite_path}: declared total_cases={declared_total!r} but "
            f"{len(cases)} cases present"
        )
    declared_positive = coverage.get("positive")
    declared_negative = coverage.get("negative")
    actual_positive = sum(1 for c in cases if c.get("category") == "positive")
    actual_negative = sum(1 for c in cases if c.get("category") == "negative")
    if declared_positive != actual_positive or declared_negative != actual_negative:
        failures.append(
            f"{suite_path}: declared positive/negative "
            f"{declared_positive}/{declared_negative} but found "
            f"{actual_positive}/{actual_negative}"
        )

    if inventory_path is not None and inventory_path.is_file():
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            inventory = None
            failures.append(f"{inventory_path}: raised {exc!r}")
        if isinstance(inventory, dict):
            if inventory.get("schema") != _COVERAGE_SCHEMA:
                failures.append(
                    f"{inventory_path}: unknown coverage schema "
                    f"{inventory.get('schema')!r}"
                )
            pinned_ids = inventory.get("case_ids")
            if isinstance(pinned_ids, list) and [str(i) for i in pinned_ids] != ids:
                failures.append(
                    f"{inventory_path}: pinned case-ID inventory does not "
                    "match the suite's ordered case IDs — coverage may not "
                    "shrink or reorder silently"
                )

    if failures:
        return 0, failures

    checked = 0
    for case in cases:
        checked += 1
        failures.extend(_run_semantic_case(case))
    return checked, failures


def _run_fingerprint_fixtures(spec_dir: Path) -> tuple[int, list[str]]:
    """Consume the shared ``fingerprint=`` corpus (docs/scholia/FINGERPRINT.md).

    The fixtures live ONCE in scholialang-spec at
    ``tests/fixtures/fingerprint/`` — this harness consumes that single copy,
    no fork. Only the NOTATION layer is executable here: ``notation_valid`` is
    exactly the ``fingerprint_well_formed`` outcome. Consumer-layer verdicts
    (rebinds / span_mismatch / stale) recompute the digest over source using
    52X-B2's single definition and are NOT decided by a notation validator.

    Absent corpus → ``(0, [])`` (nothing to check; the attribute is a proposal
    and older spec checkouts predate it), never a hard failure.
    """
    manifest_path = spec_dir / "tests" / "fixtures" / "fingerprint" / "manifest.yaml"
    if not manifest_path.is_file():
        return 0, []
    try:
        import yaml  # lazy — only needed when the corpus is present.

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - surfaced as CLI output.
        return 0, [f"{manifest_path}: raised {exc!r}"]

    fixtures = manifest.get("fixtures") if isinstance(manifest, dict) else None
    if not isinstance(fixtures, list):
        return 0, [f"{manifest_path}: top-level 'fixtures' must be a list"]

    fixture_dir = manifest_path.parent
    failures: list[str] = []
    checked = 0
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            failures.append(f"{manifest_path}: a fixture entry must be an object")
            continue
        name = str(fixture.get("name") or "?")
        trace_rel = fixture.get("trace")
        notation_valid = fixture.get("notation_valid")
        if not isinstance(trace_rel, str) or not isinstance(notation_valid, bool):
            failures.append(
                f"{manifest_path}:{name}: needs string 'trace' + bool 'notation_valid'"
            )
            continue
        checked += 1
        try:
            trace = parse((fixture_dir / trace_rel).read_text(encoding="utf-8"))
            result = validate(trace)
        except Exception as exc:  # pragma: no cover - surfaced as CLI output.
            failures.append(f"{manifest_path}:{name}: raised {exc!r}")
            continue
        fp_errors = result.errors_by_rule.get("fingerprint_well_formed", [])
        actual_valid = not fp_errors
        if actual_valid != notation_valid:
            failures.append(
                f"{manifest_path}:{name}: expected notation_valid={notation_valid}, "
                f"got {actual_valid}: {[e.message for e in fp_errors]}"
            )
    return checked, failures


def run(spec_dir: Path, *, require_semantic_suite: bool = False) -> int:
    examples = sorted((spec_dir / "examples").glob("**/*.xml"))
    if not examples:
        print(f"no XML examples found under {spec_dir / 'examples'}", file=sys.stderr)
        return 1

    failures: list[str] = []
    for path in examples:
        try:
            trace = parse(path.read_text(encoding="utf-8"))
            result = validate(trace)
        except Exception as exc:  # pragma: no cover - surfaced as CLI output.
            failures.append(f"{path}: raised {exc!r}")
            continue
        if not result.ok:
            failures.append(f"{path}: {result.errors}")

    fixture_count = 0
    semantic_count = 0
    manifests = sorted((spec_dir / "conformance").glob("**/*.json"))
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{manifest}: raised {exc!r}")
            continue
        schema = payload.get("suite_schema") if isinstance(payload, dict) else None
        plain_schema = payload.get("schema") if isinstance(payload, dict) else None
        if schema == _SEMANTIC_SUITE_SCHEMA:
            checked, semantic_failures = run_semantic_suite(
                manifest, manifest.parent / "coverage-inventory.json"
            )
            semantic_count += checked
            failures.extend(semantic_failures)
        elif plain_schema == _COVERAGE_SCHEMA:
            # Consumed as the pinned inventory by run_semantic_suite.
            continue
        elif isinstance(payload, dict) and isinstance(payload.get("cases"), list):
            checked, manifest_failures = _run_rule_manifest(manifest)
            fixture_count += checked
            failures.extend(manifest_failures)
        else:
            failures.append(
                f"{manifest}: unknown conformance JSON schema — failing closed"
            )

    if require_semantic_suite and semantic_count == 0:
        failures.append(
            f"{spec_dir}: the semantic-atoms suite is required but no "
            f"'{_SEMANTIC_SUITE_SCHEMA}' suite was found/executed under "
            f"{spec_dir / 'conformance'}"
        )

    fingerprint_count, fingerprint_failures = _run_fingerprint_fixtures(spec_dir)
    failures.extend(fingerprint_failures)

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(
        f"validated {len(examples)} scholialang-spec examples, "
        f"{fixture_count} rule fixtures, "
        f"{semantic_count} semantic-suite cases, and "
        f"{fingerprint_count} fingerprint fixtures"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec_dir", type=Path)
    parser.add_argument(
        "--require-semantic-suite",
        action="store_true",
        help=(
            "fail closed when the conformance tree carries no "
            "semantic-atoms.v1 suite (v0.7 consumer gate)"
        ),
    )
    args = parser.parse_args(argv)
    return run(
        args.spec_dir.resolve(),
        require_semantic_suite=args.require_semantic_suite,
    )


if __name__ == "__main__":
    raise SystemExit(main())
