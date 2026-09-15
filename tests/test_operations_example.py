"""Deterministic guarantees around the worked operations graphs (no model calls)."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/workflows/16-operations-brief'
SPEC = importlib.util.spec_from_file_location('operations_report', EXAMPLE / 'report.py')
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)


class OperationsExampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        report.RUN = Path(self.directory.name)
        (report.RUN / 'canonicalize.md').write_text((EXAMPLE / 'input.txt').read_text())
        (report.RUN / 'validate.md').write_text(json.dumps(report.validate()))

    def test_known_totals_keep_aggregate_and_service_values_separate(self):
        self.assertEqual(report.revenue()['total_cents'], 2520000)
        self.assertEqual(report.revenue()['leading_service_cents'], 1250000)
        self.assertEqual(report.support()['total_tickets'], 170)
        self.assertEqual(report.support()['busiest_service_tickets'], 64)
        self.assertEqual(report.reliability()['error_percent'], 2.23)

    def test_invalid_counts_fail_before_analysis(self):
        row = report.rows()[0]
        row['errors'] = row['requests'] + 1
        with self.assertRaisesRegex(AssertionError, 'Errors cannot exceed'):
            report.validate_row(row)

    def test_duplicate_services_are_rejected(self):
        text = (EXAMPLE / 'input.txt').read_text()
        (report.RUN / 'canonicalize.md').write_text(text + text.splitlines()[0] + '\n')
        with self.assertRaisesRegex(AssertionError, 'Duplicate service'):
            report.validate()

    def test_both_sides_of_escalation_threshold(self):
        (report.RUN / 'validate.md').write_text(json.dumps({'rows': [dict(service='x', requests=100, errors=2, revenue_cents=0, tickets=0, note='demo')]}))
        self.assertTrue(report.reliability()['escalate'])
        (report.RUN / 'validate.md').write_text(json.dumps({'rows': [dict(service='x', requests=100, errors=1, revenue_cents=0, tickets=0, note='demo')]}))
        self.assertFalse(report.reliability()['escalate'])


    def test_rounded_display_does_not_trigger_escalation(self):
        (report.RUN / 'validate.md').write_text(json.dumps({'rows': [dict(service='x', requests=100000, errors=1999, revenue_cents=0, tickets=0, note='demo')]}))
        self.assertEqual(report.reliability()['error_percent'], 2.0)
        self.assertFalse(report.reliability()['escalate'])


if __name__ == '__main__':
    unittest.main()
