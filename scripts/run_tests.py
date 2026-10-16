#!/usr/bin/env python3
"""Run the full suite with coverage; missing client cores must fail CI."""
import json
import os
from pathlib import Path
import sys
import unittest

import coverage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    os.chdir(ROOT)
    artifacts = Path(os.environ.get('PARSER_TEST_ARTIFACTS', ROOT / 'artifacts/unit'))
    artifacts.mkdir(parents=True, exist_ok=True)
    measured = coverage.Coverage(config_file=str(ROOT / '.coveragerc'),
                                 data_file=str(artifacts / '.coverage'))
    measured.start()
    try:
        suite = unittest.defaultTestLoader.discover('tests')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        measured.stop()
        measured.save()

    measured.xml_report(outfile=str(artifacts / 'coverage.xml'))
    measured.json_report(outfile=str(artifacts / 'coverage.json'))
    measured.html_report(directory=str(artifacts / 'html'))
    percent = measured.report()
    minimum = measured.get_option('report:fail_under')
    summary = {'tests': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped),
               'coverage_percent': percent, 'minimum_coverage': minimum}
    (artifacts / 'tests.json').write_text(json.dumps(summary, indent=2) + '\n')
    if result.skipped:
        print('Full-suite checks require zero skipped tests. Use the development Docker image.', file=sys.stderr)
    if percent < minimum:
        print(f'Coverage {percent:.2f}% is below the {minimum}% minimum.', file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped and percent >= minimum else 1


if __name__ == '__main__':
    raise SystemExit(main())
