# Scholia v0.6.2 conformance fixtures

This directory is the shared cross-implementation contract for Scholia
v0.6.2 validator changes. Consumers should load the JSON suite, parse
each `trace`, adapt `graph_edges` to their validator's optional graph
view, run the named rule, and compare the rule-local errors with
`expects`.

## `action_recorded.json`

Rule 4 accepts an `<Action>` when at least one of these result forms is
present:

1. a nested descendant result `<Finding>` or `<Concluding>` at any
   depth;
2. an immediate result sibling in the same `<Step>`;
3. a later same-trace `<Finding>` that directly `REFER`s the Action;
4. a later same-trace `<Finding>` that `REFER`s an `<Observation>` or
   `<Evidence>` which directly `REFER`s the Action;
5. a later same-trace `<Concluding>` that directly `REFER`s the Action
   and declares `for_goal`; or
6. a graph edge whose `relation` is `records_result` and whose
   `target_id` is the Action id.

For non-immediate results, chronological order alone is insufficient.
The result must carry an explicit provenance link or be represented by
the graph edge.

`graph_edges` uses a transport-neutral shape:

```json
{
  "source_id": "F_01",
  "target_id": "Act_01",
  "relation": "records_result"
}
```

An adapter may expose this as a graph object, callback, or indexed edge
set. The validator contract is only that it can answer whether a
`records_result` edge targets the Action; the fixture format does not
prescribe an implementation API.

Expectations are rule-local. A negative `action_recorded` case is
expected to emit exactly the stated errors for Rule 4; it need not be
treated as a parser failure. Implementations may additionally run the
entire validator and assert that fixtures do not produce unintended
errors in other rules.
