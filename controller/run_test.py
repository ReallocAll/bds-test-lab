#!/usr/bin/env python3
import argparse
import json
import pathlib
import subprocess
import time


def run(cmd, timeout=300):
    return subprocess.run(cmd, shell=True, timeout=timeout, text=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    out = pathlib.Path('test-results.json')
    result = {
        'platform': args.platform,
        'checks': [],
        'status': 'bootstrap'
    }
    # Provider download and BDS deployment are isolated here so providers can evolve.
    result['checks'].append({'name': 'provider-bootstrap', 'status': 'pending'})
    result['checks'].append({'name': 'bds-lifecycle', 'status': 'pending'})
    result['checks'].append({'name': 'spark-command-suite', 'status': 'pending'})
    result['checks'].append({'name': 'profiler', 'status': 'pending'})
    result['status'] = 'framework-created'
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
