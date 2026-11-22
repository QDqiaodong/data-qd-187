"""Capture-order auditing.

The recorder occasionally writes two adjacent messages of one tick in the
wrong order and marks the suspect pair on export.  An *interpretation* picks,
for every marked pair, whether to keep or swap it; replaying an interpretation
must satisfy the same FIFO, pedal and note-off causality rules as a normal
performance, while also matching every held/sustained checkpoint.

Only interpretations that replay the whole capture and satisfy every relevant
checkpoint may testify.  Each channel is audited independently from its own
events and observations, so one inconsistent channel never erases the evidence
collected for the others.
"""
from collections import defaultdict, deque

from .checks import array, fields, integer
from .model import validate
from .service import process

MAX_AUDIT_EVENTS = 2000
MAX_CHECKPOINTS = 2000
MAX_PAIRS_PER_CHANNEL = 6
COUNT_LIMIT = 2000


def validate_manifest(data):
    """Validate a capture-audit manifest; any defect rejects the input (exit 2)."""
    fields(data, ('performance', 'races', 'checkpoints'))
    performance = data['performance']
    if not isinstance(performance, dict):
        raise ValueError('performance object required')
    # The single-take four fields, no score; the tighter event cap is the only
    # structural change.  Causal errors (unmatched note offs) are left to the
    # replay stage, never used to reject the manifest up front.
    validate(performance, max_events=MAX_AUDIT_EVENTS, allow_score=False)
    events = performance['events']
    end = performance['end_tick']

    position = {event['id']: index for index, event in enumerate(events)}
    races = array(data['races'], 0, 16 * MAX_PAIRS_PER_CHANNEL)
    used = set()
    previous_first = -1
    pairs_per_channel = defaultdict(int)
    resolved = []
    for pair in races:
        if not isinstance(pair, list) or len(pair) != 2 \
                or not all(isinstance(event_id, str) for event_id in pair):
            raise ValueError('race must be two event ids')
        if pair[0] not in position or pair[1] not in position:
            raise ValueError('race references unknown event id')
        first, second = position[pair[0]], position[pair[1]]
        if second != first + 1:
            raise ValueError('race events must be adjacent in the event array')
        left, right = events[first], events[second]
        if left['tick'] != right['tick'] or left['channel'] != right['channel']:
            raise ValueError('race events must share tick and channel')
        if first in used or second in used:
            raise ValueError('an event may belong to at most one race')
        if first <= previous_first:
            raise ValueError('races must be ordered by their first event position')
        previous_first = first
        used.add(first)
        used.add(second)
        pairs_per_channel[left['channel']] += 1
        if pairs_per_channel[left['channel']] > MAX_PAIRS_PER_CHANNEL:
            raise ValueError('at most six races per channel')
        resolved.append((first, second, left['channel']))

    checkpoints = array(data['checkpoints'], 0, MAX_CHECKPOINTS)
    seen = set()
    for checkpoint in checkpoints:
        fields(checkpoint, ('tick', 'channel', 'held', 'sustained'))
        tick = integer(checkpoint['tick'], 0, end)
        channel = integer(checkpoint['channel'], 0, 15)
        integer(checkpoint['held'], 0, COUNT_LIMIT)
        integer(checkpoint['sustained'], 0, COUNT_LIMIT)
        if (tick, channel) in seen:
            raise ValueError('duplicate checkpoint for tick and channel')
        seen.add((tick, channel))
    return data, resolved


def _replay(ordered_events, checkpoints, end_tick):
    """Replay one channel under one fixed event order.

    Returns the voice records in onset order, or ``None`` when an off has no
    matching held voice or a checkpoint observation is contradicted.
    Checkpoints are sampled after every event of their tick has been handled
    and before the end-of-take force stop; ticks without events are observed
    too.
    """
    held = defaultdict(deque)
    deferred = []
    pedal_down = False
    voices = []
    cursor = 0

    def observe(tick):
        nonlocal cursor
        held_count = sum(len(queue) for queue in held.values())
        sustained_count = len(deferred)
        while cursor < len(checkpoints) and checkpoints[cursor]['tick'] == tick:
            checkpoint = checkpoints[cursor]
            if checkpoint['held'] != held_count or checkpoint['sustained'] != sustained_count:
                return False
            cursor += 1
        return True

    index = 0
    while index < len(ordered_events):
        tick = ordered_events[index]['tick']
        # Checkpoints on event-free ticks preceding this tick see an unchanged
        # state, so each one is verified against the state as it stands.
        while cursor < len(checkpoints) and checkpoints[cursor]['tick'] < tick:
            if not observe(checkpoints[cursor]['tick']):
                return None
        while index < len(ordered_events) and ordered_events[index]['tick'] == tick:
            event = ordered_events[index]
            kind = event['kind']
            if kind == 'on':
                voice = {'voice_id': event['id'], 'start_tick': tick,
                         'key_release_tick': None, 'release_event_id': None,
                         'sound_end_tick': None, 'end_reason': None,
                         'end_event_id': None}
                voices.append(voice)
                held[event['pitch']].append(voice)
            elif kind == 'off':
                queue = held[event['pitch']]
                if not queue:
                    return None
                voice = queue.popleft()
                voice['key_release_tick'] = tick
                voice['release_event_id'] = event['id']
                if pedal_down:
                    deferred.append(voice)
                else:
                    voice.update(sound_end_tick=tick, end_reason='key_release',
                                 end_event_id=event['id'])
            else:
                pedal_down = event['down']
                if not pedal_down:
                    for voice in deferred:
                        voice.update(sound_end_tick=tick, end_reason='pedal_release',
                                     end_event_id=event['id'])
                    deferred.clear()
            index += 1
        if not observe(tick):
            return None

    while cursor < len(checkpoints):
        if not observe(checkpoints[cursor]['tick']):
            return None
    for voice in voices:
        if voice['sound_end_tick'] is None:
            voice.update(sound_end_tick=end_tick, end_reason='end_of_take',
                         end_event_id=None)
    return voices


EVIDENCE_KEYS = ('key_release_tick', 'release_event_id', 'sound_end_tick',
                 'end_reason', 'end_event_id')


def _audit_channel(channel, channel_events, local_pairs, checkpoints, end_tick):
    """Enumerate every keep/swap choice for one channel and collect evidence."""
    checkpoints = sorted(checkpoints, key=lambda item: item['tick'])
    # Local pair positions stay adjacent within the channel's own event list
    # because every pair consists of two adjacent, same-channel events.
    local_indices = {event['id']: index for index, event in enumerate(channel_events)}
    local_bounds = [(local_indices[first_id], local_indices[second_id], global_index)
                    for global_index, (first_id, second_id) in local_pairs]
    pair_count = len(local_bounds)
    evidence = defaultdict(dict)
    feasible_masks = []
    for mask in range(1 << pair_count):
        ordered = list(channel_events)
        for local_index, (first, second, _) in enumerate(local_bounds):
            if (mask >> local_index) & 1:
                ordered[first], ordered[second] = ordered[second], ordered[first]
        voices = _replay(ordered, checkpoints, end_tick)
        if voices is None:
            continue
        feasible_masks.append(mask)
        for voice in voices:
            key = tuple(voice[field] for field in EVIDENCE_KEYS)
            # Masks are enumerated in ascending order, so the first replay
            # producing a five-tuple is its keep-earliest witness.
            evidence[voice['voice_id']].setdefault(key, mask)
    if not feasible_masks:
        return {'channel': channel, 'feasible': False, 'voices': []}, None

    def flips_for(mask):
        return [global_index for local_index, (_, _, global_index)
                in enumerate(local_bounds) if (mask >> local_index) & 1]

    rows = []
    for voice_id in sorted(evidence):
        alternatives = []
        for key, witness in sorted(evidence[voice_id].items(),
                                   key=lambda item: item[1]):
            alternative = {field: value for field, value in zip(EVIDENCE_KEYS, key)}
            alternative['flips'] = flips_for(witness)
            alternatives.append(alternative)
        rows.append({'voice_id': voice_id, 'alternatives': alternatives})
    entry = {'channel': channel, 'feasible': True, 'voices': rows}
    minimum = min(feasible_masks)
    return entry, flips_for(minimum)


def audit_capture(data):
    """Compile the audit result document for a validated capture manifest."""
    data, resolved = validate_manifest(data)
    performance = data['performance']
    events = performance['events']
    end_tick = performance['end_tick']

    events_by_channel = defaultdict(list)
    for event in events:
        events_by_channel[event['channel']].append(event)
    pairs_by_channel = defaultdict(list)
    for global_index, (first, second, channel) in enumerate(resolved):
        pairs_by_channel[channel].append((global_index,
                                          (events[first]['id'], events[second]['id'])))
    checkpoints_by_channel = defaultdict(list)
    for checkpoint in data['checkpoints']:
        checkpoints_by_channel[checkpoint['channel']].append(checkpoint)

    channels = []
    minimal_flips = set()
    ambiguous = False
    for channel in sorted(set(events_by_channel) | set(checkpoints_by_channel)):
        entry, flips = _audit_channel(
            channel, events_by_channel.get(channel, []),
            pairs_by_channel.get(channel, []),
            checkpoints_by_channel.get(channel, []), end_tick)
        channels.append(entry)
        if not entry['feasible']:
            continue
        minimal_flips.update(flips)
        if any(len(row['alternatives']) > 1 for row in entry['voices']):
            ambiguous = True

    if any(not entry['feasible'] for entry in channels):
        status = 'inconsistent'
    elif ambiguous:
        status = 'ambiguous'
    else:
        status = 'certified'
    if status != 'certified':
        return {'status': status, 'channels': channels,
                'performance': None, 'report': None}

    # The componentwise smallest feasible choice per channel is the smallest
    # full selection over all pair numbers; pairs are disjoint, so swaps apply
    # independently to the original event array.
    reordered = list(events)
    for global_index in minimal_flips:
        first, second, _ = resolved[global_index]
        reordered[first], reordered[second] = reordered[second], reordered[first]
    certified_performance = {
        'ppq': performance['ppq'],
        'end_tick': performance['end_tick'],
        'tempos': performance['tempos'],
        'events': reordered,
    }
    report = process(certified_performance)
    return {'status': status, 'channels': channels,
            'performance': certified_performance, 'report': report}
