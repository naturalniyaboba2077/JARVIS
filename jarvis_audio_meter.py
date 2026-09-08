"""Meter the existing microphone stream, without opening another audio device.

Pausing replaces frames with silence before speech recognition. PortAudio stays
open; this is an application-level privacy gate, not an OS hardware mute.
"""

import math
import struct
import time


def pcm_level(data, width=2):
    if width not in (1, 2, 4) or not data:
        return 0.0
    data = data[:len(data) - len(data) % width]
    if not data:
        return 0.0
    values = struct.iter_unpack({1: "<b", 2: "<h", 4: "<i"}[width], data)
    rms = math.sqrt(sum(value[0] ** 2 for value in values) / (len(data) // width))
    return min(1.0, (rms / (2 ** (8 * width - 1))) ** 0.55)


class MeteredStream:
    def __init__(self, stream, state, width=2):
        self._stream, self._state, self._width = stream, state, width

    def read(self, size):
        try:
            data = self._stream.read(size)
        except Exception:
            self._state.microphone_ready = False
            self._state.microphone_level = 0.0
            self._state.microphone_error = "Потеряна связь с микрофоном. Проверьте устройство и перезапустите Jarvis."
            raise
        if not self._state.microphone_enabled:
            self._state.microphone_level = 0.0
            return bytes(len(data))
        self._state.microphone_level = pcm_level(data, self._width)
        self._state.microphone_level_at = time.monotonic()
        return data

    def close(self):
        return self._stream.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)
