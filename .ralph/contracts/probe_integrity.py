#!/usr/bin/env python3
"""Independent PRD02 probes. Run with python -I -S to pin bundled validators.

All mutations are confined to temporary SCHOLIALANG_HOME/project directories.
Outputs observations and strict revised-contract assertions as JSON; --enforce
returns nonzero on any failed assertion. Run each host in a fresh interpreter.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


def snapshot(path):
    with sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        data = {name: sorted(conn.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr)
                for name in tables}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--host', required=True, choices=['codex', 'claude-code', 'ollama'])
    parser.add_argument('--enforce', action='store_true')
    args = parser.parse_args()
    path = args.source / 'plugins' / args.host / 'scholialang/scripts/scholialang_mcp_server.py'
    spec = importlib.util.spec_from_file_location('independent_integrity_server', path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    assert server.LINT_ENGINE == 'scholialang-vendored', server.LINT_ENGINE
    results = []

    def record(name, assertions, **observed):
        results.append({'name': name, 'pass': all(assertions.values()),
                        'assertions': assertions, 'observed': observed})

    def export_lint(dag_id, project):
        exported = server.tool_dag_export({'dag_id': dag_id, 'project_path': project, 'format': 'xml'})
        return server.tool_lint_snippet({'snippet': exported['content'][0]['text']})['structuredContent']

    for field, expected in [('tags', ['probe-tag', 'model:probe-model', 'orchestrator:probe-orchestrator']),
                            ('model', 'probe-model'), ('orchestrator', 'probe-orchestrator')]:
        with tempfile.TemporaryDirectory(prefix='j02-metadata-') as tmp:
            os.environ['SCHOLIALANG_HOME'] = tmp
            os.environ.pop('SCHOLIA_ORCHESTRATOR', None)
            project = str(Path(tmp) / 'project')
            tags = ['probe-tag', 'model:probe-model', 'orchestrator:probe-orchestrator']
            started = server.tool_dag_start({'project_path': project, 'title': 'Independent metadata fixture',
                                            'objective': 'Check metadata preservation.', 'tags': tags})['structuredContent']
            dag_id = started['dag_id']
            listed = server.tool_dag_list({'project_path': project, 'limit': 1})['structuredContent']['dags'][0]
            read = server.tool_dag_read({'project_path': project, 'dag_id': dag_id})['structuredContent']['dag']
            record('metadata_' + field, {'list_preserves_value': listed.get(field) == expected,
                                        'read_preserves_value': read.get(field) == expected,
                                        'list_read_parity': listed.get(field) == read.get(field)},
                   listed=listed.get(field), read=read.get(field), expected=expected)

    for outcome in ['met', 'unmet', 'partially_met']:
        for supported in [False, True]:
            with tempfile.TemporaryDirectory(prefix='j02-closure-') as tmp:
                os.environ['SCHOLIALANG_HOME'] = tmp
                project = str(Path(tmp) / 'project')
                identity = {'project_path': project, 'host': args.host, 'session_id': 'independent-closure', 'auto': False}
                dag_id = server.tool_dag_ensure_session({**identity, 'objective': 'Check explicit outcome safely.'})['structuredContent']['dag_id']
                premise = None
                if supported:
                    premise = server.tool_dag_add_atom({'dag_id': dag_id, 'project_path': project, 'kind': 'Observation',
                        'summary': 'Independent synthetic fixture observed the declared outcome: ' + outcome,
                        'content': 'Independent synthetic fixture observed the declared outcome: ' + outcome})['structuredContent']['atom']['id']
                db = Path(tmp) / 'scholialang.sqlite3'
                before = snapshot(db)
                response = None
                error = None
                try:
                    response = server.tool_dag_finish_session({**identity, 'outcome': outcome,
                        'summary': 'Independent test outcome: ' + outcome})['structuredContent']
                except ValueError as exc:
                    error = str(exc)
                after = snapshot(db)
                graph = server.load_dag(dag_id, project)
                lint = export_lint(dag_id, project)
                conclusions = [node for node in graph['nodes'].values() if node['kind'] == 'Concluding']
                observations = [node for node in graph['nodes'].values() if node['kind'] == 'Observation']
                if supported:
                    atom = response.get('atom', {}) if response else {}
                    edges = graph['edges']
                    record('supported_closure_' + outcome,
                        {'accepted': error is None and response is not None,
                         'explicit_outcome_preserved': atom.get('attributes', {}).get('status') == outcome,
                         'one_conclusion': len(conclusions) == 1,
                         'real_premise_link': any(edge['from'] == atom.get('id') and edge['to'] == premise for edge in edges),
                         'export_lints': lint['ok'] is True},
                        error=error, actual_status=atom.get('attributes', {}).get('status'), lint=lint)
                else:
                    record('premise_free_closure_' + outcome,
                        {'rejected': error is not None, 'zero_database_mutation': before == after,
                         'no_conclusion': not conclusions, 'no_fabricated_observation': not observations},
                        error=error, before_sha256=before, after_sha256=after,
                        conclusion_count=len(conclusions), observation_count=len(observations),
                        exported_lint_ok=lint['ok'], lint_errors=lint.get('errors', []),
                        historical_export_valid_control=lint['ok'] is True)

    for case, extra in [('lifecycle_no_outcome', {}), ('invalid_outcome', {'outcome': 'invented'}),
                         ('invalid_kind_outcome', {'kind': 'Observation', 'outcome': 'met'})]:
        with tempfile.TemporaryDirectory(prefix='j02-boundary-') as tmp:
            os.environ['SCHOLIALANG_HOME'] = tmp
            project = str(Path(tmp) / 'project')
            identity = {'project_path': project, 'host': args.host, 'session_id': 'independent-boundary', 'auto': False}
            dag_id = server.tool_dag_ensure_session({**identity, 'objective': 'Check lifecycle boundary.'})['structuredContent']['dag_id']
            db = Path(tmp) / 'scholialang.sqlite3'
            before = snapshot(db)
            error = None
            response = None
            try:
                response = server.tool_dag_finish_session({**identity, **extra})['structuredContent']
            except ValueError as exc:
                error = str(exc)
            if case == 'lifecycle_no_outcome':
                atom = response.get('atom', {}) if response else {}
                record(case, {'accepted': error is None, 'observation_only': atom.get('kind') == 'Observation'},
                       error=error, actual_kind=atom.get('kind'))
            else:
                record(case, {'rejected': error is not None, 'zero_database_mutation': before == snapshot(db)}, error=error)

    report = {'source': str(args.source.resolve()), 'host': args.host, 'server_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'validator_engine': server.LINT_ENGINE, 'validator_version': server.SCHOLIA_ATOMS.SCHOLIA_VALIDATOR_VERSION,
              'case_count': len(results), 'passed': sum(item['pass'] for item in results), 'results': results}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.enforce and report['passed'] != report['case_count'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
