import json
from pathlib import Path
import tempfile
import unittest

from pedal_performance.clock import TempoClock
from pedal_performance.comp import compose
from pedal_performance.service import process


def on(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='on', pitch=pitch, velocity=80)


def off(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(id, tick, down, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='pedal', down=down)


def take(take_id, events, costs, ppq=480, end_tick=960, tempos=None):
    return dict(take_id=take_id,
                performance=dict(ppq=ppq, end_tick=end_tick,
                                 tempos=tempos or [dict(tick=0, microseconds_per_quarter=500000)],
                                 events=events),
                section_costs=costs)


def manifest(takes, cuts, min_us=0, max_us=6 * 10 ** 14, max_switches=None):
    return dict(cuts=cuts, takes=takes, min_duration_us=min_us, max_duration_us=max_us,
                max_switches=len(cuts) - 2 if max_switches is None else max_switches)


def cut_states(result, tick=480):
    for cut in result['cut_diagnostics']:
        if cut['tick'] == tick:
            return {entry['take_id']: entry for entry in cut['takes']}
    raise AssertionError(f'no diagnostics for cut {tick}')


def plan_takes(result):
    return [section['take_id'] for section in result['plan']['sections']]


class SwitchBlockingTests(unittest.TestCase):
    def test_held_key_blocks_switch(self):
        cuts = [0, 480, 960]
        take_a = take('A', [on('a1', 0), off('a1_off', 720)], [0, 100])
        take_b = take('B', [], [10, 10])
        result = compose(manifest([take_a, take_b], cuts, max_switches=1))
        # A still holds a1 at the cut, so no plan may enter or leave A there;
        # only AA and BB remain, and BB is cheaper.
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(plan_takes(result), ['B', 'B'])
        states = cut_states(result)
        self.assertEqual(states['A']['held_voice_ids'], ['a1'])
        self.assertEqual(states['A']['sustained_voice_ids'], [])
        self.assertEqual(states['B']['held_voice_ids'], [])
        # Releasing the key before the cut unblocks the switch and the cheap
        # A-then-B splice wins.
        take_a = take('A', [on('a1', 0), off('a1_off', 240)], [0, 100])
        result = compose(manifest([take_a, take_b], cuts, max_switches=1))
        self.assertEqual(plan_takes(result), ['A', 'B'])
        self.assertEqual((result['plan']['total_cost'], result['plan']['switches']), (10, 1))

    def test_pedal_up_moved_to_cut_tick_flips_plan_to_no_plan(self):
        # The only full-coverage plan is A for section 0 and B for section 1,
        # which needs a switch at tick 480.  The key is released before the
        # cut in both variants; only the pedal-up moves.
        cuts = [0, 480, 960]
        events_ok = [pedal('p1', 0, True), on('a1', 0), off('a1_off', 240), pedal('p2', 240, False)]
        events_bad = [pedal('p1', 0, True), on('a1', 0), off('a1_off', 240), pedal('p2', 480, False)]
        take_b = take('B', [], [None, 5])
        ok = compose(manifest([take('A', events_ok, [0, None]), take_b], cuts, max_switches=1))
        self.assertEqual(ok['status'], 'ok')
        self.assertEqual(plan_takes(ok), ['A', 'B'])
        self.assertEqual(cut_states(ok)['A']['sustained_voice_ids'], [])
        # Pedal up exactly at the cut: the released voice is still sustained
        # when the cut state is sampled, the only switch is blocked, and the
        # diagnostics must list the sustained voice.
        blocked = compose(manifest([take('A', events_bad, [0, None]), take_b], cuts, max_switches=1))
        self.assertEqual(blocked['status'], 'no_plan')
        for key in ('plan', 'performance', 'event_origins', 'report'):
            self.assertIsNone(blocked[key])
        states = cut_states(blocked)
        self.assertEqual(states['A']['held_voice_ids'], [])
        self.assertEqual(states['A']['sustained_voice_ids'], ['a1'])
        self.assertEqual(states['A']['pedal_down_channels'], [0])


class SpliceTests(unittest.TestCase):
    def test_same_take_continues_sounding_across_cut(self):
        cuts = [0, 480, 960]
        events = [pedal('p1', 0, True), on('n1', 100), off('o1', 200),
                  pedal('p2', 720, False), on('n2', 800)]
        result = compose(manifest([take('A', events, [3, 4])], cuts, max_switches=0))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plan']['switches'], 0)
        self.assertFalse(any(origin['patched'] for origin in result['event_origins']))
        # n1 is released at 200 under pedal and keeps sounding across the cut
        # until the pedal lifts at 720; n2 is still held at end_tick.
        first, second = result['report']['voices']
        self.assertEqual((first['key_release_tick'], first['sound_end_tick']), (200, 720))
        self.assertEqual(first['end_reason'], 'pedal_release')
        self.assertEqual(second['end_reason'], 'end_of_take')
        self.assertEqual(result['report']['summary']['pedal_extended_voices'], 1)
        self.assertEqual([origin['event_id'] for origin in result['event_origins']],
                         ['p1', 'n1', 'o1', 'p2', 'n2'])
        self.assertEqual([origin['output_id'] for origin in result['event_origins']],
                         ['e0', 'e1', 'e2', 'e3', 'e4'])

    def test_switch_patches_pedal_channels_before_cut_tick_events(self):
        cuts = [0, 480, 960]
        events_a = [pedal('a_p1', 0, True, channel=1), pedal('a_p3', 0, True, channel=3)]
        events_b = [pedal('b_p1', 0, True, channel=1), pedal('b_p0', 0, True, channel=0),
                    on('b1', 480), off('b1_off', 720)]
        result = compose(manifest([take('A', events_a, [1, None]),
                                   take('B', events_b, [None, 1])], cuts, max_switches=1))
        self.assertEqual(result['status'], 'ok')
        states = cut_states(result)
        self.assertEqual(states['A']['pedal_down_channels'], [1, 3])
        self.assertEqual(states['B']['pedal_down_channels'], [0, 1])
        events = result['performance']['events']
        self.assertEqual([event['id'] for event in events], ['e0', 'e1', 'e2', 'e3', 'e4', 'e5'])
        # At the cut tick: patch channel 0 down (B's state), patch channel 3
        # up, both before B's own events; channel 1 matches and needs no patch.
        at_cut = [event for event in events if event['tick'] == 480]
        self.assertEqual([(event['kind'], event['channel']) for event in at_cut],
                         [('pedal', 0), ('pedal', 3), ('on', 0)])
        self.assertEqual([event['down'] for event in at_cut[:2]], [True, False])
        origins = result['event_origins']
        self.assertEqual([origin['output_id'] for origin in origins],
                         [event['id'] for event in events])
        self.assertEqual([(origin['take_id'], origin['event_id']) for origin in origins],
                         [('A', 'a_p1'), ('A', 'a_p3'), (None, None), (None, None),
                          ('B', 'b1'), ('B', 'b1_off')])
        patched = [origin for origin in origins if origin['patched']]
        self.assertEqual([(origin['tick'], origin['channel']) for origin in patched],
                         [(480, 0), (480, 3)])
        # The patch keeps B's own stopping behaviour: b1 is released under B's
        # pedal and rings to end_tick instead of stopping at the key release.
        voice = result['report']['voices'][0]
        self.assertEqual((voice['key_release_tick'], voice['sound_end_tick']), (720, 960))
        self.assertEqual(voice['end_reason'], 'end_of_take')

    def test_shared_event_ids_across_takes_replay_and_trace(self):
        cuts = [0, 480, 960]
        take_a = take('A', [on('n1', 0), off('o1', 240)], [1, None])
        take_b = take('B', [on('n1', 480), off('o1', 720)], [None, 1])
        result = compose(manifest([take_a, take_b], cuts, max_switches=1))
        self.assertEqual(result['status'], 'ok')
        performance = result['performance']
        # Original ids repeat across takes; output ids are globally unique,
        # sequential and reproducible.
        self.assertEqual([event['id'] for event in performance['events']],
                         ['e0', 'e1', 'e2', 'e3'])
        self.assertEqual([(origin['take_id'], origin['event_id'])
                          for origin in result['event_origins']],
                         [('A', 'n1'), ('A', 'o1'), ('B', 'n1'), ('B', 'o1')])
        # The spliced performance replays through the legacy pipeline and the
        # embedded report matches that replay exactly.
        self.assertEqual(result['report'], process(performance))
        self.assertEqual(result['report']['duration_us'], result['plan']['total_duration_us'])
        voices = result['report']['voices']
        self.assertEqual([(voice['voice_id'], voice['start_tick'], voice['sound_end_tick'])
                          for voice in voices], [('e0', 0, 240), ('e2', 480, 720)])
        again = compose(manifest([take_a, take_b], cuts, max_switches=1))
        self.assertEqual(again, result)


class TempoAndDurationTests(unittest.TestCase):
    def fractional_manifest(self, min_us, max_us):
        cuts = [0, 3, 9]
        take_a = take('A', [], [0, None], ppq=3, end_tick=9,
                      tempos=[dict(tick=0, microseconds_per_quarter=1)])
        take_b = take('B', [], [None, 0], ppq=3, end_tick=9,
                      tempos=[dict(tick=0, microseconds_per_quarter=2),
                              dict(tick=5, microseconds_per_quarter=7)])
        return manifest([take_a, take_b], cuts, min_us=min_us, max_us=max_us, max_switches=1)

    def test_fractional_durations_and_merged_tempo_map(self):
        # Section 0: 3 ticks at 1us/quarter over ppq 3 -> 1us.
        # Section 1: 2 ticks at 2us + 4 ticks at 7us over ppq 3 -> 32/3us.
        result = compose(self.fractional_manifest(11, 12))
        self.assertEqual(result['status'], 'ok')
        plan = result['plan']
        self.assertEqual([section['duration_us'] for section in plan['sections']], ['1', '32/3'])
        self.assertEqual(plan['total_duration_us'], '35/3')
        self.assertEqual(result['report']['duration_us'], '35/3')
        # Merged map: tick 0, the right take's tempo at the section start, and
        # the mid-section change; the splice integrates to the plan total.
        self.assertEqual(result['performance']['tempos'],
                         [dict(tick=0, microseconds_per_quarter=1),
                          dict(tick=3, microseconds_per_quarter=2),
                          dict(tick=5, microseconds_per_quarter=7)])
        # 35/3us is exact: a window ending at 11us excludes it.
        self.assertEqual(compose(self.fractional_manifest(0, 11))['status'], 'no_plan')

    def test_duration_window_bounds_are_inclusive(self):
        cuts = [0, 3, 9]
        take_a = take('A', [], [0, None], ppq=3, end_tick=9,
                      tempos=[dict(tick=0, microseconds_per_quarter=3)])
        take_b = take('B', [], [None, 0], ppq=3, end_tick=9,
                      tempos=[dict(tick=0, microseconds_per_quarter=2),
                              dict(tick=5, microseconds_per_quarter=5)])
        # Section 0: 3 ticks at 3us/quarter -> 3us; section 1: 4/3 + 20/3 = 8us.
        exact = compose(manifest([take_a, take_b], cuts, min_us=11, max_us=11, max_switches=1))
        self.assertEqual(exact['status'], 'ok')
        self.assertEqual(exact['plan']['total_duration_us'], '11')
        self.assertEqual(exact['report']['duration_us'], '11')
        below = compose(manifest([take_a, take_b], cuts, min_us=10, max_us=10, max_switches=1))
        above = compose(manifest([take_a, take_b], cuts, min_us=12, max_us=12, max_switches=1))
        self.assertEqual(below['status'], 'no_plan')
        self.assertEqual(above['status'], 'no_plan')

    def test_section_without_events_still_takes_time(self):
        cuts = [0, 480, 960]
        silent = take('A', [], [1, 1])
        result = compose(manifest([silent], cuts, max_switches=0,
                                  min_us=1000001, max_us=6 * 10 ** 14))
        self.assertEqual(result['status'], 'no_plan')
        result = compose(manifest([silent], cuts, max_switches=0,
                                  min_us=1000000, max_us=1000000))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plan']['total_duration_us'], '1000000')


class OptimisationTests(unittest.TestCase):
    def test_null_sections_and_switch_budget_change_global_optimum(self):
        cuts = [0, 100, 200, 300]
        take_a = take('A', [], [5, None, 5], end_tick=300)
        take_b = take('B', [], [4, 4, 4], end_tick=300)
        take_c = take('C', [], [None, 3, None], end_tick=300)
        # Budget 2: BCB costs 11 and beats the zero-switch BBB at 12.
        two = compose(manifest([take_a, take_b, take_c], cuts, max_switches=2))
        self.assertEqual(plan_takes(two), ['B', 'C', 'B'])
        self.assertEqual((two['plan']['total_cost'], two['plan']['switches']), (11, 2))
        # Budget 1: BCB is infeasible, so BBB wins despite the higher cost.
        one = compose(manifest([take_a, take_b, take_c], cuts, max_switches=1))
        self.assertEqual(plan_takes(one), ['B', 'B', 'B'])
        self.assertEqual((one['plan']['total_cost'], one['plan']['switches']), (12, 0))

    def test_take_id_sequence_breaks_ties_independent_of_manifest_order(self):
        cuts = [0, 480, 960]
        # AA and BB tie on cost and switches; the smaller take_id sequence
        # wins regardless of take order in the manifest.
        for order in ([take('A', [], [1, 1]), take('B', [], [1, 1])],
                      [take('B', [], [1, 1]), take('A', [], [1, 1])]):
            result = compose(manifest(order, cuts, max_switches=0))
            self.assertEqual(plan_takes(result), ['A', 'A'])
        # Four-way tie across a forced switch: (A,B) is the smallest sequence.
        takes = [take('D', [], [None, 1]), take('C', [], [5, None]),
                 take('B', [], [None, 1]), take('A', [], [5, None])]
        result = compose(manifest(takes, cuts, max_switches=1))
        self.assertEqual(plan_takes(result), ['A', 'B'])
        self.assertEqual((result['plan']['total_cost'], result['plan']['switches']), (6, 1))
        # Every plan here needs a switch, so a zero budget is infeasible.
        self.assertEqual(compose(manifest(takes, cuts, max_switches=0))['status'], 'no_plan')

    def test_plan_matches_brute_force_on_randomised_manifests(self):
        import random
        from itertools import product
        rng = random.Random(20261121)
        for trial in range(25):
            sections = rng.randint(1, 8)
            inner = sorted(rng.sample(range(1, 960), sections - 1))
            cuts = [0] + inner + [960]
            takes = []
            for index in range(rng.randint(1, 4)):
                costs = [rng.choice([None, rng.randint(0, 50)]) for _ in range(sections)]
                tempos = [dict(tick=0, microseconds_per_quarter=rng.randint(1, 1000000))]
                takes.append(take(f'T{index}', [], costs, tempos=tempos))
            min_us = rng.randint(0, 500000)
            max_us = min_us + rng.randint(0, 1000000)
            budget = rng.randint(0, sections - 1)
            data = manifest(takes, cuts, min_us=min_us, max_us=max_us, max_switches=budget)
            clocks = [TempoClock(480, item['performance']['tempos']) for item in takes]
            durations = [[clock.at(cuts[i + 1]) - clock.at(cuts[i]) for clock in clocks]
                         for i in range(sections)]
            best = None
            for choice in product(range(len(takes)), repeat=sections):
                costs = [takes[j]['section_costs'][i] for i, j in enumerate(choice)]
                if any(cost is None for cost in costs):
                    continue
                switches = sum(choice[i] != choice[i - 1] for i in range(1, sections))
                if switches > budget:
                    continue
                duration = sum(durations[i][j] for i, j in enumerate(choice))
                if not min_us <= duration <= max_us:
                    continue
                candidate = (sum(costs), switches, tuple(takes[j]['take_id'] for j in choice))
                if best is None or candidate < best:
                    best = candidate
            result = compose(data)
            if best is None:
                self.assertEqual(result['status'], 'no_plan', msg=f'trial {trial}')
            else:
                self.assertEqual(result['status'], 'ok', msg=f'trial {trial}')
                self.assertEqual(result['plan']['total_cost'], best[0], msg=f'trial {trial}')
                self.assertEqual(result['plan']['switches'], best[1], msg=f'trial {trial}')
                self.assertEqual(tuple(plan_takes(result)), best[2], msg=f'trial {trial}')


class NoPlanTests(unittest.TestCase):
    def test_no_plan_publishes_diagnostics_with_null_fields(self):
        cuts = [0, 480, 960]
        take_a = take('A', [on('a1', 0), off('a1_off', 600)], [None, None])
        take_b = take('B', [], [1, None])
        result = compose(manifest([take_a, take_b], cuts, max_switches=1))
        self.assertEqual(result['status'], 'no_plan')
        for key in ('plan', 'performance', 'event_origins', 'report'):
            self.assertIsNone(result[key])
        # Diagnostics are still published, ordered by cut then take_id, and a
        # key held past the cut is reported even though no plan exists.
        self.assertEqual([cut['tick'] for cut in result['cut_diagnostics']], [480])
        states = cut_states(result)
        self.assertEqual(list(states), ['A', 'B'])
        self.assertEqual(states['A']['held_voice_ids'], ['a1'])
        self.assertEqual(states['B']['held_voice_ids'], [])


class ManifestValidationTests(unittest.TestCase):
    def base_manifest(self):
        return manifest([take('A', [on('n1', 0), off('o1', 240)], [1, 2]),
                         take('B', [], [2, 1])], [0, 480, 960], max_switches=1)

    def test_invalid_manifests_are_rejected(self):
        bad = []
        data = self.base_manifest(); data.pop('cuts'); bad.append(data)
        data = self.base_manifest(); data['extra'] = 1; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [0, 480]; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [480, 960]; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [0, 480, 480, 960]; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [0, True, 960]; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [0, 480, 960.0]; bad.append(data)
        data = self.base_manifest(); data['cuts'] = [0] * 14; bad.append(data)
        data = self.base_manifest(); data['takes'] = []; bad.append(data)
        data = self.base_manifest()
        data['takes'] += [take('C', [], [1, 1]), take('D', [], [1, 1]), take('E', [], [1, 1])]
        bad.append(data)
        data = self.base_manifest(); data['takes'][1]['take_id'] = 'A'; bad.append(data)
        data = self.base_manifest(); data['takes'][0]['take_id'] = 'bad id!'; bad.append(data)
        data = self.base_manifest(); data['takes'][0].pop('section_costs'); bad.append(data)
        data = self.base_manifest()
        data['takes'][0]['performance']['score'] = dict(ppq=480, end_tick=960, tempos=[], cues=[])
        bad.append(data)
        data = self.base_manifest(); data['takes'][0]['performance'].pop('tempos'); bad.append(data)
        data = self.base_manifest(); data['takes'][1]['performance']['ppq'] = 240; bad.append(data)
        data = self.base_manifest(); data['takes'][1]['performance']['end_tick'] = 480; bad.append(data)
        data = self.base_manifest(); data['takes'][0]['section_costs'] = [1]; bad.append(data)
        data = self.base_manifest(); data['takes'][0]['section_costs'] = [1, 10 ** 9 + 1]; bad.append(data)
        data = self.base_manifest(); data['takes'][0]['section_costs'] = [1, True]; bad.append(data)
        data = self.base_manifest(); data['takes'][0]['section_costs'] = [1, 1.5]; bad.append(data)
        data = self.base_manifest(); data['min_duration_us'] = -1; bad.append(data)
        data = self.base_manifest(); data['min_duration_us'] = 6 * 10 ** 14 + 1; bad.append(data)
        data = self.base_manifest()
        data['min_duration_us'], data['max_duration_us'] = 10, 5
        bad.append(data)
        data = self.base_manifest(); data['max_switches'] = 2; bad.append(data)
        data = self.base_manifest(); data['max_switches'] = True; bad.append(data)
        # An unmatched off invalidates the manifest even in a take whose
        # sections are all deprecated (null) and could never be selected.
        data = self.base_manifest()
        data['takes'].append(take('C', [off('orphan', 0)], [None, None]))
        bad.append(data)
        # Aggregate caps: 8000 events and 980 tempo points across all takes.
        data = self.base_manifest()
        data['takes'][0]['performance']['events'] = [on(f'n{i}', 0) for i in range(4001)]
        data['takes'][1]['performance']['events'] = [on(f'n{i}', 0) for i in range(4001)]
        bad.append(data)
        data = self.base_manifest()
        tempos = [dict(tick=i, microseconds_per_quarter=500000) for i in range(491)]
        data['takes'][0]['performance']['tempos'] = tempos
        data['takes'][1]['performance']['tempos'] = tempos
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                compose(data)

    def test_valid_edge_values_are_accepted(self):
        data = self.base_manifest()
        data['min_duration_us'] = 0
        data['max_duration_us'] = 6 * 10 ** 14
        data['takes'][0]['section_costs'] = [0, 10 ** 9]
        self.assertEqual(compose(data)['status'], 'ok')


class CompCliTests(unittest.TestCase):
    def run_cli(self, argv):
        import contextlib
        import io
        from pedal_performance.cli import main
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_comp_cli_publishes_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json', manifest(
                [take('A', [on('n1', 0), off('o1', 240)], [1, 2]),
                 take('B', [], [2, 1])], [0, 480, 960], max_switches=1))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(target)]), 0)
            published = json.loads(target.read_text())
            self.assertEqual(published['status'], 'ok')
            self.assertEqual(set(published),
                             {'status', 'cut_diagnostics', 'plan', 'performance',
                              'event_origins', 'report'})
            original = target.read_bytes()
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(target)]), 2)
            self.assertEqual(target.read_bytes(), original)

    def test_comp_no_plan_still_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json', manifest(
                [take('A', [], [None, None])], [0, 480, 960], max_switches=0))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(target)]), 0)
            published = json.loads(target.read_text())
            self.assertEqual(published['status'], 'no_plan')
            self.assertIsNone(published['plan'])
            self.assertEqual(len(published['cut_diagnostics']), 1)

    def test_invalid_manifest_exits_2_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json', manifest(
                [take('A', [off('orphan', 0)], [1, 1])], [0, 480, 960], max_switches=1))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(target)]), 2)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).glob('.report-*')), [])

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json',
                                '{"cuts": [0, 960], "cuts": [0, 960], "takes": [],'
                                ' "min_duration_us": 0, "max_duration_us": 0, "max_switches": 0}')
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(target)]), 2)
            self.assertFalse(target.exists())

    def test_source_and_comp_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', manifest([take('A', [], [1])], [0, 960]))
            target = Path(tmp) / 'out.json'
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--source', str(source), '--comp', str(source),
                              '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(target.exists())

    def test_comp_performance_feeds_legacy_source_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json', manifest(
                [take('A', [on('n1', 0), off('o1', 240)], [1, None]),
                 take('B', [on('m1', 480), off('m1_off', 720)], [None, 1])],
                [0, 480, 960], max_switches=1))
            comp_target = Path(tmp) / 'comp.json'
            self.assertEqual(self.run_cli(['--comp', str(source), '--output', str(comp_target)]), 0)
            comp = json.loads(comp_target.read_text())
            # The spliced performance is itself a valid legacy --source input
            # and reproduces the embedded report byte for byte.
            performance = self.write(tmp, 'performance.json', comp['performance'])
            legacy_target = Path(tmp) / 'legacy.json'
            self.assertEqual(self.run_cli(['--source', str(performance),
                                           '--output', str(legacy_target)]), 0)
            self.assertEqual(json.loads(legacy_target.read_text()), comp['report'])
            # The legacy mode itself is unchanged.
            legacy_source = self.write(tmp, 'plain.json', json.loads(
                (Path(__file__).resolve().parents[1] / 'fixtures/input.json').read_text()))
            plain_target = Path(tmp) / 'plain-report.json'
            self.assertEqual(self.run_cli(['--source', str(legacy_source),
                                           '--output', str(plain_target)]), 0)
            self.assertEqual(json.loads(plain_target.read_text())['summary'],
                             dict(voices=3, pedal_extended_voices=2, unfinished_keys=1,
                                  peak_polyphony=2))


if __name__ == '__main__':
    unittest.main()
