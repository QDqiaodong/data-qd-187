"""Offline usage settlement across recording batches.

A settlement run prices one material project.  Each recording's voices are
converted to exact microsecond sound intervals using that recording's own
tempo map; at any instant the distinct owners sharing the sound split the
interval evenly.  Per-recording usage is exact and additive across the
project, so integer cents are produced by a single largest-remainder
distribution over the *whole project*, never by rounding each recording.

Only the difference against previously booked totals is emitted.  The
checkpoint carries derived per-recording usage (never raw events), the booked
cumulative amounts and the accepted batch fingerprints, so the next batch can
continue after the old take files have gone offline.
"""
from fractions import Fraction
from hashlib import sha256
import json

from .checks import array, fields, ident, integer
from .clock import TempoClock
from .model import validate
from .voices import perform

MICROSECONDS_PER_SECOND = 1_000_000
MAX_RECORDINGS = 32
MAX_BATCHES = 256
MAX_CHANGES = 32
MAX_EVENTS = 2000
MAX_CHANNELS = 16
RATE_LIMIT = 1_000_000
SCHEMA_VERSION = 1

_RESULT_FIELDS = ('schema_version', 'status', 'project_id', 'batch_id',
                  'head_batch_id', 'cents_per_second', 'total_cents',
                  'balances', 'adjustments', 'checkpoint')


def _nonneg(value):
    if type(value) is not int or value < 0:
        raise ValueError('non-negative integer required')
    return value


def _exact_positive(value):
    """Parse a canonical positive exact value (integer or ``n/d`` string)."""
    if not isinstance(value, str):
        raise ValueError('exact usage string required')
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise ValueError('invalid exact usage string')
    if fraction <= 0 or str(fraction) != value:
        raise ValueError('exact usage must be positive and in lowest terms')
    return fraction


def _owner_map(value):
    """Validate the channel ownership table; returns channel -> owner_id."""
    owners = array(value, 0, MAX_CHANNELS)
    result = {}
    for item in owners:
        fields(item, ('channel', 'owner_id'))
        channel = integer(item['channel'], 0, 15)
        owner = ident(item['owner_id'])
        if channel in result:
            raise ValueError('duplicate channel ownership')
        result[channel] = owner
    return result


def _usage(intervals):
    """Exact per-owner sounding microseconds for one recording.

    ``intervals`` are ``(start, end, owner)`` exact times.  Only positive
    segments count, and each distinct owner active over a segment receives an
    equal share regardless of how many voices or channels they occupy.
    """
    marks = []
    for start, end, owner in intervals:
        if end <= start:
            continue
        marks.append((start, owner, 1))
        marks.append((end, owner, -1))
    marks.sort(key=lambda mark: mark[0])
    active = {}
    usage = {}
    previous = None
    index = 0
    while index < len(marks):
        time = marks[index][0]
        if previous is not None and time > previous:
            count = len(active)
            if count:
                share = (time - previous) / count
                for owner in active:
                    usage[owner] = usage.get(owner, Fraction(0)) + share
        while index < len(marks) and marks[index][0] == time:
            _, owner, delta = marks[index]
            active[owner] = active.get(owner, 0) + delta
            if active[owner] == 0:
                del active[owner]
            index += 1
        previous = time
    return usage


def _recording_usage(performance, owners):
    """Validate one take and return its exact per-owner sounding usage."""
    if not isinstance(performance, dict):
        raise ValueError('performance object required')
    data = validate(performance, max_events=MAX_EVENTS, allow_score=False)
    voices = perform(data)
    sounding = {event['channel'] for event in data['events'] if event['kind'] == 'on'}
    if sounding - set(owners):
        raise ValueError('every onset channel needs an owner')
    clock = TempoClock(data['ppq'], data['tempos'])
    intervals = [(clock.at(voice['start_tick']), clock.at(voice['sound_end_tick']),
                  owners[voice['channel']]) for voice in voices]
    return _usage(intervals)


def _parse_batch(batch):
    """Structural and causal validation of a whole batch."""
    fields(batch, ('project_id', 'batch_id', 'base_batch_id',
                   'cents_per_second', 'changes'))
    project = ident(batch['project_id'])
    batch_id = ident(batch['batch_id'])
    base = batch['base_batch_id']
    if base is not None:
        ident(base)
    rate = integer(batch['cents_per_second'], 1, RATE_LIMIT)
    raw_changes = array(batch['changes'], 1, MAX_CHANGES)
    changes = []
    seen = set()
    for change in raw_changes:
        if not isinstance(change, dict) or 'op' not in change:
            raise ValueError('change object with op required')
        op = change['op']
        if op == 'upsert':
            fields(change, ('op', 'recording_id', 'revision', 'performance',
                            'channel_owners'))
            recording_id = ident(change['recording_id'])
            revision = integer(change['revision'], 1, 256)
            owners = _owner_map(change['channel_owners'])
            usage = _recording_usage(change['performance'], owners)
            changes.append({
                'op': 'upsert',
                'recording_id': recording_id,
                'revision': revision,
                'performance': change['performance'],
                'channel_owners': sorted(({'channel': channel, 'owner_id': owner}
                                          for channel, owner in owners.items()),
                                         key=lambda row: (row['channel'], row['owner_id'])),
                'usage': usage,
            })
        elif op == 'withdraw':
            fields(change, ('op', 'recording_id', 'revision'))
            recording_id = ident(change['recording_id'])
            revision = integer(change['revision'], 1, 256)
            changes.append({'op': 'withdraw', 'recording_id': recording_id,
                            'revision': revision})
        else:
            raise ValueError('unknown change operation')
        if recording_id in seen:
            raise ValueError('recording repeated within a batch')
        seen.add(recording_id)
    return {'project_id': project, 'batch_id': batch_id, 'base_batch_id': base,
            'cents_per_second': rate, 'changes': changes}


def _fingerprint(batch):
    """Canonical content fingerprint for replay detection.

    Object key order, ``changes`` order and ``channel_owners`` order are
    ignored; performance arrays keep their own ordering (same-tick event order
    is semantic).  The original ``base_batch_id`` is part of the content.
    """
    changes = []
    for change in batch['changes']:
        if change['op'] == 'upsert':
            changes.append({'op': 'upsert', 'recording_id': change['recording_id'],
                            'revision': change['revision'],
                            'performance': change['performance'],
                            'channel_owners': change['channel_owners']})
        else:
            changes.append({'op': 'withdraw', 'recording_id': change['recording_id'],
                            'revision': change['revision']})
    changes.sort(key=lambda change: change['recording_id'])
    canonical = {'project_id': batch['project_id'], 'batch_id': batch['batch_id'],
                 'base_batch_id': batch['base_batch_id'],
                 'cents_per_second': batch['cents_per_second'],
                 'changes': changes}
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    return sha256(payload.encode('utf-8')).hexdigest()


def _billing(total_usage, rate):
    """Whole-project exact amounts to integer cents (largest remainder)."""
    exact = {owner: usage * rate / MICROSECONDS_PER_SECOND
             for owner, usage in total_usage.items() if usage > 0}
    floors = {owner: amount.numerator // amount.denominator
              for owner, amount in exact.items()}
    total_exact = sum(exact.values(), Fraction(0))
    total = total_exact.numerator // total_exact.denominator
    extra = total - sum(floors.values())
    order = sorted(exact, key=lambda owner: (-(exact[owner] - floors[owner]), owner))
    payable = dict(floors)
    for owner in order[:extra]:
        payable[owner] += 1
    rows = [{'owner_id': owner, 'usage_us': str(total_usage[owner]),
             'payable_cents': payable[owner]} for owner in sorted(exact)]
    return rows, total, payable


def _project_usage(recordings):
    total = {}
    for record in recordings.values():
        if not record['active']:
            continue
        for owner, usage in record['usage'].items():
            total[owner] = total.get(owner, Fraction(0)) + usage
    return total


def _checkpoint(project, rate, head, recordings, booked, batches):
    rows = []
    for recording_id in sorted(recordings):
        record = recordings[recording_id]
        usage = []
        if record['active']:
            usage = [{'owner_id': owner, 'usage_us': str(record['usage'][owner])}
                     for owner in sorted(record['usage'])
                     if record['usage'][owner] > 0]
        rows.append({'recording_id': recording_id, 'revision': record['revision'],
                     'active': record['active'], 'usage': usage})
    return {
        'schema_version': SCHEMA_VERSION,
        'project_id': project,
        'cents_per_second': rate,
        'head_batch_id': head,
        'recordings': rows,
        'booked': [{'owner_id': owner, 'cents': booked[owner]}
                   for owner in sorted(booked) if booked[owner] != 0],
        'batches': [{'batch_id': batch['batch_id'],
                     'base_batch_id': batch['base_batch_id'],
                     'content': batch['content']} for batch in batches],
    }


def _validate_adjustments(value):
    if not isinstance(value, list):
        raise ValueError('adjustments array required')
    rows = []
    seen = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError('adjustment object required')
        fields(row, ('owner_id', 'previous_cents', 'payable_cents', 'delta_cents'))
        owner = ident(row['owner_id'])
        previous = _nonneg(row['previous_cents'])
        payable = _nonneg(row['payable_cents'])
        if type(row['delta_cents']) is not int:
            raise ValueError('integer delta required')
        if row['delta_cents'] != payable - previous or row['delta_cents'] == 0:
            raise ValueError('inconsistent adjustment')
        if owner in seen:
            raise ValueError('duplicate adjustment owner')
        seen.add(owner)
        rows.append({'owner_id': owner, 'previous_cents': previous,
                     'payable_cents': payable, 'delta_cents': row['delta_cents']})
    return rows


def _parse_previous(previous):
    """Validate a previous result and rebuild its continuation state."""
    if not isinstance(previous, dict):
        raise ValueError('previous result object required')
    for key in _RESULT_FIELDS:
        if key not in previous:
            raise ValueError(f'missing previous field: {key}')
    integer(previous['schema_version'], 1, 1)
    if previous['status'] not in ('applied', 'replayed'):
        raise ValueError('invalid previous status')
    project = ident(previous['project_id'])
    ident(previous['batch_id'])
    head = previous['head_batch_id']
    if head is not None:
        ident(head)
    rate = integer(previous['cents_per_second'], 1, RATE_LIMIT)
    total_cents = _nonneg(previous['total_cents'])
    adjustments = _validate_adjustments(previous['adjustments'])

    checkpoint = previous['checkpoint']
    if not isinstance(checkpoint, dict):
        raise ValueError('checkpoint object required')
    fields(checkpoint, ('schema_version', 'project_id', 'cents_per_second',
                        'head_batch_id', 'recordings', 'booked', 'batches'))
    integer(checkpoint['schema_version'], 1, 1)
    if ident(checkpoint['project_id']) != project:
        raise ValueError('checkpoint project mismatch')
    if integer(checkpoint['cents_per_second'], 1, RATE_LIMIT) != rate:
        raise ValueError('checkpoint rate mismatch')
    checkpoint_head = checkpoint['head_batch_id']
    if checkpoint_head is not None:
        ident(checkpoint_head)
    if checkpoint_head != head:
        raise ValueError('checkpoint head mismatch')

    recordings = {}
    for record in array(checkpoint['recordings'], 0, MAX_RECORDINGS):
        fields(record, ('recording_id', 'revision', 'active', 'usage'))
        recording_id = ident(record['recording_id'])
        revision = integer(record['revision'], 1, 256)
        if type(record['active']) is not bool:
            raise ValueError('active flag must be boolean')
        usage = {}
        for item in array(record['usage'], 0, MAX_CHANNELS):
            fields(item, ('owner_id', 'usage_us'))
            owner = ident(item['owner_id'])
            if owner in usage:
                raise ValueError('duplicate usage owner')
            usage[owner] = _exact_positive(item['usage_us'])
        if not record['active'] and usage:
            raise ValueError('withdrawn recording must have no usage')
        if recording_id in recordings:
            raise ValueError('duplicate checkpoint recording')
        recordings[recording_id] = {'revision': revision,
                                    'active': record['active'], 'usage': usage}

    booked = {}
    for item in array(checkpoint['booked'], 0, MAX_RECORDINGS * MAX_CHANNELS):
        fields(item, ('owner_id', 'cents'))
        owner = ident(item['owner_id'])
        if owner in booked:
            raise ValueError('duplicate booked owner')
        booked[owner] = _nonneg(item['cents'])

    batches = []
    seen_batches = set()
    for item in array(checkpoint['batches'], 0, MAX_BATCHES):
        fields(item, ('batch_id', 'base_batch_id', 'content'))
        batch_id = ident(item['batch_id'])
        base = item['base_batch_id']
        if base is not None:
            ident(base)
        content = item['content']
        if (not isinstance(content, str) or len(content) != 64
                or any(character not in '0123456789abcdef' for character in content)):
            raise ValueError('invalid batch fingerprint')
        if batch_id in seen_batches:
            raise ValueError('duplicate accepted batch')
        seen_batches.add(batch_id)
        batches.append({'batch_id': batch_id, 'base_batch_id': base, 'content': content})

    if batches:
        if head != batches[-1]['batch_id']:
            raise ValueError('head does not match the latest accepted batch')
    elif head is not None:
        raise ValueError('head set without accepted batches')
    if previous['batch_id'] not in seen_batches:
        raise ValueError('requested batch is not in the accepted archive')

    rows, total, payable = _billing(_project_usage(recordings), rate)
    if rows != previous['balances']:
        raise ValueError('previous balances do not match checkpoint')
    if total != total_cents:
        raise ValueError('previous total does not match checkpoint')
    expected_booked = {owner: cents for owner, cents in payable.items() if cents}
    if booked != expected_booked:
        raise ValueError('previous booked totals do not match checkpoint')
    # Every recorded adjustment must agree with the recomputed payable target,
    # so a tampered delta/payable pair cannot be used to continue.
    for row in adjustments:
        if expected_booked.get(row['owner_id'], 0) != row['payable_cents']:
            raise ValueError('previous adjustments do not match checkpoint')

    return {'project_id': project, 'cents_per_second': rate, 'head_batch_id': head,
            'recordings': recordings, 'booked': booked, 'batches': batches}


def _replayed(batch, state):
    rows, total, _ = _billing(_project_usage(state['recordings']),
                              state['cents_per_second'])
    return {
        'schema_version': SCHEMA_VERSION,
        'status': 'replayed',
        'project_id': state['project_id'],
        'batch_id': batch['batch_id'],
        'head_batch_id': state['head_batch_id'],
        'cents_per_second': state['cents_per_second'],
        'total_cents': total,
        'balances': rows,
        'adjustments': [],
        'checkpoint': _checkpoint(state['project_id'], state['cents_per_second'],
                                  state['head_batch_id'], state['recordings'],
                                  state['booked'], state['batches']),
    }


def settle(batch, previous=None):
    """Settle one batch against an optional previous result."""
    parsed = _parse_batch(batch)
    fingerprint = _fingerprint(parsed)
    if previous is None:
        state = {'project_id': parsed['project_id'],
                 'cents_per_second': parsed['cents_per_second'],
                 'head_batch_id': None, 'recordings': {}, 'booked': {},
                 'batches': []}
    else:
        state = _parse_previous(previous)
        if parsed['project_id'] != state['project_id']:
            raise ValueError('project mismatch')
        if parsed['cents_per_second'] != state['cents_per_second']:
            raise ValueError('cents_per_second differs from the fixed project rate')

    archive = {record['batch_id']: record for record in state['batches']}
    if parsed['batch_id'] in archive:
        if archive[parsed['batch_id']]['content'] != fingerprint:
            raise ValueError('batch id reused with different content')
        return _replayed(parsed, state)

    if previous is None:
        if parsed['base_batch_id'] is not None:
            raise ValueError('the first batch must have a null base_batch_id')
    elif parsed['base_batch_id'] != state['head_batch_id']:
        raise ValueError('stale base_batch_id')

    if len(state['batches']) >= MAX_BATCHES:
        raise ValueError('project already accepted the maximum number of batches')

    recordings = {recording_id: {'revision': record['revision'],
                                 'active': record['active'],
                                 'usage': dict(record['usage'])}
                  for recording_id, record in state['recordings'].items()}
    for change in parsed['changes']:
        recording_id = change['recording_id']
        revision = change['revision']
        record = recordings.get(recording_id)
        if record is None:
            if change['op'] != 'upsert' or revision != 1:
                raise ValueError('a new recording must be upserted at revision 1')
            recordings[recording_id] = {'revision': 1, 'active': True,
                                        'usage': change['usage']}
            continue
        if revision != record['revision'] + 1:
            raise ValueError('recording revision must increase by exactly one')
        if change['op'] == 'withdraw':
            if not record['active']:
                raise ValueError('only an active recording may be withdrawn')
            record['active'] = False
            record['usage'] = {}
        else:
            record['active'] = True
            record['usage'] = change['usage']
        record['revision'] = revision
    if len(recordings) > MAX_RECORDINGS:
        raise ValueError('too many distinct recordings registered')

    rows, total, payable = _billing(_project_usage(recordings),
                                    state['cents_per_second'])
    adjustments = []
    for owner in sorted(set(state['booked']) | set(payable)):
        previous_cents = state['booked'].get(owner, 0)
        payable_cents = payable.get(owner, 0)
        if payable_cents != previous_cents:
            adjustments.append({'owner_id': owner,
                                'previous_cents': previous_cents,
                                'payable_cents': payable_cents,
                                'delta_cents': payable_cents - previous_cents})
    booked = {owner: cents for owner, cents in payable.items() if cents}
    batches = state['batches'] + [{'batch_id': parsed['batch_id'],
                                   'base_batch_id': parsed['base_batch_id'],
                                   'content': fingerprint}]
    head = parsed['batch_id']
    return {
        'schema_version': SCHEMA_VERSION,
        'status': 'applied',
        'project_id': state['project_id'],
        'batch_id': parsed['batch_id'],
        'head_batch_id': head,
        'cents_per_second': state['cents_per_second'],
        'total_cents': total,
        'balances': rows,
        'adjustments': adjustments,
        'checkpoint': _checkpoint(state['project_id'], state['cents_per_second'],
                                  head, recordings, booked, batches),
    }
