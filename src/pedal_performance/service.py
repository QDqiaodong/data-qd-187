from .clock import TempoClock
from .evaluate import exercise
from .model import validate
from .voices import peak_polyphony, perform

def process(source):
    data = validate(source)
    clock = TempoClock(data['ppq'], data['tempos'])
    voices = perform(data)
    for voice in voices:
        for key in ('start', 'key_release', 'sound_end'):
            tick = voice[f'{key}_tick']
            voice[f'{key}_us'] = str(clock.at(tick)) if tick is not None else None
        voice['sound_duration_us'] = str(clock.at(voice['sound_end_tick']) - clock.at(voice['start_tick']))
    unfinished = [v['voice_id'] for v in voices if v['key_release_tick'] is None]
    summary = {'voices': len(voices), 'pedal_extended_voices': sum(v['key_release_tick'] is not None and v['sound_end_tick'] > v['key_release_tick'] for v in voices),
               'unfinished_keys': len(unfinished), 'peak_polyphony': peak_polyphony(voices)}
    result = {'schema_version': 1, 'duration_us': str(clock.at(data['end_tick'])), 'voices': voices, 'unfinished_key_ids': unfinished,
              'summary': summary}
    score_evaluation, additions = exercise(data, voices)
    if score_evaluation is not None:
        result['score_evaluation'] = score_evaluation
        summary.update(additions)
    return result
