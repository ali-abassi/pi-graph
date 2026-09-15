"""Small deterministic stages shared by the two operations example graphs."""
import hashlib
import html
import json
import os
from pathlib import Path
import sys


WORK = Path(os.environ.get('WORKFLOW_DIR', Path(__file__).parent))
RUN = Path(os.environ.get('RUN', '.'))


def read(step):
    return json.loads((RUN / f'{step}.md').read_text())


def validate_identity(row):
    assert set(row) == {'service', 'requests', 'errors', 'revenue_cents', 'tickets', 'note'}, 'Unexpected record fields'
    assert isinstance(row['service'], str), 'Service must be text'
    assert row['service'].strip(), 'Service is required'
    assert isinstance(row['note'], str), 'Note must be text'


def validate_counts(row):
    values = (row['requests'], row['errors'], row['revenue_cents'], row['tickets'])
    assert all(type(value) is int for value in values), 'Counts must be integers'
    assert min(values) >= 0, 'Counts must be nonnegative'
    assert row['errors'] <= row['requests'], 'Errors cannot exceed requests'


def validate_row(row):
    validate_identity(row)
    validate_counts(row)
    assert len(row['note']) <= 500, 'Note must be at most 500 characters'
    return row


def validate():
    rows = [validate_row(json.loads(line)) for line in (RUN / 'canonicalize.md').read_text().splitlines()]
    assert 1 <= len(rows) <= 100, 'Expected 1–100 records'
    assert len({row['service'] for row in rows}) == len(rows), 'Duplicate service'
    return {'rows': rows, 'records': len(rows), 'data_kind': 'synthetic demo'}


def rows():
    return read('validate')['rows']


def revenue():
    values = rows()
    return {'id': 'revenue', 'total_cents': sum(row['revenue_cents'] for row in values),
            'leading_service': max(values, key=lambda row: row['revenue_cents'])['service'],
            'leading_service_cents': max(row['revenue_cents'] for row in values)}


def reliability():
    values = rows()
    requests = sum(row['requests'] for row in values)
    errors = sum(row['errors'] for row in values)
    rate = round(errors / max(requests, 1) * 100, 2)
    return {'id': 'reliability', 'requests': requests, 'errors': errors, 'error_percent': rate,
            'escalate': requests > 0 and errors * 100 >= requests * 2, 'threshold_percent': 2}


def support():
    values = rows()
    return {'id': 'support', 'total_tickets': sum(row['tickets'] for row in values),
            'busiest_service': max(values, key=lambda row: row['tickets'])['service'],
            'busiest_service_tickets': max(row['tickets'] for row in values)}


def facts():
    return {name: read(name) for name in ('revenue', 'reliability', 'support')}


def route():
    return {'escalate': read('reliability')['escalate'], 'rule': 'Aggregate error rate >= 2%'}


def validate_prose(value):
    assert set(value) == {'title', 'body', 'sources'}, 'Expected title, body, sources'
    assert 1 <= len(value['title']) <= 100, 'Title length'
    assert 20 <= len(value['body']) <= 1200, 'Body length'


def model_gate():
    value = json.loads(Path(os.environ['OUT']).read_text())
    validate_prose(value)
    assert value['sources'], 'Evidence references required'
    assert set(value['sources']) <= {'revenue', 'reliability', 'support'}, 'Unknown reference'
    return {'verified': 'shape and source identifiers only; prose requires human review'}


def section(title, value):
    return f'<section><h2>{html.escape(title)}</h2><pre>{html.escape(json.dumps(value, indent=2))}</pre></section>'


def render():
    data = facts()
    summaries = [read(name) for name in ('summary', 'next_actions')]
    prose = ''.join(f'<section><h2>{html.escape(item["title"])}</h2><p>{html.escape(item["body"])}</p><small>Sources: {html.escape(", ".join(item["sources"]))}</small></section>' for item in summaries)
    metrics = ''.join(section(name.title(), value) for name, value in data.items())
    document = (WORK / 'report-template.html').read_text()
    (RUN / 'report.html').write_text(document.replace('{{prose}}', prose).replace('{{metrics}}', metrics))
    return {'artifact': 'report.html', 'sha256': hashlib.sha256((RUN / 'report.html').read_bytes()).hexdigest(),
            'escalation_required': data['reliability']['escalate'], 'facts': data}


def report_gate():
    value = json.loads(Path(os.environ['OUT']).read_text())
    payload = (RUN / 'report.html').read_bytes()
    assert hashlib.sha256(payload).hexdigest() == value['sha256'], 'Report integrity mismatch'
    assert b'Synthetic demo data' in payload and b'</html>' in payload, 'Report incomplete'
    return {'verified': True}


def triage():
    data = read('facts')['reliability']
    return {'priority': 'urgent' if data['escalate'] else 'routine', 'error_percent': data['error_percent'],
            'external_effects': False}


def timeline():
    return {'steps': ['Confirm telemetry against source records', 'Isolate the affected service',
                      'Review a reversible mitigation', 'Recheck the same error-rate window'],
            'status': 'proposed; nothing executed'}


def ownership():
    return {'roles': ['Incident lead', 'Service operator', 'Support liaison'],
            'assignment': 'unassigned; demo contains no real people'}


def communications():
    return {'audiences': ['Internal operations', 'Support'], 'delivery': 'draft only; no message sent',
            'required_facts': read('facts')['reliability']}


def packet():
    return {name: read(name) for name in ('triage', 'timeline', 'ownership', 'communications')}


COMMANDS = {function.__name__: function for function in (
    validate, revenue, reliability, support, facts, route, model_gate, render, report_gate,
    triage, timeline, ownership, communications, packet)}

if __name__ == '__main__':
    print(json.dumps(COMMANDS[sys.argv[1]](), indent=2))
