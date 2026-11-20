from collections import defaultdict, deque

def perform(data):
    held = defaultdict(deque)
    deferred = defaultdict(list)
    pedal = defaultdict(bool)
    voices = []
    def stop(voice, tick, reason, event_id):
        voice.update(sound_end_tick=tick, end_reason=reason, end_event_id=event_id)
    for event in data['events']:
        channel, tick = event['channel'], event['tick']
        if event['kind'] == 'on':
            voice = {'voice_id': event['id'], 'channel': channel, 'pitch': event['pitch'], 'velocity': event['velocity'], 'start_tick': tick,
                     'key_release_tick': None, 'release_event_id': None}
            voices.append(voice)
            held[channel, event['pitch']].append(voice)
        elif event['kind'] == 'off':
            queue = held[channel, event['pitch']]
            if not queue:
                raise ValueError(f'unmatched note off {event["id"]}')
            voice = queue.popleft()
            voice.update(key_release_tick=tick, release_event_id=event['id'])
            if pedal[channel]:
                deferred[channel].append(voice)
            else:
                stop(voice, tick, 'key_release', event['id'])
        else:
            pedal[channel] = event['down']
            if not event['down']:
                for voice in deferred[channel]:
                    stop(voice, tick, 'pedal_release', event['id'])
                deferred[channel].clear()
    for voice in voices:
        if 'sound_end_tick' not in voice:
            stop(voice, data['end_tick'], 'end_of_take', None)
    return voices

def peak_polyphony(voices):
    changes = defaultdict(int)
    for voice in voices:
        if voice['start_tick'] < voice['sound_end_tick']:
            changes[voice['start_tick']] += 1
            changes[voice['sound_end_tick']] -= 1
    active = peak = 0
    for tick in sorted(changes):
        active += changes[tick]
        peak = max(peak, active)
    return peak
