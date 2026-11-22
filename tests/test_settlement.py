import json
from pathlib import Path
import tempfile
import unittest

from pedal_performance.io import load, publish
from pedal_performance.settlement import settle


def on(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='on', pitch=pitch, velocity=80)


def off(id, tick, pitch=60, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='off', pitch=pitch)


def pedal(id, tick, down, channel=0):
    return dict(id=id, tick=tick, channel=channel, kind='pedal', down=down)


def perf(events, end_tick=2, ppq=1, tempos=None):
    return dict(ppq=ppq, end_tick=end_tick,
                tempos=tempos or [dict(tick=0, microseconds_per_quarter=1000000)],
                events=events)


def upsert(recording_id, revision, performance, owners):
    return dict(op='upsert', recording_id=recording_id, revision=revision,
                performance=performance,
                channel_owners=[dict(channel=channel, owner_id=owner)
                                for channel, owner in owners.items()])


def withdraw(recording_id, revision):
    return dict(op='withdraw', recording_id=recording_id, revision=revision)


def batch(project='proj', batch_id='b1', base=None, rate=1, changes=None):
    return dict(project_id=project, batch_id=batch_id, base_batch_id=base,
                cents_per_second=rate, changes=changes or [])


def acceptance_batches():
    """The documented first batch and its revision."""
    # Recording 甲: b sounds [0,1s), c sounds [0.5,1s); b's second half is
    # pedal sustain.  Recording 乙: a sounds for 0.5s only.
    jia_v1 = perf([
        on('b', 0, channel=0),
        pedal('p', 0, True, channel=0),
        off('b_off', 1, channel=0),
        on('c', 1, channel=1),
    ], end_tick=2, ppq=2)
    yi = perf([on('a', 0, channel=0)], end_tick=1, ppq=2)
    first = batch(batch_id='first', rate=1, changes=[
        upsert('jia', 1, jia_v1, {0: 'b', 1: 'c'}),
        upsert('yi', 1, yi, {0: 'a'}),
    ])
    # 甲 v2: b stops at 0.5s; everything else unchanged.  No events for 乙.
    jia_v2 = perf([
        on('b', 0, channel=0),
        off('b_off', 1, channel=0),
        on('c', 1, channel=1),
    ], end_tick=2, ppq=2)
    second = batch(batch_id='second', base='first', rate=1, changes=[
        upsert('jia', 2, jia_v2, {0: 'b', 1: 'c'}),
    ])
    return first, second


class AcceptanceTests(unittest.TestCase):
    def test_first_batch_prices_the_whole_project(self):
        first, _ = acceptance_batches()
        result = settle(first)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['batch_id'], 'first')
        self.assertEqual(result['head_batch_id'], 'first')
        self.assertEqual(result['total_cents'], 1)
        self.assertEqual(result['balances'], [
            {'owner_id': 'a', 'usage_us': '500000', 'payable_cents': 0},
            {'owner_id': 'b', 'usage_us': '750000', 'payable_cents': 1},
            {'owner_id': 'c', 'usage_us': '250000', 'payable_cents': 0},
        ])
        self.assertEqual(result['adjustments'], [
            {'owner_id': 'b', 'previous_cents': 0, 'payable_cents': 1,
             'delta_cents': 1},
        ])
        self.assertEqual(set(result), {
            'schema_version', 'status', 'project_id', 'batch_id', 'head_batch_id',
            'cents_per_second', 'total_cents', 'balances', 'adjustments',
            'checkpoint'})

    def test_revision_redistributes_the_single_cent_to_a(self):
        first, second = acceptance_batches()
        first_result = settle(first)
        second_result = settle(second, first_result)
        self.assertEqual(second_result['status'], 'applied')
        self.assertEqual(second_result['total_cents'], 1)
        # The whole project has not changed size, but every owner now ties on
        # the fractional remainder, so the one cent moves to ``a``.
        self.assertEqual(second_result['balances'], [
            {'owner_id': 'a', 'usage_us': '500000', 'payable_cents': 1},
            {'owner_id': 'b', 'usage_us': '500000', 'payable_cents': 0},
            {'owner_id': 'c', 'usage_us': '500000', 'payable_cents': 0},
        ])
        self.assertEqual(second_result['adjustments'], [
            {'owner_id': 'a', 'previous_cents': 0, 'payable_cents': 1,
             'delta_cents': 1},
            {'owner_id': 'b', 'previous_cents': 1, 'payable_cents': 0,
             'delta_cents': -1},
        ])
        self.assertEqual(second_result['head_batch_id'], 'second')

    def test_replaying_the_first_batch_after_the_revision_books_nothing(self):
        first, second = acceptance_batches()
        first_result = settle(first)
        second_result = settle(second, first_result)
        before = json.dumps(first_result, sort_keys=True)
        replay = settle(first, second_result)
        self.assertEqual(replay['status'], 'replayed')
        self.assertEqual(replay['batch_id'], 'first')
        self.assertEqual(replay['head_batch_id'], 'second')
        self.assertEqual(replay['adjustments'], [])
        # The correction is not undone: balances still show a receiving the cent.
        self.assertEqual([row['payable_cents'] for row in replay['balances']],
                         [1, 0, 0])
        # The original first result is untouched.
        self.assertEqual(json.dumps(first_result, sort_keys=True), before)
        # And the replay's own checkpoint can continue the project.
        third = batch(batch_id='third', base='second', rate=1,
                      changes=[withdraw('yi', 2)])
        continued = settle(third, replay)
        self.assertEqual(continued['status'], 'applied')
        self.assertEqual(continued['head_batch_id'], 'third')


class UsageTests(unittest.TestCase):
    def test_one_owner_on_several_overlapping_channels_counts_once(self):
        # Owner ``x`` owns channels 0 and 1 and has two overlapping voices,
        # plus a second voice on channel 1 partly outside the first.  The
        # owner is one sharer for the whole span [0, 6).
        performance = perf([
            on('v0', 0, channel=0),
            on('v1', 2, channel=1),
            off('v1o', 6, channel=1),
        ], end_tick=6)
        # v0 is unfinished and rings to end_tick 6.  x alone => 6 seconds.
        result = settle(batch(rate=1, changes=[upsert('r', 1, performance, {0: 'x', 1: 'x'})]))
        self.assertEqual(result['balances'],
                         [{'owner_id': 'x', 'usage_us': '6000000', 'payable_cents': 6}])

    def test_two_owners_overlap_splits_each_segment_in_half(self):
        performance = perf([
            on('a', 0, channel=0),
            on('b', 0, channel=1),
        ], end_tick=2)
        result = settle(batch(rate=1, changes=[
            upsert('r', 1, performance, {0: 'x', 1: 'y'})]))
        self.assertEqual([row['usage_us'] for row in result['balances']],
                         ['1000000', '1000000'])
        self.assertEqual([row['payable_cents'] for row in result['balances']], [1, 1])

    def test_unfinished_voice_rings_to_end_and_zero_length_is_silent(self):
        performance = perf([
            on('held', 0, channel=0),          # unfinished -> ends at end_tick
            on('zero', 1, pitch=62, channel=1),
            off('zerooff', 1, pitch=62, channel=1),  # zero length: silent
        ], end_tick=4)
        result = settle(batch(rate=1, changes=[
            upsert('r', 1, performance, {0: 'x', 1: 'y'})]))
        # Only x sounds, for the full 4 seconds; y has zero usage and is absent.
        self.assertEqual(result['balances'],
                         [{'owner_id': 'x', 'usage_us': '4000000', 'payable_cents': 4}])

    def test_tempo_change_and_fractional_microseconds_are_exact(self):
        # ppq 2 with a tempo change: clock.at(t) = t/2 for t<2, then 1+(t-2)*3/2.
        performance = perf([
            on('x1', 0, channel=0),
            on('y1', 1, channel=1),
            off('y1o', 3, channel=1),
        ], end_tick=4, ppq=2,
            tempos=[dict(tick=0, microseconds_per_quarter=1),
                    dict(tick=2, microseconds_per_quarter=3)])
        result = settle(batch(rate=500000, changes=[
            upsert('r', 1, performance, {0: 'x', 1: 'y'})]))
        # x sounds over [0,4); y over [1,3) where both share every segment:
        # x alone for [0,1) and [3,4), plus half of [1,3) => 3us; y gets 1us.
        # At 0.5 cent/us that is 1.5 and 0.5; floors are 1 and 0, the project
        # total floors to 2, so the one spare cent goes to x (larger remainder
        # than y is equal, but x sorts first anyway).
        self.assertEqual(result['balances'], [
            {'owner_id': 'x', 'usage_us': '3', 'payable_cents': 2},
            {'owner_id': 'y', 'usage_us': '1', 'payable_cents': 0},
        ])

    def test_usage_is_summed_across_recordings_before_rounding(self):
        # Two separate recordings each give owner ``x`` 3/5 us.  Individually
        # each would floor to 0 cents at 1 cent/us; together 6/5 floors to 1.
        performance = perf([on('x1', 0, channel=0), off('x1o', 3, channel=0)],
                           end_tick=5, ppq=5,
                           tempos=[dict(tick=0, microseconds_per_quarter=1)])
        result = settle(batch(rate=1000000, changes=[
            upsert('r1', 1, performance, {0: 'x'}),
            upsert('r2', 1, performance, {0: 'x'}),
        ]))
        self.assertEqual(result['balances'],
                         [{'owner_id': 'x', 'usage_us': '6/5', 'payable_cents': 1}])

    def test_fractional_remainder_tie_breaks_by_owner_id(self):
        # ppq 2, one tick = 1/2 us.  Both owners sound over the full tick, so
        # each has 1/4 us.  At 1 cent/us each amount is 1/4 cent: floors 0, the
        # project total floors to 0, so nobody is paid.
        performance = perf([on('a', 0, channel=0), on('b', 0, channel=1)],
                           end_tick=1, ppq=2,
                           tempos=[dict(tick=0, microseconds_per_quarter=1)])
        result = settle(batch(rate=1000000, changes=[
            upsert('r', 1, performance, {0: 'a', 1: 'b'})]))
        self.assertEqual(result['balances'], [
            {'owner_id': 'a', 'usage_us': '1/4', 'payable_cents': 0},
            {'owner_id': 'b', 'usage_us': '1/4', 'payable_cents': 0},
        ])
        self.assertEqual(result['total_cents'], 0)

    def test_remainder_tie_pays_the_lexicographically_smaller_owner(self):
        # Three separate recordings each give one owner 1/2 us, so each amount
        # is 1/2 cent.  Floors are 0,0,0 and the project total floors to 1; all
        # remainders are equal, so the cent goes to the lexicographically
        # smallest owner id.
        performance = perf([on('a', 0, channel=0)], end_tick=1, ppq=2,
                           tempos=[dict(tick=0, microseconds_per_quarter=1)])
        result = settle(batch(rate=1000000, changes=[
            upsert('r1', 1, performance, {0: 'm'}),
            upsert('r2', 1, performance, {0: 'z'}),
            upsert('r3', 1, performance, {0: 'a'}),
        ]))
        self.assertEqual([(row['owner_id'], row['payable_cents'])
                          for row in result['balances']],
                         [('a', 1), ('m', 0), ('z', 0)])


class LifecycleTests(unittest.TestCase):
    def base_recording(self):
        return perf([on('n', 0, channel=0), off('no', 1, channel=0)], end_tick=2)

    def test_withdraw_and_restore_continue_the_revision(self):
        first = settle(batch(batch_id='b1', rate=1, changes=[
            upsert('r', 1, self.base_recording(), {0: 'x'})]))
        self.assertEqual(first['balances'],
                         [{'owner_id': 'x', 'usage_us': '1000000', 'payable_cents': 1}])
        withdrawn = settle(batch(batch_id='b2', base='b1', rate=1, changes=[
            withdraw('r', 2)]), first)
        self.assertEqual(withdrawn['status'], 'applied')
        self.assertEqual(withdrawn['total_cents'], 0)
        self.assertEqual(withdrawn['balances'], [])
        self.assertEqual(withdrawn['adjustments'], [
            {'owner_id': 'x', 'previous_cents': 1, 'payable_cents': 0,
             'delta_cents': -1}])
        restored = settle(batch(batch_id='b3', base='b2', rate=1, changes=[
            upsert('r', 3, self.base_recording(), {0: 'x'})]), withdrawn)
        self.assertEqual(restored['status'], 'applied')
        self.assertEqual(restored['balances'],
                         [{'owner_id': 'x', 'usage_us': '1000000', 'payable_cents': 1}])
        self.assertEqual(restored['adjustments'][0]['delta_cents'], 1)

    def test_invalid_revision_sequences_are_rejected(self):
        first = settle(batch(batch_id='b1', rate=1, changes=[
            upsert('r', 1, self.base_recording(), {0: 'x'})]))
        bad_batches = [
            batch(batch_id='b2', base='b1', rate=1,
                  changes=[upsert('r', 3, self.base_recording(), {0: 'x'})]),
            batch(batch_id='b2', base='b1', rate=1,
                  changes=[upsert('new', 2, self.base_recording(), {0: 'x'})]),
            batch(batch_id='b2', base='b1', rate=1,
                  changes=[withdraw('missing', 1)]),
        ]
        for bad in bad_batches:
            with self.assertRaises(ValueError, msg=repr(bad['changes'])):
                settle(bad, first)
        # A withdrawn recording cannot be withdrawn again.
        withdrawn = settle(batch(batch_id='b2', base='b1', rate=1,
                                 changes=[withdraw('r', 2)]), first)
        with self.assertRaises(ValueError):
            settle(batch(batch_id='b3', base='b2', rate=1,
                         changes=[withdraw('r', 3)]), withdrawn)

    def test_each_recording_appears_once_and_ids_are_bounded(self):
        change = upsert('r', 1, self.base_recording(), {0: 'x'})
        with self.assertRaises(ValueError):
            settle(batch(changes=[change, change]))
        # A batch is 1..32 changes.
        with self.assertRaises(ValueError):
            settle(batch(changes=[]))
        with self.assertRaises(ValueError):
            settle(batch(changes=[upsert(f'r{i}', 1, self.base_recording(), {0: 'x'})
                                  for i in range(33)]))
        # A project registers at most 32 distinct recording ids.
        first = settle(batch(batch_id='b1', rate=1, changes=[
            upsert(f'r{i}', 1, self.base_recording(), {0: 'x'}) for i in range(32)]))
        with self.assertRaises(ValueError):
            settle(batch(batch_id='b2', base='b1', rate=1, changes=[
                upsert('overflow', 1, self.base_recording(), {0: 'x'})]), first)

    def test_channel_owners_must_cover_every_onset_channel(self):
        performance = perf([on('n', 0, channel=0)], end_tick=1)
        with self.assertRaises(ValueError):
            settle(batch(changes=[upsert('r', 1, performance, {})]))
        # Owners for channels without onsets are allowed.
        result = settle(batch(rate=1, changes=[
            upsert('r', 1, performance, {0: 'x', 5: 'y'})]))
        self.assertEqual([row['owner_id'] for row in result['balances']], ['x'])


class ReplayTests(unittest.TestCase):
    def test_reordered_content_replays_and_changed_content_conflicts(self):
        performance = perf([on('n', 0, channel=0), off('no', 1, channel=0)], end_tick=2)
        original = batch(batch_id='b1', rate=1, changes=[
            upsert('r', 1, performance, {0: 'x', 1: 'y'}),
            upsert('s', 1, perf([on('m', 0, channel=0)], end_tick=1), {0: 'y'}),
        ])
        result = settle(original)
        # Same content: object key order, changes order and channel_owners
        # order are ignored.
        reordered = dict(changes=list(reversed(original['changes'])),
                         cents_per_second=1, base_batch_id=None,
                         batch_id='b1', project_id='proj')
        reordered['changes'][0] = dict(reordered['changes'][0])
        reordered['changes'][0]['channel_owners'] = list(
            reversed(reordered['changes'][0]['channel_owners']))
        replay = settle(reordered, result)
        self.assertEqual(replay['status'], 'replayed')
        self.assertEqual(replay['adjustments'], [])
        # Changing a value under the same id is a conflict.
        conflict = json.loads(json.dumps(original))
        conflict['changes'][0]['revision'] = 2
        with self.assertRaises(ValueError):
            settle(conflict, result)
        # Changing the base_batch_id is also a content change.
        conflict = json.loads(json.dumps(original))
        conflict['base_batch_id'] = 'other'
        with self.assertRaises(ValueError):
            settle(conflict, result)
        # Same-tick event order inside a performance stays semantic.
        swapped = json.loads(json.dumps(original))
        swapped['changes'][0]['performance']['events'][0]['velocity'] = 81
        with self.assertRaises(ValueError):
            settle(swapped, result)
        # Reordering two distinct same-tick events changes the content even
        # though the set of events is unchanged.
        reordered_events = json.loads(json.dumps(original))
        events = reordered_events['changes'][0]['performance']['events']
        events[0], events[1] = events[1], events[0]
        with self.assertRaises(ValueError):
            settle(reordered_events, result)

    def test_non_latest_batch_replays_after_its_recording_advanced(self):
        rec = perf([on('n', 0, channel=0), off('no', 1, channel=0)], end_tick=2)
        first = settle(batch(batch_id='b1', rate=1, changes=[upsert('r', 1, rec, {0: 'x'})]))
        second = settle(batch(batch_id='b2', base='b1', rate=1,
                              changes=[upsert('r', 2, rec, {0: 'x'})]), first)
        third = settle(batch(batch_id='b3', base='b2', rate=1,
                             changes=[upsert('r', 3, rec, {0: 'x'})]), second)
        # b2's original base is stale and r has since moved to revision 3, yet
        # replaying b2 must not re-book, undo the correction, or revert head.
        before = json.dumps(third, sort_keys=True)
        replay = settle(batch(batch_id='b2', base='b1', rate=1,
                              changes=[upsert('r', 2, rec, {0: 'x'})]), third)
        self.assertEqual(replay['status'], 'replayed')
        self.assertEqual(replay['adjustments'], [])
        self.assertEqual(replay['head_batch_id'], 'b3')
        self.assertEqual(json.dumps(third, sort_keys=True), before)
        # A genuinely new id using b2's stale base is still rejected.
        with self.assertRaises(ValueError):
            settle(batch(batch_id='b4', base='b2', rate=1,
                         changes=[withdraw('r', 4)]), third)

    def test_stale_base_is_rejected_for_new_ids(self):
        rec = perf([on('n', 0, channel=0), off('no', 1, channel=0)], end_tick=2)
        first = settle(batch(batch_id='b1', rate=1, changes=[upsert('r', 1, rec, {0: 'x'})]))
        second = settle(batch(batch_id='b2', base='b1', rate=1,
                              changes=[withdraw('r', 2)]), first)
        for base in (None, 'b1', 'unknown'):
            with self.assertRaises(ValueError, msg=str(base)):
                settle(batch(batch_id='b3', base=base, rate=1,
                             changes=[upsert('r', 3, rec, {0: 'x'})]), second)
        # The correct base continues.
        third = settle(batch(batch_id='b3', base='b2', rate=1,
                             changes=[upsert('r', 3, rec, {0: 'x'})]), second)
        self.assertEqual(third['status'], 'applied')

    def test_first_batch_requires_null_base_and_matching_project(self):
        rec = perf([on('n', 0, channel=0)], end_tick=1)
        with self.assertRaises(ValueError):
            settle(batch(base='x', changes=[upsert('r', 1, rec, {0: 'x'})]))
        first = settle(batch(batch_id='b1', rate=5,
                             changes=[upsert('r', 1, rec, {0: 'x'})]))
        with self.assertRaises(ValueError):
            settle(batch(project='other', batch_id='b2', base='b1', rate=5,
                         changes=[withdraw('r', 2)]), first)
        with self.assertRaises(ValueError):
            settle(batch(batch_id='b2', base='b1', rate=6,
                         changes=[withdraw('r', 2)]), first)

    def test_one_bad_change_publishes_nothing(self):
        rec = perf([on('n', 0, channel=0)], end_tick=1)
        good = upsert('r', 1, rec, {0: 'x'})
        bad = dict(good, revision=2, recording_id='s')  # new recording must be rev 1
        with self.assertRaises(ValueError):
            settle(batch(changes=[good, bad]))
        # Legitimate but empty settlement still records as applied.
        result = settle(batch(rate=1, changes=[upsert('r', 1,
                                                      perf([], end_tick=1), {})]))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['adjustments'], [])
        self.assertEqual(result['balances'], [])


class BatchValidationTests(unittest.TestCase):
    def rec(self):
        return perf([on('n', 0, channel=0), off('no', 1, channel=0)], end_tick=2)

    def test_structural_defects_are_rejected(self):
        def make():
            return batch(rate=1, changes=[upsert('r', 1, self.rec(), {0: 'x'})])
        bad = []
        data = make(); data.pop('batch_id'); bad.append(data)
        data = make(); data['extra'] = 1; bad.append(data)
        data = make(); data['project_id'] = 'bad id!'; bad.append(data)
        data = make(); data['cents_per_second'] = 0; bad.append(data)
        data = make(); data['cents_per_second'] = 1000001; bad.append(data)
        data = make(); data['cents_per_second'] = True; bad.append(data)
        data = make(); data['changes'][0]['revision'] = 0; bad.append(data)
        data = make(); data['changes'][0]['revision'] = 257; bad.append(data)
        data = make(); data['changes'][0]['op'] = 'delete'; bad.append(data)
        data = make(); data['changes'][0]['channel_owners'][0]['channel'] = 16; bad.append(data)
        data = make()
        data['changes'][0]['channel_owners'] = [dict(channel=0, owner_id='x'),
                                                dict(channel=0, owner_id='y')]
        bad.append(data)
        data = make(); data['changes'][0]['performance']['score'] = {}
        data['changes'][0]['performance']['score'] = dict(ppq=2, end_tick=2,
                                                         tempos=[dict(tick=0, microseconds_per_quarter=1)],
                                                         cues=[])
        bad.append(data)
        data = make()
        data['changes'][0]['performance']['events'] = [on(f'n{i}', 0) for i in range(2001)]
        bad.append(data)
        data = make(); data['changes'][0]['performance']['events'] = [off('orphan', 0)]
        bad.append(data)
        data = make(); data['changes'][0]['channel_owners'] = []
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                settle(data)

    def test_corrupt_previous_results_are_rejected(self):
        rec = self.rec()
        first = settle(batch(batch_id='b1', rate=1, changes=[upsert('r', 1, rec, {0: 'x'})]))
        bad = []
        data = json.loads(json.dumps(first)); data.pop('checkpoint'); bad.append(data)
        data = json.loads(json.dumps(first)); data['schema_version'] = 2; bad.append(data)
        data = json.loads(json.dumps(first)); data['total_cents'] = 99; bad.append(data)
        data = json.loads(json.dumps(first))
        data['checkpoint']['recordings'][0]['usage'][0]['usage_us'] = '-1'
        bad.append(data)
        data = json.loads(json.dumps(first))
        data['checkpoint']['batches'] = []
        bad.append(data)
        data = json.loads(json.dumps(first)); data['balances'] = []; bad.append(data)
        data = json.loads(json.dumps(first))
        data['adjustments'][0]['payable_cents'] = 5
        data['adjustments'][0]['delta_cents'] = 5
        bad.append(data)
        data = json.loads(json.dumps(first))
        data['checkpoint']['booked'][0]['cents'] = 7
        bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError, msg=repr(data)):
                settle(batch(batch_id='b2', base='b1', rate=1,
                             changes=[withdraw('r', 2)]), data)


class CliTests(unittest.TestCase):
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

    def test_cli_first_and_continuation_batches(self):
        first, second = acceptance_batches()
        with tempfile.TemporaryDirectory() as tmp:
            first_in = self.write(tmp, 'first.json', first)
            second_in = self.write(tmp, 'second.json', second)
            first_out = Path(tmp) / 'first-out.json'
            second_out = Path(tmp) / 'second-out.json'
            self.assertEqual(self.run_cli(['--settlement', str(first_in),
                                           '--output', str(first_out)]), 0)
            self.assertEqual(load(first_out)['status'], 'applied')
            self.assertEqual(self.run_cli(['--settlement', str(second_in),
                                           '--previous', str(first_out),
                                           '--output', str(second_out)]), 0)
            published = load(second_out)
            self.assertEqual(published['status'], 'applied')
            self.assertEqual(published['adjustments'], [
                {'owner_id': 'a', 'previous_cents': 0, 'payable_cents': 1,
                 'delta_cents': 1},
                {'owner_id': 'b', 'previous_cents': 1, 'payable_cents': 0,
                 'delta_cents': -1},
            ])
            # The previous result is untouched and the output cannot be reused.
            original = first_out.read_bytes()
            self.assertEqual(self.run_cli(['--settlement', str(second_in),
                                           '--previous', str(first_out),
                                           '--output', str(second_out)]), 2)
            self.assertEqual(first_out.read_bytes(), original)
            self.assertEqual(list(Path(tmp).glob('.report-*')), [])

    def test_cli_rejects_previous_without_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = self.write(tmp, 'source.json', dict(ppq=2, end_tick=2,
                                                         tempos=[dict(tick=0, microseconds_per_quarter=1)],
                                                         events=[]))
            target = Path(tmp) / 'out.json'
            with self.assertRaises(SystemExit) as ctx:
                self.run_cli(['--source', str(source), '--previous', str(source),
                              '--output', str(target)])
            self.assertEqual(ctx.exception.code, 2)
            self.assertFalse(target.exists())

    def test_cli_invalid_change_exits_2_without_publishing(self):
        first, _ = acceptance_batches()
        with tempfile.TemporaryDirectory() as tmp:
            broken = json.loads(json.dumps(first))
            broken['changes'][0]['revision'] = 2
            source = self.write(tmp, 'broken.json', broken)
            target = Path(tmp) / 'out.json'
            self.assertEqual(self.run_cli(['--settlement', str(source),
                                           '--output', str(target)]), 2)
            self.assertFalse(target.exists())

    def test_legacy_modes_still_work(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            for name, flag, summary_key in (
                    ('input.json', '--source', 'summary'),
                    ('input-score.json', '--source', 'score_evaluation'),
                    ('input-comp.json', '--comp', 'status')):
                source = self.write(tmp, name,
                                    json.loads((root / 'fixtures' / name).read_text()))
                target = Path(tmp) / f'out-{name}'
                self.assertEqual(self.run_cli([flag, str(source),
                                               '--output', str(target)]), 0)
                self.assertIn(summary_key, load(target))


if __name__ == '__main__':
    unittest.main()
