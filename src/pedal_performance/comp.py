"""Multi-take comping: splice aligned takes into one playable reference.

A comping manifest aligns several takes of the same piece to shared cut
points and prices every section of every take.  The compiler reports the
per-take performance state just before each internal cut, picks one take per
section under a switch budget and an exact duration window, and emits a
synthesised performance that the legacy single-take pipeline can replay.

All section and total durations are exact ``Fraction`` microsecond values;
nothing is rounded before the final string form.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from fractions import Fraction

from .checks import array, fields, ident, integer, unique
from .clock import TempoClock
from .model import validate
from .service import process

DURATION_LIMIT = 6 * 10 ** 14
COST_LIMIT = 10 ** 9
MAX_TAKES = 4
MAX_SECTIONS = 12
MAX_TOTAL_EVENTS = 8000
MAX_TOTAL_TEMPOS = 980


def validate_manifest(data):
    """Validate a comping manifest; any defect makes the whole input invalid."""
    fields(data, ('cuts', 'takes', 'min_duration_us', 'max_duration_us', 'max_switches'))
    takes = array(data['takes'], 1, MAX_TAKES)
    minimum = integer(data['min_duration_us'], 0, DURATION_LIMIT)
    maximum = integer(data['max_duration_us'], 0, DURATION_LIMIT)
    if maximum < minimum:
        raise ValueError('max_duration_us must not be below min_duration_us')
    take_ids = []
    ppq = end = None
    total_events = total_tempos = 0
    for take in takes:
        fields(take, ('take_id', 'performance', 'section_costs'))
        take_ids.append(ident(take['take_id']))
        performance = take['performance']
        if not isinstance(performance, dict):
            raise ValueError('performance object required')
        # A take performance uses the four legacy required fields; no score.
        fields(performance, ('ppq', 'end_tick', 'tempos', 'events'))
        validate(performance)
        if ppq is None:
            ppq, end = performance['ppq'], performance['end_tick']
        elif performance['ppq'] != ppq or performance['end_tick'] != end:
            raise ValueError('takes must share ppq and end_tick')
        total_events += len(performance['events'])
        total_tempos += len(performance['tempos'])
    unique(take_ids)
    if total_events > MAX_TOTAL_EVENTS:
        raise ValueError('too many events across takes')
    if total_tempos > MAX_TOTAL_TEMPOS:
        raise ValueError('too many tempo points across takes')
    cuts = array(data['cuts'], 2, MAX_SECTIONS + 1)
    previous = -1
    for cut in cuts:
        integer(cut, 0, end)
        if cut <= previous:
            raise ValueError('cuts must strictly increase')
        previous = cut
    if cuts[0] != 0 or cuts[-1] != end:
        raise ValueError('cuts must run from 0 to the shared end_tick')
    sections = len(cuts) - 1
    integer(data['max_switches'], 0, sections - 1)
    for take in takes:
        costs = array(take['section_costs'], sections, sections)
        for cost in costs:
            if cost is not None:
                integer(cost, 0, COST_LIMIT)
    return data


def _cut_states(events, cut_ticks):
    """Performance state just before the first event at each cut tick.

    Only events with ``tick < cut`` are applied, so later events and the
    end-of-take force-stop never erase voices active at the cut.  An
    unmatched note off is a causality error and invalidates the manifest.
    """
    cuts = sorted(cut_ticks)
    states = {}
    held = defaultdict(deque)
    deferred = defaultdict(list)
    pedal = defaultdict(bool)
    index = 0

    def snapshot(tick):
        states[tick] = (
            sorted(voice for queue in held.values() for voice in queue),
            sorted(voice for voices in deferred.values() for voice in voices),
            sorted(channel for channel, down in pedal.items() if down),
        )

    for event in events:
        tick = event['tick']
        while index < len(cuts) and cuts[index] <= tick:
            snapshot(cuts[index])
            index += 1
        channel = event['channel']
        if event['kind'] == 'on':
            held[channel, event['pitch']].append(event['id'])
        elif event['kind'] == 'off':
            queue = held[channel, event['pitch']]
            if not queue:
                raise ValueError(f'unmatched note off {event["id"]}')
            voice = queue.popleft()
            if pedal[channel]:
                deferred[channel].append(voice)
        else:
            pedal[channel] = event['down']
            if not event['down']:
                deferred[channel].clear()
    while index < len(cuts):
        snapshot(cuts[index])
        index += 1
    return states


def _diagnostics(takes, cuts):
    """Per-cut, per-take state report plus the raw states for planning."""
    internal = cuts[1:-1]
    states = {}
    for take in takes:
        states[take['take_id']] = _cut_states(take['performance']['events'], internal)
    report = []
    for cut in internal:
        report.append({
            'tick': cut,
            'takes': [{
                'take_id': take_id,
                'held_voice_ids': states[take_id][cut][0],
                'sustained_voice_ids': states[take_id][cut][1],
                'pedal_down_channels': states[take_id][cut][2],
            } for take_id in sorted(states)],
        })
    return report, states


class _SparseMin:
    """Static range-minimum table over a comparable sequence."""

    def __init__(self, values):
        self._levels = [list(values)]
        span = 1
        while 2 * span <= len(values):
            previous = self._levels[-1]
            self._levels.append([min(previous[i], previous[i + span])
                                 for i in range(len(values) - 2 * span + 1)])
            span *= 2

    def query(self, lo, hi):
        """Minimum over ``values[lo:hi]``; ``None`` for an empty range."""
        if lo >= hi:
            return None
        width = hi - lo
        level = self._levels[width.bit_length() - 1]
        span = 1 << (width.bit_length() - 1)
        return min(level[lo], level[hi - span])


def _enumerate(costs, durations, clear, cuts, take_ids, sections, max_switches, max_duration):
    """All feasible take assignments over ``sections`` as (cost, us, switches, seq)."""
    entries = []

    def rec(pos, cost, duration, switches, seq):
        if pos == len(sections):
            entries.append((cost, duration, switches, seq))
            return
        section = sections[pos]
        boundary = cuts[section]
        for index, take_id in enumerate(take_ids):
            cost_here = costs[section][index]
            if cost_here is None:
                continue
            added = 0
            if seq and take_id != seq[-1]:
                if not (clear[seq[-1]][boundary] and clear[take_id][boundary]):
                    continue
                added = 1
            if switches + added > max_switches:
                continue
            total = duration + durations[section][index]
            if total > max_duration:
                continue
            rec(pos + 1, cost + cost_here, total, switches + added, seq + (take_id,))

    rec(0, 0, Fraction(0), 0, ())
    return entries


def _plan(takes, cuts, states, min_duration, max_duration, max_switches):
    """Pick one take per section minimising (cost, switches, take_id sequence).

    Meet in the middle: both halves are enumerated, the right half is indexed
    by first take and switch cap in duration-sorted range-minimum tables, then
    every left half is joined with the best right half inside the exact
    duration window.  Returns ``(plan, sequence)`` or ``None`` when infeasible.
    """
    take_ids = [take['take_id'] for take in takes]
    clocks = {take['take_id']: TempoClock(take['performance']['ppq'], take['performance']['tempos'])
              for take in takes}
    sections = len(cuts) - 1
    costs = [[take['section_costs'][i] for take in takes] for i in range(sections)]
    durations = [[clocks[take['take_id']].at(cuts[i + 1]) - clocks[take['take_id']].at(cuts[i])
                  for take in takes] for i in range(sections)]
    clear = {take_id: {cut: not (states[take_id][cut][0] or states[take_id][cut][1])
                       for cut in cuts[1:-1]}
             for take_id in take_ids}
    split = sections // 2
    left = _enumerate(costs, durations, clear, cuts, take_ids,
                      list(range(split)), max_switches, max_duration)
    right = _enumerate(costs, durations, clear, cuts, take_ids,
                       list(range(split, sections)), max_switches, max_duration)
    by_first = defaultdict(list)
    for entry in right:
        by_first[entry[3][0]].append(entry)
    pools = {}
    for first, entries in by_first.items():
        for limit in range(max_switches + 1):
            pool = sorted((entry for entry in entries if entry[2] <= limit),
                          key=lambda entry: entry[1])
            pools[first, limit] = (
                [entry[1] for entry in pool],
                _SparseMin([(entry[0], entry[2], entry[3], entry[1]) for entry in pool])
                if pool else None)
    boundary = cuts[split] if split else None
    best = None
    for cost_l, duration_l, switches_l, seq_l in left:
        last_l = seq_l[-1] if seq_l else None
        for first in by_first:
            if last_l is None or first == last_l:
                added = 0
            else:
                if not (clear[last_l][boundary] and clear[first][boundary]):
                    continue
                added = 1
            remaining = max_switches - switches_l - added
            if remaining < 0:
                continue
            durations_r, table = pools[first, remaining]
            if table is None:
                continue
            lo = bisect_left(durations_r, min_duration - duration_l)
            hi = bisect_right(durations_r, max_duration - duration_l)
            found = table.query(lo, hi)
            if found is None:
                continue
            cost_r, switches_r, seq_r, duration_r = found
            candidate = (cost_l + cost_r, switches_l + switches_r + added, seq_l + seq_r)
            if best is None or candidate < best[0]:
                best = (candidate, duration_l + duration_r)
    if best is None:
        return None
    (total_cost, total_switches, seq), total_duration = best
    index_of = {take_id: index for index, take_id in enumerate(take_ids)}
    plan = {
        'sections': [
            {'start_tick': cuts[i], 'end_tick': cuts[i + 1], 'take_id': seq[i],
             'cost': costs[i][index_of[seq[i]]],
             'duration_us': str(durations[i][index_of[seq[i]]])}
            for i in range(sections)
        ],
        'total_cost': total_cost,
        'total_duration_us': str(total_duration),
        'switches': total_switches,
    }
    return plan, seq


def _merged_tempos(seq, takes_by_id, cuts):
    """Tempo map of the splice: tick 0 plus every duration-affecting change.

    Inside a section the selected take's effective tempo applies; at a section
    start the take chosen for the section on the right governs that tick.
    """
    points = []
    for i, take_id in enumerate(seq):
        left, right = cuts[i], cuts[i + 1]
        tempos = takes_by_id[take_id]['performance']['tempos']
        ticks = [tempo['tick'] for tempo in tempos]
        index = bisect_right(ticks, left) - 1
        points.append((left, tempos[index]['microseconds_per_quarter']))
        points.extend((tempo['tick'], tempo['microseconds_per_quarter'])
                      for tempo in tempos if left < tempo['tick'] < right)
    merged = []
    for tick, rate in points:
        if merged and merged[-1]['microseconds_per_quarter'] == rate:
            continue
        merged.append({'tick': tick, 'microseconds_per_quarter': rate})
    return merged


def _merged_events(seq, takes_by_id, cuts, states):
    """Splice selected sections' events and patch pedal state at switches.

    Every output event gets a fresh sequential id; ``origins`` records, in
    output order, where each event came from.  A switch adds one pedal event
    per channel whose pre-cut pedal state differs between the two takes, set
    to the entering take's pre-cut state, ordered by channel and placed before
    the entering take's own events at the cut tick.
    """
    events = []
    origins = []
    counter = 0
    last_section = len(seq) - 1
    for i, take_id in enumerate(seq):
        left, right = cuts[i], cuts[i + 1]
        if i > 0 and seq[i - 1] != take_id:
            before = set(states[seq[i - 1]][left][2])
            after = set(states[take_id][left][2])
            for channel in sorted(before ^ after):
                output_id = f'e{counter}'
                counter += 1
                events.append({'id': output_id, 'tick': left, 'channel': channel,
                               'kind': 'pedal', 'down': channel in after})
                origins.append({'output_id': output_id, 'take_id': None, 'event_id': None,
                                'patched': True, 'tick': left, 'channel': channel})
        for event in takes_by_id[take_id]['performance']['events']:
            tick = event['tick']
            if tick < left or tick > right or (tick == right and i < last_section):
                continue
            output_id = f'e{counter}'
            counter += 1
            events.append({**event, 'id': output_id})
            origins.append({'output_id': output_id, 'take_id': take_id,
                            'event_id': event['id'], 'patched': False})
    return events, origins


def compose(data):
    """Compile a comping manifest into the publishable result document."""
    validate_manifest(data)
    cuts = data['cuts']
    takes = data['takes']
    diagnostics, states = _diagnostics(takes, cuts)
    planned = _plan(takes, cuts, states,
                    data['min_duration_us'], data['max_duration_us'], data['max_switches'])
    if planned is None:
        return {'status': 'no_plan', 'cut_diagnostics': diagnostics,
                'plan': None, 'performance': None, 'event_origins': None, 'report': None}
    plan, seq = planned
    takes_by_id = {take['take_id']: take for take in takes}
    events, origins = _merged_events(seq, takes_by_id, cuts, states)
    performance = {'ppq': takes[0]['performance']['ppq'],
                   'end_tick': takes[0]['performance']['end_tick'],
                   'tempos': _merged_tempos(seq, takes_by_id, cuts),
                   'events': events}
    report = process(performance)
    return {'status': 'ok', 'cut_diagnostics': diagnostics, 'plan': plan,
            'performance': performance, 'event_origins': origins, 'report': report}
