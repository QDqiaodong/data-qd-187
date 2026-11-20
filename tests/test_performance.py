import json
from pathlib import Path
import unittest
from pedal_performance.service import process

def sample():
    return json.loads((Path(__file__).resolve().parents[1] / 'fixtures/input.json').read_text())

def on(id,tick,pitch=60,channel=0): return dict(id=id,tick=tick,channel=channel,kind='on',pitch=pitch,velocity=80)
def off(id,tick,pitch=60,channel=0): return dict(id=id,tick=tick,channel=channel,kind='off',pitch=pitch)
def pedal(id,tick,down,channel=0): return dict(id=id,tick=tick,channel=channel,kind='pedal',down=down)

class PerformanceTests(unittest.TestCase):
    def test_tempo_and_pedal_share_voice_timing(self):
        r=process(sample())
        self.assertEqual(r['summary'],dict(voices=3,pedal_extended_voices=2,unfinished_keys=1,peak_polyphony=2))
        self.assertEqual(r['duration_us'],'2500000')
        a,b,c=r['voices']
        self.assertEqual((a['key_release_us'],a['sound_end_us']),('250000','1500000'))
        self.assertEqual((b['start_us'],b['sound_duration_us']),('500000','1000000'))
        self.assertEqual(c['end_reason'],'end_of_take')

    def test_repeated_pitch_fifo_not_latest(self):
        d=sample();d['events']=[on('a',0),on('b',10),off('x',20),off('y',30)]
        r=process(d)
        self.assertEqual([v['key_release_tick'] for v in r['voices']],[20,30])

    def test_channel_pedal_does_not_release_held_keys(self):
        d=sample();d['events']=[pedal('p',0,True),on('a',1),on('b',1,channel=1),off('b_off',5,channel=1),pedal('up',10,False),off('a_off',20)]
        r=process(d)
        self.assertEqual([v['sound_end_tick'] for v in r['voices']],[20,5])

    def test_same_tick_input_order_matters(self):
        d=sample();d['events']=[on('a',0),pedal('p',10,True),off('x',10),pedal('up',20,False)]
        self.assertEqual(process(d)['voices'][0]['sound_end_tick'],20)
        d['events'][1:3]=d['events'][1:3][::-1]
        self.assertEqual(process(d)['voices'][0]['sound_end_tick'],10)

    def test_exact_submicrosecond_and_zero_duration(self):
        d=sample();d.update(ppq=3,end_tick=3,tempos=[dict(tick=0,microseconds_per_quarter=1)],events=[on('a',1),off('x',2),on('b',3),off('y',3)])
        r=process(d)
        self.assertEqual(r['voices'][0]['sound_duration_us'],'1/3')
        self.assertEqual(r['summary']['peak_polyphony'],1)

    def test_invalid_event_and_tempo(self):
        d=sample();d['events']=[off('orphan',0)]
        with self.assertRaises(ValueError): process(d)
        d=sample();d['tempos'][1]['tick']=0
        with self.assertRaises(ValueError): process(d)
        d=sample();d['events'][0]['tick']=200
        with self.assertRaises(ValueError): process(d)

if __name__=='__main__':unittest.main()
