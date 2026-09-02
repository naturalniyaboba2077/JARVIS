"""Синтез речи: Piper локально, edge-tts из облака, XTTS для клонированного голоса.

Реплики режутся на предложения и озвучиваются по мере готовности, поэтому
первая фраза звучит через доли секунды, а не после генерации всего ответа.
Частые фразы («Слушаю, сэр») лежат в кэше и проигрываются мгновенно.

Пока Джарвис говорит, он поднимает порог микрофона и помечает это в общем
состоянии — иначе слушатель принял бы его собственный голос из колонок за
новую команду.
"""

import asyncio
import hashlib
import math
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import pygame

import jarvis_state as _state
from jarvis_config import (
    FOLLOWUP_MODE, FOLLOWUP_WINDOW, JARVIS_DIR, SPEAK_COOLDOWN, _pythonw_exe,
)
from jarvis_log import jarvis_logger
from jarvis_ui import (
    OVERLAY_ENABLED, _main_window_minimized, _overlay_send, ui_msg, ui_state,
    ui_sub,
)

try:
    import edge_tts
except ImportError:
    edge_tts = None

__all__ = [
    "TTS_ENGINE", "EDGE_VOICE", "PIPER_VOICE", "PIPER_MODEL_PATH",
    "PIPER_LENGTH_SCALE", "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE",
    "INSTANT_PHRASES", "speak", "speak_streaming", "generate_speech",
    "tts_to_bytes", "prewarm_tts_cache",
    "_mark_speaking", "_set_done_speaking", "_effective_tts_engine",
    "_piper_available", "_clean_tts_text", "_cache_ext", "_run_edge_tts_sync",
    "_edge_tts_to_bytes", "_wav_envelope", "_play_audio_bytes",
    "_load_xtts_if_needed", "_TTS_CACHE_DIR", "_TTS_INSTANT_CACHE",
]


TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").lower()

tts = None
XTTS_DEVICE = None

def _load_xtts_if_needed():
    global tts, XTTS_DEVICE
    if tts is not None:
        return
    print("Loading XTTS-v2 (this will be slow on first use)...")
    try:
        import torch
        import torchaudio
        import soundfile as sf

        _original_load = torch.load
        def _patched_load(*args, **kwargs):
            kwargs['weights_only'] = False
            return _original_load(*args, **kwargs)
        torch.load = _patched_load

        def _patched_audio_load(filepath, **kwargs):
            data, samplerate = sf.read(filepath, dtype='float32')
            data = data.T if len(data.shape) > 1 else data.reshape(1, -1)
            return torch.tensor(data), samplerate
        torchaudio.load = _patched_audio_load

        from TTS.api import TTS
        XTTS_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"XTTS device: {XTTS_DEVICE} (RTX 5070 CUDA preferred)")
        tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(XTTS_DEVICE)
    except Exception as e:
        print(f"XTTS load error: {e}")
        tts = None



# начали говорить — запоминаем порог микрофона, чтобы наш голос его не сбил
def _mark_speaking():
    """Enter the speaking state and remember the mic threshold from before it."""
    if not _state.is_speaking and _state.recognizer is not None:
        _state.threshold_before_speech = _state.recognizer.energy_threshold
    _state.is_speaking = True
    # Звук пошёл — синтез уже позади, подпись под шаром должна это показывать.
    ui_sub("говорю…")


# закончили говорить — ненадолго глушим микрофон, чтобы не слышать себя
def _set_done_speaking():
    """Mark TTS as finished, start the mic cooldown, and open the follow-up window."""
    _state.is_speaking = False
    _state.speaking_cooldown_until = time.time() + SPEAK_COOLDOWN
    _state.wake_active_until = (time.time() + FOLLOWUP_WINDOW) if FOLLOWUP_MODE != "off" else 0.0
    # Пока Джарвис говорил, его собственный голос задирал порог микрофона через
    # dynamic_energy_threshold. Возвращаем то значение, что было до речи, иначе
    # он глохнет к пользователю после каждого своего ответа.
    if _state.recognizer is not None and _state.threshold_before_speech is not None:
        _state.recognizer.energy_threshold = min(_state.threshold_before_speech, 1500)
        jarvis_logger.debug(
            f"[SPEAK] порог микрофона восстановлен: {_state.recognizer.energy_threshold:.0f}")
    _state.threshold_before_speech = None
    ui_state("idle")
    ui_sub("")   # вернуть подпись по умолчанию, даже если состояние не менялось
    jarvis_logger.debug(f"[SPEAK] закончил → cooldown {SPEAK_COOLDOWN:.1f} с, "
                        f"окно продолжения {FOLLOWUP_WINDOW:.0f} с")


_TTS_INSTANT_CACHE: dict[str, str] = {}
_TTS_CACHE_DIR = JARVIS_DIR / "tts_cache"

INSTANT_PHRASES = [
    "Слушаю, сэр.",
    "Слушаю.",
    "Выполняю, сэр.",
    "Готово, сэр.",
    "Открываю, сэр.",
    "Включаю, сэр.",
    "Открываю браузер, сэр.",
    "Открываю Яндекс Музыку, сэр.",
    "Блокирую, сэр.",
    "Системы на связи, сэр.",
    "Одну секунду, сэр.",
    "Есть, сэр.",
    "Секунду, обрабатываю, сэр.",
    "Тише, сэр.",
    "Громче, сэр.",
    "Звук выключен, сэр.",
    "Следующий, сэр.",
    "Предыдущий, сэр.",
    "Всегда пожалуйста, сэр.",
    "Здравствуйте, сэр.",
]


def _cache_ext() -> str:
    return "mp3" if _effective_tts_engine() == "edge" else "wav"


def prewarm_tts_cache():
    """Pre-generate audio for common phrases into on-disk cache (once).

    Runs at startup. Files persist between runs, so after the first ever launch
    these phrases are instant even on a cold start.
    """
    effective = _effective_tts_engine()
    if not ((effective == "piper" and _piper_available())
            or (effective == "edge" and edge_tts is not None)):
        return
    try:
        _TTS_CACHE_DIR.mkdir(exist_ok=True)
    except Exception:
        return
    engine = effective
    # В ключ входит не только движок, но и конкретный голос: иначе после смены
    # PIPER_VOICE готовые фразы продолжали бы играть старым голосом, а остальной
    # ответ — новым. Это тот самый баг «два голоса в одном ответе».
    voice_id = (f"{PIPER_MODEL_PATH.stem}:{PIPER_LENGTH_SCALE}:"
                f"{PIPER_NOISE_SCALE}:{PIPER_NOISE_W_SCALE}"
                if engine == "piper" else EDGE_VOICE)
    for phrase in INSTANT_PHRASES:
        import hashlib
        h = hashlib.md5(f"{engine}:{voice_id}:{phrase}".encode("utf-8")).hexdigest()[:12]
        existing = _TTS_CACHE_DIR / f"{h}.{_cache_ext()}"
        if existing.exists():
            _TTS_INSTANT_CACHE[phrase] = str(existing)
            continue
        for stale in _TTS_CACHE_DIR.glob(f"{h}.*"):
            try:
                stale.unlink()
                print(f"[TTS cache] удалён файл от другого движка: {stale.name}")
                jarvis_logger.warning(f"[TTS cache] удалён файл от другого движка: {stale.name}")
            except Exception:
                pass
        data, suffix = tts_to_bytes(phrase)
        if data:
            fpath = _TTS_CACHE_DIR / f"{h}{suffix}"
            try:
                fpath.write_bytes(data)
                _TTS_INSTANT_CACHE[phrase] = str(fpath)
            except Exception:
                continue


def _wav_envelope(data: bytes, fps: int = 60):
    """Per-frame loudness (0..1) of a WAV, for driving the overlay bars.

    Returns None for anything we can't read (e.g. the mp3 fallback path).
    """
    try:
        import io, wave
        import numpy as np
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                return None
            rate, ch = w.getframerate(), w.getnchannels()
            samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            samples = samples.reshape(-1, ch).mean(axis=1)
        samples = samples.astype(np.float32) / 32768.0
        hop = max(1, rate // fps)
        n = len(samples) // hop
        if n < 1:
            return None
        frames = samples[:n * hop].reshape(n, hop)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        env = rms ** 0.55
        peak = env.max()
        if peak <= 1e-6:
            return None
        return (env / peak).clip(0, 1).tolist()
    except Exception:
        return None


def _playback_pump(env, fps: int = 60) -> bool:
    """Block until playback ends, feeding the overlay real amplitude as it goes.

    Returns False if playback was interrupted (barge-in), True if it finished.
    """
    show = OVERLAY_ENABLED and _main_window_minimized()
    if show:
        _overlay_send(show=True, amp=0.0)
    clock = pygame.time.Clock()
    try:
        while pygame.mixer.music.get_busy():
            if _state.interrupt_event.is_set():
                pygame.mixer.music.stop()
                return False
            if show:
                if env:
                    pos = pygame.mixer.music.get_pos()
                    i = int(pos / 1000.0 * fps) if pos >= 0 else 0
                    amp = env[i] if 0 <= i < len(env) else 0.0
                else:
                    amp = 0.45 + 0.25 * math.sin(time.perf_counter() * 9.0)
                _overlay_send(amp=amp)
            clock.tick(fps)
        return True
    finally:
        if show:
            _overlay_send(amp=0.0, show=False)


def _play_cached_file(path: str) -> bool:
    """Play a pre-generated cache file instantly through pygame."""
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(path)
        _state.interrupt_event.clear()
        _mark_speaking()
        env = None
        if OVERLAY_ENABLED and path.endswith(".wav") and _main_window_minimized():
            try:
                env = _wav_envelope(Path(path).read_bytes())
            except Exception:
                env = None
        pygame.mixer.music.play()
        _playback_pump(env)
        _set_done_speaking()
        pygame.mixer.music.unload()
        return True
    except Exception as e:
        _set_done_speaking()
        print(f"Cached playback error: {e}")
        return False


def _clean_tts_text(text: str) -> str:
    """Strip emoji and special unicode that cause edge-tts to fail silently."""
    import unicodedata
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith('S'):
            cleaned.append(' ')
        else:
            cleaned.append(ch)
    result = ''.join(cleaned)
    result = re.sub(r'  +', ' ', result).strip()
    return result


EDGE_VOICE = os.getenv("EDGE_VOICE", "ru-RU-DmitryNeural")


def _run_edge_tts_sync(text: str, output: str) -> bool:
    """Run edge-tts in its own event loop (Windows-safe, works from any thread)."""
    text = _clean_tts_text(text)
    if not text:
        return False
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        communicate = edge_tts.Communicate(text, EDGE_VOICE)
        loop.run_until_complete(communicate.save(output))
        return True
    except Exception as e:
        print(f"edge-tts error: {e}")
        jarvis_logger.error(f"[TTS:edge] save failed: {e}")
        return False
    finally:
        if loop is not None:
            loop.close()
            asyncio.set_event_loop(None)


def _edge_tts_to_bytes(text: str) -> bytes | None:
    """Generate TTS audio to memory bytes (no temp file needed)."""
    text = _clean_tts_text(text)
    if not text:
        return None
    loop = None
    try:
        import io
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _collect():
            buf = io.BytesIO()
            communicate = edge_tts.Communicate(text, EDGE_VOICE)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()

        data = loop.run_until_complete(_collect())
        return data if data else None
    except Exception as e:
        print(f"edge-tts bytes error: {e}")
        return None
    finally:
        if loop is not None:
            loop.close()
            asyncio.set_event_loop(None)


PIPER_VOICE = os.getenv("PIPER_VOICE", "dmitri")
PIPER_MODEL_PATH = Path(os.getenv(
    "PIPER_MODEL", str(JARVIS_DIR / "piper_models" / f"ru_RU-{PIPER_VOICE}-medium.onnx")))
PIPER_LENGTH_SCALE = float(os.getenv("PIPER_LENGTH_SCALE", "1.08"))
PIPER_NOISE_SCALE = float(os.getenv("PIPER_NOISE_SCALE", "0.50"))
PIPER_NOISE_W_SCALE = float(os.getenv("PIPER_NOISE_W_SCALE", "0.65"))
_piper_voice = None
_piper_tried = False


def _piper_available() -> bool:
    return PIPER_MODEL_PATH.exists()


def _effective_tts_engine() -> str:
    """Resolve auto once per call without allowing mid-answer voice switching."""
    if TTS_ENGINE == "auto":
        return "piper" if _piper_available() else "edge"
    return TTS_ENGINE


def _load_piper():
    """Lazy-load the piper voice (one-time ~2s cost, done at startup pre-warm)."""
    global _piper_voice, _piper_tried
    if _piper_voice is not None or _piper_tried:
        return _piper_voice
    _piper_tried = True
    try:
        from piper import PiperVoice
        _piper_voice = PiperVoice.load(str(PIPER_MODEL_PATH))
        print("Piper local TTS loaded (offline, fast).")
    except Exception as e:
        print(f"Piper load error (falling back to edge): {e}")
        _piper_voice = None
    return _piper_voice


def _piper_syn_config():
    """Calm, measured local voice settings."""
    try:
        from piper import SynthesisConfig
        return SynthesisConfig(
            length_scale=PIPER_LENGTH_SCALE,
            noise_scale=PIPER_NOISE_SCALE,
            noise_w_scale=PIPER_NOISE_W_SCALE,
            normalize_audio=True,
        )
    except Exception:
        return None


def _piper_to_wav_bytes(text: str) -> bytes | None:
    """Synthesize text to WAV bytes locally with piper."""
    voice = _load_piper()
    if voice is None:
        return None
    text = _clean_tts_text(text)
    if not text:
        return None
    try:
        import io, wave
        chunks = list(voice.synthesize(text, syn_config=_piper_syn_config()))
        if not chunks:
            return None
        sr = chunks[0].sample_rate
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            for c in chunks:
                wf.writeframes(c.audio_int16_bytes)
        return buf.getvalue()
    except Exception as e:
        print(f"Piper synth error: {e}")
        return None


def tts_to_bytes(text: str):
    """Unified TTS: return (audio_bytes, suffix) using the configured engine.

    TTS_ENGINE=edge  → Microsoft cloud voice (DmitryNeural, high quality)
    TTS_ENGINE=piper → local neural (fast, offline)
    """
    _t0 = time.perf_counter()
    engine = _effective_tts_engine()
    if engine == "piper" and _piper_available():
        data = _piper_to_wav_bytes(text)
        if data:
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:piper] {_state.last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".wav"
    if engine == "edge" and edge_tts is not None:
        data = _edge_tts_to_bytes(text)
        if data:
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:edge] {_state.last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".mp3"
        jarvis_logger.warning(f"[TTS:edge] FAILED (сеть?): {text[:60]!r}")
    if engine not in {"edge", "piper"} and _piper_available():
        data = _piper_to_wav_bytes(text)
        if data:
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.warning(f"[TTS:piper-fallback] {_state.last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".wav"
    jarvis_logger.error(f"[TTS] все движки отказали: {text[:60]!r}")
    return None, None


def generate_speech(text: str) -> bool:
    """Fast or cloned speech. Prioritizes speed."""
    engine = _effective_tts_engine()
    if engine == "piper":
        data = _piper_to_wav_bytes(text)
        if not data:
            return False
        Path("temp_jarvis_speech.wav").write_bytes(data)
        return True
    if engine == "edge":
        if edge_tts is None:
            print("edge-tts not installed. Falling back to print only.")
            return False
        output = "temp_jarvis_speech.mp3"
        return _run_edge_tts_sync(text, output)

    _load_xtts_if_needed()
    if tts is None:
        print("TTS not available.")
        return False

    reference_audio = "jarvis_sample.wav"
    output_audio_raw = "temp_jarvis_speech_raw.wav"
    output_audio = "temp_jarvis_speech.wav"

    if not os.path.exists(reference_audio):
        print(f"WARNING: {reference_audio} not found for cloning.")
        return False

    try:
        tts.tts_to_file(text=text, speaker_wav=reference_audio, language="ru", file_path=output_audio_raw)
        subprocess.run(
            ['ffmpeg', '-y', '-i', output_audio_raw, '-filter:a', 'atempo=1.3', output_audio],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except Exception as e:
        print(f"XTTS generation error: {e}")
        return False


def _play_audio_bytes(data: bytes, suffix: str = ".mp3") -> bool:
    """Play audio bytes through pygame via a temp file. Returns True if completed (not interrupted)."""
    import tempfile
    tmp = None
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            tmp = f.name
        env = _wav_envelope(data) if (OVERLAY_ENABLED and suffix == ".wav") else None
        pygame.mixer.music.load(tmp)
        pygame.mixer.music.play()
        return _playback_pump(env)
    except Exception as e:
        print(f"Playback error: {e}")
        return False
    finally:
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


# озвучиваем ответ вслух
def speak(text: str):
    """Speak text aloud. Interruptible — stops instantly on barge-in."""
    _state.last_spoken_text = (text or "").strip()
    print(f"Jarvis: {text}")
    jarvis_logger.info(f"[SPEAK] {text!r}")

    ui_state("speaking")
    if text.strip() != "Секунду, обрабатываю, сэр.":
        ui_msg("jarvis", text)

    cached = _TTS_INSTANT_CACHE.get(text.strip())
    if cached and os.path.exists(cached):
        jarvis_logger.debug("[SPEAK] → instant cache")
        _play_cached_file(cached)
        return

    if _effective_tts_engine() in {"piper", "edge"}:
        data, suffix = tts_to_bytes(text)
        if data:
            _state.interrupt_event.clear()
            _mark_speaking()
            _play_audio_bytes(data, suffix)
            _set_done_speaking()
            return

    success = generate_speech(text)
    if not success:
        return

    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        audio_file = "temp_jarvis_speech.mp3" if _effective_tts_engine() == "edge" else "temp_jarvis_speech.wav"
        pygame.mixer.music.load(audio_file)

        _state.interrupt_event.clear()
        _mark_speaking()

        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            if _state.interrupt_event.is_set():
                pygame.mixer.music.stop()
                print("[Прерывание TTS]")
                break
            pygame.time.Clock().tick(30)

        _set_done_speaking()

        pygame.mixer.music.unload()
        if os.path.exists(audio_file):
            try:
                os.remove(audio_file)
            except Exception:
                pass
    except Exception as e:
        _set_done_speaking()
        print(f"Playback error: {e}")


def speak_streaming(sentences_iter):
    """Streaming TTS pipeline: generate + play sentences concurrently.

    Takes an iterable of sentence strings. For each sentence:
    - Fires edge-tts generation in a background thread
    - Plays the previous sentence's audio while the next is being generated
    - First word starts playing in ~300-500ms instead of waiting for full response
    """
    ui_state("speaking")

    jarvis_logger.info("[SPEAK:stream] start")
    if not (_piper_available() or edge_tts is not None):
        full = " ".join(sentences_iter)
        speak(full)
        return

    audio_queue: queue.Queue = queue.Queue(maxsize=3)
    SENTINEL = object()
    spoken_parts = []

    def producer():
        """Background thread: converts each sentence to (bytes, suffix) and enqueues."""
        for sentence in sentences_iter:
            sentence = sentence.strip()
            if not sentence or not re.search(r'[A-Za-zА-Яа-я0-9]', sentence):
                continue
            if _state.interrupt_event.is_set():
                break
            spoken_parts.append(sentence)
            data, suffix = tts_to_bytes(sentence)
            if data:
                audio_queue.put((data, suffix))
            else:
                jarvis_logger.error(f"[SPEAK:stream] TTS отказал, фраза пропущена: {sentence[:60]!r}")
        audio_queue.put(SENTINEL)

    _state.interrupt_event.clear()
    _mark_speaking()

    prod_thread = threading.Thread(target=producer, daemon=True)
    prod_thread.start()

    try:
        while True:
            if _state.interrupt_event.is_set():
                print("[Прерывание streaming TTS]")
                break
            try:
                item = audio_queue.get(timeout=15)
            except queue.Empty:
                break
            if item is SENTINEL:
                break
            data, suffix = item
            completed = _play_audio_bytes(data, suffix)
            if not completed:
                break
    finally:
        if spoken_parts:
            _state.last_spoken_text = " ".join(spoken_parts)
        _set_done_speaking()
        prod_thread.join(timeout=2)
        jarvis_logger.info("[SPEAK:stream] done")
