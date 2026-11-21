"""Score-cue evaluation.

Attribution, deviation sums and tolerance decisions use only exact integer
arithmetic.  Every score/performance time is converted to an integer multiple
of ``1/L`` microseconds, where ``L`` is the least common multiple of the two
``ppq`` values, so no float accumulation or rounding can affect matching or
pass/fail status.
"""
from collections import defaultdict
from fractions import Fraction
from math import lcm

from .clock import TempoClock


def _scaled(clock, scale, tick):
    value = clock.at(tick) * scale
    if value.denominator != 1:
        raise ValueError('non-integral scaled time')
    return value.numerator


def _match_more_voices(cue_ids, voice_ids, cue_on, cue_dur, voice_on, voice_dur):
    """Match every cue, choosing a subset of the (more numerous) voices.

    Order-preserving matching with the maximum pair count (always ``len(cues)``
    because a monotone injection exists).  Costs are compared lexicographically
    as ``(onset deviation sum, observable duration deviation sum)``, then the
    ``(cue_id, voice_id)`` pair sequence in cue order is made lexicographically
    smallest.  Cue order is fixed, so at each cue the first differing pair is
    decided by the chosen voice identity.

    The DP row is indexed by ``k``, the number of voices skipped so far, so
    every row holds ``m - n + 1`` states and no cost matrix is materialised.
    """
    n, m = len(cue_ids), len(voice_ids)
    delta = m - n
    width = delta + 1
    # Base row i = n: every cue matched, cost zero.
    next_cost = [0] * width       # onset sum
    next_dur = [0] * width        # duration sum
    choice = [0] * n
    abs_ = abs
    for i in range(n - 1, -1, -1):
        c_on = cue_on[i]
        c_dur = cue_dur[i]
        row_cost = [0] * width
        row_dur = [0] * width
        row_first = [0] * width
        bits = 0
        for k in range(delta, -1, -1):
            j = i + k
            cost = abs_(voice_on[j] - c_on)
            dur = 0 if voice_dur[j] is None else abs_(voice_dur[j] - c_dur)
            mc = cost + next_cost[k]
            md = dur + next_dur[k]
            if k < delta:
                sc = row_cost[k + 1]
                sd = row_dur[k + 1]
                if sc < mc or (sc == mc and sd < md):
                    row_cost[k] = sc
                    row_dur[k] = sd
                    row_first[k] = row_first[k + 1]
                    continue
                if sc == mc and sd == md and voice_ids[row_first[k + 1]] < voice_ids[j]:
                    row_cost[k] = sc
                    row_dur[k] = sd
                    row_first[k] = row_first[k + 1]
                    continue
            row_cost[k] = mc
            row_dur[k] = md
            row_first[k] = j
            bits |= 1 << k
        choice[i] = bits
        next_cost, next_dur = row_cost, row_dur
    pairs = []
    i = k = 0
    while i < n:
        if (choice[i] >> k) & 1:
            pairs.append((i, i + k))
            i += 1
        else:
            k += 1
    return pairs


def _match_more_cues(cue_ids, voice_ids, cue_on, cue_dur, voice_on, voice_dur):
    """Match every voice, leaving some of the (more numerous) cues unmatched.

    Order-preserving matching with the maximum pair count (always
    ``len(voices)``).  Same lexicographic objectives as
    :func:`_match_more_voices`; here the voice is fixed at each step, so at
    each matched cue the first differing pair is decided by the cue identity.

    Only reachable states ``j in [max(0, i - delta), min(i, m)]`` are stored.
    """
    n, m = len(cue_ids), len(voice_ids)
    delta = n - m
    # Base row i = n: all voices matched, which requires j == m.
    next_cost = [None] * (m + 1)
    next_dur = [None] * (m + 1)
    next_first = [None] * (m + 1)
    next_cost[m] = 0
    next_dur[m] = 0
    next_first[m] = n
    choice = [0] * n
    abs_ = abs
    for i in range(n - 1, -1, -1):
        c_on = cue_on[i]
        c_dur = cue_dur[i]
        row_cost = [None] * (m + 1)
        row_dur = [None] * (m + 1)
        row_first = [None] * (m + 1)
        bits = 0
        low, high = max(0, i - delta), min(m, i)
        for j in range(high, low - 1, -1):
            sc = next_cost[j]
            sd = next_dur[j]
            mc = md = None
            tail = next_cost[j + 1] if j < m else None
            if tail is not None:
                cost = abs_(voice_on[j] - c_on)
                dur = 0 if voice_dur[j] is None else abs_(voice_dur[j] - c_dur)
                mc = cost + tail
                md = dur + next_dur[j + 1]
            if sc is None:
                take_skip = False
            elif mc is None:
                take_skip = True
            elif sc < mc or (sc == mc and sd < md):
                take_skip = True
            elif sc == mc and sd == md and cue_ids[next_first[j]] < cue_ids[i]:
                take_skip = True
            else:
                take_skip = False
            if take_skip:
                row_cost[j] = sc
                row_dur[j] = sd
                row_first[j] = next_first[j]
            else:
                row_cost[j] = mc
                row_dur[j] = md
                row_first[j] = i
                bits |= 1 << j
        choice[i] = bits
        next_cost, next_dur, next_first = row_cost, row_dur, row_first
    pairs = []
    i = j = 0
    while j < m:
        if (choice[i] >> j) & 1:
            pairs.append((i, j))
            i += 1
            j += 1
        else:
            i += 1
    return pairs


def _match(cue_ids, voice_ids, cue_on, cue_dur, voice_on, voice_dur):
    n, m = len(cue_ids), len(voice_ids)
    if n == 0 or m == 0:
        return []
    if n == m:
        # The only order-preserving bijection of equal-length sequences is the
        # identity, so it uniquely has maximal pair count.
        return [(i, i) for i in range(n)]
    if n > m:
        return _match_more_cues(cue_ids, voice_ids, cue_on, cue_dur, voice_on, voice_dur)
    return _match_more_voices(cue_ids, voice_ids, cue_on, cue_dur, voice_on, voice_dur)


def exercise(data, voices):
    """Evaluate performance voices against ``data['score']``.

    Returns ``(evaluation, summary_additions)``.  ``evaluation`` is ``None``
    when no score is present, leaving the legacy report untouched.
    """
    score = data.get('score')
    if score is None:
        return None, {}

    performance_clock = TempoClock(data['ppq'], data['tempos'])
    score_clock = TempoClock(score['ppq'], score['tempos'])
    scale = lcm(data['ppq'], score['ppq'])

    cues = score['cues']
    cue_onset = [_scaled(score_clock, scale, cue['start_tick']) for cue in cues]
    cue_release = [_scaled(score_clock, scale, cue['release_tick']) for cue in cues]
    voice_onset = [_scaled(performance_clock, scale, voice['start_tick']) for voice in voices]
    voice_release = [None if voice['key_release_tick'] is None
                     else _scaled(performance_clock, scale, voice['key_release_tick'])
                     for voice in voices]

    cue_by_place = defaultdict(list)
    for index, cue in enumerate(cues):
        cue_by_place[cue['channel'], cue['pitch']].append(index)
    voice_by_place = defaultdict(list)
    for index, voice in enumerate(voices):
        voice_by_place[voice['channel'], voice['pitch']].append(index)

    assigned = [None] * len(cues)
    matched_voice = [False] * len(voices)
    for place in sorted(set(cue_by_place) | set(voice_by_place)):
        cue_indices = cue_by_place.get(place, [])
        voice_indices = voice_by_place.get(place, [])
        if len(cue_indices) == len(voice_indices):
            # The only order-preserving bijection of equal-length sequences is
            # the identity, so it uniquely has maximal pair count.
            pairs = [(i, i) for i in range(len(cue_indices))]
        else:
            cue_ids = [cues[i]['cue_id'] for i in cue_indices]
            voice_ids = [voices[i]['voice_id'] for i in voice_indices]
            pairs = _match(cue_ids, voice_ids,
                           [cue_onset[i] for i in cue_indices],
                           [cue_release[i] - cue_onset[i] for i in cue_indices],
                           [voice_onset[i] for i in voice_indices],
                           [None if voice_release[i] is None else voice_release[i] - voice_onset[i]
                            for i in voice_indices])
        for local_cue, local_voice in pairs:
            cue_index = cue_indices[local_cue]
            voice_index = voice_indices[local_voice]
            assigned[cue_index] = voice_index
            matched_voice[voice_index] = True

    cue_results = []
    onset_ok = duration_ok = duration_undetermined = missed = 0
    for index, cue in enumerate(cues):
        voice_index = assigned[index]
        if voice_index is None:
            missed += 1
            cue_results.append({
                'cue_id': cue['cue_id'],
                'channel': cue['channel'],
                'pitch': cue['pitch'],
                'voice_id': None,
                'onset_deviation_us': None,
                'onset_status': 'missed',
                'duration_deviation_us': None,
                'duration_status': 'not_applicable',
            })
            continue
        voice = voices[voice_index]
        deviation = voice_onset[voice_index] - cue_onset[index]
        if abs(deviation) <= cue['onset_tolerance_us'] * scale:
            onset_status = 'ok'
            onset_ok += 1
        elif deviation < 0:
            onset_status = 'early'
        else:
            onset_status = 'late'
        if voice['key_release_tick'] is None:
            # Held at end_tick: the key duration is genuinely unknown.  Keep
            # the onset result and do not fabricate a release.
            duration_deviation = None
            duration_status = 'undetermined'
            duration_undetermined += 1
        else:
            duration_deviation = (voice_release[voice_index] - voice_onset[voice_index]) - (
                cue_release[index] - cue_onset[index])
            if abs(duration_deviation) <= cue['duration_tolerance_us'] * scale:
                duration_status = 'ok'
                duration_ok += 1
            elif duration_deviation < 0:
                duration_status = 'early'
            else:
                duration_status = 'late'
        cue_results.append({
            'cue_id': cue['cue_id'],
            'channel': cue['channel'],
            'pitch': cue['pitch'],
            'voice_id': voice['voice_id'],
            'onset_deviation_us': str(Fraction(deviation, scale)),
            'onset_status': onset_status,
            'duration_deviation_us': None if duration_deviation is None else str(Fraction(duration_deviation, scale)),
            'duration_status': duration_status,
        })

    extra_voice_ids = [voice['voice_id'] for index, voice in enumerate(voices) if not matched_voice[index]]
    evaluation = {
        'cues': cue_results,
        'missed_cue_ids': [cues[i]['cue_id'] for i, voice_index in enumerate(assigned) if voice_index is None],
        'extra_voice_ids': extra_voice_ids,
    }
    additions = {
        'matched_cues': len(cues) - missed,
        'missed_cues': missed,
        'extra_voices': len(extra_voice_ids),
        'onset_ok': onset_ok,
        'duration_ok': duration_ok,
        'duration_undetermined': duration_undetermined,
    }
    return evaluation, additions
