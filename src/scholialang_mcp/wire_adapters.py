"""Host-injected modern stdio adapters; no automatic production activation.

Task execution belongs to the host worker. The adapter persists the queue,
inputs and cancellation intent before returning, and never infers completion
from a cancellation acknowledgement or heartbeat. See docs/real-adapters.md.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ._heartbeat.issuer import HeartbeatIssuer
from ._heartbeat.errors import HeartbeatError
from ._heartbeat.model import Heartbeat, format_rfc3339
from .durable_store import DurableCapabilityStore, StoreError, _json
from .wire_contract import (
    CLIENT_CAPABILITIES, HEARTBEAT, IDEMPOTENCY_KEY, PROTOCOL_VERSION,
    SUBSCRIPTION_ID, TASKS, VERSION, HostBinding, WireError,
    implementation_digest, validate,
)

TERMINAL = {"completed", "failed", "cancelled"}
TASK_FIELDS = ("taskId", "status", "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs")
INPUT_RESULTS = {"elicitation/create": "ElicitResult", "roots/list": "ListRootsResult",
                 "sampling/createMessage": "CreateMessageResult"}


class WireAdapters:
    """One instance per stdio connection, one scoped durable store per host.

    The binding and store are host configuration, never parsed from wire data.
    The host registers tools eligible for asynchronous execution. Workers use
    TasksAdapter's host API; request IDs, task IDs and correlation IDs differ.
    """
    def __init__(self, binding: HostBinding, store: DurableCapabilityStore | None = None,
                 *, task_tools: tuple[dict, ...] = (), participant: str | None = None):
        self.binding = binding
        self.store = store
        self.digest = implementation_digest()
        if store is not None and binding.scope != store.scope:
            raise ValueError("scope_denied")
        self.lock = threading.RLock()
        self.tasks = TasksAdapter(self, task_tools)
        self.heartbeat = HeartbeatAdapter(self, participant)
        self.events = EventsAdapter(self)

    def enabled(self, facet: str, key: str | None = None) -> bool:
        return (self.store is not None and self.binding.scope == self.store.scope
                and self.binding.permits(facet, self.digest, key))

    def require(self, facet: str, key: str | None = None) -> None:
        if not self.enabled(facet, key):
            raise WireError("adapter_unavailable", -32601)

    def record(self, kind: str, key: str) -> dict:
        if not isinstance(key, str) or not key or len(key.encode()) > 256:
            raise WireError("invalid_identifier")
        for record in self.store.refetch()["records"]:
            if (record["kind"], record["key"]) == (kind, key):
                return record
        raise WireError("not_found", -32602)

    def capabilities(self) -> dict:
        result: dict[str, Any] = {}
        if self.enabled("events"):
            result["tools"] = {"listChanged": True}
        extensions = {}
        if self.enabled("tasks"):
            extensions[TASKS] = {}
        if self.enabled("heartbeat") and self.heartbeat.participant:
            events = self.enabled("events")
            extensions[HEARTBEAT] = {
                "extension_version": "0.1", "resource_uri_template": "heartbeat://participants/{participant_id}",
                "max_lease_seconds": 30, "change_hints": events, "identity_binding": False,
            }
            result["resources"] = {"subscribe": events, "listChanged": False}
        if extensions:
            result["extensions"] = extensions
        return result

    @staticmethod
    def peer(params: dict, extension: str) -> bool:
        caps = params["_meta"][CLIENT_CAPABILITIES]
        extensions = caps.get("extensions", {})
        if not isinstance(extensions, dict):
            return False
        value = extensions.get(extension)
        if extension == TASKS:
            return isinstance(value, dict) and not value
        return isinstance(value, dict) and value.get("extension_version") == "0.1"

    def negotiate(self, params: dict, facet: str, key: str | None = None) -> None:
        self.require(facet, key)
        if facet != "events" and not self.peer(params, TASKS if facet == "tasks" else HEARTBEAT):
            extension = TASKS if facet == "tasks" else HEARTBEAT
            settings = {} if facet == "tasks" else {"extension_version": "0.1"}
            raise WireError("missing_required_client_capability", -32021,
                            {"requiredCapabilities": {"extensions": {extension: settings}}})

    def handle(self, payload: dict) -> list[dict] | None:
        """None delegates an ordinary request; [] means no response is due."""
        from .server import _err, _ok
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "notifications/cancelled" and request_id is None:
            try:
                validate("core", "CancelledNotification", payload)
                self.events.cancel(payload["params"]["requestId"])
            except WireError:
                pass
            return []
        if request_id is None:
            return []
        # Check routing keys before set/dict membership can hash wire values.
        if type(request_id) not in (str, int):
            response = _err(None, -32600, "invalid_request_id")
            del response["id"]  # Modern errors omit an unresolvable request ID.
            return [response]
        if not isinstance(method, str):
            return [_err(request_id, -32600, "invalid_method")]
        params = payload.get("params", {})
        relevant = method in {"subscriptions/listen", "tasks/get", "tasks/update", "tasks/cancel",
                              "resources/read", "resources/list", "tools/list", "server/discover"}
        if method == "tools/call" and isinstance(params, dict):
            if not isinstance(params.get("name"), str):
                return [_err(request_id, -32602, "invalid_tool_name")]
            relevant = params.get("name") in self.tasks.tools
        if not relevant:
            return None
        try:
            if (method in {"tools/list", "server/discover"} and isinstance(params, dict)
                    and "_meta" not in params):
                return None
            validate("core", "JSONRPCRequest", payload)
            if type(request_id) not in (str, int):
                raise WireError("invalid_request_id", -32600)
            meta = params.get("_meta", {})
            if isinstance(meta, dict) and meta.get(PROTOCOL_VERSION) not in (None, VERSION):
                from .server import _unsupported_version
                return [_unsupported_version(request_id, meta[PROTOCOL_VERSION])]
            if not isinstance(meta, dict) or meta.get(PROTOCOL_VERSION) != VERSION:
                # Preserve ordinary legacy operations; never expose new facets.
                if method in {"tools/list", "server/discover"}:
                    return None
                raise WireError("modern_protocol_required", -32601)
            if not isinstance(meta.get(CLIENT_CAPABILITIES), dict):
                raise WireError("missing_client_capabilities")
            # Validate the required modern metadata as well as era-specific shapes.
            validate("core", "RequestMetaObject", meta)
            if method == "server/discover":
                return None  # server combines the host-filtered capability map
            if request_id in self.events.subscriptions:
                raise WireError("request_id_in_use", -32600)
            if method == "tools/list":
                return None
            if method == "subscriptions/listen":
                self.negotiate(params, "events")
                validate("core", "SubscriptionsListenRequest", payload)
                return [self.events.listen(request_id, params)]
            if method.startswith("tasks/"):
                self.negotiate(params, "tasks", params.get("taskId"))
                definition = {"tasks/get": "GetTaskRequest", "tasks/update": "UpdateTaskRequest",
                              "tasks/cancel": "CancelTaskRequest"}[method]
                validate("tasks", definition, payload)
                if method == "tasks/get":
                    result = self.tasks.get(params["taskId"])
                elif method == "tasks/update":
                    self.tasks.update(params["taskId"], params["inputResponses"])
                    result = {}
                else:
                    self.tasks.cancel(params["taskId"])
                    result = {}
                return [_ok(request_id, result)]
            if method == "tools/call":
                self.negotiate(params, "tasks")
                validate("core", "CallToolRequest", payload)
                if "inputResponses" in params or "requestState" in params:
                    raise WireError("mrtr_not_supported_for_task_creation")
                task = self.tasks.create(params["name"], params.get("arguments", {}), meta)
                return [_ok(request_id, {**task, "resultType": "task"})]
            self.negotiate(params, "heartbeat")
            if method == "resources/list":
                return [_ok(request_id, {"resources": self.heartbeat.resources(),
                                         "ttlMs": 0, "cacheScope": "private"})]
            validate("core", "ReadResourceRequest", payload)
            return [_ok(request_id, self.heartbeat.read(params["uri"]))]
        except WireError as exc:
            return [_err(request_id, exc.code, str(exc), exc.data)]
        except StoreError as exc:
            return [_err(request_id, -32602, exc.code)]
        except HeartbeatError as exc:
            return [_err(request_id, -32602, str(exc.code))]
        except sqlite3.Error:
            return [_err(request_id, -32603, "capability_store_unavailable")]


class EventsAdapter:
    def __init__(self, runtime: WireAdapters):
        self.runtime = runtime
        self.subscriptions: dict[str | int, dict] = {}

    @staticmethod
    def notification(method: str, request_id: str | int, params: dict) -> dict:
        return {"jsonrpc": "2.0", "method": method,
                "params": {**params, "_meta": {SUBSCRIPTION_ID: request_id}}}

    def listen(self, request_id: str | int, params: dict) -> dict:
        r = self.runtime
        if len(self.subscriptions) >= 32:
            raise WireError("subscription_limit")
        requested = params["notifications"]
        accepted: dict[str, Any] = {}
        if requested.get("toolsListChanged") is True:
            accepted["toolsListChanged"] = True
        uris = requested.get("resourceSubscriptions", [])
        task_ids = requested.get("taskIds", [])
        if not isinstance(task_ids, list) or len(task_ids) > 64 or len(uris) > 64:
            raise WireError("invalid_subscription_filter")
        if uris:
            r.negotiate(params, "heartbeat")
            for uri in uris:
                r.heartbeat.check_uri(uri)
            accepted["resourceSubscriptions"] = list(dict.fromkeys(uris))
        if "taskIds" in requested:
            r.negotiate(params, "tasks")
            for task_id in task_ids:
                r.tasks.get(task_id)
            accepted["taskIds"] = list(dict.fromkeys(task_ids))
        self.subscriptions[request_id] = {
            "notifications": accepted, "revision": r.store.refetch()["revision"],
        }
        return self.notification("notifications/subscriptions/acknowledged", request_id,
                                 {"notifications": accepted})

    def cancel(self, request_id: str | int) -> None:
        self.subscriptions.pop(request_id, None)

    def close(self, request_id: str | int) -> dict:
        from .server import _ok
        self.cancel(request_id)
        return _ok(request_id, {"_meta": {SUBSCRIPTION_ID: request_id}})

    def publish_tools_changed(self) -> None:
        r = self.runtime
        r.require("events")
        r.store.put("catalog", "tools", {}, idempotency_key=secrets.token_hex(16))

    def poll(self) -> list[dict]:
        """Committed, bounded invalidations; every reconnect starts afresh.

        Read per-connection cursors, not the store's single dispatcher marker:
        marking delivery in one session must never suppress another's stream.
        Overflow or authority loss closes the subscription and requires refetch.
        """
        r = self.runtime
        output = []
        for request_id, subscription in list(self.subscriptions.items()):
            filters = subscription["notifications"]
            try:
                r.require("events")
                for uri in filters.get("resourceSubscriptions", []):
                    r.heartbeat.check_uri(uri)
                for task_id in filters.get("taskIds", []):
                    r.require("tasks", task_id)
                events = r.store.events(after_revision=subscription["revision"])
                for event in events:
                    method = None
                    params = {}
                    if event["kind"] == "catalog" and event["key"] == "tools" and filters.get("toolsListChanged"):
                        method = "notifications/tools/list_changed"
                    elif event["kind"] == "heartbeat" and event["key"] in filters.get("resourceSubscriptions", []):
                        method, params = "notifications/resources/updated", {"uri": event["key"]}
                    elif event["kind"] == "mcp_tasks" and event["key"] in filters.get("taskIds", []):
                        method, params = "notifications/tasks", r.tasks.get(event["key"])
                    if method:
                        output.append(self.notification(method, request_id, params))
                    subscription["revision"] = event["revision"]
            except (StoreError, WireError, sqlite3.Error):
                output.append(self.close(request_id))
        return output


class TasksAdapter:
    def __init__(self, runtime: WireAdapters, tools: tuple[dict, ...]):
        self.runtime = runtime
        self.tools = {}
        from .server import TOOL_DEFINITIONS
        reserved = {tool["name"] for tool in TOOL_DEFINITIONS}
        for tool in tools:
            validate("core", "Tool", tool)
            name = tool["name"]
            if name in self.tools or name in reserved:
                raise ValueError("duplicate_task_tool")
            self.tools[name] = json.loads(_json(tool))

    def _record(self, task_id: str) -> dict:
        self.runtime.require("tasks", task_id)
        return self.runtime.record("mcp_tasks", task_id)

    def get(self, task_id: str) -> dict:
        task = self._record(task_id)["payload"]["task"]
        validate("tasks", "GetTaskResult", {**task, "resultType": "complete"})
        return task

    def create(self, tool: str, arguments: dict, meta: dict) -> dict:
        from jsonschema import Draft202012Validator
        from referencing import Registry
        r = self.runtime
        r.require("tasks")
        if tool not in self.tools:
            raise WireError("unknown_tool")
        # Tool schemas are trusted host registration, with remote refs disabled.
        if not Draft202012Validator(self.tools[tool]["inputSchema"], registry=Registry()).is_valid(arguments):
            raise WireError("invalid_tool_arguments")
        token = meta.get(IDEMPOTENCY_KEY, secrets.token_hex(32))
        if not isinstance(token, str) or not token or len(token.encode()) > 256:
            raise WireError("invalid_idempotency_key")
        identity = _json([r.store.scope.owner, r.store.scope.project, token])
        task_id = "mcp-task-" + hashlib.sha256(identity.encode()).hexdigest()
        r.require("tasks", task_id)
        fingerprint = hashlib.sha256(_json([tool, arguments]).encode()).hexdigest()
        with r.lock:
            try:
                old = self._record(task_id)["payload"]
            except WireError as exc:
                if str(exc) != "not_found":
                    raise
            else:
                if old["fingerprint"] != fingerprint:
                    raise WireError("idempotency_conflict")
                return {key: old["task"][key] for key in TASK_FIELDS}
            stamp = format_rfc3339(datetime.fromtimestamp(r.store.clock(), timezone.utc))
            task = {"taskId": task_id, "status": "working", "createdAt": stamp,
                    "lastUpdatedAt": stamp, "ttlMs": r.store.retention.state_ttl_ms,
                    "pollIntervalMs": 100}
            payload = {"task": task, "fingerprint": fingerprint, "tool": tool,
                       "arguments": arguments, "inputResponses": {}, "cancel_requested": False,
                       "client_capabilities": meta.get(CLIENT_CAPABILITIES, {})}
            try:
                r.store.put("mcp_tasks", task_id, payload, expected_revision=0,
                            idempotency_key="create:" + task_id)
            except StoreError as exc:
                if exc.code not in {"revision_conflict", "idempotency_conflict"}:
                    raise
                # A concurrent host may have durably created this same token.
                old = self._record(task_id)["payload"]
                if old["fingerprint"] != fingerprint:
                    raise WireError("idempotency_conflict") from exc
                return {key: old["task"][key] for key in TASK_FIELDS}
            return task

    def _change(self, task_id: str, change) -> None:
        r = self.runtime
        with r.lock:
            for _ in range(4):
                record = self._record(task_id)
                payload = record["payload"]
                if change(payload) is False:
                    return
                now = r.store.clock()
                remaining = record["expires_ms"] - math.ceil(now * 1000)
                if remaining <= 0:
                    raise WireError("not_found")
                payload["task"]["lastUpdatedAt"] = format_rfc3339(datetime.fromtimestamp(now, timezone.utc))
                validate("tasks", "GetTaskResult", {**payload["task"], "resultType": "complete"})
                try:
                    r.store.put("mcp_tasks", task_id, payload, expected_revision=record["revision"],
                                ttl_ms=min(remaining, r.store.retention.state_ttl_ms),
                                idempotency_key=secrets.token_hex(32))
                    return
                except StoreError as exc:
                    if exc.code != "revision_conflict":
                        raise
            raise WireError("revision_conflict")

    def update(self, task_id: str, responses: dict) -> None:
        validate("tasks", "InputResponses", responses)
        if not responses:
            raise WireError("input_responses_required")

        def change(payload):
            task = payload["task"]
            receipts = payload["inputResponses"]
            outstanding = task.get("inputRequests", {})
            fresh = {}
            for key, value in responses.items():
                if key in receipts:
                    if receipts[key] != value:
                        raise WireError("input_response_conflict")
                elif task["status"] != "input_required" or key not in outstanding:
                    raise WireError("input_not_outstanding")
                else:
                    definition = INPUT_RESULTS[outstanding[key]["method"]]
                    validate("core", definition, value)
                    request = outstanding[key]
                    if (request["method"] == "elicitation/create" and value.get("action") == "accept"
                            and request["params"].get("mode", "form") == "form"):
                        from jsonschema import Draft202012Validator
                        from referencing import Registry
                        schema = request["params"]["requestedSchema"]
                        if not Draft202012Validator(schema, registry=Registry()).is_valid(value.get("content", {})):
                            raise WireError("invalid_input_content")
                    fresh[key] = value
            if not fresh:
                return False
            receipts.update(fresh)
            task["inputRequests"] = {k: v for k, v in outstanding.items() if k not in fresh}
            if not task["inputRequests"]:
                task.pop("inputRequests")
                task["status"] = "working"
        self._change(task_id, change)

    def cancel(self, task_id: str) -> None:
        def change(payload):
            if payload["cancel_requested"] or payload["task"]["status"] in TERMINAL:
                return False
            payload["cancel_requested"] = True
        self._change(task_id, change)

    def require_input(self, task_id: str, requests: dict) -> None:
        validate("core", "InputRequests", requests)
        if not requests:
            raise WireError("input_requests_required")
        def change(payload):
            if payload["task"]["status"] != "working" or set(requests) & set(payload["inputResponses"]):
                raise WireError("invalid_task_transition")
            caps = payload["client_capabilities"]
            for request in requests.values():
                method = request["method"]
                capability = {"elicitation/create": "elicitation", "roots/list": "roots",
                              "sampling/createMessage": "sampling"}[method]
                if not isinstance(caps.get(capability), dict):
                    raise WireError("input_capability_not_negotiated", -32021)
                if (method == "elicitation/create"
                        and request["params"].get("mode", "form") not in caps[capability]):
                    raise WireError("input_capability_not_negotiated", -32021)
            payload["task"].update(status="input_required", inputRequests=requests)
        self._change(task_id, change)

    def finish(self, task_id: str, *, result: dict | None = None,
               error: dict | None = None, cancelled: bool = False) -> None:
        """Host worker evidence only; never called by wire cancellation itself."""
        if sum((result is not None, error is not None, cancelled is True)) != 1:
            raise WireError("one_terminal_outcome_required")
        if result is not None:
            result = {**result, "resultType": "complete"}
            validate("core", "CallToolResult", result)
        def change(payload):
            task = payload["task"]
            if task["status"] in TERMINAL:
                raise WireError("terminal_task")
            if cancelled and not payload["cancel_requested"]:
                raise WireError("cancellation_not_requested")
            task.pop("inputRequests", None)
            if result is not None:
                task.update(status="completed", result=result)
            elif error is not None:
                task.update(status="failed", error=error)
            else:
                task["status"] = "cancelled"
        self._change(task_id, change)


class HeartbeatAdapter:
    def __init__(self, runtime: WireAdapters, participant: str | None):
        self.runtime = runtime
        self.participant = participant
        self.issuer = HeartbeatIssuer(participant_id=participant) if participant else None
        self.uri = "heartbeat://participants/" + quote(participant, safe="") if participant else None

    def check_uri(self, uri: str) -> None:
        self.runtime.require("heartbeat", uri)
        if not self.participant or uri != self.uri:
            raise WireError("not_found")

    def resources(self) -> list[dict]:
        self.check_uri(self.uri)
        return [{"uri": self.uri, "name": self.participant, "mimeType": "application/json"}]

    def renew(self) -> None:
        """Trusted publisher hook; each runtime has a randomly minted epoch."""
        self.check_uri(self.uri)
        r = self.runtime
        with r.lock:
            document = self.issuer.issue().to_dict()
            Heartbeat.from_dict(document)
            r.store.put("heartbeat", self.uri, document,
                        ttl_ms=min(30_000, r.store.retention.state_ttl_ms),
                        idempotency_key="heartbeat:" + secrets.token_hex(16))

    def read(self, uri: str) -> dict:
        self.check_uri(uri)
        r = self.runtime
        document = r.record("heartbeat", uri)["payload"]
        heartbeat = Heartbeat.from_dict(document)
        now = datetime.fromtimestamp(r.store.clock(), timezone.utc)
        if (heartbeat.node_id != self.participant or heartbeat.boot_id != self.issuer.epoch_id
                or not heartbeat.is_fresh(now) or heartbeat.issued_at > now):
            raise WireError("invalid_lease")
        # Zero TTL prevents a cache from surviving revocation or lease expiry.
        # Identity binding is deliberately not advertised: local configured
        # scope is not proof of an upstream publisher behind a relay.
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": _json(document)}],
                "ttlMs": 0, "cacheScope": "private"}
