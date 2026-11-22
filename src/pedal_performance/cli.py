import argparse
import json
import sys
from .audit import audit_capture
from .comp import compose
from .io import load, publish
from .service import process
from .settlement import settle

def main(argv=None):
    parser = argparse.ArgumentParser(prog='pedal-performance')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--source')
    modes.add_argument('--comp')
    modes.add_argument('--capture-audit')
    modes.add_argument('--settlement')
    parser.add_argument('--previous')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.previous is not None and args.settlement is None:
        parser.error('--previous is only valid with --settlement')
    try:
        if args.settlement is not None:
            previous = load(args.previous) if args.previous is not None else None
            result = settle(load(args.settlement), previous)
            summary = {'status': result['status'],
                       'total_cents': result['total_cents'],
                       'adjustments': len(result['adjustments'])}
        elif args.capture_audit is not None:
            result = audit_capture(load(args.capture_audit))
            summary = {'status': result['status'], 'channels': len(result['channels'])}
        elif args.comp is not None:
            result = compose(load(args.comp))
            summary = {'status': result['status']}
            if result['plan'] is not None:
                summary.update(switches=result['plan']['switches'],
                               total_cost=result['plan']['total_cost'],
                               total_duration_us=result['plan']['total_duration_us'])
        else:
            result = process(load(args.source))
            summary = result['summary']
        publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
