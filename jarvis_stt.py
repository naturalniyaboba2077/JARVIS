"""Распознавание речи и слово активации.

Локально работает faster-whisper (на видеокарте, если она есть), запасной
вариант — облачное распознавание Google. Модель грузится в фоне при старте:
первый вызов иначе занял бы секунды.

Отдельная забота — слово «Джарвис». Whisper коверкает его по-разному
(«Джарез», «Жарвес», «Джаммитс» — всё это реальные промахи из логов), поэтому
имя ловится с допуском, но обычные похожие слова вроде «держись» и «дарвин»
при этом не должны срабатывать.
"""

import os
import re
import threading
import time
from difflib import SequenceMatcher

import numpy as np

import jarvis_state as _state
from jarvis_log import jarvis_logger

__all__ = [
    "STT_ENGINE", "WHISPER_MODEL_SIZE",
    "WAKE_CANON", "WAKE_VARIANT_RE", "WAKE_FUZZY_THRESHOLD",
    "WAKE_ONSET_RE", "WAKE_ONSET_THRESHOLD", "WAKE_ONSET_MIN_LEN",
    "WAKE_BLOCKLIST",
    "transcribe_whisper", "transcribe_speech", "warmup_whisper",
    "contains_wake_word", "strip_wake_word",
    "_wake_tokens", "_is_wake_token", "_wake_indices", "_audio_duration",
    "_whisper_available", "_load_whisper", "_setup_cuda_dll_paths",
]


WAKE_CANON = ("джарвис", "jarvis")
WAKE_VARIANT_RE = re.compile(
    r"^(?:дж|ж|ч|щ|д|ш|х|з|тр)[аоуяею]р?[вб][еиыія][сзц]ь?$"
    r"|^(?:ярвис|арвис|ярвись)$"
    r"|^(?:jarvis|travis|djarvis|jarvis|harvey)$",
    re.UNICODE,
)
WAKE_FUZZY_THRESHOLD = 0.72

# Whisper коверкает имя по-разному, но почти всегда сохраняет характерное начало:
# «Джарез», «Джаммитс», «Джанес», «Жарвес» — всё это реальные пропущенные обращения
# из логов, они не дотягивали до 0.72 и Джарвис молчал. А обычные слова, которые
# случайно похожи целиком («держись», «договаривались», «дарим», «жарим»,
# «ужаристы»), начинаются иначе. Поэтому порог опускаем только для токенов с таким
# началом — общий порог 0.72 при этом не трогаем, иначе полезет фоновая речь.
WAKE_ONSET_RE = re.compile(r"^(?:джа|жарв)", re.UNICODE)
WAKE_ONSET_THRESHOLD = 0.60
WAKE_ONSET_MIN_LEN = 5

WAKE_BLOCKLIST = frozenset({"дарвин", "давись"})


STT_ENGINE = os.getenv("STT_ENGINE", "whisper").lower()
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
_whisper_model = None
_whisper_tried = False
_whisper_lock = threading.Lock()


def _setup_cuda_dll_paths():
    """Put pip-installed CUDA 12 libs (cublas/cudnn/runtime/nvrtc) on PATH.

    CTranslate2 loads these via plain LoadLibrary, which only searches PATH —
    os.add_dll_directory alone is not enough on Windows.
    """
    try:
        import nvidia
        base = list(nvidia.__path__)[0]
        bins = []
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            d = os.path.join(base, sub, "bin")
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
                bins.append(d)
        if bins:
            os.environ["PATH"] = os.pathsep.join(bins) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def _load_whisper():
    """Lazy-load the whisper model. GPU first, CPU fallback."""
    global _whisper_tried
    if _whisper_model is not None or _whisper_tried:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None or _whisper_tried:
            return _whisper_model
        _whisper_tried = True
        return _load_whisper_locked()


def _load_whisper_locked():
    global _whisper_model
    try:
        _setup_cuda_dll_paths()
        from faster_whisper import WhisperModel
        try:
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cuda", compute_type="float16")
            print(f"faster-whisper '{WHISPER_MODEL_SIZE}' loaded on GPU (CUDA, RTX 5070).")
            jarvis_logger.info(f"[STT] whisper '{WHISPER_MODEL_SIZE}' на GPU (cuda/float16)")
        except Exception as ge:
            print(f"Whisper GPU load failed ({str(ge)[:80]}); falling back to CPU int8.")
            jarvis_logger.warning(f"[STT] GPU недоступен ({str(ge)[:80]}) → CPU")
            _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
            print(f"faster-whisper '{WHISPER_MODEL_SIZE}' loaded on CPU (slower).")
            jarvis_logger.warning(f"[STT] whisper '{WHISPER_MODEL_SIZE}' на CPU (int8) — будет медленно")
    except Exception as e:
        print(f"Whisper unavailable ({str(e)[:80]}); using Google STT fallback.")
        _whisper_model = None
    return _whisper_model


def _whisper_available() -> bool:
    return STT_ENGINE == "whisper" and _load_whisper() is not None


def transcribe_whisper(audio) -> str | None:
    """Transcribe a speech_recognition AudioData object locally with whisper.
    Returns the recognized text ('' if silence), or None on failure."""
    model = _load_whisper()
    if model is None:
        return None
    try:
        import numpy as np
        _t0 = time.perf_counter()
        raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # Обрывок короче трети секунды речью быть не может — это щелчок или шум.
        # Гонять на него модель бессмысленно.
        if len(samples) < 16000 * 0.3:
            _state.last_stt_ms = (time.perf_counter() - _t0) * 1000.0
            return ""
        segments, _ = model.transcribe(
            samples, language="ru", beam_size=1,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=200),
            # Не тащим текст прошлой фразы в подсказку: это лишние токены на
            # каждый запрос и главный источник «призраков» вроде
            # «продолжение следует» на тишине.
            condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments).strip()
        _state.last_stt_ms = (time.perf_counter() - _t0) * 1000.0
        return text
    except Exception as e:
        print(f"[Whisper STT error]: {e}")
        return None


# переводим речь в текст
def transcribe_speech(recognizer, audio) -> str:
    """Unified STT: local whisper if available, else Google. '' means no speech."""
    started = time.perf_counter()
    if _whisper_available():
        t = transcribe_whisper(audio)
        if t is not None:
            jarvis_logger.debug(f"[STT:metrics] engine=whisper "
                                f"audio={_audio_duration(audio):.2f}s "
                                f"transcribe={_state.last_stt_ms:.0f}ms chars={len(t)}")
            return t
    result = recognizer.recognize_google(audio, language="ru-RU")
    _state.last_stt_ms = (time.perf_counter() - started) * 1000.0
    jarvis_logger.debug(f"[STT:metrics] engine=google "
                        f"audio={_audio_duration(audio):.2f}s "
                        f"transcribe={_state.last_stt_ms:.0f}ms chars={len(result)}")
    return result


def warmup_whisper():
    """Pre-load + JIT-compile whisper so the first real command isn't cold (~1s)."""
    model = _load_whisper()
    if model is None:
        return
    try:
        import numpy as np
        silence = np.zeros(16000, dtype=np.float32)
        segs, _ = model.transcribe(silence, language="ru", beam_size=1)
        list(segs)
        print("Whisper warmed up (ready for instant transcription).")
    except Exception as e:
        print(f"[Whisper warmup error]: {e}")


def _wake_tokens(text: str) -> list:
    """Lowercase, de-punctuate and split text for wake-word comparison."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return t.split()


def _is_wake_token(tok: str) -> bool:
    """True if this token is the wake word or a plausible mis-hearing of it."""
    if tok in WAKE_BLOCKLIST:
        return False
    if WAKE_VARIANT_RE.match(tok):
        return True
    best = max(SequenceMatcher(None, tok, c).ratio() for c in WAKE_CANON)
    if best >= WAKE_FUZZY_THRESHOLD:
        return True
    # Смягчённый порог — только для токенов с характерным началом имени.
    return (len(tok) >= WAKE_ONSET_MIN_LEN
            and WAKE_ONSET_RE.match(tok) is not None
            and best >= WAKE_ONSET_THRESHOLD)


def _wake_indices(text: str):
    """Indices of tokens forming the wake word, or None if it isn't there.

    Also catches the wake word split across two tokens ("жар весь" → "жарвесь").
    """
    toks = _wake_tokens(text)
    for i, tok in enumerate(toks):
        if _is_wake_token(tok):
            return toks, {i}
    for i, (a, b) in enumerate(zip(toks, toks[1:])):
        if _is_wake_token(a + b):
            return toks, {i, i + 1}
    return toks, None


# ловим "Джарвис", даже если распозналось криво
def contains_wake_word(text: str) -> bool:
    """True if the text contains the wake word in any form Whisper might render it."""
    return _wake_indices(text)[1] is not None


def strip_wake_word(text: str) -> str:
    """Remove the wake word (however it was transcribed) and return the command."""
    toks, idx = _wake_indices(text)
    if idx is None:
        return re.sub(r"\s+", " ", " ".join(toks)).strip(" ,!?.:")
    rest = [t for i, t in enumerate(toks) if i not in idx]
    return re.sub(r"\s+", " ", " ".join(rest)).strip(" ,!?.:")


def _audio_duration(audio) -> float:
    """Length of a speech_recognition AudioData in seconds (0.0 if unknown)."""
    try:
        return len(audio.frame_data) / float(audio.sample_rate * audio.sample_width)
    except Exception:
        return 0.0


# сюда приходит распознанная с микрофона речь
