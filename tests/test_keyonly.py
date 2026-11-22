import json
from pathlib import Path
import tempfile
import unittest

from pedal_performance.cli import main
from pedal_performance.io import load
from pedal_performance.keyonly import export_key_only
from pedal_performance.service import process
from pedal_performance.voices import perform

ROOT = Path(__file__).resolve().parents[1]


def on(event_id, tick, pitch=60, channel=0, velocity=80):
    return dict(id=event_id, tick=tick, channel=channel, kind='on',
                pitch=pitch, velocity=velocity)


def off(event_id, tick, pitch=60, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(event_id, tick, down, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='pedal', down=down)


def perf(events, ppq=1, end_tick=20, tempos=None):
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=1000000)],
                events=events)


def costs(program_id, load_cost):
    return dict(program_id=program_id, load_cost=load_cost)


def lane(number, *entries):
    return dict(lane=number, program_costs=list(entries))


def manifest(performance, channels, lanes):
    return dict(performance=performance,
                channel_programs=[dict(channel=channel, program_id=program)
                                  for channel, program in channels],
                lanes=lanes)


def acceptance_events(lift_tick=7):
    """Voice a is pedal-sustained to ``lift_tick``; voice b sits on top of it."""
    events = [
        on('a-on', 0), pedal('pedal-down', 1, True), off('a-off', 2),
        on('b-on', 4, velocity=70), off('b-off', 6),
        pedal('pedal-up', lift_tick, False),
        on('q-on', 9, pitch=64, channel=1, velocity=90), off('q-off', 10, pitch=64, channel=1),
    ]
    priority = {'pedal': 0, 'on': 1, 'off': 2}
    return sorted(events, key=lambda event: (event['tick'], priority[event['kind']]))


def acceptance_manifest(lift_tick=7, lane_count=3):
    device = [
        lane(0, costs('Q', 2), costs('P', 1)),
        lane(1, costs('P', 3)),
        lane(2, costs('P', 4)),
    ][:lane_count]
    return manifest(perf(acceptance_events(lift_tick)),
                    [(0, 'P'), (1, 'Q')], device)


class AcceptanceTests(unittest.TestCase):
    def assert_replays_exactly(self, data, result):
        """Every route replays to its source's pitch, velocity, onset and stop."""
        source = {voice['voice_id']: voice
                  for voice in perform(data['performance'])}
        played = {voice['voice_id']: voice for voice in process(result['performance'])['voices']}
        for route in result['voice_routes']:
            got = played[route['on_event_id']]
            want = source[route['source_voice_id']]
            self.assertEqual((got['pitch'], got['velocity']),
                             (want['pitch'], want['velocity']))
            self.assertEqual((got['start_tick'], got['sound_end_tick']),
                             (want['start_tick'], want['sound_end_tick']))

    def test_pedal_bakes_two_lanes_and_reserves_the_shared_lane(self):
        data = acceptance_manifest()
        result = export_key_only(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['demands'], [
            {'program_id': 'P', 'min_lanes': 2},
            {'program_id': 'Q', 'min_lanes': 1},
        ])
        self.assertEqual(result['configuration'], [
            {'lane': 0, 'program_id': 'Q'},
            {'lane': 1, 'program_id': 'P'},
            {'lane': 2, 'program_id': 'P'},
        ])
        # 2 (Q) + 3 + 4, not 1 + 2 even though P and Q never sound together.
        self.assertEqual(result['total_load_cost'], 9)
        self.assert_replays_exactly(data, result)

    def test_two_lanes_report_demand_instead_of_shortening(self):
        result = export_key_only(acceptance_manifest(lane_count=2))
        self.assertEqual(result['status'], 'no_layout')
        self.assertEqual(result['demands'], [
            {'program_id': 'P', 'min_lanes': 2},
            {'program_id': 'Q', 'min_lanes': 1},
        ])
        for field in ('configuration', 'total_load_cost', 'voice_routes', 'performance'):
            self.assertIsNone(result[field])

    def test_lifting_the_pedal_at_the_second_onset_fits_one_lane(self):
        result = export_key_only(acceptance_manifest(lift_tick=4))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['demands'], [
            {'program_id': 'P', 'min_lanes': 1},
            {'program_id': 'Q', 'min_lanes': 1},
        ])
        p_lanes = {route['lane'] for route in result['voice_routes']
                   if route['source_voice_id'] in ('a-on', 'b-on')}
        self.assertEqual(len(p_lanes), 1)
        lane_number = p_lanes.pop()
        events = result['performance']['events']
        same = [event for event in events if event['channel'] == lane_number
                and event['pitch'] == 60]
        self.assertEqual([(event['kind'], event['tick']) for event in same],
                         [('on', 0), ('off', 4), ('on', 4), ('off', 6)])


class VoiceRulesTests(unittest.TestCase):
    def test_same_pitch_overlap_across_source_channels(self):
        # Same pitch sounds overlap across two channels with the same program.
        events = [on('a', 0, channel=0), on('b', 1, channel=1),
                  off('a-off', 5, channel=0), off('b-off', 6, channel=1)]
        data = manifest(perf(events), [(0, 'P'), (1, 'P')],
                        [lane(0, costs('P', 1)), lane(1, costs('P', 1))])
        result = export_key_only(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['demands'], [{'program_id': 'P', 'min_lanes': 2}])
        self.assertEqual(result['total_load_cost'], 2)
        lanes = {route['lane'] for route in result['voice_routes']}
        self.assertEqual(len(lanes), 2)

    def test_other_pitch_overlapping_does_not_raise_the_lane_count(self):
        # Two simultaneous pitches on one channel still share a single lane.
        events = [on('a', 0, pitch=60), on('b', 0, pitch=67),
                  off('a-off', 5, pitch=60), off('b-off', 5, pitch=67)]
        data = manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))])
        result = export_key_only(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['demands'], [{'program_id': 'P', 'min_lanes': 1}])
        self.assertEqual({route['lane'] for route in result['voice_routes']}, {0})

    def test_touching_intervals_share_a_lane(self):
        # A previous voice stopping exactly at the next onset is not overlap.
        events = [on('a', 0, pitch=60), off('a-off', 4, pitch=60),
                  on('b', 4, pitch=60), off('b-off', 8, pitch=60)]
        data = manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))])
        result = export_key_only(data)
        self.assertEqual(result['demands'], [{'program_id': 'P', 'min_lanes': 1}])
        self.assertEqual(result['status'], 'ok')

    def test_generated_order_puts_off_before_on_and_sorts_by_lane(self):
        # At tick 4 voice a stops and voices b/c (pitch 60/61) begin; a new
        # same-pitch key can start on that same lane only after the off.
        events = [on('a', 0, pitch=60), off('a-off', 4, pitch=60),
                  on('b', 4, pitch=60), on('c', 4, pitch=61),
                  off('b-off', 8, pitch=60), off('c-off', 8, pitch=61)]
        data = manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))])
        result = export_key_only(data)
        at_four = [(event['kind'], event['pitch']) for event in result['performance']['events']
                   if event['tick'] == 4]
        self.assertEqual(at_four, [('off', 60), ('on', 60), ('on', 61)])
        self.assertEqual([route['source_voice_id'] for route in result['voice_routes']],
                         ['a', 'b', 'c'])

    def test_arrangement_prefers_lowest_free_lane(self):
        # Two overlapping same-pitch voices must split lanes 0 and 1; the earlier
        # voice takes the lower lane.
        events = [on('a', 0, pitch=60), on('b', 1, pitch=60),
                  off('a-off', 5, pitch=60), off('b-off', 6, pitch=60)]
        data = manifest(perf(events), [(0, 'P')],
                        [lane(0, costs('P', 5)), lane(1, costs('P', 5))])
        result = export_key_only(data)
        lanes = {route['source_voice_id']: route['lane'] for route in result['voice_routes']}
        self.assertEqual(lanes, {'a': 0, 'b': 1})


class OptimisationTests(unittest.TestCase):
    def test_global_cost_beats_a_cheaper_single_lane(self):
        # Program Q is only playable on the expensive lane 0; P could take the
        # cheap lane 0, but the globally cheapest layout loads Q there.
        events = [on('p', 0, channel=0), off('p-off', 2, channel=0),
                  on('q', 5, pitch=70, channel=1), off('q-off', 7, pitch=70, channel=1)]
        data = manifest(perf(events), [(0, 'P'), (1, 'Q')],
                        [lane(0, costs('P', 1), costs('Q', 5)),
                         lane(1, costs('P', 9)),
                         lane(2, costs('P', 9))])
        result = export_key_only(data)
        self.assertEqual(result['total_load_cost'], 14)
        self.assertEqual(result['configuration'][0], {'lane': 0, 'program_id': 'Q'})

    def test_equal_cost_stability_under_device_reordering(self):
        events = [on('p', 0, channel=0), off('p-off', 2, channel=0),
                  on('q', 5, pitch=70, channel=1), off('q-off', 7, pitch=70, channel=1)]
        base = manifest(perf(events), [(0, 'P'), (1, 'Q')],
                        [lane(0, costs('P', 1), costs('Q', 3)),
                         lane(1, costs('Q', 2), costs('P', 1)),
                         lane(2, costs('P', 1), costs('Q', 2))])
        first = export_key_only(base)
        shuffled = json.loads(json.dumps(base))
        shuffled['lanes'] = [shuffled['lanes'][2], shuffled['lanes'][0], shuffled['lanes'][1]]
        shuffled['lanes'].reverse()
        second = export_key_only(shuffled)
        self.assertEqual(first, second)
        # Equal minimal cost: choose the lexicographically smallest program sequence.
        self.assertEqual(first['total_load_cost'], 3)
        self.assertEqual(first['configuration'], [
            {'lane': 0, 'program_id': 'P'},
            {'lane': 1, 'program_id': 'Q'},
            {'lane': 2, 'program_id': None},
        ])

    def test_unloaded_lane_sorts_above_programs(self):
        events = [on('p', 0, channel=0), off('p-off', 2, channel=0)]
        data = manifest(perf(events), [(0, 'P')],
                        [lane(0, costs('P', 2)), lane(1, costs('P', 2))])
        result = export_key_only(data)
        self.assertEqual(result['configuration'], [
            {'lane': 0, 'program_id': 'P'},
            {'lane': 1, 'program_id': None},
        ])
        self.assertEqual(result['total_load_cost'], 2)

    def test_fractional_tempo_and_end_of_take_stop(self):
        events = [on('a', 0, pitch=60), off('a-off', 1, pitch=60),
                  on('b', 5, pitch=61)]
        data = manifest(
            perf(events, ppq=3, end_tick=10,
                 tempos=[dict(tick=0, microseconds_per_quarter=1),
                         dict(tick=5, microseconds_per_quarter=7)]),
            [(0, 'P')], [lane(0, costs('P', 4))])
        result = export_key_only(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['performance']['tempos'], data['performance']['tempos'])
        generated = result['performance']
        self.assertEqual(generated['end_tick'], 10)
        # Voice a stops at its key release; voice b falls back to end-of-take.
        stops = [(event['kind'], event['tick']) for event in generated['events']
                 if event['kind'] == 'off']
        self.assertEqual(stops, [('off', 1), ('off', 10)])
        report = process(generated)
        self.assertEqual(report['duration_us'], '40/3')

    def test_voice_partials_and_zero_duration(self):
        events = [on('a', 0, pitch=60), off('a-off', 0, pitch=60),
                  on('b', 1, pitch=60), off('b-off', 3, pitch=60)]
        data = manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))])
        result = export_key_only(data)
        self.assertEqual(result['omitted_zero_voice_ids'], ['a'])
        self.assertEqual([route['source_voice_id'] for route in result['voice_routes']], ['b'])


class EdgeCaseTests(unittest.TestCase):
    def test_empty_performance_exports_nothing(self):
        data = manifest(perf([]), [], [lane(0, costs('P', 1))])
        result = export_key_only(data)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['demands'], [])
        self.assertEqual(result['configuration'], [{'lane': 0, 'program_id': None}])
        self.assertEqual(result['total_load_cost'], 0)
        self.assertEqual(result['voice_routes'], [])
        self.assertEqual(result['performance']['events'], [])
        self.assertEqual(result['performance']['end_tick'], 20)

    def test_missing_mapping_is_an_input_error(self):
        events = [on('a', 0, channel=0), off('a-off', 1)]
        with self.assertRaises(ValueError):
            export_key_only(manifest(perf(events), [(1, 'P')], [lane(0, costs('P', 1))]))

    def test_zero_duration_channel_still_needs_mapping(self):
        events = [on('a', 0, channel=0), off('a-off', 0)]
        with self.assertRaises(ValueError):
            export_key_only(manifest(perf(events), [], [lane(0, costs('P', 1))]))
        # A channel without any onset needs no mapping.
        events = [on('a', 0, channel=0), off('a-off', 1)]
        result = export_key_only(manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))]))
        self.assertEqual(result['status'], 'ok')

    def test_unmatched_off_and_unknown_program(self):
        events = [off('orphan', 0)]
        with self.assertRaises(ValueError):
            export_key_only(manifest(perf(events), [(0, 'P')], [lane(0, costs('P', 1))]))
        events = [on('a', 0), off('a-off', 1)]
        # A program not offered by any lane yields no_layout, not an error.
        result = export_key_only(manifest(perf(events), [(0, 'Z')], [lane(0, costs('P', 1))]))
        self.assertEqual(result['status'], 'no_layout')

    def test_structural_defects_are_rejected(self):
        good = manifest(perf([on('a', 0), off('a-off', 1)]), [(0, 'P')],
                        [lane(0, costs('P', 1))])
        bad = [
            {k: v for k, v in good.items() if k != 'lanes'},
            {**good, 'extra': 1},
            {**good, 'channel_programs': [dict(channel=0, program_id='P'),
                                          dict(channel=0, program_id='P')]},
            {**good, 'lanes': [lane(0, costs('P', 1)), lane(0, costs('P', 1))]},
            {**good, 'lanes': [lane(0, costs('P', 1), costs('P', 2))]},
            {**good, 'lanes': [lane(0, costs('P', 0))]},
            {**good, 'lanes': [lane(16, costs('P', 1))]},
        ]
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                export_key_only(data)

    def test_max_distinct_programs(self):
        events = [on('a', 0), off('a-off', 1)]
        programs = [f'P{i}' for i in range(9)]
        data = manifest(perf(events), [(0, 'P0')],
                        [lane(0, *[costs(program, 1) for program in programs])])
        with self.assertRaises(ValueError):
            export_key_only(data)


class CliTests(unittest.TestCase):
    def run_cli(self, argv):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_cli_publishes_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', acceptance_manifest())
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--key-only', str(source),
                                           '--output', str(target)]), 0)
            published = load(target)
            self.assertEqual(published['status'], 'ok')
            self.assertEqual(published['total_load_cost'], 9)
            # The generated performance replays through the legacy command.
            replay = self.write(tmp, 'replay.json', published['performance'])
            report = Path(tmp) / 'report.json'
            self.assertEqual(self.run_cli(['--source', str(replay),
                                           '--output', str(report)]), 0)
            self.assertEqual(len(load(report)['voices']), 3)

    def test_cli_no_layout_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', acceptance_manifest(lane_count=2))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--key-only', str(source),
                                           '--output', str(target)]), 0)
            self.assertEqual(load(target)['status'], 'no_layout')

    def test_cli_invalid_input_exits_two_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = acceptance_manifest()
            broken['channel_programs'] = []
            source = self.write(tmp, 'in.json', broken)
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--key-only', str(source),
                                           '--output', str(target)]), 2)
            self.assertFalse(target.exists())

    def test_cli_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', acceptance_manifest())
            target = Path(tmp) / 'out.json'
            target.write_text('existing')
            self.assertEqual(self.run_cli(['--key-only', str(source),
                                           '--output', str(target)]), 2)
            self.assertEqual(target.read_text(), 'existing')

    def test_cli_is_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', acceptance_manifest())
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--key-only', str(source), '--source', str(source),
                              '--output', str(Path(tmp) / 'out.json')])
            self.assertEqual(ctx.exception.code, 2)

    def test_cli_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json',
                                json.loads((ROOT / 'fixtures' / 'input-keyonly.json').read_text()))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--key-only', str(source),
                                           '--output', str(target)]), 0)
            self.assertEqual(load(target)['demands'], [
                {'program_id': 'P', 'min_lanes': 2},
                {'program_id': 'Q', 'min_lanes': 1},
            ])


if __name__ == '__main__':
    unittest.main()
