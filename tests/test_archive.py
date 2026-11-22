import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from pedal_performance.archive import (ARCHIVE_VERSION, build_archive,
                                       query_archive)
from pedal_performance.cli import main
from pedal_performance.io import load
from pedal_performance.service import process

ROOT = Path(__file__).resolve().parents[1]
BUDGET = 32_768


def on(event_id, tick, pitch=60, channel=0, velocity=80):
    return dict(id=event_id, tick=tick, channel=channel, kind='on',
                pitch=pitch, velocity=velocity)


def off(event_id, tick, pitch=60, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(event_id, tick, down, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='pedal', down=down)


def take(events, ppq=1, end_tick=20, tempos=None, sort=True):
    """One tick is exactly one microsecond unless a tempo map is given."""
    events = list(events)
    if sort:
        events.sort(key=lambda event: event['tick'])
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=ppq)],
                events=events)


class BytesReader:
    """Real range reader over a file that counts every requested byte."""

    def __init__(self, payload):
        self.payload = payload
        self.bytes = 0
        self.calls = []

    def __call__(self, offset, length):
        self.calls.append((offset, length))
        data = self.payload[offset:offset + length]
        self.bytes += length
        return data


class FileReader:
    """Range reader that really seeks within a file, counting every byte."""

    def __init__(self, path):
        self.path = path
        self.bytes = 0
        self.calls = []

    def __call__(self, offset, length):
        self.calls.append((offset, length))
        self.bytes += length
        with open(self.path, 'rb') as source:
            source.seek(offset)
            return source.read(length)


def query(payload, request):
    reader = BytesReader(payload)
    result = query_archive(reader, len(payload), request)
    return result, reader


def acceptance_take(lift_tick=6001):
    """ppq 3, ten microseconds per quarter, a long take.

    Voice ``a`` starts at tick 0, pedal at 1, key released at 2 and the pedal
    lifts at ``lift_tick``.  Voice ``b`` sounds over ticks [6000, 6003).  Two
    thousand three hundred short notes stop between ticks 10 and 4609 and
    another 2300 start and stop between 7000 and 11599; both batches are on
    their own channels and are untouched by ``a``'s pedal.
    """
    events = [on('a', 0), pedal('ped', 1, True), off('a_off', 2),
              pedal('lift', lift_tick, False),
              on('b', 6000, pitch=64, channel=1),
              off('b_off', 6003, pitch=64, channel=1)]
    tick = 10
    for index in range(2300):
        events.append(on(f'x{index}', tick, pitch=70, channel=2))
        events.append(off(f'xo{index}', tick + 1, pitch=70, channel=2))
        tick += 2
    tick = 7000
    for index in range(2300):
        events.append(on(f'y{index}', tick, pitch=72, channel=3))
        events.append(off(f'yo{index}', tick + 1, pitch=72, channel=3))
        tick += 2
    return take(events, ppq=3, end_tick=12000,
                tempos=[dict(tick=0, microseconds_per_quarter=10)])


class AcceptanceTests(unittest.TestCase):
    def test_two_item_window_and_budget(self):
        payload = build_archive(acceptance_take())
        self.assertLessEqual(len(payload), 8_000_000)
        first, reader = query(payload, {'from_us': '20000', 'to_us': '20010',
                                        'limit': 1})
        self.assertEqual([item['voice']['voice_id'] for item in first['items']],
                         ['a'])
        self.assertEqual(first['items'][0]['visible_duration_us'], '10/3')
        self.assertEqual(first['items'][0]['visible_from_us'], '20000')
        self.assertEqual(first['items'][0]['visible_to_us'], '60010/3')
        voice = first['items'][0]['voice']
        self.assertEqual((voice['key_release_tick'], voice['release_event_id']),
                         (2, 'a_off'))
        self.assertEqual((voice['sound_end_tick'], voice['end_reason'],
                          voice['end_event_id']), (6001, 'pedal_release', 'lift'))
        self.assertIsNotNone(first['next_cursor'])
        self.assertLessEqual(reader.bytes, BUDGET)

        second, reader = query(payload, {'from_us': '20000', 'to_us': '20010',
                                         'limit': 1,
                                         'cursor': first['next_cursor']})
        self.assertEqual([item['voice']['voice_id'] for item in second['items']],
                         ['b'])
        self.assertEqual(second['items'][0]['visible_duration_us'], '10')
        self.assertEqual(second['items'][0]['visible_from_us'], '20000')
        self.assertEqual(second['items'][0]['visible_to_us'], '20010')
        self.assertIsNone(second['next_cursor'])
        self.assertLessEqual(reader.bytes, BUDGET)

    def test_moving_the_lift_changes_the_window_and_invalidates_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / 'first.pparch'
            moved_path = Path(tmp) / 'moved.pparch'
            first_path.write_bytes(build_archive(acceptance_take(lift_tick=6001)))
            moved_path.write_bytes(build_archive(acceptance_take(lift_tick=6000)))
            first_reader = FileReader(first_path)
            first = query_archive(first_reader, first_path.stat().st_size,
                                  {'from_us': '20000', 'to_us': '20010', 'limit': 1})
            self.assertIsNotNone(first['next_cursor'])
            self.assertLessEqual(first_reader.bytes, BUDGET)
            continuation = query_archive(
                first_reader, first_path.stat().st_size,
                {'from_us': '20000', 'to_us': '20010', 'limit': 1,
                 'cursor': first['next_cursor']})
            self.assertEqual([item['voice']['voice_id']
                              for item in continuation['items']], ['b'])
            self.assertLessEqual(first_reader.bytes, BUDGET)

            moved_reader = FileReader(moved_path)
            moved = query_archive(moved_reader, moved_path.stat().st_size,
                                  {'from_us': '20000', 'to_us': '20010', 'limit': 1})
            # a now stops at 6000 ticks = 20000 us, so the window is only b.
            self.assertEqual([item['voice']['voice_id'] for item in moved['items']],
                             ['b'])
            self.assertIsNone(moved['next_cursor'])
            self.assertLessEqual(moved_reader.bytes, BUDGET)
            self.assertNotEqual(first['archive_id'], moved['archive_id'])
            moved_payload = moved_path.read_bytes()
            with self.assertRaises(ValueError):
                query_archive(BytesReader(moved_payload), len(moved_payload),
                              {'from_us': '20000', 'to_us': '20010', 'limit': 1,
                               'cursor': first['next_cursor']})


class EvidenceTests(unittest.TestCase):
    def test_report_voice_objects_match_the_legacy_report(self):
        source = load(ROOT / 'fixtures' / 'input.json')
        payload = build_archive(source)
        result, _ = query(payload, {'from_us': '0', 'to_us': '2500000',
                                    'limit': 100})
        legacy = process(source)['voices']
        self.assertEqual(len(result['items']), len(legacy))
        self.assertEqual([item['voice'] for item in result['items']], legacy)

    def test_trimmed_window_keeps_real_stop_evidence(self):
        payload = build_archive(acceptance_take())
        # Right end 19999 us is before a's real stop at 20003 1/3 us, and b
        # only begins at 20000 us, so only a is in the window.
        result, _ = query(payload, {'from_us': '19990', 'to_us': '19999',
                                    'limit': 10})
        self.assertEqual([item['voice']['voice_id'] for item in result['items']],
                         ['a'])
        item = result['items'][0]
        self.assertEqual(item['visible_to_us'], '19999')
        self.assertEqual(item['visible_duration_us'], '9')
        self.assertEqual(item['voice']['sound_end_us'], '60010/3')
        self.assertEqual((item['voice']['end_reason'], item['voice']['end_event_id']),
                         ('pedal_release', 'lift'))
        self.assertEqual(item['voice']['key_release_us'], '20/3')

    def test_same_tick_onsets_page_by_voice_id(self):
        events = [on('n2', 4), on('n10', 4, pitch=61), on('n1', 4, pitch=62),
                  off('o2', 8), off('o10', 8, pitch=61), off('o1', 8, pitch=62)]
        payload = build_archive(take(events, ppq=1, end_tick=10))
        seen = []
        cursor = None
        while True:
            request = {'from_us': '0', 'to_us': '10', 'limit': 1}
            if cursor is not None:
                request['cursor'] = cursor
            page, _ = query(payload, request)
            seen.extend(item['voice']['voice_id'] for item in page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(seen, ['n1', 'n10', 'n2'])

    def test_same_pitch_overlap_releases_fifo(self):
        events = [on('a', 0), on('b', 1), pedal('p', 2, True),
                  off('first', 5), off('second', 6), pedal('up', 9, False)]
        payload = build_archive(take(events))
        result, _ = query(payload, {'from_us': '0', 'to_us': '10', 'limit': 10})
        voices = {item['voice']['voice_id']: item['voice']
                  for item in result['items']}
        self.assertEqual((voices['a']['key_release_tick'],
                          voices['a']['release_event_id'],
                          voices['a']['end_reason'],
                          voices['a']['end_event_id']),
                         (5, 'first', 'pedal_release', 'up'))
        self.assertEqual((voices['b']['key_release_tick'],
                          voices['b']['release_event_id'],
                          voices['b']['end_reason'],
                          voices['b']['end_event_id']),
                         (6, 'second', 'pedal_release', 'up'))

    def test_end_of_take_and_zero_duration(self):
        events = [on('held', 0, pitch=60), on('zero', 5, pitch=62),
                  off('zero_off', 5, pitch=62),
                  on('done', 2, pitch=61), off('done_off', 3, pitch=61)]
        payload = build_archive(take(events, ppq=1, end_tick=10))
        result, _ = query(payload, {'from_us': '0', 'to_us': '10', 'limit': 10})
        self.assertEqual([item['voice']['voice_id'] for item in result['items']],
                         ['held', 'done'])
        held = result['items'][0]['voice']
        self.assertEqual((held['key_release_tick'], held['release_event_id'],
                          held['end_reason'], held['end_event_id']),
                         (None, None, 'end_of_take', None))
        self.assertEqual(held['sound_end_tick'], 10)

    def test_silent_window_returns_empty(self):
        payload = build_archive(take([on('a', 0), off('o', 2)],
                                     ppq=1, end_tick=100))
        result, _ = query(payload, {'from_us': '10', 'to_us': '90', 'limit': 10})
        self.assertEqual(result['items'], [])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual(result['from_us'], '10')
        self.assertEqual(result['to_us'], '90')

    def test_fractional_boundaries_and_mid_segment_tempo(self):
        # ppq 3, rate changes inside the window; exact thirds throughout.
        events = [on('a', 0), off('a_off', 4)]
        payload = build_archive(take(
            events, ppq=3, end_tick=12,
            tempos=[dict(tick=0, microseconds_per_quarter=10),
                    dict(tick=2, microseconds_per_quarter=7)]))
        # tick 0 -> 0; tick 2 -> 20/3; tick 4 -> 20/3 + 2*7/3 = 34/3.
        result, _ = query(payload, {'from_us': '3', 'to_us': '15/2',
                                    'limit': 10})
        self.assertEqual(len(result['items']), 1)
        item = result['items'][0]
        self.assertEqual(item['visible_from_us'], '3')
        self.assertEqual(item['visible_to_us'], '15/2')
        self.assertEqual(item['visible_duration_us'], '9/2')
        self.assertEqual(item['voice']['sound_end_us'], '34/3')

    def test_pagination_concatenation_matches_whole_window(self):
        source = load(ROOT / 'fixtures' / 'input-score.json')
        source.pop('score')
        payload = build_archive(source)
        whole, _ = query(payload, {'from_us': '0', 'to_us': '2500000',
                                   'limit': 100})
        collected = []
        cursor = None
        while True:
            request = {'from_us': '0', 'to_us': '2500000', 'limit': 1}
            if cursor is not None:
                request['cursor'] = cursor
            page, _ = query(payload, request)
            collected.extend(page['items'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(collected, whole['items'])

    def test_changing_page_size_with_a_cursor(self):
        payload = build_archive(load(ROOT / 'fixtures' / 'input.json'))
        first, _ = query(payload, {'from_us': '0', 'to_us': '2500000',
                                   'limit': 1})
        rest, _ = query(payload, {'from_us': '0', 'to_us': '2500000', 'limit': 100,
                                  'cursor': first['next_cursor']})
        self.assertEqual(len(rest['items']), 2)
        self.assertIsNone(rest['next_cursor'])


class IdentityTests(unittest.TestCase):
    def test_rebuild_is_stable_and_key_order_is_irrelevant(self):
        source = load(ROOT / 'fixtures' / 'input.json')
        first = build_archive(source)
        self.assertEqual(first, build_archive(json.loads(json.dumps(source))))
        reordered = {key: source[key] for key in reversed(list(source))}
        reordered['events'] = [dict(reversed(list(event.items())))
                               for event in source['events']]
        self.assertEqual(first, build_archive(reordered))
        one, _ = query(first, {'from_us': '0', 'to_us': '2500000', 'limit': 100})
        two, _ = query(build_archive(reordered),
                       {'from_us': '0', 'to_us': '2500000', 'limit': 100})
        self.assertEqual(one['archive_id'], two['archive_id'])
        self.assertEqual(one['items'], two['items'])

    def test_same_tick_order_changes_identity(self):
        events = [on('a', 0), pedal('p', 4, True), off('a_off', 4)]
        source = take(events)
        swapped = json.loads(json.dumps(source))
        swapped['events'][1], swapped['events'][2] = \
            swapped['events'][2], swapped['events'][1]
        self.assertNotEqual(build_archive(source), build_archive(swapped))
        one, _ = query(build_archive(source),
                       {'from_us': '0', 'to_us': '10', 'limit': 10})
        two, _ = query(build_archive(swapped),
                       {'from_us': '0', 'to_us': '10', 'limit': 10})
        self.assertNotEqual(one['archive_id'], two['archive_id'])


class RequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = build_archive(load(ROOT / 'fixtures' / 'input.json'))

    def ask(self, request):
        return query_archive(BytesReader(self.payload), len(self.payload), request)

    def test_invalid_requests(self):
        bad = [
            {},
            {'from_us': '0', 'to_us': '1'},
            {'from_us': '0', 'to_us': '1', 'limit': 1, 'extra': 1},
            {'from_us': 0, 'to_us': '1', 'limit': 1},
            {'from_us': '0', 'to_us': 1, 'limit': 1},
            {'from_us': '0', 'to_us': '1', 'limit': 0},
            {'from_us': '0', 'to_us': '1', 'limit': 101},
            {'from_us': '0', 'to_us': '1', 'limit': True},
            {'from_us': '0', 'to_us': '1', 'limit': 1.0},
            {'from_us': '-1', 'to_us': '1', 'limit': 1},
            {'from_us': '01', 'to_us': '1', 'limit': 1},
            {'from_us': '1', 'to_us': '1', 'limit': 1},
            {'from_us': '2', 'to_us': '1', 'limit': 1},
            {'from_us': '0', 'to_us': '2500001', 'limit': 1},
            {'from_us': '1', 'to_us': '1/2', 'limit': 1},
            {'from_us': '2/4', 'to_us': '1', 'limit': 1},
            {'from_us': '0', 'to_us': '1/1', 'limit': 1},
            {'from_us': '0', 'to_us': '0/1', 'limit': 1},
            {'from_us': '0', 'to_us': '1', 'limit': 1, 'cursor': ''},
            {'from_us': '0', 'to_us': '1', 'limit': 1, 'cursor': 5},
            {'from_us': '0', 'to_us': '1', 'limit': 1, 'cursor': 'bogus'},
        ]
        for request in bad:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.ask(request)

    def test_window_limit_is_the_exact_total_duration(self):
        total = process(load(ROOT / 'fixtures' / 'input.json'))['duration_us']
        result = self.ask({'from_us': '0', 'to_us': total, 'limit': 10})
        self.assertEqual(result['to_us'], total)

    def test_invalid_source_events_do_not_build(self):
        for events in ([off('orphan', 0)],
                       [on('a', 0), off('o', 0), off('o2', 1)]):
            with self.assertRaises(ValueError):
                build_archive(take(events))

    def test_cursor_is_bound_to_archive_and_window(self):
        other_source = load(ROOT / 'fixtures' / 'input.json')
        other_source['events'][0]['velocity'] = 91
        other = build_archive(other_source)
        first = self.ask({'from_us': '0', 'to_us': '2500000', 'limit': 1})
        with self.assertRaises(ValueError):
            query_archive(BytesReader(other), len(other),
                          {'from_us': '0', 'to_us': '2500000', 'limit': 1,
                           'cursor': first['next_cursor']})
        with self.assertRaises(ValueError):
            self.ask({'from_us': '0', 'to_us': '2000000', 'limit': 1,
                      'cursor': first['next_cursor']})
        with self.assertRaises(ValueError):
            self.ask({'from_us': '1', 'to_us': '2500000', 'limit': 1,
                      'cursor': first['next_cursor']})


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.payload = build_archive(load(ROOT / 'fixtures' / 'input.json'))

    def test_truncated_package_fails(self):
        short = self.payload[:-4]
        with self.assertRaises(ValueError):
            query_archive(BytesReader(short), len(short),
                          {'from_us': '0', 'to_us': '2500000', 'limit': 100})

    def test_short_read_is_not_an_empty_page(self):
        def short_reader(offset, length):
            return self.payload[offset:offset + length][:-1]
        with self.assertRaises(ValueError):
            query_archive(short_reader, len(self.payload),
                          {'from_us': '0', 'to_us': '2500000', 'limit': 100})

    def test_read_beyond_the_package_fails(self):
        def overrun(offset, length):
            return b'\0' * length
        with self.assertRaises(ValueError):
            query_archive(overrun, len(self.payload) - 1,
                          {'from_us': '0', 'to_us': '2500000', 'limit': 100})

    def test_wrong_magic_and_version_fail(self):
        bad_magic = bytearray(self.payload)
        bad_magic[0] = ord('X')
        with self.assertRaises(ValueError):
            query_archive(BytesReader(bytes(bad_magic)), len(self.payload),
                          {'from_us': '0', 'to_us': '2500000', 'limit': 1})
        bad_version = bytearray(self.payload)
        bad_version[8] = ARCHIVE_VERSION + 1
        with self.assertRaises(ValueError):
            query_archive(BytesReader(bytes(bad_version)), len(self.payload),
                          {'from_us': '0', 'to_us': '2500000', 'limit': 1})

    def test_small_window_does_not_read_the_whole_package(self):
        payload = build_archive(acceptance_take())
        _, reader = query(payload, {'from_us': '20000', 'to_us': '20010',
                                    'limit': 1})
        self.assertLess(reader.bytes, len(payload) // 10)

    def test_file_reader_covers_both_pages_within_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'take.pparch'
            path.write_bytes(build_archive(acceptance_take()))
            reader = FileReader(path)
            first = query_archive(reader, path.stat().st_size,
                                  {'from_us': '20000', 'to_us': '20010',
                                   'limit': 1})
            self.assertEqual([item['voice']['voice_id'] for item in first['items']],
                             ['a'])
            self.assertLessEqual(reader.bytes, BUDGET)
            second = query_archive(reader, path.stat().st_size,
                                   {'from_us': '20000', 'to_us': '20010',
                                    'limit': 1,
                                    'cursor': first['next_cursor']})
            self.assertEqual([item['voice']['voice_id'] for item in second['items']],
                             ['b'])
            self.assertLessEqual(reader.bytes, BUDGET)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_build_then_query_after_source_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json',
                                json.loads((ROOT / 'fixtures' / 'input.json')
                                           .read_text()))
            archive = Path(tmp) / 'take.pparch'
            request = self.write(tmp, 'query.json',
                                 {'from_us': '0', 'to_us': '2500000', 'limit': 1})
            result = Path(tmp) / 'page.json'
            self.assertEqual(self.run_cli(['--archive-build', str(source),
                                           '--output', str(archive)]), 0)
            self.assertTrue(archive.exists())
            source.unlink()
            self.assertEqual(self.run_cli(['--archive-query', str(request),
                                           '--archive', str(archive),
                                           '--output', str(result)]), 0)
            published = load(result)
            self.assertEqual(set(published),
                             {'schema_version', 'archive_id', 'from_us', 'to_us',
                              'items', 'next_cursor'})
            self.assertEqual(published['schema_version'], 1)
            self.assertEqual(len(published['items']), 1)
            self.assertEqual(len(published['archive_id']), 64)
            self.assertIsNotNone(published['next_cursor'])

    def test_archive_and_result_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', load(ROOT / 'fixtures' / 'input.json'))
            archive = Path(tmp) / 'take.pparch'
            self.assertEqual(self.run_cli(['--archive-build', str(source),
                                           '--output', str(archive)]), 0)
            original = archive.read_bytes()
            self.assertEqual(self.run_cli(['--archive-build', str(source),
                                           '--output', str(archive)]), 2)
            self.assertEqual(archive.read_bytes(), original)
            request = self.write(tmp, 'query.json',
                                 {'from_us': '0', 'to_us': '2500000', 'limit': 10})
            result = Path(tmp) / 'page.json'
            result.write_text('existing')
            self.assertEqual(self.run_cli(['--archive-query', str(request),
                                           '--archive', str(archive),
                                           '--output', str(result)]), 2)
            self.assertEqual(result.read_text(), 'existing')
            self.assertEqual(list(Path(tmp).glob('.report-*')), [])

    def test_invalid_inputs_exit_two_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = self.write(tmp, 'broken.json',
                                take([off('orphan', 0)]))
            archive = Path(tmp) / 'take.pparch'
            self.assertEqual(self.run_cli(['--archive-build', str(broken),
                                           '--output', str(archive)]), 2)
            self.assertFalse(archive.exists())

            source = self.write(tmp, 'in.json', load(ROOT / 'fixtures' / 'input.json'))
            self.assertEqual(self.run_cli(['--archive-build', str(source),
                                           '--output', str(archive)]), 0)
            bad_request = self.write(tmp, 'bad.json',
                                     {'from_us': '0', 'to_us': '1', 'limit': 0})
            result = Path(tmp) / 'page.json'
            self.assertEqual(self.run_cli(['--archive-query', str(bad_request),
                                           '--archive', str(archive),
                                           '--output', str(result)]), 2)
            self.assertFalse(result.exists())

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json',
                                '{"ppq": 1, "ppq": 1, "end_tick": 1,'
                                ' "tempos": [], "events": []}')
            target = Path(tmp) / 'take.pparch'
            self.assertEqual(self.run_cli(['--archive-build', str(source),
                                           '--output', str(target)]), 2)
            self.assertFalse(target.exists())

    def test_modes_and_archive_flag_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', load(ROOT / 'fixtures' / 'input.json'))
            target = Path(tmp) / 'out'
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--archive-build', str(source),
                              '--archive-query', str(source),
                              '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--archive-query', str(source), '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--source', str(source), '--archive', str(target),
                              '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
