import threading
import time

"""Состояние, общее для нескольких модулей ядра.

Подтверждения отправки живут здесь, потому что ставит их один модуль
(Telegram или почта), а читают другие: фильтр посторонней речи и главный цикл.
Через `from ... import *` такое не передать — каждый модуль получил бы
собственную копию и перестал видеть чужие изменения.

Поэтому обращаться только через атрибут модуля:

    import jarvis_state as _state
    _state.pending_telegram_send = {...}

Ждущая подтверждения отправка: {"chat": ..., "text": ...} для Telegram,
{"to": ..., "subject": ..., "body": ...} для почты. Одновременно ждать может
только одна — постановка новой сбрасывает другую.
"""

pending_telegram_send = None
pending_email_send = None
pending_project_selection = None

# Audio UI telemetry; never enumerate/open PortAudio from a UI worker.
microphone_enabled = True
assistant_ready = False
microphone_ready = False
microphone_error = ""
microphone_generation = 0
microphone_resumed_at = 0.0
microphone_level = 0.0
microphone_level_at = 0.0
microphone_lock = threading.RLock()
playback_level = 0.0
playback_level_available = False
playback_level_at = 0.0


# Метрики последнего цикла «услышал — подумал — ответил». Пишет их тот модуль,
# который отработал, а читают статус и главный цикл, рисующий задержки в окне.
last_stt_ms = 0.0
last_llm_ttft_ms = 0.0
last_tts_ms = 0.0
response_started_at = 0.0
last_audio_start_ms = 0.0

# Сколько раз за сессию движок вернул пустой ответ и пришлось откатываться.
llm_empty_failovers = 0


# Состояние речи. Синтез и распознавание делят его на двоих: пока Джарвис
# говорит, слушатель обязан знать об этом, иначе примет собственный голос за
# новую команду. Поэтому переменные общие, а не по копии на модуль.
is_speaking = False
speaking_cooldown_until = 0.0

# Распознаватель речи: синтез поднимает ему порог срабатывания на время
# собственной реплики и возвращает прежний после неё.
recognizer = None
threshold_before_speech = None

# Последняя произнесённая фраза — по ней отсеивается эхо своего же голоса.
last_spoken_text = ""
last_response_text = ""
last_user_command = ""
speech_finished_at = 0.0

# До какого момента команда принимается без слова «Джарвис».
wake_active_until = 0.0
wake_window_kind = "followup"  # 'address' after a wake-only phrase or hotkey


class InterruptEvent(threading.Event):
    """Event-compatible interruption with a monotonic generation per set().

    Repeated set() calls are distinct barge-ins even while the flag is already
    true. clear() only resets the flag; it cannot erase a generation observed by
    an old response. The extra lock makes flag/generation snapshots consistent.
    Normal Event.wait()/is_set() retain their standard level-triggered semantics.
    """

    def __init__(self):
        super().__init__()
        self._generation_lock = threading.Lock()
        self._generation = 0

    def snapshot(self):
        with self._generation_lock:
            return self._generation, super().is_set()

    @property
    def generation(self):
        return self.snapshot()[0]

    def set(self):
        with self._generation_lock:
            self._generation += 1
            super().set()

    def clear(self):
        with self._generation_lock:
            super().clear()


# The dispatcher may clear the level at a new command boundary. Background
# notifications snapshot the generation instead; they never clear this event.
interrupt_event = InterruptEvent()


class PipelineCancellation:
    """A run owns its cancellation; clearing the input cannot revive that run.

    The command dispatcher clears interrupt_event only at a NEW command boundary.
    Pass one instance to LLM and TTS to cancel the entire response together.
    Never reset this instance or clear the shared interrupt from a worker.
    """

    def __init__(self, source=None, *, fresh=False):
        self._source = interrupt_event if source is None else source
        self._event = threading.Event()
        self._generation = None
        if isinstance(self._source, InterruptEvent):
            self._generation, already_set = self._source.snapshot()
            if already_set and not fresh:
                self._event.set()
        elif fresh:
            raise TypeError("A fresh token requires an InterruptEvent generation")
        elif self._source.is_set():
            self._event.set()

    @classmethod
    def for_notification(cls):
        """New independent speech: ignore old level, observe every future set."""
        return cls(interrupt_event, fresh=True)

    def is_set(self):
        changed = (self._source.generation != self._generation
                   if self._generation is not None else self._source.is_set())
        if changed:
            self._event.set()
        return self._event.is_set()

    def set(self):
        self._event.set()

    def wait(self, timeout):
        until = time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            self._event.wait(min(0.02, remaining))
        return True
