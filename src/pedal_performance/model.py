from .checks import array, fields, ident, integer, unique

def validate(data):
    fields(data, ('ppq', 'end_tick', 'tempos', 'events'))
    integer(data['ppq'], 1, 9600)
    end = integer(data['end_tick'], 1, 10000000)
    tempos = array(data['tempos'], 1, 1000)
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
    previous = -1
    events = array(data['events'], 0, 10000)
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
    return data
