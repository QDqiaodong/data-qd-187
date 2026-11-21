import json
from pathlib import Path
import unittest
from pedal_performance.service import process


def sample():
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures/input.json').read_text())


def on(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='on', pitch=pitch, velocity=80)


def off(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(id, tick, down, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='pedal', down=down)


def cue(cue_id, start, release, pitch=60, channel=0, onset=0, duration=0):
    return dict(cue_id=cue_id, channel=channel, pitch=pitch, start_tick=start,
                release_tick=release, onset_tolerance_us=onset, duration_tolerance_us=duration)


def score(cues, ppq=480, end_tick=1440, tempos=None):
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=500000)], cues=cues)


def with_score(events, cues, ppq=480, end_tick=1440, tempos=None, score_ppq=480, score_tempos=None,
               score_end=None):
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=500000)],
                events=events,
                score=score(cues, ppq=score_ppq, end_tick=score_end or end_tick,
                            tempos=score_tempos))


def by_cue(result):
    return {entry['cue_id']: entry for entry in result['score_evaluation']['cues']}


class LegacyBehaviourTests(unittest.TestCase):
    def test_absent_score_keeps_legacy_report_exactly(self):
        data = sample()
        result = process(data)
        self.assertEqual(set(result), {'schema_version', 'duration_us', 'voices', 'unfinished_key_ids', 'summary'})
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['duration_us'], '2500000')
        self.assertEqual(result['summary'],
                         dict(voices=3, pedal_extended_voices=2, unfinished_keys=1, peak_polyphony=2))
        a, b, c = result['voices']
        self.assertEqual((a['key_release_us'], a['sound_end_us']), ('250000', '1500000'))
        self.assertEqual((b['start_us'], b['sound_duration_us']), ('500000', '1000000'))
        self.assertEqual(c['end_reason'], 'end_of_take')
        # The optional score key never leaks into the report.
        self.assertNotIn('score_evaluation', result)

    def test_same_tick_event_order_changes_pedal_causality(self):
        data = sample()
        data['events'] = [on('a', 0), pedal('p', 10, True), off('x', 10), pedal('up', 20, False)]
        first = process(data)['voices'][0]['sound_end_tick']
        data['events'][1:3] = data['events'][1:3][::-1]
        second = process(data)['voices'][0]['sound_end_tick']
        self.assertEqual((first, second), (20, 10))


class ScoreAttributionTests(unittest.TestCase):
    def test_consecutive_same_pitch_cues_pair_distinct_overlapping_keys(self):
        # Cues c1 (0..100) and c2 (50..60) share pitch; the keys overlap.  The
        # admissible two-pair assignments are (v1,v2), (v1,v3), (v2,v3).
        # Onset-deviation sums are 0, 5 and 55 ticks, so (v1,v2) wins even
        # though (v1,v3) has the smaller duration sum: the onset criterion
        # comes first.  No key may serve two cues.
        data = with_score(
            [on('v1', 0), on('v2', 50), on('v3', 55), off('r1', 70), off('r2', 150), off('r3', 200)],
            [cue('c1', 0, 100), cue('c2', 50, 60)],
            end_tick=200, score_end=200)
        result = process(data)
        cues = by_cue(result)
        self.assertEqual(cues['c1']['voice_id'], 'v1')
        self.assertEqual(cues['c2']['voice_id'], 'v2')
        self.assertEqual(result['score_evaluation']['extra_voice_ids'], ['v3'])
        # c1 target 100 ticks, v1 key 70 ticks -> -30 ticks.
        # c2 target 10 ticks, v2 key 100 ticks -> +90 ticks.
        self.assertEqual(cues['c1']['duration_deviation_us'], '-31250')
        self.assertEqual(cues['c2']['duration_deviation_us'], '93750')

    def test_single_voice_cannot_satisfy_two_cues(self):
        data = with_score([on('v1', 95), off('r1', 120)],
                          [cue('c1', 0, 10, onset=10 ** 6), cue('c2', 100, 110, onset=10 ** 6)])
        result = process(data)
        cues = by_cue(result)
        self.assertEqual(cues['c2']['voice_id'], 'v1')
        self.assertEqual(cues['c1']['onset_status'], 'missed')
        self.assertIsNone(cues['c1']['voice_id'])
        self.assertEqual(result['score_evaluation']['missed_cue_ids'], ['c1'])
        self.assertEqual(result['summary']['missed_cues'], 1)
        self.assertEqual(result['summary']['extra_voices'], 0)

    def test_attack_deviations_are_reported_early_and_late(self):
        data = with_score([on('v_early', 0), off('r1', 100), on('v_late', 100), off('r2', 200)],
                          [cue('c_early', 10, 110), cue('c_late', 90, 190)],
                          end_tick=200, score_end=200)
        cues = by_cue(process(data))
        # 10 ticks at 500000us/480ppq = 10417/... exact fraction
        self.assertEqual(cues['c_early']['onset_deviation_us'], '-31250/3')
        self.assertEqual(cues['c_early']['onset_status'], 'early')
        self.assertEqual(cues['c_late']['onset_deviation_us'], '31250/3')
        self.assertEqual(cues['c_late']['onset_status'], 'late')

    def test_tolerance_boundary_is_inclusive_and_exact(self):
        # With ppq 480 and 480us/quarter every tick is exactly 1us, so a
        # 10-tick onset deviation is exactly 10us and tolerance boundaries are
        # unambiguous integers.
        tempos = [dict(tick=0, microseconds_per_quarter=480)]
        at_bound = with_score([on('v', 10), off('r', 110)], [cue('c', 0, 90, onset=10, duration=10)],
                              tempos=tempos, end_tick=200, score_tempos=tempos, score_end=200)
        below = with_score([on('v', 10), off('r', 110)], [cue('c', 0, 90, onset=9, duration=9)],
                           tempos=tempos, end_tick=200, score_tempos=tempos, score_end=200)
        entry = by_cue(process(at_bound))['c']
        self.assertEqual((entry['onset_deviation_us'], entry['onset_status']), ('10', 'ok'))
        self.assertEqual((entry['duration_deviation_us'], entry['duration_status']), ('10', 'ok'))
        entry = by_cue(process(below))['c']
        self.assertEqual(entry['onset_status'], 'late')
        self.assertEqual(entry['duration_status'], 'late')

    def test_missed_and_extra_are_both_reported(self):
        # Pitch 60 has two cues but one voice: the cue at the later position
        # is missed.  Pitches 62 and 64 have voices but no cues: extra.
        data = with_score([on('v1', 0), on('v2', 0, pitch=62), on('v3', 0, pitch=64),
                           off('r1', 10), off('r2', 10, pitch=62), off('r3', 10, pitch=64)],
                          [cue('c1', 0, 10), cue('c2', 200, 210)])
        result = process(data)
        evaluation = result['score_evaluation']
        cues = by_cue(result)
        self.assertEqual(cues['c1']['voice_id'], 'v1')
        self.assertEqual(cues['c1']['onset_status'], 'ok')
        self.assertEqual(cues['c2']['onset_status'], 'missed')
        self.assertEqual(evaluation['missed_cue_ids'], ['c2'])
        self.assertEqual(evaluation['extra_voice_ids'], ['v2', 'v3'])
        self.assertEqual(result['summary']['missed_cues'], 1)
        self.assertEqual(result['summary']['extra_voices'], 2)
        self.assertEqual(result['summary']['matched_cues'], 1)

    def test_equal_cost_voice_tie_breaks_on_smallest_voice_id(self):
        # Two identical voices at the same tick, listed 'z' before 'a'.  Both
        # pairings cost the same, so the lexicographically smallest
        # (cue_id, voice_id) sequence picks voice 'a'.
        data = with_score([on('z', 10), on('a', 10), off('rz', 20), off('ra', 20)],
                          [cue('c1', 10, 20)], end_tick=200, score_end=200)
        result = process(data)
        self.assertEqual(by_cue(result)['c1']['voice_id'], 'a')
        self.assertEqual(result['score_evaluation']['extra_voice_ids'], ['z'])

    def test_equal_cost_cue_tie_breaks_on_smallest_cue_id(self):
        # Two cues share start_tick, so input order (z_cue, a_cue) is valid.
        # One voice and equal costs: the smaller cue_id is matched.
        data = with_score([on('v1', 10), off('r1', 20)],
                          [cue('z_cue', 10, 20), cue('a_cue', 10, 20)],
                          end_tick=200, score_end=200)
        result = process(data)
        self.assertEqual(by_cue(result)['a_cue']['voice_id'], 'v1')
        self.assertEqual(by_cue(result)['z_cue']['onset_status'], 'missed')
        self.assertEqual(result['score_evaluation']['missed_cue_ids'], ['z_cue'])

    def test_onset_sum_wins_before_duration_sum(self):
        # One voice, two cues: max one pair.  c1 deviates 10 ticks in onset but
        # 10 in duration; c2 deviates 15 in onset but only 5 in duration.  The
        # onset criterion is applied first, so c1 is chosen.
        data = with_score([on('v', 90), off('r', 150)],
                          [cue('c1', 100, 150), cue('c2', 105, 155)],
                          end_tick=200, score_end=200)
        result = process(data)
        cues = by_cue(result)
        self.assertEqual(cues['c1']['voice_id'], 'v')
        self.assertEqual(cues['c1']['duration_deviation_us'], '31250/3')
        self.assertEqual(cues['c2']['onset_status'], 'missed')

    def test_duration_sum_decides_only_after_onset_sum_ties(self):
        # Both cues start at the voice onset, so onset sums tie at zero.  c1's
        # target duration is 10 ticks, c2's is 40; the voice's key duration is
        # 20, so matching c1 leaves the smaller duration deviation.
        data = with_score([on('v', 0), off('r', 20)],
                          [cue('c1', 0, 10), cue('c2', 0, 40)],
                          end_tick=200, score_end=200)
        result = process(data)
        cues = by_cue(result)
        self.assertEqual(cues['c1']['voice_id'], 'v')
        self.assertEqual(cues['c1']['duration_deviation_us'], '31250/3')
        self.assertEqual(cues['c2']['onset_status'], 'missed')


class DurationEvaluationTests(unittest.TestCase):
    def test_unreleased_key_duration_is_undetermined_not_pedal_extension(self):
        # v1 is released at 20 but pedal keeps it sounding to 100; v2 is never
        # released and is force-stopped at end_tick.  Only v1's key release is
        # observable, and v2 must not borrow its sound_end_tick as a release.
        data = with_score(
            [pedal('p', 0, True), on('v1', 0), off('r1', 20), on('v2', 50), pedal('up', 100, False)],
            [cue('c1', 0, 20), cue('c2', 50, 70)],
            end_tick=120, score_end=120)
        result = process(data)
        voices = {v['voice_id']: v for v in result['voices']}
        self.assertEqual(voices['v1']['sound_end_tick'], 100)
        self.assertGreater(voices['v1']['sound_end_tick'], voices['v1']['key_release_tick'])
        cues = by_cue(result)
        self.assertEqual(cues['c1']['duration_deviation_us'], '0')
        self.assertEqual(cues['c1']['duration_status'], 'ok')
        self.assertIsNone(cues['c2']['duration_deviation_us'])
        self.assertEqual(cues['c2']['duration_status'], 'undetermined')
        self.assertEqual(cues['c2']['onset_status'], 'ok')
        self.assertEqual(result['summary']['duration_undetermined'], 1)

    def test_duration_uses_key_release_even_when_pedal_extends_sound(self):
        data = with_score([pedal('p', 0, True), on('v', 0), off('r', 20), pedal('up', 400, False)],
                          [cue('c1', 0, 20)], end_tick=500, score_end=500)
        result = process(data)
        self.assertEqual(result['voices'][0]['sound_end_tick'], 400)
        self.assertEqual(by_cue(result)['c1']['duration_deviation_us'], '0')
        self.assertEqual(by_cue(result)['c1']['duration_status'], 'ok')

    def test_short_key_duration_is_early(self):
        data = with_score([on('v', 0), off('r', 10)], [cue('c1', 0, 20)],
                          end_tick=100, score_end=100)
        entry = by_cue(process(data))['c1']
        self.assertEqual(entry['duration_deviation_us'], '-31250/3')
        self.assertEqual(entry['duration_status'], 'early')


class PreciseClockTests(unittest.TestCase):
    def test_different_ppq_and_mid_score_tempo_change_exact(self):
        # Performance: 500000us/q for ticks [0,240), 1000000us/q after.
        # Score: 250000us/q for ticks [0,480), 400000us/q after, ppq 960.
        data = dict(
            ppq=480, end_tick=960,
            tempos=[dict(tick=0, microseconds_per_quarter=500000),
                    dict(tick=240, microseconds_per_quarter=1000000)],
            events=[on('v', 480), off('r', 600)],
            score=dict(ppq=960, end_tick=960,
                       tempos=[dict(tick=0, microseconds_per_quarter=250000),
                               dict(tick=480, microseconds_per_quarter=400000)],
                       cues=[cue('c1', 720, 820, onset=100000, duration=100000)]))
        entry = by_cue(process(data))['c1']
        # voice onset 240*500000/480 + 240*1000000/480 = 750000us
        # cue onset 480*250000/960 + 240*400000/960 = 225000us
        self.assertEqual(entry['onset_deviation_us'], '525000')
        self.assertEqual(entry['onset_status'], 'late')
        # key duration 120 ticks over ppq 480 at 1000000us/q = 250000us
        # cue duration 100 ticks over ppq 960 at 400000us/q = 125000/3 us
        self.assertEqual(entry['duration_deviation_us'], '625000/3')
        self.assertEqual(entry['duration_status'], 'late')

    def test_fractional_microsecond_deviation_stays_exact(self):
        # ppq 3 vs 5: lcm 15 makes every scaled time an integer.
        data = dict(
            ppq=3, end_tick=9,
            tempos=[dict(tick=0, microseconds_per_quarter=1)],
            events=[on('v', 1), off('r', 4)],
            score=dict(ppq=5, end_tick=9,
                       tempos=[dict(tick=0, microseconds_per_quarter=1)],
                       cues=[cue('c1', 1, 6, onset=0, duration=0)]))
        entry = by_cue(process(data))['c1']
        self.assertEqual(entry['onset_deviation_us'], '2/15')
        self.assertEqual(entry['duration_deviation_us'], '0')
        self.assertEqual(entry['onset_status'], 'late')
        self.assertEqual(entry['duration_status'], 'ok')

    def test_zero_tolerance_zero_deviation_is_ok(self):
        data = with_score([on('v', 10), off('r', 20)], [cue('c1', 10, 20)],
                          end_tick=100, score_end=100)
        entry = by_cue(process(data))['c1']
        self.assertEqual((entry['onset_deviation_us'], entry['onset_status']), ('0', 'ok'))
        self.assertEqual((entry['duration_deviation_us'], entry['duration_status']), ('0', 'ok'))


class ScoreValidationTests(unittest.TestCase):
    def base(self):
        return with_score([on('v', 0), off('r', 10)], [cue('c1', 0, 10)], end_tick=100, score_end=100)

    def test_rejects_missing_extra_and_bad_fields(self):
        for mutate in (
            lambda d: d['score'].pop('ppq'),
            lambda d: d['score'].update(extra=1),
            lambda d: d['score']['cues'][0].pop('cue_id'),
            lambda d: d['score']['cues'][0].update(extra=1),
            lambda d: d['score']['cues'][0].update(channel=16),
            lambda d: d['score']['cues'][0].update(pitch=128),
            lambda d: d['score']['cues'][0].update(start_tick=-1),
            lambda d: d['score']['cues'][0].update(start_tick=101),
            lambda d: d['score']['cues'][0].update(onset_tolerance_us=-1),
            lambda d: d['score']['cues'][0].update(duration_tolerance_us=-1),
            lambda d: d['score']['cues'][0].update(onset_tolerance_us=1.5),
            lambda d: d['score']['cues'][0].update(duration_tolerance_us=True),
        ):
            data = self.base()
            mutate(data)
            with self.assertRaises(ValueError, msg=repr(data['score'])):
                process(data)

    def test_rejects_duplicate_cue_ids_and_unordered_cues(self):
        data = self.base()
        data['score']['cues'] = [cue('same', 0, 10), cue('same', 20, 30)]
        with self.assertRaises(ValueError):
            process(data)
        data = self.base()
        data['score']['cues'] = [cue('later', 20, 30), cue('earlier', 0, 10)]
        with self.assertRaises(ValueError):
            process(data)
        data = self.base()
        data['score']['cues'] = [cue('c1', 0, 10)]
        data['score']['cues'][0]['release_tick'] = 101
        with self.assertRaises(ValueError):
            process(data)

    def test_same_start_tick_keeps_input_order(self):
        # Equal start ticks are allowed; only decreasing order is rejected.
        data = with_score([on('v1', 0), on('v2', 0), off('r1', 10), off('r2', 10)],
                          [cue('b', 0, 10), cue('a', 0, 10)],
                          end_tick=100, score_end=100)
        result = process(data)
        self.assertEqual([entry['cue_id'] for entry in result['score_evaluation']['cues']], ['b', 'a'])

    def test_rejects_bad_score_tempo_map(self):
        data = self.base()
        data['score']['tempos'] = [dict(tick=1, microseconds_per_quarter=500000)]
        with self.assertRaises(ValueError):
            process(data)
        data = self.base()
        data['score']['tempos'] = [dict(tick=0, microseconds_per_quarter=0)]
        with self.assertRaises(ValueError):
            process(data)

    def test_rejects_non_object_score(self):
        data = self.base()
        data['score'] = []
        with self.assertRaises(ValueError):
            process(data)

    def test_invalid_performance_prevents_any_evaluation(self):
        data = self.base()
        data['events'] = [off('orphan', 0)]
        with self.assertRaises(ValueError):
            process(data)


class PublishTests(unittest.TestCase):
    def run_cli(self, argv):
        import contextlib, io
        from pedal_performance.cli import main
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return main(argv)

    def test_cli_publishes_score_report_and_never_overwrites(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            data = sample()
            data['score'] = score([cue('c1', 0, 240, onset=10 ** 6, duration=10 ** 6),
                                   cue('c2', 480, 720, pitch=64, onset=10 ** 6, duration=10 ** 6)])
            source.write_text(json.dumps(data))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--source', str(source), '--output', str(target)]), 0)
            published = json.loads(target.read_text())
            self.assertEqual(published['summary']['matched_cues'], 2)
            self.assertIn('score_evaluation', published)
            original = target.read_bytes()
            # Second publication to the same path must fail without touching it.
            self.assertEqual(self.run_cli(['--source', str(source), '--output', str(target)]), 2)
            self.assertEqual(target.read_bytes(), original)

    def test_invalid_score_leaves_no_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            data = sample()
            data['score'] = {'ppq': 480}
            source.write_text(json.dumps(data))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--source', str(source), '--output', str(target)]), 2)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).glob('.report-*')), [])

    def test_legacy_report_has_no_score_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.json'
            source.write_text(json.dumps(sample()))
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--source', str(source), '--output', str(target)]), 0)
            published = json.loads(target.read_text())
            self.assertNotIn('score_evaluation', published)
            self.assertEqual(published['summary'],
                             dict(voices=3, pedal_extended_voices=2, unfinished_keys=1, peak_polyphony=2))


if __name__ == '__main__':
    unittest.main()
