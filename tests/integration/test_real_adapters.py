"""Real subprocess stdio against pinned schemas, with synthetic host workloads."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scholialang_mcp.durable_store import DurableCapabilityStore, Scope
from scholialang_mcp.wire_adapters import WireAdapters
from scholialang_mcp.wire_contract import (
    AdapterPolicy, HostBinding, WireError, CLIENT_CAPABILITIES, PROTOCOL_VERSION,
    HEARTBEAT, TASKS, SUBSCRIPTION_ID, IDEMPOTENCY_KEY, PINS, SCHEMAS, validate,
)

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / 'tests/fixtures/real_adapters/stdio_host.py'
URI = 'heartbeat://participants/fixture%2Fnode'
EXTENSIONS = {TASKS: {}, HEARTBEAT: {'extension_version': '0.1'}}


class Peer:
    def __init__(self, root, config):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / 'config.json').write_text(json.dumps(config))
        env = os.environ.copy()
        if not env.get('MCP_EXPECT_IMPORT_PREFIX'):
            env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
        self.proc = subprocess.Popen([sys.executable, str(HOST), str(root)], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        self.capture = []
        self.counter = 100
        self.notifications = []
        def read():
            for line in self.proc.stdout:
                self.lines.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()

    def send(self, method, params=None, *, request_id=None, extensions=None, version='2026-07-28'):
        if request_id is None:
            self.counter += 1
            request_id = self.counter
        value = {'jsonrpc': '2.0', 'id': request_id, 'method': method,
                 'params': {'_meta': {PROTOCOL_VERSION: version,
                                     CLIENT_CAPABILITIES: {'extensions': EXTENSIONS if extensions is None else extensions}},
                            **(params or {})}}
        self.raw(value)
        return request_id

    def raw(self, value):
        self.capture.append({'direction': 'client', 'message': value})
        self.proc.stdin.write(json.dumps(value) + '\n')
        self.proc.stdin.flush()

    def recv(self):
        value = self.lines.get(timeout=3)
        self.capture.append({'direction': 'server', 'message': value})
        method = value.get('method')
        if method:
            definition = {'notifications/subscriptions/acknowledged': 'SubscriptionsAcknowledgedNotification',
                          'notifications/resources/updated': 'ResourceUpdatedNotification',
                          'notifications/tools/list_changed': 'ToolListChangedNotification',
                          'notifications/tasks': 'TaskStatusNotification'}[method]
            validate('tasks' if method == 'notifications/tasks' else 'core', definition, value)
        return value

    def rpc(self, method, params=None, **kwargs):
        request_id = self.send(method, params, **kwargs)
        while True:
            value = self.recv()
            if value.get('id') == request_id:
                return value
            self.notifications.append(value)

    def control(self, **values):
        path = self.root / 'control.tmp'
        path.write_text(json.dumps(values))
        path.replace(self.root / 'control.json')

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill(); self.proc.wait()
        stderr = self.proc.stderr.read()
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.stdout.close(); self.proc.stderr.close()
        (self.root / f'wire-{self.proc.pid}.json').write_text(json.dumps(self.capture, indent=2) + '\n')
        (self.root / f'stderr-{self.proc.pid}.txt').write_text(stderr)
        assert not stderr, stderr


@pytest.fixture
def peers(tmp_path):
    active = []
    def start(config=None, root=None):
        peer = Peer(root or tmp_path / str(len(active)), config or {
            'policy': dict.fromkeys(('events', 'tasks', 'heartbeat'), 'enforce'),
            'synthetic_certified': True,
        })
        active.append(peer)
        return peer
    yield start
    for peer in active:
        if not peer.proc.stdout.closed:
            peer.close()


def create(peer, behavior='wait', token='creation'):
    result = peer.rpc('tools/call', {'name': 'fixture_job', 'arguments': {'behavior': behavior},
        '_meta': {PROTOCOL_VERSION: '2026-07-28', CLIENT_CAPABILITIES: {'extensions': EXTENSIONS, 'elicitation': {'form': {}}},
                  IDEMPOTENCY_KEY: token}})['result']
    validate('tasks', 'CreateTaskResult', result)
    assert result['resultType'] == 'task' and 'task' not in result
    assert result['taskId'].startswith('mcp-task-')
    return result['taskId']


def state(peer, task_id, expected=None):
    deadline = time.monotonic() + 3
    while True:
        result = peer.rpc('tasks/get', {'taskId': task_id})['result']
        validate('tasks', 'GetTaskResult', result)
        if expected is None or result['status'] == expected:
            return result
        assert time.monotonic() < deadline, result
        time.sleep(0.03)


def test_schema_and_vendor_bytes_are_pinned():
    for path, digest in PINS['files'].items():
        assert hashlib.sha256((SCHEMAS.parent / path).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('policy', ['off', 'enforce'])
@pytest.mark.parametrize('field,value,code', [
    ('method', [], -32600), ('method', {}, -32600),
    ('name', [], -32602), ('name', {}, -32602),
    ('id', [], -32600), ('id', {}, -32600), ('id', True, -32600),
])
def test_malformed_routing_keys_do_not_kill_stdio(peers, policy, field, value, code):
    p = peers({'policy': dict.fromkeys(('events', 'tasks', 'heartbeat'), policy),
               'synthetic_certified': True})
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': 'fixture_job'}}
    if field == 'name':
        request['params']['name'] = value
    else:
        request[field] = value
    p.raw(request)
    response = p.recv()
    if field == 'id':
        assert 'id' not in response
    else:
        assert response['id'] == 1
    assert response['error']['code'] == code
    validate('core', 'JSONRPCErrorResponse', response)
    assert 'result' in p.rpc('server/discover')
    assert p.proc.poll() is None


@pytest.mark.parametrize('enabled', list(itertools.product((False, True), repeat=3)))
def test_eight_independent_capability_combinations(peers, enabled):
    policy = {facet: 'enforce' if on else 'off' for facet, on in zip(('events', 'tasks', 'heartbeat'), enabled)}
    p = peers({'policy': policy, 'synthetic_certified': True})
    result = p.rpc('server/discover')['result']
    validate('core', 'DiscoverResult', result)
    caps = result['capabilities']; ext = caps.get('extensions', {})
    assert caps['tools']['listChanged'] is enabled[0]
    assert (TASKS in ext) is enabled[1]
    assert (HEARTBEAT in ext) is enabled[2]
    assert caps.get('resources', {}).get('subscribe', False) is (enabled[0] and enabled[2])
    names = [t['name'] for t in p.rpc('tools/list')['result']['tools']]
    assert ('fixture_job' in names) is enabled[1]


@pytest.mark.parametrize('modes', list(itertools.product(('off', 'observe', 'enforce'), repeat=3)))
def test_27_policy_combinations_without_certification_are_inert(peers, modes):
    p = peers({'policy': dict(zip(('events', 'tasks', 'heartbeat'), modes))})
    caps = p.rpc('server/discover')['result']['capabilities']
    assert not caps.get('extensions')
    assert caps['tools']['listChanged'] is False
    for method, params in [('subscriptions/listen', {'notifications': {'toolsListChanged': True}}),
                           ('tools/call', {'name': 'fixture_job', 'arguments': {'behavior': 'complete'}}),
                           ('resources/read', {'uri': URI}), ('tasks/cancel', {'taskId': 'forged'})]:
        assert 'error' in p.rpc(method, params)
    db = DurableCapabilityStore(p.root / 'state.db', Scope('fixture-owner', 'fixture-project'), enabled=True)
    assert db.refetch()['revision'] == 0


@pytest.mark.parametrize('mode', ['off', 'observe'])
def test_certification_cannot_override_off_or_observe(peers, mode):
    p = peers({'policy': dict.fromkeys(('events', 'tasks', 'heartbeat'), mode), 'synthetic_certified': True})
    assert not p.rpc('server/discover')['result']['capabilities'].get('extensions')
    assert 'error' in p.rpc('tools/call', {'name': 'fixture_job', 'arguments': {'behavior': 'complete'}})


def test_unbound_and_per_request_capabilities_fail_closed(peers):
    p = peers({'policy': dict.fromkeys(('events', 'tasks', 'heartbeat'), 'enforce'),
               'synthetic_certified': True, 'bound': False})
    assert not p.rpc('server/discover')['result']['capabilities'].get('extensions')
    assert not (p.root / 'state.db').exists()
    p = peers()
    task_id = create(p)
    assert p.rpc('tasks/get', {'taskId': task_id}, extensions={})['error']['code'] == -32021
    assert p.rpc('resources/read', {'uri': URI}, extensions={TASKS: {}})['error']['code'] == -32021
    assert 'fixture_job' not in [t['name'] for t in p.rpc('tools/list', extensions={})['result']['tools']]
    assert p.rpc('tasks/get', {'taskId': task_id}, extensions={TASKS: {'version': 'legacy'}})['error']['code'] == -32021


def test_subscriptions_ack_filters_ids_cancel_and_graceful_close(peers):
    p = peers()
    for sid in ('stream-a', 7):
        p.send('subscriptions/listen', {'notifications': {'toolsListChanged': True, 'promptsListChanged': True}}, request_id=sid)
        ack = p.recv()
        assert ack['params']['_meta'][SUBSCRIPTION_ID] == sid
        assert ack['params']['notifications'] == {'toolsListChanged': True}
    p.raw({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 'stream-a'}})
    p.rpc('server/discover')  # barrier; cancellation itself has no response
    p.control(catalog=True)
    event = p.recv()
    assert event['method'] == 'notifications/tools/list_changed'
    assert event['params']['_meta'][SUBSCRIPTION_ID] == 7
    p.control()
    p.proc.stdin.close()
    while True:
        end = p.recv()
        if end.get('id') == 7:
            validate('core', 'SubscriptionsListenResultResponse', end)
            break
    p.proc.wait(timeout=3)
    assert p.proc.returncode == 0


def test_subscriptions_receive_task_changes_after_ack(peers):
    p = peers()
    tid = create(p, 'cooperative')
    p.send('subscriptions/listen', {'notifications': {'taskIds': [tid]}}, request_id='sub-task')
    assert p.recv()['method'] == 'notifications/subscriptions/acknowledged'
    p.rpc('tasks/cancel', {'taskId': tid})
    state(p, tid, 'cancelled')
    all_events = list(p.notifications)
    while not any(e.get('method') == 'notifications/tasks' for e in all_events):
        all_events.append(p.recv())
    event = next(e for e in all_events if e.get('method') == 'notifications/tasks')
    assert event['params']['taskId'] == tid
    assert event['params']['_meta'][SUBSCRIPTION_ID] == 'sub-task'


@pytest.mark.parametrize('behavior,status', [('complete','completed'), ('tool_error','completed'),
                                            ('rpc_error','failed'), ('input','completed')])
def test_task_states_and_keyed_input_idempotency(peers, behavior, status):
    p = peers()
    tid = create(p, behavior)
    if behavior == 'input':
        state(p, tid, 'input_required')
        assert 'error' in p.rpc('tasks/update', {'taskId': tid, 'inputResponses': {'forged': {'action': 'accept'}}})
        assert 'error' in p.rpc('tasks/update', {'taskId': tid, 'inputResponses': {'prompt': {'roots': []}}})
        args = {'taskId': tid, 'inputResponses': {'prompt': {'action': 'accept', 'content': {'value': 'fixture'}}}}
        assert p.rpc('tasks/update', args)['result']['resultType'] == 'complete'
        assert p.rpc('tasks/update', args)['result']['resultType'] == 'complete'
        assert 'error' in p.rpc('tasks/update', {'taskId': tid, 'inputResponses': {'prompt': {'action': 'decline'}}})
    result = state(p, tid, status)
    assert 'task' not in result and result['ttlMs'] > 0 and 'ttl' not in result
    if behavior == 'tool_error':
        assert result['result']['isError'] is True


@pytest.mark.parametrize('behavior,expected', [('wait','working'), ('cooperative','cancelled'), ('race','completed')])
def test_cancellation_is_intent_and_can_race(peers, behavior, expected):
    p = peers()
    tid = create(p, behavior)
    reply = p.rpc('tasks/cancel', {'taskId': tid})['result']
    validate('tasks', 'CancelTaskResult', reply)
    assert set(reply) == {'resultType','_meta'}
    assert state(p, tid, expected)['status'] == expected
    assert p.rpc('tasks/cancel', {'taskId': tid})['result']['resultType'] == 'complete'


def test_reconnect_creation_retry_refetch_and_identity(peers):
    p = peers()
    tid = create(p)
    root = p.root
    p.send('subscriptions/listen', {'notifications': {'taskIds': [tid]}}, request_id='old')
    assert p.recv()['method'] == 'notifications/subscriptions/acknowledged'
    p.close()
    p = peers(root=root)
    assert create(p) == tid
    assert state(p, tid)['status'] == 'working'
    p.send('subscriptions/listen', {'notifications': {'taskIds': [tid]}}, request_id='new')
    assert p.recv()['params']['_meta'][SUBSCRIPTION_ID] == 'new'
    assert 'error' in p.rpc('tasks/get', {'taskId': 'ot-run-id'})
    other = peers()
    assert 'error' in other.rpc('tasks/get', {'taskId': tid, 'owner': 'fixture-owner', 'project': 'fixture-project'})
    other_scope = peers({'policy': {'tasks':'enforce'}, 'synthetic_certified': True, 'owner': 'other'})
    assert create(other_scope) != tid


def test_idempotency_collision_and_invalid_wire(peers):
    p = peers(); tid = create(p)
    assert create(p) == tid
    reply = p.rpc('tools/call', {'name': 'fixture_job', 'arguments': {'behavior': 'complete'},
        '_meta': {PROTOCOL_VERSION: '2026-07-28', CLIENT_CAPABILITIES: {'extensions': EXTENSIONS}, IDEMPOTENCY_KEY: 'creation'}})
    assert reply['error']['message'] == 'idempotency_conflict'
    for method in ['tasks/result','tasks/list','tasks/provide_input','heartbeat/get','events/ack']:
        assert p.rpc(method, {'taskId': tid})['error']['code'] == -32601
    for responses in [[], None, {}, {'x': {}}]:
        assert 'error' in p.rpc('tasks/update', {'taskId': tid, 'inputResponses': responses})
    assert 'error' in p.rpc('subscriptions/listen', {'notifications': {'taskIds': ['forged']}})
    assert 'error' in p.rpc('subscriptions/listen', {'notifications': {'toolsListChanged': 'true'}})
    assert 'error' in p.rpc('resources/read', {'uri': 'heartbeat://participants/foreign'})


def test_heartbeat_reads_hints_and_new_epoch_on_restart(peers):
    p = peers(); p.control(renew=True)
    deadline = time.monotonic() + 3
    while True:
        reply = p.rpc('resources/read', {'uri': URI})
        if 'result' in reply:
            break
        assert time.monotonic() < deadline
        time.sleep(.03)
    result = reply['result']; validate('core','ReadResourceResult',result)
    lease = json.loads(result['contents'][0]['text'])
    assert set(lease) == {'extension_version','node_id','boot_id','sequence','issued_at','expires_at'}
    assert lease['extension_version'] == '0.1' and lease['node_id'] == 'fixture/node'
    assert result['cacheScope'] == 'private' and result['ttlMs'] == 0
    p.send('subscriptions/listen', {'notifications': {'resourceSubscriptions': [URI]}}, request_id='lease-stream')
    assert p.recv()['method'] == 'notifications/subscriptions/acknowledged'
    hint = p.recv(); assert hint['method'] == 'notifications/resources/updated'
    assert hint['params']['uri'] == URI
    root = p.root; p.control(); p.close()
    p = peers(root=root)
    assert 'error' in p.rpc('resources/read', {'uri': URI})  # prior process lease is not this publisher
    p.control(renew=True)
    deadline = time.monotonic() + 3
    while True:
        reply = p.rpc('resources/read', {'uri': URI})
        if 'result' in reply:
            break
        assert time.monotonic() < deadline
        time.sleep(.03)
    assert json.loads(reply['result']['contents'][0]['text'])['boot_id'] != lease['boot_id']


def test_revocation_closes_subscription_and_blocks_refetch(peers):
    p = peers(); tid = create(p)
    p.send('subscriptions/listen', {'notifications': {'taskIds': [tid]}}, request_id='revoked')
    assert p.recv()['method'] == 'notifications/subscriptions/acknowledged'
    p.control(revoked=True)
    closed = p.recv()
    assert closed['id'] == 'revoked'
    validate('core','SubscriptionsListenResultResponse',closed)
    assert 'error' in p.rpc('tasks/get', {'taskId': tid})
    assert not p.rpc('server/discover')['result']['capabilities'].get('extensions')


def local_runtime(tmp_path, *, clock=None, retention=None, binding=None):
    from scholialang_mcp.durable_store import Retention
    scope = Scope('local-owner','local-project')
    binding = binding or HostBinding(scope, AdapterPolicy('enforce','enforce','enforce'),
                                    lambda *a: True, lambda *a: True)
    kwargs = {'enabled': True, 'retention': retention or Retention()}
    if clock:
        kwargs['clock'] = clock
    store = DurableCapabilityStore(tmp_path/'local.db', scope, **kwargs)
    tool = {'name': 'local_job', 'inputSchema': {'type':'object'}}
    return WireAdapters(binding, store, task_tools=(tool,), participant='local/node')


def test_certification_binds_exact_code_pins_and_downgrade(tmp_path):
    from scholialang_mcp.wire_contract import implementation_digest
    seen = []
    digest = implementation_digest()
    def verifier(facet, actual_digest, pins):
        seen.append((facet, actual_digest, pins))
        return actual_digest == digest and pins == PINS
    b = HostBinding(Scope('local-owner','local-project'), AdapterPolicy('enforce','enforce','enforce'),
                    lambda *a: True, verifier)
    r = local_runtime(tmp_path, binding=b)
    assert TASKS in r.capabilities()['extensions']
    assert {v[0] for v in seen} == {'events','tasks','heartbeat'}
    r.digest = 'changed-code'
    assert r.capabilities() == {}
    r.digest = digest
    b.policy = AdapterPolicy('observe','observe','observe')
    assert r.capabilities() == {}
    assert r.store.refetch()['revision'] == 0
    b.policy = AdapterPolicy('enforce','enforce','enforce')
    b.authorize = lambda *a: None
    assert r.capabilities() == {}
    b.authorize = lambda *a: 1  # truthiness is not an affirmative authority verdict
    assert r.capabilities() == {}


def test_scope_mismatch_and_reopen_denied(tmp_path):
    from scholialang_mcp.durable_store import StoreError
    r = local_runtime(tmp_path)
    with pytest.raises(ValueError, match='scope_denied'):
        WireAdapters(HostBinding(Scope('foreign','project')),r.store)
    with pytest.raises(StoreError, match='scope_denied'):
        DurableCapabilityStore(tmp_path/'local.db',Scope('foreign','project'),enabled=True)
    task_id = r.tasks.create('local_job',{}, {})['taskId']
    r.events.listen('prior-principal',{'notifications':{'toolsListChanged':True}})
    r.binding.scope = Scope('foreign','project')
    assert r.capabilities() == {}
    with pytest.raises(WireError,match='adapter_unavailable'):
        r.tasks.get(task_id)
    assert r.events.poll()[0]['id'] == 'prior-principal'


def test_retention_overflow_requires_resubscription_and_refetch(tmp_path):
    from scholialang_mcp.durable_store import Retention
    r = local_runtime(tmp_path, retention=Retention(max_events=1))
    params = {'notifications': {'toolsListChanged': True}}
    r.events.listen('slow',params)
    r.events.publish_tools_changed(); r.events.publish_tools_changed()
    messages = r.events.poll()
    assert len(messages) == 1 and messages[0]['id'] == 'slow'
    assert 'slow' not in r.events.subscriptions
    assert r.events.listen('fresh',params)['params']['_meta'][SUBSCRIPTION_ID] == 'fresh'
    snapshot = r.store.refetch()
    assert snapshot['revision'] == 2
    r.events.publish_tools_changed()
    assert r.events.poll()[0]['params']['_meta'][SUBSCRIPTION_ID] == 'fresh'


def test_task_input_atomicity_expiry_and_terminal_guards(tmp_path):
    from scholialang_mcp.durable_store import Retention
    now = [1000.0]
    r = local_runtime(tmp_path, clock=lambda: now[0], retention=Retention(state_ttl_ms=1000))
    tid = r.tasks.create('local_job',{}, {IDEMPOTENCY_KEY:'one', CLIENT_CAPABILITIES:{'elicitation':{'form':{}}}})['taskId']
    request = {'method':'elicitation/create','params': {'mode':'form','message':'fixture',
                'requestedSchema':{'type':'object','properties':{'a':{'type':'string'}},'required':['a']}}}
    r.tasks.require_input(tid,{'one':request, 'two':request})
    before = r.store.refetch()
    with pytest.raises(WireError, match='input_not_outstanding'):
        r.tasks.update(tid,{'one':{'action':'accept','content':{'a':'x'}}, 'forged':{'action':'decline'}})
    assert r.store.refetch() == before
    with pytest.raises(WireError, match='invalid_input_content'):
        r.tasks.update(tid,{'one':{'action':'accept','content':{'a':3}}})
    r.tasks.update(tid,{'one':{'action':'accept','content':{'a':'x'}}})
    assert set(r.tasks.get(tid)['inputRequests']) == {'two'}
    r.tasks.update(tid,{'two':{'action':'decline'}})
    assert r.tasks.get(tid)['status'] == 'working'
    with pytest.raises(WireError, match='invalid_task_transition'):
        r.tasks.require_input(tid,{'one':request})
    r.tasks.finish(tid, result={'content':[], 'isError':True})
    assert r.tasks.get(tid)['status'] == 'completed'
    with pytest.raises(WireError, match='terminal_task'):
        r.tasks.finish(tid,error={'code':-32603,'message':'late'})
    now[0] += 1
    with pytest.raises(WireError,match='not_found'):
        r.tasks.get(tid)


def test_heartbeat_invalid_expired_future_and_wrong_identity_fail_closed(tmp_path):
    from scholialang_mcp._heartbeat.model import format_rfc3339
    from datetime import datetime, timedelta, timezone
    r = local_runtime(tmp_path)
    r.heartbeat.renew()
    record = r.record('heartbeat',r.heartbeat.uri)
    original = record['payload']
    params = {'_meta': {PROTOCOL_VERSION:'2026-07-28', CLIENT_CAPABILITIES:{'extensions':EXTENSIONS}}, 'uri':r.heartbeat.uri}
    cases = [
        {'extension_version':'future'}, {'node_id':'forged'}, {'sequence':-1},
        {'boot_id':'other-epoch'},
        {'issued_at':format_rfc3339(datetime.now(timezone.utc)-timedelta(seconds=35)),
         'expires_at':format_rfc3339(datetime.now(timezone.utc)-timedelta(seconds=5))},
        {'issued_at':format_rfc3339(datetime.now(timezone.utc)+timedelta(seconds=5)),
         'expires_at':format_rfc3339(datetime.now(timezone.utc)+timedelta(seconds=35))},
    ]
    for index, patch in enumerate(cases):
        r.store.put('heartbeat',r.heartbeat.uri,{**original,**patch},idempotency_key=str(index))
        reply = r.handle({'jsonrpc':'2.0','id':index,'method':'resources/read','params':params})
        assert 'error' in reply[0]


def test_portable_heartbeat_lineage_replay_and_clock_contract():
    from datetime import timedelta
    from scholialang_mcp._heartbeat import FakeClock, HeartbeatIssuer, LineageState, admit
    clock = FakeClock()
    issuer = HeartbeatIssuer(participant_id='fixture/node',epoch_id='epoch-one',clock=clock)
    first = issuer.issue().to_dict(); second = issuer.issue().to_dict()
    state = LineageState('fixture/node')
    accepted = admit(state,first,clock.now()); assert accepted.accepted
    assert admit(accepted.state,first,clock.now()).duplicate
    conflict = admit(accepted.state,{**first,'expires_at':second['expires_at'].replace('30.000','31.000')},clock.now())
    assert str(conflict.reason) == 'sequence_conflict'
    second_state = admit(accepted.state,second,clock.now()).state
    rollback = admit(second_state,first,clock.now()+timedelta(days=1))
    assert str(rollback.reason) == 'sequence_rollback' and rollback.state == second_state
    next_epoch = HeartbeatIssuer(participant_id='fixture/node',epoch_id='epoch-two',clock=clock).issue().to_dict()
    new_state = admit(second_state,next_epoch,clock.now()).state
    assert str(admit(new_state,first,clock.now()).reason) == 'boot_id_reuse'
    assert str(admit(state,first,clock.now()+timedelta(seconds=31),max_skew_seconds=40).reason) == 'expired_on_arrival'
    assert str(admit(state,first,clock.now()+timedelta(seconds=10)).reason) == 'clock_skew_exceeded'
    assert str(admit(state,{**first,'node_id':'wrong'},clock.now()).reason) == 'node_id_mismatch'


def test_cross_process_creation_same_token_is_one_durable_task(peers):
    p = peers()
    # Complete discovery before launching another process against the same DB.
    p.rpc('server/discover')
    second = peers(root=p.root)
    second.rpc('server/discover')
    params = {'name':'fixture_job','arguments':{'behavior':'wait'},'_meta':{
        PROTOCOL_VERSION:'2026-07-28', CLIENT_CAPABILITIES:{'extensions':EXTENSIONS}, IDEMPOTENCY_KEY:'concurrent'}}
    p.send('tools/call',params); second.send('tools/call',params)
    one = p.recv()['result']['taskId']; two = second.recv()['result']['taskId']
    assert one == two
    db = DurableCapabilityStore(p.root/'state.db',Scope('fixture-owner','fixture-project'),enabled=True)
    assert db.refetch()['revision'] == 1


@pytest.mark.parametrize('version',['2025-11-25','1999-01-01'])
def test_new_adapters_do_not_inherit_legacy_or_unknown_versions(peers,version):
    p = peers(); tid = create(p)
    assert p.rpc('tasks/get',{'taskId':tid},version=version)['error']['code'] == -32022
    p.raw({'jsonrpc':'2.0','id':'legacy','method':'tasks/get','params':{'taskId':tid}})
    assert 'error' in p.recv()
    p.raw({'jsonrpc':'2.0','id':'legacy-list','method':'tools/list','params':{}})
    assert 'fixture_job' not in [t['name'] for t in p.recv()['result']['tools']]


def test_subscription_bound_duplicate_ids_and_forged_cancel(peers):
    p = peers()
    for i in range(32):
        p.send('subscriptions/listen',{'notifications':{}},request_id=f'sub-{i}')
        assert p.recv()['method'] == 'notifications/subscriptions/acknowledged'
    assert p.rpc('subscriptions/listen',{'notifications':{}})['error']['message'] == 'subscription_limit'
    assert p.rpc('subscriptions/listen',{'notifications':{}},request_id='sub-0')['error']['message'] == 'request_id_in_use'
    p.raw({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':{'forged':'id'}}})
    assert 'result' in p.rpc('server/discover')
    p.raw({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':'sub-0'}})
    p.send('subscriptions/listen',{'notifications':{}},request_id='replacement')
    assert p.recv()['params']['_meta'][SUBSCRIPTION_ID] == 'replacement'


ORACLE = ROOT / 'tests/fixtures/real_adapters/upstream-oracle.json'
ORACLE_CASES = json.loads(ORACLE.read_text())


def test_independent_oracle_fixture_hash():
    assert hashlib.sha256(ORACLE.read_bytes()).hexdigest() == '1b0c7aaec377477f201182999629cc52868971210d9427b6ed8c90f336c701bf'


@pytest.mark.parametrize('case', ORACLE_CASES, ids=lambda case: case['name'])
def test_independently_prepared_schema_oracle(case):
    # Authored before this implementation; includes intentional semantic blind
    # spots. Schema agreement alone is explicitly not behavioral certification.
    valid = True
    for check in case['checks']:
        value = case['wire']
        for part in check['path'].split('/')[1:]:
            part = part.replace('~1','/').replace('~0','~')
            value = value[int(part)] if isinstance(value,list) else value[part]
        try:
            validate(check['schema'],check['definition'],value)
        except WireError:
            valid = False
    assert valid is case['expected_schema_valid']


def test_missing_capability_error_data_and_mrtr_separation(peers):
    p = peers(); tid = create(p)
    reply = p.rpc('tasks/get',{'taskId':tid},extensions={})
    assert reply['error']['data'] == {'requiredCapabilities':{'extensions':{TASKS:{}}}}
    for field,value in [('inputResponses',{}),('requestState','untrusted')]:
        assert p.rpc('tools/call',{'name':'fixture_job','arguments':{'behavior':'wait'},field:value})['error']['message'] == 'mrtr_not_supported_for_task_creation'
    reply = p.rpc('subscriptions/listen',{'notifications':{'taskIds':[]}},extensions={})
    assert reply['error']['code'] == -32021


def test_input_capabilities_and_result_errors_do_not_mutate_state(tmp_path):
    r = local_runtime(tmp_path)
    tid = r.tasks.create('local_job',{}, {})['taskId']
    before = r.store.refetch()
    with pytest.raises(WireError, match='input_capability_not_negotiated'):
        r.tasks.require_input(tid,{'roots':{'method':'roots/list'}})
    assert r.store.refetch() == before
    with pytest.raises(WireError):
        r.tasks.finish(tid,error={'code':'not-an-integer','message':'invalid'})
    assert r.store.refetch() == before


def test_native_database_failure_is_wire_error_and_closes_stream(tmp_path):
    r = local_runtime(tmp_path)
    r.events.listen('stream',{'notifications':{'toolsListChanged':True}})
    path = r.store.path
    path.unlink(); path.mkdir()  # deliberate storage failure in a disposable fixture
    response = r.handle({'jsonrpc':'2.0','id':1,'method':'tasks/get','params':{
        'taskId':'missing','_meta':{PROTOCOL_VERSION:'2026-07-28',CLIENT_CAPABILITIES:{'extensions':EXTENSIONS}}}})
    assert response[0]['error']['code'] == -32603
    assert r.events.poll()[0]['id'] == 'stream'
