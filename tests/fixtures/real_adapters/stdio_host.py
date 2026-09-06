"""Disposable host/worker fixture. This is NOT a production certification.

The transport and adapters under test are the shipped implementation. Only
workloads, authority and the independent-receipt verifier are synthetic.
"""
import json
import sys
import threading
import time
from pathlib import Path

from scholialang_mcp.durable_store import DurableCapabilityStore, Scope
from scholialang_mcp.server import ScholiaMCPServer, ScholiaServerConfig, serve_stdio
from scholialang_mcp.wire_adapters import WireAdapters
from scholialang_mcp.wire_contract import AdapterPolicy, HostBinding

root = Path(sys.argv[1])
config = json.loads((root / 'config.json').read_text())
scope = Scope(config.get('owner', 'fixture-owner'), config.get('project', 'fixture-project'))
control = root / 'control.json'

def controls():
    return json.loads(control.read_text()) if control.exists() else {}

def authorize(scope, facet, key):
    current = controls()
    return not current.get('revoked', False) and key not in current.get('denied', [])

binding = HostBinding(
    scope=scope if config.get('bound', True) else None,
    policy=AdapterPolicy(**config.get('policy', {})),
    authorize=authorize,
    certified=lambda facet, digest, pins: config.get('synthetic_certified', False),
)
store = DurableCapabilityStore(root / 'state.db', scope, enabled=True) if binding.scope else None
TOOL = {'name': 'fixture_job', 'description': 'Synthetic disposable task workload',
        'inputSchema': {'type': 'object', 'properties': {'behavior': {'type': 'string'}},
                        'required': ['behavior'], 'additionalProperties': False}}
runtime = WireAdapters(binding, store, task_tools=(TOOL,), participant='fixture/node')
server = ScholiaMCPServer(ScholiaServerConfig(root, adapters=runtime))

stop = threading.Event()
def worker():
    while not stop.wait(0.03):
        try:
            if runtime.enabled('tasks'):
                for record in store.refetch()['records']:
                    if record['kind'] != 'mcp_tasks':
                        continue
                    p = record['payload']; task = p['task']; tid = record['key']
                    if task['status'] != 'working':
                        continue
                    behavior = p['arguments']['behavior']
                    if behavior == 'input' and not p['inputResponses']:
                        runtime.tasks.require_input(tid, {'prompt': {
                            'method': 'elicitation/create', 'params': {
                                'mode': 'form', 'message': 'Fixture input',
                                'requestedSchema': {'type': 'object', 'properties': {'value': {'type': 'string'}}},
                            }}})
                    elif behavior == 'input' or behavior in ('complete', 'tool_error'):
                        runtime.tasks.finish(tid, result={'content': [{'type': 'text', 'text': 'fixture result'}],
                                                         'isError': behavior == 'tool_error', 'resultType': 'complete'})
                    elif behavior == 'rpc_error':
                        runtime.tasks.finish(tid, error={'code': -32603, 'message': 'fixture execution error'})
                    elif p['cancel_requested'] and behavior == 'cooperative':
                        runtime.tasks.finish(tid, cancelled=True)
                    elif p['cancel_requested'] and behavior == 'race':
                        runtime.tasks.finish(tid, result={'content': [], 'resultType': 'complete'})
            current = controls()
            if runtime.enabled('heartbeat') and current.get('renew', False):
                runtime.heartbeat.renew()
            if runtime.enabled('events') and current.get('catalog', False):
                runtime.events.publish_tools_changed()
        except Exception as exc:
            print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr, flush=True)

thread = threading.Thread(target=worker, daemon=True)
thread.start()
try:
    raise SystemExit(serve_stdio(server))
finally:
    stop.set()
    thread.join(timeout=1)
