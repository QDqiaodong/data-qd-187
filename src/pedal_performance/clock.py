from bisect import bisect_right
from fractions import Fraction

class TempoClock:
    def __init__(self, ppq, tempos):
        self.ppq = ppq
        self.ticks = [t['tick'] for t in tempos]
        self.rates = [t['microseconds_per_quarter'] for t in tempos]
        self.starts = [Fraction(0)]
        for index in range(1, len(tempos)):
            self.starts.append(self.starts[-1] + Fraction((self.ticks[index] - self.ticks[index - 1]) * self.rates[index - 1], ppq))

    def at(self, tick):
        index = bisect_right(self.ticks, tick) - 1
        return self.starts[index] + Fraction((tick - self.ticks[index]) * self.rates[index], self.ppq)
