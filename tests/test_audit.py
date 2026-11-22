import json
from pathlib import Path
import tempfile
import unittest

from pedal_performance.audit import audit_capture
from pedal_performance.cli import main
from pedal_performance.service import process


def on(event_id, tick, pitch=60, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='on', pitch=pitch, velocity=80)


def off(event_id, tick, pitch=60, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(event_id, tick, down, channel=0):
    return dict(id=event_id, tick=tick, channel=channel, kind='pedal', down=down)


def performance(events, ppq=480, end_tick=960, tempos=None):
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=500000)],
                events=events)


def manifest(perf, races, checkpoints):
    return dict(performance=perf, races=races, checkpoints=checkpoints)


def cp(tick, channel=0, held=0, sustained=0):
    return dict(tick=tick, channel=channel, held=held, sustained=sustained)


class PedalRaceTests(unittest.TestCase):
    # Base scenario: the key is already held when, on one tick, the recorder
    # may have swapped the key release and the pedal down.  The pedal lifts
    # later.
    def base_events(self):
        return [on('a', 0), off('r', 10), pedal('pd', 10, True),
                pedal('pu', 20, False)]

    def test_checkpoint_forces_flip_and_pedal_lift_stops_sound(self):
        data = manifest(performance(self.base_events()), [['r', 'pd']],
                        [cp(15, held=0, sustained=1)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'certified')
        self.assertIsNotNone(result['performance'])
        channel = result['channels'][0]
        self.assertTrue(channel['feasible'])
        voice = channel['voices'][0]
        self.assertEqual(voice['voice_id'], 'a')
        self.assertEqual(len(voice['alternatives']), 1)
        alternative = voice['alternatives'][0]
        self.assertEqual(alternative['flips'], [0])
        self.assertEqual((alternative['key_release_tick'],
                          alternative['release_event_id'],
                          alternative['sound_end_tick'],
                          alternative['end_reason'],
                          alternative['end_event_id']),
                         (10, 'r', 20, 'pedal_release', 'pu'))
        # The published ordering swaps the raced pair; ids, ticks and payloads
        # are otherwise untouched, and the embedded report replays it.
        kinds = [(event['id'], event['kind']) for event in result['performance']['events']]
        self.assertEqual(kinds, [('a', 'on'), ('pd', 'pedal'), ('r', 'off'),
                                 ('pu', 'pedal')])
        self.assertEqual(result['report'], process(result['performance']))
        self.assertEqual(result['report']['voices'][0]['sound_end_tick'], 20)

    def test_without_distinguishing_observation_both_orders_testify(self):
        data = manifest(performance(self.base_events()), [['r', 'pd']], [])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['performance'])
        self.assertIsNone(result['report'])
        alternatives = result['channels'][0]['voices'][0]['alternatives']
        self.assertEqual(len(alternatives), 2)
        # Witness order: keep-earliest first (empty flips), then the swap.
        self.assertEqual(alternatives[0]['flips'], [])
        self.assertEqual(alternatives[0]['end_reason'], 'key_release')
        self.assertEqual(alternatives[0]['sound_end_tick'], 10)
        self.assertEqual(alternatives[1]['flips'], [0])
        self.assertEqual(alternatives[1]['end_reason'], 'pedal_release')
        self.assertEqual(alternatives[1]['sound_end_tick'], 20)
        # Every five-tuple stays coherent: release and stop fields come from
        # the same replay.
        for alternative in alternatives:
            self.assertEqual(alternative['release_event_id'], 'r')
            self.assertIsNotNone(alternative['key_release_tick'])

    def test_checkpoint_after_states_converge_still_records_both_witnesses(self):
        # After the pedal lift both interpretations hold the same state, yet
        # the historical evidence still differs and no playback is published.
        data = manifest(performance(self.base_events()), [['r', 'pd']],
                        [cp(25, held=0, sustained=0)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['performance'])
        self.assertEqual(
            len(result['channels'][0]['voices'][0]['alternatives']), 2)

    def test_impossible_observation_publishes_inconsistency_only(self):
        data = manifest(performance(self.base_events()), [['r', 'pd']],
                        [cp(15, held=1, sustained=1)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'inconsistent')
        self.assertIsNone(result['performance'])
        self.assertIsNone(result['report'])
        self.assertEqual(result['channels'],
                         [{'channel': 0, 'feasible': False, 'voices': []}])

    def test_checkpoints_need_not_be_sorted(self):
        # Input order is arbitrary; replay still verifies every observation.
        data = manifest(performance(self.base_events()), [['r', 'pd']],
                        [cp(20, held=0, sustained=0), cp(15, held=0, sustained=1)])
        self.assertEqual(audit_capture(data)['status'], 'certified')
        data['checkpoints'][1] = cp(15, held=1, sustained=0)
        self.assertEqual(audit_capture(data)['status'], 'inconsistent')


class FifoAndWitnessTests(unittest.TestCase):
    def test_overlapping_same_pitch_fifo_evidence(self):
        events = [on('a', 0), on('b', 10), off('ra', 20), off('rb', 30)]
        data = manifest(performance(events), [], [cp(15, held=2), cp(480)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'certified')
        voices = result['channels'][0]['voices']
        self.assertEqual([row['voice_id'] for row in voices], ['a', 'b'])
        first, second = voices
        self.assertEqual(first['alternatives'][0]['release_event_id'], 'ra')
        self.assertEqual(second['alternatives'][0]['release_event_id'], 'rb')
        for row in voices:
            self.assertEqual(len(row['alternatives']), 1)
            self.assertEqual(row['alternatives'][0]['flips'], [])

    def test_prefix_that_later_hits_unmatched_off_is_not_a_witness(self):
        # The raced pair is an off followed by an on at one tick, with an
        # empty held queue before the tick.  The original order (off first)
        # orphans at that tick; the swapped order (on first) lets the off
        # release the new voice, and a later off closes the voice started
        # after the race.  Checkpoints along the original prefix cannot rescue
        # an interpretation that never finishes: the swap is the only witness.
        events = [off('r0', 10), on('b', 10), on('a', 20), off('r1', 30)]
        data = manifest(performance(events), [['r0', 'b']],
                        [cp(5, held=0), cp(10, held=0), cp(20, held=1),
                         cp(30, held=0)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'certified')
        voices = {row['voice_id']: row for row in result['channels'][0]['voices']}
        self.assertEqual(voices['b']['alternatives'][0]['flips'], [0])
        self.assertEqual(voices['b']['alternatives'][0]['release_event_id'], 'r0')
        self.assertEqual(voices['a']['alternatives'][0]['release_event_id'], 'r1')

    def test_end_tick_force_stop_observation(self):
        events = [pedal('pd', 0, True), on('a', 5), off('r', 10)]
        data = manifest(performance(events), [], [cp(960, held=0, sustained=1)])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'certified')
        alternative = result['channels'][0]['voices'][0]['alternatives'][0]
        self.assertEqual(alternative['sound_end_tick'], 960)
        self.assertEqual(alternative['end_reason'], 'end_of_take')
        self.assertIsNone(alternative['end_event_id'])

    def test_same_tick_onset_order_only_differs_in_display_order(self):
        # Distinct pitches: swapping the two onsets changes only their display
        # order, never any release/stop five-tuple.
        events = [on('a', 10, pitch=60), on('b', 10, pitch=64),
                  off('ra', 20, pitch=60), off('rb', 30, pitch=64)]
        data = manifest(performance(events), [['a', 'b']], [])
        result = audit_capture(data)
        # Two legal orders exist, but every voice keeps one unique five-tuple,
        # so the capture is certified despite the remaining order ambiguity.
        self.assertEqual(result['status'], 'certified')
        channel = result['channels'][0]
        self.assertTrue(all(len(row['alternatives']) == 1
                            for row in channel['voices']))
        self.assertEqual([row['voice_id'] for row in channel['voices']],
                         ['a', 'b'])
        # The minimal choice keeps the original order and the legacy report
        # lists voices by onset order.
        self.assertEqual(
            [event['id'] for event in result['performance']['events']],
            ['a', 'b', 'ra', 'rb'])
        self.assertEqual(result['report'], process(result['performance']))

    def test_alternatives_sorted_by_keep_earliest_witness(self):
        # Two independent races.  Pair 0 changes voice a's stop evidence;
        # pair 1 only orders two redundant pedal-down events and never changes
        # any five-tuple.  Masks enumerate in keep-earliest order, so voice
        # a's witnesses are [] (00), [0] (01), [0, 1] (11); mask [1] (10)
        # reproduces the original evidence and never creates a separate entry.
        events = [on('a', 0),
                  off('r1', 10), pedal('p1', 10, True),
                  pedal('x', 15, True), pedal('y', 15, True),
                  on('b', 20), off('r2', 30),
                  pedal('u', 40, False)]
        data = manifest(performance(events), [['r1', 'p1'], ['x', 'y']], [])
        result = audit_capture(data)
        self.assertEqual(result['status'], 'ambiguous')
        alternatives = result['channels'][0]['voices'][0]['alternatives']
        witnesses = [alternative['flips'] for alternative in alternatives]
        # Pair 1 never changes the five-tuple, so masks 01 and 11 share one
        # entry whose keep-earliest witness is [0].
        self.assertEqual(witnesses, [[], [0]])
        self.assertEqual(alternatives[0]['end_reason'], 'key_release')
        self.assertEqual(alternatives[1]['end_reason'], 'pedal_release')
        # The same five-tuple is never listed twice.
        tuples = [(a['key_release_tick'], a['release_event_id'],
                   a['sound_end_tick'], a['end_reason'], a['end_event_id'])
                  for a in alternatives]
        self.assertEqual(len(tuples), len(set(tuples)))


class MultiChannelTests(unittest.TestCase):
    def test_twelve_channels_six_pairs_all_choices_feasible(self):
        channel_events = []
        races = []
        checkpoints = []
        for channel in range(12):
            ids = []
            base = channel * 20
            # Six same-tick pairs (two onsets per tick on distinct pitches).
            for index in range(6):
                first_id = f'c{channel}_n{2 * index}'
                second_id = f'c{channel}_n{2 * index + 1}'
                ids.extend((first_id, second_id))
                tick = base + 2 * index
                channel_events.append(on(first_id, tick, pitch=60 + index,
                                         channel=channel))
                channel_events.append(on(second_id, tick, pitch=70 + index,
                                         channel=channel))
            for index in range(6):
                races.append([ids[2 * index], ids[2 * index + 1]])
            checkpoints.append(cp(960, channel=channel, held=12, sustained=0))
        data = manifest(performance(channel_events, end_tick=960),
                        races, checkpoints)
        result = audit_capture(data)
        self.assertEqual(result['status'], 'certified')
        self.assertEqual([entry['channel'] for entry in result['channels']],
                         list(range(12)))
        for entry in result['channels']:
            self.assertTrue(entry['feasible'])
            self.assertEqual(len(entry['voices']), 12)
            for row in entry['voices']:
                self.assertEqual(len(row['alternatives']), 1)
        # Every pair may legally be swapped; the minimal choice flips none.
        self.assertEqual(
            [event['id'] for event in result['performance']['events']],
            [event['id'] for event in channel_events])
        self.assertEqual(result['report']['summary']['voices'], 144)

    def test_one_contradiction_keeps_other_channels_evidence(self):
        good_events = [on('g', 0, channel=0), off('gr', 10, channel=0)]
        bad_events = [on('b', 0, channel=1), pedal('bd', 10, True, channel=1),
                      off('br', 10, channel=1), pedal('bu', 20, False, channel=1)]
        events = sorted(good_events + bad_events, key=lambda event: event['tick'])
        data = manifest(
            performance(events),
            [['bd', 'br']],
            [cp(15, channel=1, held=1, sustained=0)])  # impossible for ch. 1
        result = audit_capture(data)
        self.assertEqual(result['status'], 'inconsistent')
        self.assertIsNone(result['performance'])
        entries = {entry['channel']: entry for entry in result['channels']}
        self.assertTrue(entries[0]['feasible'])
        self.assertEqual(entries[0]['voices'][0]['voice_id'], 'g')
        self.assertFalse(entries[1]['feasible'])
        self.assertEqual(entries[1]['voices'], [])

    def test_observation_only_and_pedal_only_channels(self):
        # Channel 2 has only observations (no events): every sample must show
        # zero voices.  Channel 3 only presses a pedal with no sounding voice,
        # so sustained stays zero even while the pedal is down.
        events = [pedal('p', 10, True, channel=3), pedal('u', 40, False, channel=3)]
        checkpoints = [cp(20, channel=2, held=0, sustained=0),
                       cp(20, channel=3, held=0, sustained=0),
                       cp(50, channel=3, held=0, sustained=0)]
        result = audit_capture(manifest(performance(events), [], checkpoints))
        self.assertEqual(result['status'], 'certified')
        entries = {entry['channel']: entry for entry in result['channels']}
        self.assertEqual(sorted(entries), [2, 3])
        self.assertTrue(entries[2]['feasible'])
        self.assertEqual(entries[2]['voices'], [])
        self.assertEqual(entries[3]['voices'], [])
        # A nonzero observation on a silent channel makes it infeasible but
        # leaves the other channel's evidence intact.
        checkpoints[0] = cp(20, channel=2, held=1, sustained=0)
        result = audit_capture(manifest(performance(events), [], checkpoints))
        self.assertEqual(result['status'], 'inconsistent')
        entries = {entry['channel']: entry for entry in result['channels']}
        self.assertFalse(entries[2]['feasible'])
        self.assertTrue(entries[3]['feasible'])


class ValidationTests(unittest.TestCase):
    def base(self):
        events = [on('a', 0), off('r', 10), pedal('p', 10, True)]
        return manifest(performance(events), [['r', 'p']], [])

    def test_illegal_races_rejected(self):
        bad = []
        data = self.base()
        data['races'] = [['r', 'nope']]
        bad.append(data)
        data = self.base()
        data['races'] = [['a', 'p']]  # not adjacent
        bad.append(data)
        data = self.base()
        data['races'] = [['r', 'a']]  # different ticks
        bad.append(data)
        data = self.base()
        data['races'] = [['a', 'r'], ['r', 'p']]  # event r used twice
        bad.append(data)
        data = self.base()
        data['races'] = [['r', 'p'], ['a', 'r']]  # not position ordered
        bad.append(data)
        data = self.base()
        data['races'] = [['r', 'p'], ['r', 'p']]  # reuse
        bad.append(data)
        data = self.base()
        data['races'] = 'nope'
        bad.append(data)
        data = self.base()
        data['races'] = [['r']]
        bad.append(data)
        # Seven races on one channel.
        data = self.base()
        ids = [f'n{i}' for i in range(14)]
        data['performance']['events'] = [on(event_id, i) for i, event_id in enumerate(ids)]
        data['races'] = [[ids[2 * i], ids[2 * i + 1]] for i in range(7)]
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                audit_capture(data)

    def test_illegal_checkpoints_rejected(self):
        bad = []
        data = self.base()
        data['checkpoints'] = [cp(10), cp(10)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [cp(961)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [dict(tick=10, channel=16, held=0, sustained=0)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [dict(tick=10, channel=0, held=2001, sustained=0)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [dict(tick=10, channel=0, held=True, sustained=0)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [dict(tick=10, channel=0, held=0)]  # missing field
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                audit_capture(data)

    def test_manifest_structure_and_performance_rules(self):
        bad = []
        data = self.base()
        data['extra'] = 1
        bad.append(data)
        data = self.base()
        data.pop('races')
        bad.append(data)
        data = self.base()
        data['performance']['score'] = {}
        bad.append(data)
        data = self.base()
        data['performance']['events'] = [on(f'n{i}', 0) for i in range(2001)]
        bad.append(data)
        data = self.base()
        data['checkpoints'] = [cp(i) for i in range(2001)]
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                audit_capture(data)

    def test_unmatched_off_is_an_audit_result_not_input_error(self):
        events = [off('orphan', 0)]
        result = audit_capture(manifest(performance(events), [], []))
        self.assertEqual(result['status'], 'inconsistent')
        self.assertFalse(result['channels'][0]['feasible'])


class AuditCliTests(unittest.TestCase):
    def run_cli(self, argv):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return path

    def test_publishes_audit_and_never_overwrites(self):
        events = [on('a', 0), off('r', 10), pedal('p', 10, True),
                  pedal('u', 20, False)]
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json',
                                manifest(performance(events), [['r', 'p']],
                                         [cp(15, held=0, sustained=1)]))
            target = Path(tmp) / 'audit.json'
            self.assertEqual(
                self.run_cli(['--capture-audit', str(source),
                              '--output', str(target)]), 0)
            published = json.loads(target.read_text())
            self.assertEqual(published['status'], 'certified')
            self.assertEqual(set(published),
                             {'status', 'channels', 'performance', 'report'})
            original = target.read_bytes()
            self.assertEqual(
                self.run_cli(['--capture-audit', str(source),
                              '--output', str(target)]), 2)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob('.report-*')), [])

    def test_invalid_manifest_exits_2_without_publishing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = manifest(performance([on('a', 0)]), [['a', 'ghost']], [])
            source = self.write(tmp, 'manifest.json', data)
            target = Path(tmp) / 'audit.json'
            self.assertEqual(
                self.run_cli(['--capture-audit', str(source),
                              '--output', str(target)]), 2)
            self.assertFalse(target.exists())

    def test_inconsistent_result_still_publishes(self):
        events = [on('a', 0), off('r', 10), pedal('p', 10, True)]
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'manifest.json',
                                manifest(performance(events), [['r', 'p']],
                                         [cp(20, held=9, sustained=9)]))
            target = Path(tmp) / 'audit.json'
            self.assertEqual(
                self.run_cli(['--capture-audit', str(source),
                              '--output', str(target)]), 0)
            self.assertEqual(json.loads(target.read_text())['status'],
                             'inconsistent')

    def test_modes_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'in.json', self.base_payload())
            target = Path(tmp) / 'out.json'
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--source', str(source),
                              '--capture-audit', str(source),
                              '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(target.exists())

    def base_payload(self):
        events = [on('a', 0), off('r', 10), pedal('p', 10, True)]
        return manifest(performance(events), [['r', 'p']], [])


if __name__ == '__main__':
    unittest.main()
