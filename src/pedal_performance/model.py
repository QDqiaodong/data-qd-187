from .checks import array, fields, ident, integer, unique

# Tolerances are exact non-negative microsecond counts.  The upper bound keeps
# every accepted value inside a clearly documented, bounded integer range.
TOLERANCE_LIMIT = 10 ** 12


def _tempos(value, end):
    """Validate a tempo map that spans ticks [0, end]."""
    tempos = array(value, 1, 1000)
    previous = -1
    for tempo in tempos:
        fields(tempo, ('tick', 'microseconds_per_quarter'))
        tick = integer(tempo['tick'], 0, end)
        integer(tempo['microseconds_per_quarter'], 1, 60000000)
        if tick <= previous:
            raise ValueError('tempo ticks must strictly increase')
        previous = tick
    if tempos[0]['tick'] != 0:
        raise ValueError('tempo map must begin at tick zero')
    return tempos


def _events(value, end, max_events=10000):
    events = array(value, 0, max_events)
    previous = -1
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('event object required')
        kind = event.get('kind')
        required = {'on': ('pitch', 'velocity'), 'off': ('pitch',), 'pedal': ('down',)}
        if not isinstance(kind, str) or kind not in required:
            raise ValueError('unknown performance event')
        fields(event, ('id', 'tick', 'channel', 'kind') + required[kind])
        ident(event['id'])
        tick = integer(event['tick'], 0, end)
        if tick < previous:
            raise ValueError('events must be in nondecreasing tick order')
        previous = tick
        integer(event['channel'], 0, 15)
        if kind in ('on', 'off'):
            integer(event['pitch'], 0, 127)
        if kind == 'on':
            integer(event['velocity'], 1, 127)
        if kind == 'pedal' and type(event['down']) is not bool:
            raise ValueError('pedal down must be boolean')
    unique([e['id'] for e in events])
    return events


def _score(score):
    if not isinstance(score, dict):
        raise ValueError('score object required')
    fields(score, ('ppq', 'end_tick', 'tempos', 'cues'))
    integer(score['ppq'], 1, 9600)
    end = integer(score['end_tick'], 1, 10000000)
    _tempos(score['tempos'], end)
    cues = array(score['cues'], 0, 10000)
    previous = -1
    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError('cue object required')
        fields(cue, ('cue_id', 'channel', 'pitch', 'start_tick', 'release_tick',
                     'onset_tolerance_us', 'duration_tolerance_us'))
        ident(cue['cue_id'])
        integer(cue['channel'], 0, 15)
        integer(cue['pitch'], 0, 127)
        start = integer(cue['start_tick'], 0, end)
        integer(cue['release_tick'], 0, end)
        if start < previous:
            raise ValueError('cues must be in nondecreasing start_tick order')
        previous = start
        integer(cue['onset_tolerance_us'], 0, TOLERANCE_LIMIT)
        integer(cue['duration_tolerance_us'], 0, TOLERANCE_LIMIT)
    unique([cue['cue_id'] for cue in cues])
    return score


def validate(data, max_events=10000, allow_score=True):
    fields(data, ('ppq', 'end_tick', 'tempos', 'events'),
           optional=('score',) if allow_score else ())
    integer(data['ppq'], 1, 9600)
    end = integer(data['end_tick'], 1, 10000000)
    _tempos(data['tempos'], end)
    _events(data['events'], end, max_events)
    if allow_score and 'score' in data:
        _score(data['score'])
    return data
