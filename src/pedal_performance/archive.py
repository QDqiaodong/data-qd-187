"""Self-contained, block-indexed evidence archive for one recorded take.

An archive stores the derived evidence of every voice -- onset, key release,
real stop, exact microsecond times, stop reason and the causal event ids -- in
a compact binary package.  It never stores the raw event stream, so a query
answers from archived evidence alone and never replays the take.

The package layout is fixed and versioned::

    header (fixed size)
    block directory (fixed-size entries, one per block of voices)
    records (fixed-size entries, one per voice, in (start_tick, voice_id) order)
    string table (identifiers referenced by the records)

Every block entry carries the exact start of its first voice and the maximum
stop time in the block.  A window query can therefore skip whole blocks whose
voices all stopped before the window or all start after it, and only decodes
the records it actually needs.  Pagination is by the archive's global voice
order; a cursor names the next matching voice, so an old-onset voice that is
still sounding is never skipped and is never emitted twice.
"""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from fractions import Fraction
from hashlib import sha256
import json
import re
import struct

from .checks import fields, ident, integer
from .clock import TempoClock
from .io import pairs, reject_constant
from .model import validate
from .voices import perform

ARCHIVE_VERSION = 1
MAGIC = b'PPARCH01'
MAX_ARCHIVE_BYTES = 8_000_000
MAX_LIMIT = 100
BLOCK_SIZE = 32

_HEADER = struct.Struct('<8sHH32sqIIIIQIQIQQ')
_DIRECTORY = struct.Struct('<I H q I q I 2x')
_RECORD = struct.Struct('<IiIBBBBqIqIqIIBIBIBx')

assert _HEADER.size == 108
assert _DIRECTORY.size == 32
assert _RECORD.size == 68

_NULL_REF = 0xFFFFFFFF
_REASONS = ('key_release', 'pedal_release', 'end_of_take')
_REASON_CODES = {reason: code for code, reason in enumerate(_REASONS)}

_INTEGER = re.compile(r'(?:0|[1-9][0-9]{0,29})\Z')
_FRACTION = re.compile(r'[1-9][0-9]{0,29}/[1-9][0-9]{0,29}\Z')


# ---------------------------------------------------------------------------
# Exact time strings


def _exact(value):
    """Parse a canonical non-negative exact microsecond string."""
    if not isinstance(value, str):
        raise ValueError('exact time string required')
    if _INTEGER.fullmatch(value):
        return Fraction(int(value))
    if not _FRACTION.fullmatch(value):
        raise ValueError('invalid exact time string')
    numerator, denominator = (int(part) for part in value.split('/'))
    if denominator <= 1:
        raise ValueError('fraction denominator must exceed one')
    fraction = Fraction(numerator, denominator)
    if str(fraction) != value:
        raise ValueError('exact time must be a lowest-terms fraction')
    return fraction


def _format(value):
    return str(Fraction(value))


# ---------------------------------------------------------------------------
# Building


def _canonical_identity(data):
    """Stable identity independent of JSON object key order.

    Arrays keep their order: same-tick event order is semantic, so a take that
    merely reorders an object's keys keeps its identity while a take that
    reorders same-tick events does not.
    """
    text = json.dumps(data, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)
    return sha256(text.encode('utf-8')).digest()


def _times(clock, voice):
    """Exact (start, key_release, sound_end) microsecond Fractions."""
    pairs_ = {}
    for key in ('start', 'key_release', 'sound_end'):
        tick = voice[f'{key}_tick']
        pairs_[key] = clock.at(tick) if tick is not None else None
    return pairs_['start'], pairs_['key_release'], pairs_['sound_end']


def _pack_strings(ordered):
    table = bytearray()
    refs = {}

    def add(value):
        if value is None:
            return _NULL_REF, 0
        if value not in refs:
            encoded = value.encode('ascii')
            if not 1 <= len(encoded) <= 64:
                raise ValueError('archived identifier length out of range')
            refs[value] = (len(table), len(encoded))
            table.extend(encoded)
        return refs[value]

    packed = []
    for voice in ordered:
        voice_ref = add(voice['voice_id'])
        release_ref = add(voice['release_event_id'])
        end_ref = add(voice['end_event_id'])
        packed.append((voice_ref, release_ref, end_ref))
    return bytes(table), packed


def build_archive(source):
    """Validate a single take and return the publishable archive bytes."""
    data = validate(source, allow_score=False)
    clock = TempoClock(data['ppq'], data['tempos'])
    voices = perform(data)
    for voice in voices:
        start_us, release_us, end_us = _times(clock, voice)
        voice['start_us_exact'] = start_us
        voice['release_us_exact'] = release_us
        voice['end_us_exact'] = end_us
    # Query order is (start_tick, voice_id), independent of event input order.
    ordered = sorted(voices, key=lambda voice: (voice['start_tick'], voice['voice_id']))
    strings, refs = _pack_strings(ordered)
    archive_id = _canonical_identity(data)

    records = bytearray()
    blocks = []
    for position, (voice, refs_) in enumerate(zip(ordered, refs)):
        if voice['end_reason'] not in _REASON_CODES:
            raise ValueError('unsupported stop reason')
        key_tick = voice['key_release_tick']
        release_num, release_den = (0, 0) if key_tick is None else (
            voice['release_us_exact'].numerator, voice['release_us_exact'].denominator)
        records.extend(_RECORD.pack(
            voice['start_tick'], -1 if key_tick is None else key_tick,
            voice['sound_end_tick'], voice['channel'], voice['pitch'],
            voice['velocity'], _REASON_CODES[voice['end_reason']],
            voice['start_us_exact'].numerator, voice['start_us_exact'].denominator,
            release_num, release_den,
            voice['end_us_exact'].numerator, voice['end_us_exact'].denominator,
            refs_[0][0], refs_[0][1], refs_[1][0], refs_[1][1],
            refs_[2][0], refs_[2][1]))
        if position % BLOCK_SIZE == 0:
            blocks.append({'first': position,
                           'min_start': voice['start_us_exact'],
                           'max_end': voice['end_us_exact']})
        else:
            block = blocks[-1]
            if voice['start_us_exact'] < block['min_start']:
                raise ValueError('voices are not ordered by exact onset')
            if voice['end_us_exact'] > block['max_end']:
                block['max_end'] = voice['end_us_exact']

    directory = bytearray()
    for block in blocks:
        directory.extend(_DIRECTORY.pack(
            block['first'], min(BLOCK_SIZE, len(ordered) - block['first']),
            block['min_start'].numerator, block['min_start'].denominator,
            block['max_end'].numerator, block['max_end'].denominator))

    directory_offset = _HEADER.size
    records_offset = directory_offset + len(directory)
    string_offset = records_offset + len(records)
    file_length = string_offset + len(strings)
    total = clock.at(data['end_tick'])
    header = _HEADER.pack(
        MAGIC, ARCHIVE_VERSION, _HEADER.size, archive_id,
        total.numerator, total.denominator, len(ordered), BLOCK_SIZE,
        len(blocks), string_offset, len(strings), directory_offset,
        _RECORD.size, records_offset, file_length)
    payload = header + bytes(directory) + bytes(records) + strings
    if len(payload) != file_length:
        raise ValueError('archive layout mismatch')
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError('archive exceeds 8,000,000 bytes')
    return payload


def archive_summary(payload):
    """Small summary of an archive payload (used by the CLI)."""
    header = _HEADER.unpack_from(payload, 0)
    return {'schema_version': ARCHIVE_VERSION, 'archive_id': header[3].hex(),
            'voices': header[6]}


# ---------------------------------------------------------------------------
# Query


def _parse_request(request):
    fields(request, ('from_us', 'to_us', 'limit'), optional=('cursor',))
    start = _exact(request['from_us'])
    end = _exact(request['to_us'])
    if not start < end:
        raise ValueError('from_us must be less than to_us')
    limit = integer(request['limit'], 1, MAX_LIMIT)
    cursor = request.get('cursor')
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError('cursor must be a non-empty string or null')
    return start, end, limit, cursor


class _Reader:
    """Lazy, bounds-checked view of the archive through ``read_at``.

    Nothing outside the header is fetched until a query needs it, and every
    block or record is fetched at most once per request.  A block whose starts
    are all at or past the window, or whose stops are all before it, is skipped
    from its directory entry alone.
    """

    def __init__(self, read_at, archive_size):
        if not callable(read_at):
            raise ValueError('read_at callback required')
        if type(archive_size) is not int or archive_size < 0:
            raise ValueError('archive size must be a non-negative integer')
        self._read_at = read_at
        self.size = archive_size
        self._blocks = {}
        self._records = {}
        self.archive_id = None
        self.total = None
        self.voice_count = None
        self.block_size = None
        self.block_count = None
        self.string_offset = None
        self.string_length = None
        self.directory_offset = None
        self.records_offset = None
        self._header()

    def _read(self, offset, length):
        if offset < 0 or length < 0 or offset > self.size or length > self.size - offset:
            raise ValueError('archive read crosses the package boundary')
        if length == 0:
            return b''
        chunk = self._read_at(offset, length)
        if isinstance(chunk, (bytearray, memoryview)):
            chunk = bytes(chunk)
        if not isinstance(chunk, bytes):
            raise ValueError('archive reader must return bytes')
        if len(chunk) != length:
            raise ValueError('archive is truncated')
        return chunk

    def _header(self):
        if self.size < _HEADER.size:
            raise ValueError('archive is truncated')
        (magic, version, header_size, archive_id, total_num, total_den,
         voice_count, block_size, block_count, string_offset, string_length,
         directory_offset, record_stride, records_offset, file_length) = \
            _HEADER.unpack(self._read(0, _HEADER.size))
        if magic != MAGIC:
            raise ValueError('not a performance archive')
        if version != ARCHIVE_VERSION:
            raise ValueError('unsupported archive version')
        if header_size != _HEADER.size:
            raise ValueError('invalid archive header size')
        if file_length != self.size:
            raise ValueError('archive is truncated or resized')
        if total_den <= 0 or total_num < 0:
            raise ValueError('invalid archive duration')
        if voice_count > 10000 or not 1 <= block_size <= 4096:
            raise ValueError('invalid archive header')
        if block_size != BLOCK_SIZE:
            raise ValueError('invalid archive block size')
        expected_blocks = (voice_count + block_size - 1) // block_size
        if block_count != expected_blocks:
            raise ValueError('invalid archive block count')
        if record_stride != _RECORD.size:
            raise ValueError('invalid archive record stride')
        if directory_offset != _HEADER.size:
            raise ValueError('invalid directory offset')
        if records_offset != directory_offset + block_count * _DIRECTORY.size:
            raise ValueError('invalid record offset')
        if string_offset != records_offset + voice_count * _RECORD.size:
            raise ValueError('invalid string offset')
        if string_offset > file_length or string_offset + string_length != file_length:
            raise ValueError('invalid string table bounds')
        if string_offset + string_length > MAX_ARCHIVE_BYTES:
            raise ValueError('archive exceeds the documented size limit')
        self.archive_id = archive_id
        self.total = Fraction(total_num, total_den)
        self.voice_count = voice_count
        self.block_size = block_size
        self.block_count = block_count
        self.string_offset = string_offset
        self.string_length = string_length
        self.directory_offset = directory_offset
        self.records_offset = records_offset

    def block(self, number):
        if not 0 <= number < self.block_count:
            raise ValueError('block index out of range')
        entry = self._blocks.get(number)
        if entry is not None:
            return entry
        raw = self._read(self.directory_offset + number * _DIRECTORY.size,
                         _DIRECTORY.size)
        first, count, start_num, start_den, end_num, end_den = _DIRECTORY.unpack(raw)
        expected_first = number * self.block_size
        expected_count = min(self.block_size, self.voice_count - expected_first)
        if first != expected_first or count != expected_count:
            raise ValueError('invalid block directory entry')
        if start_den <= 0 or end_den <= 0 or start_num < 0 or end_num < 0:
            raise ValueError('invalid block time bounds')
        min_start = Fraction(start_num, start_den)
        max_end = Fraction(end_num, end_den)
        if max_end < min_start:
            raise ValueError('invalid block bounds')
        entry = {'first': first, 'count': count, 'min_start': min_start,
                 'max_end': max_end}
        self._blocks[number] = entry
        return entry

    def record(self, index):
        if not 0 <= index < self.voice_count:
            raise ValueError('voice index out of range')
        cached = self._records.get(index)
        if cached is not None:
            return cached
        raw = self._read(self.records_offset + index * _RECORD.size, _RECORD.size)
        (start_tick, key_tick, end_tick, channel, pitch, velocity, reason_code,
         start_num, start_den, release_num, release_den, end_num, end_den,
         voice_off, voice_len, release_off, release_len,
         end_off, end_len) = _RECORD.unpack(raw)
        if not 0 <= channel <= 15 or not 0 <= pitch <= 127 or not 1 <= velocity <= 127:
            raise ValueError('invalid archived voice')
        if not 0 <= start_tick <= 10_000_000 or not 0 <= end_tick <= 10_000_000:
            raise ValueError('invalid archived tick')
        if start_tick > end_tick:
            raise ValueError('invalid archived voice interval')
        if reason_code >= len(_REASONS):
            raise ValueError('invalid archived stop reason')
        if start_den <= 0 or end_den <= 0 or start_num < 0 or end_num < 0:
            raise ValueError('invalid archived exact time')
        start = Fraction(start_num, start_den)
        end = Fraction(end_num, end_den)
        if not start <= end:
            raise ValueError('invalid archived exact interval')
        if key_tick == -1:
            if release_den != 0 or release_num != 0 or release_len != 0:
                raise ValueError('invalid archived key release')
            release = None
        else:
            if not 0 <= key_tick <= 10_000_000 or not start_tick <= key_tick <= end_tick:
                raise ValueError('invalid archived key release tick')
            if release_den <= 0 or release_num < 0 or release_len == 0:
                raise ValueError('invalid archived key release time')
            release = Fraction(release_num, release_den)
            if not start <= release <= end:
                raise ValueError('invalid archived key release time')
        reason = _REASONS[reason_code]
        if reason == 'end_of_take':
            if end_len != 0:
                raise ValueError('end_of_take must not name a stop event')
        else:
            if end_len == 0:
                raise ValueError('a stopped voice needs a stop event')
        if reason == 'key_release' and (release_off, release_len) != (end_off, end_len):
            raise ValueError('key release evidence mismatch')
        if reason != 'end_of_take' and release_len == 0:
            raise ValueError('a stopped voice needs a key release')
        if voice_len == 0:
            raise ValueError('missing archived voice id')
        record = {'index': index, 'start_tick': start_tick, 'key_tick': key_tick,
                  'end_tick': end_tick, 'channel': channel, 'pitch': pitch,
                  'velocity': velocity, 'reason': reason, 'start': start,
                  'release': release, 'end': end, 'voice_ref': (voice_off, voice_len),
                  'release_ref': (release_off, release_len),
                  'end_ref': (end_off, end_len)}
        self._records[index] = record
        return record

    def string(self, reference):
        offset, length = reference
        if length == 0:
            return None
        if not 1 <= length <= 64 or offset + length > self.string_length:
            raise ValueError('invalid archived string reference')
        raw = self._read(self.string_offset + offset, length)
        try:
            value = raw.decode('ascii')
        except UnicodeDecodeError:
            raise ValueError('invalid archived identifier encoding')
        return ident(value)

    def locate(self, index, start, end):
        """First matching voice at or after ``index`` in global order."""
        while index < self.voice_count:
            number = index // self.block_size
            entry = self.block(number)
            last = entry['first'] + entry['count']
            if index >= last:
                index = last
                continue
            if entry['min_start'] >= end:
                return None
            if entry['max_end'] <= start:
                index = last
                continue
            while index < last:
                record = self.record(index)
                if not entry['min_start'] <= record['start'] \
                        or record['end'] > entry['max_end']:
                    raise ValueError('archived record contradicts its block')
                if record['start'] >= end:
                    return None
                if record['end'] > start and record['end'] > record['start']:
                    return index, record
                index += 1
        return None

    def item(self, record):
        voice_id = self.string(record['voice_ref'])
        release_id = self.string(record['release_ref'])
        end_id = self.string(record['end_ref'])
        voice = {
            'voice_id': voice_id,
            'channel': record['channel'],
            'pitch': record['pitch'],
            'velocity': record['velocity'],
            'start_tick': record['start_tick'],
            'key_release_tick': None if record['key_tick'] == -1 else record['key_tick'],
            'release_event_id': release_id,
            'sound_end_tick': record['end_tick'],
            'end_reason': record['reason'],
            'end_event_id': end_id,
            'start_us': _format(record['start']),
            'key_release_us': None if record['release'] is None else _format(record['release']),
            'sound_end_us': _format(record['end']),
            'sound_duration_us': _format(record['end'] - record['start']),
        }
        return voice


def _encode_cursor(archive_id, start, end, index):
    payload = json.dumps({'v': 1, 'a': archive_id.hex(), 'f': _format(start),
                          't': _format(end), 'i': index},
                         sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'v1.' + urlsafe_b64encode(payload).decode('ascii').rstrip('=')


def _decode_cursor(cursor, archive_id, start, end, voice_count):
    if not isinstance(cursor, str) or not cursor.startswith('v1.'):
        raise ValueError('invalid cursor')
    text = cursor[3:]
    try:
        padded = text + '=' * (-len(text) % 4)
        payload = urlsafe_b64decode(padded.encode('ascii'))
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=reject_constant)
    except (ValueError, UnicodeError) as error:
        raise ValueError(f'invalid cursor: {error}')
    fields(value, ('v', 'a', 'f', 't', 'i'))
    if integer(value['v'], 1, 1) != 1:
        raise ValueError('unsupported cursor version')
    if value['a'] != archive_id.hex():
        raise ValueError('cursor belongs to a different archive')
    if value['f'] != _format(start) or value['t'] != _format(end):
        raise ValueError('cursor belongs to a different window')
    index = integer(value['i'], 0, max(voice_count - 1, 0))
    if not index < voice_count:
        raise ValueError('cursor position out of range')
    return index


def query_archive(read_at, archive_size, request):
    """Answer one window page using only the range reader.

    ``read_at(offset, length)`` must return exactly ``length`` raw bytes for
    that range; ``archive_size`` is the caller-known package length.  Every
    request is independent: nothing is cached across calls.
    """
    start, end, limit, cursor = _parse_request(request)
    reader = _Reader(read_at, archive_size)
    if not 0 <= start < end <= reader.total:
        raise ValueError('window lies outside the recorded duration')

    items = []
    index = 0
    if cursor is not None:
        index = _decode_cursor(cursor, reader.archive_id, start, end,
                               reader.voice_count)
        record = reader.record(index)
        matches = (record['start'] < end and record['end'] > start
                   and record['end'] > record['start'])
        if not matches:
            raise ValueError('cursor does not point at a matching voice')

    while len(items) < limit:
        found = reader.locate(index, start, end)
        if found is None:
            index = reader.voice_count
            break
        index, record = found
        visible_from = max(record['start'], start)
        visible_to = min(record['end'], end)
        items.append({
            'voice': reader.item(record),
            'visible_from_us': _format(visible_from),
            'visible_to_us': _format(visible_to),
            'visible_duration_us': _format(visible_to - visible_from),
        })
        index += 1

    next_cursor = None
    if len(items) == limit:
        found = reader.locate(index, start, end)
        if found is not None:
            next_cursor = _encode_cursor(reader.archive_id, start, end, found[0])

    return {
        'schema_version': 1,
        'archive_id': reader.archive_id.hex(),
        'from_us': _format(start),
        'to_us': _format(end),
        'items': items,
        'next_cursor': next_cursor,
    }
