import argparse
import json
import os
import sys
from .archive import archive_summary, build_archive, query_archive
from .audit import audit_capture
from .comp import compose
from .io import load, publish, publish_bytes
from .keyonly import export_key_only
from .service import process
from .settlement import settle

def main(argv=None):
    parser = argparse.ArgumentParser(prog='pedal-performance')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--source')
    modes.add_argument('--comp')
    modes.add_argument('--capture-audit')
    modes.add_argument('--settlement')
    modes.add_argument('--key-only')
    modes.add_argument('--archive-build')
    modes.add_argument('--archive-query')
    parser.add_argument('--previous')
    parser.add_argument('--archive')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.previous is not None and args.settlement is None:
        parser.error('--previous is only valid with --settlement')
    if args.archive is not None and args.archive_query is None:
        parser.error('--archive is only valid with --archive-query')
    if args.archive_query is not None and args.archive is None:
        parser.error('--archive-query requires --archive')
    try:
        if args.archive_build is not None:
            payload = build_archive(load(args.archive_build))
            publish_bytes(args.output, payload)
            summary = archive_summary(payload)
        elif args.archive_query is not None:
            def read_at(offset, length):
                with open(args.archive, 'rb') as source:
                    source.seek(offset)
                    return source.read(length)
            result = query_archive(read_at, os.path.getsize(args.archive),
                                   load(args.archive_query))
            publish(args.output, result)
            summary = {'voices': len(result['items']),
                       'next_cursor': result['next_cursor'] is not None}
        elif args.settlement is not None:
            previous = load(args.previous) if args.previous is not None else None
            result = settle(load(args.settlement), previous)
            summary = {'status': result['status'],
                       'total_cents': result['total_cents'],
                       'adjustments': len(result['adjustments'])}
        elif args.key_only is not None:
            result = export_key_only(load(args.key_only))
            summary = {'status': result['status'],
                       'demands': len(result['demands']),
                       'omitted_zero_voices': len(result['omitted_zero_voice_ids'])}
            if result['status'] == 'ok':
                summary.update(total_load_cost=result['total_load_cost'],
                               voices=len(result['voice_routes']))
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
        if args.archive_build is None and args.archive_query is None:
            publish(args.output, result)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
