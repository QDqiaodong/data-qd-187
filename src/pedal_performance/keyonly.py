"""Single-take key-only export for key-message teaching terminals.

The terminals do not understand pedals, and a lane can sound at most one key
per pitch at a time (different pitches may sound together).  Each lane loads a
single program for the whole take and can never be retuned, even after its
voices have gone silent.

This module bakes the pedal-decided sounding interval of every positive
duration voice into plain note-on/note-off messages and picks the cheapest
lane configuration that can hold the whole take.  Zero-duration voices are
registered but not played.
"""
from collections import defaultdict
from functools import lru_cache

from .checks import array, fields, ident, integer, unique
from .model import validate
from .voices import perform

MAX_EVENTS = 4000
MAX_CHANNEL_PROGRAMS = 16
MAX_LANES = 12
MAX_PROGRAM_COSTS = 8
MAX_DISTINCT_PROGRAMS = 8
MAX_LOAD_COST = 1_000_000


def _channel_programs(value):
    items = array(value, 0, MAX_CHANNEL_PROGRAMS)
    mapping = {}
    for item in items:
        fields(item, ('channel', 'program_id'))
        channel = integer(item['channel'], 0, 15)
        program = ident(item['program_id'])
        if channel in mapping:
            raise ValueError('duplicate channel program')
        mapping[channel] = program
    return mapping


def _lanes(value):
    items = array(value, 1, MAX_LANES)
    lanes = {}
    programs = set()
    for item in items:
        fields(item, ('lane', 'program_costs'))
        lane = integer(item['lane'], 0, 15)
        if lane in lanes:
            raise ValueError('duplicate lane')
        costs = {}
        for cost in array(item['program_costs'], 1, MAX_PROGRAM_COSTS):
            fields(cost, ('program_id', 'load_cost'))
            program = ident(cost['program_id'])
            if program in costs:
                raise ValueError('duplicate lane program')
            costs[program] = integer(cost['load_cost'], 1, MAX_LOAD_COST)
            programs.add(program)
        lanes[lane] = costs
    return lanes, programs


def _peaks(program_voices):
    """Per program and pitch, the peak number of simultaneously sounding keys."""
    spans = defaultdict(list)
    for voice, program in program_voices:
        spans[program, voice['pitch']].append(
            (voice['start_tick'], voice['sound_end_tick']))
    peaks = defaultdict(int)
    for (program, _pitch), intervals in spans.items():
        changes = defaultdict(int)
        for start, end in intervals:
            changes[start] += 1
            changes[end] -= 1
        active = peak = 0
        for tick in sorted(changes):
            active += changes[tick]
            if active > peak:
                peak = active
        if peak > peaks[program]:
            peaks[program] = peak
    return peaks


def _minimum_cost(lanes, programs, demand):
    """Cheapest total cost giving every demanded program ``demand`` lanes.

    Programs need exactly their independent minimum lane count: extra lanes
    only add positive cost and never unlock a layout.  A lane serves one
    program for the whole take.
    """
    index = {program: position for position, program in enumerate(programs)}

    @lru_cache(maxsize=None)
    def solve(position, remaining):
        if not any(remaining):
            return 0
        if position == len(lanes):
            return None
        best = solve(position + 1, remaining)
        for program, cost in lanes[position].items():
            slot = index.get(program)
            if slot is None or remaining[slot] == 0:
                continue
            loosened = list(remaining)
            loosened[slot] -= 1
            tail = solve(position + 1, tuple(loosened))
            if tail is not None:
                total = cost + tail
                if best is None or total < best:
                    best = total
        return best

    return solve(0, tuple(demand[program] for program in programs)), solve


def _configuration(lanes, programs, demand):
    """Cheapest configuration, ties broken by the lane program sequence.

    Sequences are compared over lanes in ascending lane number; an unloaded
    lane sorts above every program id, so a loadable program is always tried
    before leaving the lane empty.
    """
    if sum(demand[program] for program in programs) > len(lanes):
        return None, None
    total, solve = _minimum_cost(lanes, programs, demand)
    if total is None:
        return None, None
    index = {program: position for position, program in enumerate(programs)}
    remaining = [demand[program] for program in programs]
    chosen = []
    spent = 0
    for position, costs in enumerate(lanes):
        picked = None
        for program in sorted(costs):
            slot = index.get(program)
            if slot is None or remaining[slot] == 0:
                continue
            loosened = list(remaining)
            loosened[slot] -= 1
            tail = solve(position + 1, tuple(loosened))
            if tail is not None and spent + costs[program] + tail == total:
                picked = program
                remaining = loosened
                spent += costs[program]
                break
        chosen.append(picked)
    return chosen, total


def _arrange(voices, lanes_by_program):
    """Assign voices to lanes in (start tick, voice id) order.

    For each pitch this is the lexicographically smallest proper colouring of
    an interval graph with the lanes loaded for the program.  Processing in
    start order and taking the lowest free lane can never dead-end: every
    blocking lane holds an interval covering the current onset, so if all
    lanes were busy the take would need more lanes than it has.
    """
    busy = {}
    routes = []
    for voice in voices:
        program = voice['program']
        pitch = voice['pitch']
        start, end = voice['start_tick'], voice['sound_end_tick']
        for lane in lanes_by_program[program]:
            key = (lane, pitch)
            if busy.get(key, -1) <= start:
                busy[key] = end
                routes.append({'voice': voice, 'lane': lane})
                break
        else:  # pragma: no cover - guaranteed by the peak lane count
            raise ValueError('lane arrangement failed')
    return routes


def _events(routes):
    """Key-only performance events plus the ids used by each route.

    At one tick every off precedes every on; within a kind the order is lane,
    pitch and source voice id, so a key can restart at the exact stop tick.
    """
    events = []
    output = []
    for number, route in enumerate(routes):
        voice = route['voice']
        lane = route['lane']
        on_id = f'on{number}'
        off_id = f'off{number}'
        output.append({'source_voice_id': voice['voice_id'], 'lane': lane,
                       'on_event_id': on_id, 'off_event_id': off_id})
        events.append((voice['start_tick'], 1, lane, voice['pitch'],
                       voice['voice_id'], {'id': on_id, 'tick': voice['start_tick'],
                                           'channel': lane, 'kind': 'on',
                                           'pitch': voice['pitch'],
                                           'velocity': voice['velocity']}))
        events.append((voice['sound_end_tick'], 0, lane, voice['pitch'],
                       voice['voice_id'], {'id': off_id, 'tick': voice['sound_end_tick'],
                                           'channel': lane, 'kind': 'off',
                                           'pitch': voice['pitch']}))
    events.sort(key=lambda item: item[:5])
    return [item[5] for item in events], output


def export_key_only(manifest):
    """Compile a key-only export manifest into the publishable result."""
    fields(manifest, ('performance', 'channel_programs', 'lanes'))
    performance = manifest['performance']
    if not isinstance(performance, dict):
        raise ValueError('performance object required')
    validate(performance, max_events=MAX_EVENTS, allow_score=False)
    mapping = _channel_programs(manifest['channel_programs'])
    lanes, device_programs = _lanes(manifest['lanes'])
    if len(set(mapping.values()) | device_programs) > MAX_DISTINCT_PROGRAMS:
        raise ValueError('too many distinct programs')

    voices = perform(performance)
    onset_channels = {event['channel'] for event in performance['events']
                      if event['kind'] == 'on'}
    if onset_channels - set(mapping):
        raise ValueError('every onset channel needs a program')

    positive = []
    zero_ids = []
    for voice in voices:
        if voice['start_tick'] == voice['sound_end_tick']:
            zero_ids.append(voice['voice_id'])
            continue
        voice = dict(voice)
        voice['program'] = mapping[voice['channel']]
        positive.append(voice)

    peaks = _peaks([(voice, voice['program']) for voice in positive])
    programs = sorted(peaks)
    demand = {program: peaks[program] for program in programs}
    demands = [{'program_id': program, 'min_lanes': demand[program]}
               for program in programs]
    omitted = sorted(zero_ids)

    lane_numbers = sorted(lanes)
    ordered = [lanes[lane] for lane in lane_numbers]
    chosen, total = _configuration(ordered, programs, demand)
    if chosen is None:
        return {'status': 'no_layout', 'demands': demands,
                'omitted_zero_voice_ids': omitted,
                'configuration': None, 'total_load_cost': None,
                'voice_routes': None, 'performance': None}

    configuration = [{'lane': lane, 'program_id': program}
                     for lane, program in zip(lane_numbers, chosen)]
    lanes_by_program = defaultdict(list)
    for lane, program in zip(lane_numbers, chosen):
        if program is not None:
            lanes_by_program[program].append(lane)

    positive.sort(key=lambda voice: (voice['start_tick'], voice['voice_id']))
    routes = _arrange(positive, lanes_by_program)
    events, voice_routes = _events(routes)
    generated = {'ppq': performance['ppq'], 'end_tick': performance['end_tick'],
                 'tempos': [dict(tempo) for tempo in performance['tempos']],
                 'events': events}
    return {'status': 'ok', 'demands': demands,
            'omitted_zero_voice_ids': omitted,
            'configuration': configuration, 'total_load_cost': total,
            'voice_routes': voice_routes, 'performance': generated}
