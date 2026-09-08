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
from jarvis_speech_chunks import CAPABILITY_REPLY
from jarvis_speech_chunks import SpeechChunks
from jarvis_response import Response, SpeechFences, clean_speech, remember_report

import jarvis_state as _state
from jarvis_settings import activity as _settings_activity
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
    "TTS_ENGINE", "EDGE_VOICE", "EDGE_RATE", "EDGE_PITCH", "VOICE_STYLE", "PIPER_VOICE", "PIPER_MODEL_PATH", "XTTS_SPEED", "XTTS_LANGUAGE",
    "PIPER_LENGTH_SCALE", "PIPER_NOISE_SCALE", "PIPER_NOISE_W_SCALE",
    "INSTANT_PHRASES", "speak", "speak_notification", "speak_streaming", "generate_speech",
    "tts_to_bytes", "prewarm_tts_cache", "start_tts_cache_warmup",
    "_mark_speaking", "_set_done_speaking", "_effective_tts_engine",
    "_piper_available", "_clean_tts_text", "_cache_ext", "_run_edge_tts_sync",
    "_edge_tts_to_bytes", "_wav_envelope", "_play_audio_bytes",
    "_load_xtts_if_needed", "_TTS_CACHE_DIR", "_TTS_INSTANT_CACHE",
]


TTS_ENGINE = os.getenv("TTS_ENGINE", "auto").lower()
_TTS_NETWORK_TIMEOUT = 15.0
_TTS_QUEUE_TIMEOUT = 15.0
_PIPELINE_POLL = 0.02

tts = None
XTTS_DEVICE = None
XTTS_SPEED = float(os.getenv("XTTS_SPEED", "1.0"))
XTTS_LANGUAGE = os.getenv("XTTS_LANGUAGE", "ru")
_xtts_lock = threading.RLock()
_xtts_conditioning = None
_xtts_reference_key = None

def _load_xtts_if_needed():
    global tts, XTTS_DEVICE
    if tts is not None:
        return
    print("Loading XTTS-v2 (this will be slow on first use)...")
    try:
        import torch
        import torchaudio
        import soundfile as sf

        from TTS.api import TTS
        from TTS.tts.configs.xtts_config import XttsConfig
        from TTS.tts.models.xtts import XttsArgs, XttsAudioConfig
        from TTS.config.shared_configs import BaseDatasetConfig
        XTTS_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"XTTS device: {XTTS_DEVICE}")
        # Do not replace torch.load globally or disable its safe loader for STT.
        with torch.serialization.safe_globals([XttsConfig, XttsArgs, XttsAudioConfig, BaseDatasetConfig]):
            tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(XTTS_DEVICE)
    except Exception as e:
        print(f"XTTS load error: {e}")
        tts = None



_speech_context = threading.local()


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
    now = time.time()
    _state.speech_finished_at = now
    _state.speaking_cooldown_until = now + SPEAK_COOLDOWN
    if not getattr(_speech_context, "notification", False):
        _state.wake_active_until = (now + FOLLOWUP_WINDOW) if FOLLOWUP_MODE != "off" else 0.0
        _state.wake_window_kind = "followup"
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
_tts_warmup_lock = threading.Lock()
_tts_warmup_thread = None

INSTANT_PHRASES = [
    CAPABILITY_REPLY,
    "Начинаю поиск, сэр.",
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
    "Начинаю проверку проекта, сэр.",
    "Начинаю работу с проектом, сэр.",
]


def _cache_ext() -> str:
    return "mp3" if _effective_tts_engine() == "edge" else "wav"


def _instant_voice_key(engine):
    if engine == "xtts":
        return _xtts_voice_key()
    if engine == "piper":
        return (f"{PIPER_MODEL_PATH.stem}:{PIPER_LENGTH_SCALE}:"
                f"{PIPER_NOISE_SCALE}:{PIPER_NOISE_W_SCALE}:{VOICE_STYLE}")
    return _edge_voice_key()


def _index_existing_tts_cache():
    """Cheap reindex after a voice change: no synthesis, model load or downloads."""
    engine = _effective_tts_engine()
    if engine == "xtts":
        return  # Hashing a large reference belongs to actual XTTS use, not Apply.
    voice_id = _instant_voice_key(engine)
    for phrase in INSTANT_PHRASES:
        key = hashlib.md5(f"{engine}:{voice_id}:{phrase}".encode("utf-8")).hexdigest()[:12]
        path = _TTS_CACHE_DIR / f"{key}.{_cache_ext()}"
        if path.is_file():
            _TTS_INSTANT_CACHE[phrase] = str(path)


@_settings_activity
def prewarm_tts_cache():
    """Pre-generate audio for common phrases into on-disk cache (once).

    Runs at startup. Files persist between runs, so after the first ever launch
    these phrases are instant even on a cold start.
    """
    effective = _effective_tts_engine()
    if not ((effective == "piper" and _piper_available())
            or (effective == "edge" and edge_tts is not None)
            or (effective == "xtts" and (JARVIS_DIR / "jarvis_sample.wav").is_file())):
        return
    try:
        _TTS_CACHE_DIR.mkdir(exist_ok=True)
    except Exception:
        return
    engine = effective
    # В ключ входит не только движок, но и конкретный голос: иначе после смены
    # PIPER_VOICE готовые фразы продолжали бы играть старым голосом, а остальной
    # ответ — новым. Это тот самый баг «два голоса в одном ответе».
    voice_id = _instant_voice_key(engine)
    phrases = INSTANT_PHRASES if engine != "xtts" else INSTANT_PHRASES[:3]
    missing = []
    for phrase in phrases:
        import hashlib
        h = hashlib.md5(f"{engine}:{voice_id}:{phrase}".encode("utf-8")).hexdigest()[:12]
        existing = _TTS_CACHE_DIR / f"{h}.{_cache_ext()}"
        if existing.exists():
            _TTS_INSTANT_CACHE[phrase] = str(existing)
            continue
        missing.append((phrase, h))
    # Publish existing files first: cached replies can play during model loading.
    # Still warm the synthesizer, even if all phrases were already cached.
    if effective == "piper":
        _load_piper()
    for phrase, h in missing:
        if _state.is_speaking:
            break  # A live answer takes priority over optional cache population.
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
        else:
            # Do not repeat a network failure for every phrase at startup.
            break


def start_tts_cache_warmup():
    """Warm any selected engine off the command loop, at most one worker."""
    global _tts_warmup_thread
    with _tts_warmup_lock:
        if _tts_warmup_thread is not None and _tts_warmup_thread.is_alive():
            return
        _tts_warmup_thread = threading.Thread(
            target=prewarm_tts_cache, name="jarvis-tts-cache", daemon=True)
        _tts_warmup_thread.start()


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


def _playback_pump(env, fps: int = 60, cancel_event=None) -> bool:
    """Block until playback ends, feeding the overlay real amplitude as it goes.

    Returns False if playback was interrupted (barge-in), True if it finished.
    """
    cancel = _state.PipelineCancellation(cancel_event)
    show = OVERLAY_ENABLED and _main_window_minimized()
    if show:
        _overlay_send(show=True, amp=0.0)
    clock = pygame.time.Clock()
    last_overlay, last_visibility = 0.0, time.monotonic()
    try:
        while pygame.mixer.music.get_busy():
            if cancel.is_set():
                pygame.mixer.music.stop()
                return False
            pos = pygame.mixer.music.get_pos() if env else 0
            i = int(pos / 1000.0 * fps) if pos >= 0 else 0
            amp = env[i] if env and 0 <= i < len(env) else 0.0
            _state.playback_level = amp
            _state.playback_level_available = bool(env)
            _state.playback_level_at = time.monotonic()
            now = time.monotonic()
            if OVERLAY_ENABLED and now - last_visibility >= 0.25:
                visible = _main_window_minimized()
                if visible != show:
                    show = visible
                    _overlay_send(show=show, amp=amp if show else 0.0)
                last_visibility = now
            if show and now - last_overlay >= 1 / 30:
                _overlay_send(amp=amp)
                last_overlay = now
            clock.tick(fps)
        return True
    finally:
        _state.playback_level = 0.0
        _state.playback_level_available = False
        if show:
            _overlay_send(amp=0.0, show=False)


def _record_audio_start():
    if _state.response_started_at and not _state.last_audio_start_ms:
        _state.last_audio_start_ms = (time.perf_counter() - _state.response_started_at) * 1000
        jarvis_logger.info("[LATENCY] first playback %.0f ms", _state.last_audio_start_ms)


def _play_cached_file(path: str, cancel_event=None) -> bool:
    """Play a pre-generated cache file instantly through pygame."""
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return False
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(path)
        _mark_speaking()
        env = None
        if path.endswith((".wav", ".mp3")):
            try:
                env = _audio_envelope(Path(path).read_bytes(), Path(path).suffix)
            except Exception:
                env = None
        if cancel.is_set():
            return False
        pygame.mixer.music.play()
        _record_audio_start()
        return _playback_pump(env, cancel_event=cancel)
    except Exception as e:
        print(f"Cached playback error: {e}")
        return False
    finally:
        _set_done_speaking()
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass


def _clean_tts_text(text: str) -> str:
    return clean_speech(text)


EDGE_VOICE = os.getenv("EDGE_VOICE", "ru-RU-DmitryNeural")
EDGE_RATE = os.getenv("EDGE_RATE", "-5%")
EDGE_PITCH = os.getenv("EDGE_PITCH", "-10Hz")
VOICE_STYLE = os.getenv("JARVIS_VOICE_STYLE", "lively").lower()


def _edge_voice_key():
    return f"{EDGE_VOICE}:{EDGE_RATE}:{EDGE_PITCH}:{VOICE_STYLE}"


def _edge_options(text):
    """Conservative prosody, not unsupported Edge SSML emotion tags."""
    if VOICE_STYLE == "neutral":
        return {"rate": EDGE_RATE, "pitch": EDGE_PITCH}
    rate, pitch = int(EDGE_RATE[:-1]), int(EDGE_PITCH[:-2])
    if VOICE_STYLE == "lively":
        rate += 5
        pitch += 5
        if re.search(r"не удалось|ошибк|проблем|сожал", text, re.I):
            rate -= 5
            pitch -= 4
    elif VOICE_STYLE == "calm":
        rate -= 2
        pitch -= 2
    return {"rate": f"{max(-30, min(30, rate)):+d}%",
            "pitch": f"{max(-30, min(30, pitch)):+d}Hz"}


def _run_edge_tts_sync(text: str, output: str) -> bool:
    """Run edge-tts in its own event loop (Windows-safe, works from any thread)."""
    text = _clean_tts_text(text)
    if not text:
        return False
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        communicate = edge_tts.Communicate(text, EDGE_VOICE, **_edge_options(text))
        loop.run_until_complete(asyncio.wait_for(
            communicate.save(output), timeout=_TTS_NETWORK_TIMEOUT))
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
            communicate = edge_tts.Communicate(text, EDGE_VOICE, **_edge_options(text))
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()

        data = loop.run_until_complete(asyncio.wait_for(
            _collect(), timeout=_TTS_NETWORK_TIMEOUT))
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
_piper_lock = threading.RLock()


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
    with _piper_lock:
        if _piper_voice is not None or _piper_tried:
            return _piper_voice
        try:
            from piper import PiperVoice
            _piper_voice = PiperVoice.load(str(PIPER_MODEL_PATH))
            print("Piper local TTS loaded (offline, fast).")
        except Exception as e:
            print(f"Piper load error: {e}")
            _piper_voice = None
        finally:
            _piper_tried = True
        return _piper_voice


def _piper_syn_config(text=""):
    """A modest delivery adjustment; Piper has no dedicated emotion model."""
    try:
        from piper import SynthesisConfig
        delivery = {"lively": 0.94, "calm": 1.03}.get(VOICE_STYLE, 1.0)
        if VOICE_STYLE == "lively" and re.search(r"не удалось|ошибк|проблем|сожал", text, re.I):
            delivery = 1.03
        return SynthesisConfig(
            length_scale=PIPER_LENGTH_SCALE * delivery,
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
        with _piper_lock:
            chunks = list(voice.synthesize(text, syn_config=_piper_syn_config(text)))
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


@_settings_activity
def tts_to_bytes(text: str, engine: str = None):
    """Unified TTS: return (audio_bytes, suffix) using the configured engine.

    TTS_ENGINE=edge  → Microsoft cloud voice (DmitryNeural, high quality)
    TTS_ENGINE=piper → local neural (fast, offline)
    """
    text = _clean_tts_text(text)
    if not text or not any(ch.isalnum() for ch in text):
        return None, None
    _t0 = time.perf_counter()
    engine = _effective_tts_engine() if engine is None else engine
    import jarvis_dashboard as dashboard
    dashboard.service("tts", engine=engine, status="working")
    if engine == "piper" and _piper_available():
        data = _piper_to_wav_bytes(text)
        if data:
            dashboard.service("tts", engine=engine, status="ready")
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:piper] {_state.last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".wav"
    if engine == "edge" and edge_tts is not None:
        data = _edge_tts_to_bytes(text)
        if data:
            dashboard.service("tts", engine=engine, status="ready")
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            jarvis_logger.debug(f"[TTS:edge] {_state.last_tts_ms:.0f} ms: {text[:50]!r}")
            return data, ".mp3"
        jarvis_logger.warning(f"[TTS:edge] FAILED (сеть?): {text[:60]!r}")
    if engine == "xtts":
        data = _xtts_to_wav_bytes(text)
        if data:
            dashboard.service("tts", engine=engine, status="ready")
            _state.last_tts_ms = (time.perf_counter() - _t0) * 1000.0
            return data, ".wav"
    jarvis_logger.error(f"[TTS] все движки отказали: {text[:60]!r}")
    dashboard.service("tts", engine=engine, status="error", detail="Озвучивание недоступно; ответ остаётся в диалоге")
    return None, None


def _xtts_voice_key():
    reference = JARVIS_DIR / "jarvis_sample.wav"
    return f"xtts-v2:{hashlib.sha256(reference.read_bytes()).hexdigest()}:{XTTS_LANGUAGE}:{XTTS_SPEED}"


def _xtts_to_wav_bytes(text: str):
    """Natural-speed reference voice; cache speaker conditioning, no ffmpeg hop."""
    global _xtts_conditioning, _xtts_reference_key
    reference_audio = JARVIS_DIR / "jarvis_sample.wav"
    if not reference_audio.exists():
        print(f"WARNING: {reference_audio} not found for cloning.")
        return None
    try:
        import io
        import wave
        import numpy as np
        import torch
        import torchaudio.functional
        import soundfile as sf
        from unittest.mock import patch
        import TTS.tts.models.xtts as xtts_module

        def load_reference(path, rate):
            data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
            samples = torch.from_numpy(data.mean(axis=1)).unsqueeze(0)
            if sample_rate != rate:
                samples = torchaudio.functional.resample(samples, sample_rate, rate)
            return samples.clamp(-1, 1)

        with _xtts_lock:
            _load_xtts_if_needed()
            if tts is None:
                return None
            model = tts.synthesizer.tts_model
            reference_key = _xtts_voice_key()
            if _xtts_conditioning is None or reference_key != _xtts_reference_key:
                # Adapt only XTTS's file reader; leave global torchaudio/STT intact.
                with patch.object(xtts_module, "load_audio", load_reference):
                    _xtts_conditioning = model.get_conditioning_latents(audio_path=[str(reference_audio)])
                _xtts_reference_key = reference_key
            with torch.inference_mode():
                output = model.inference(_clean_tts_text(text), XTTS_LANGUAGE, *_xtts_conditioning,
                                         speed=XTTS_SPEED, temperature=0.65, enable_text_splitting=True)
            samples = np.asarray(output["wav"], dtype=np.float32)
            if not samples.size or not np.isfinite(samples).all():
                return None
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes((samples.clip(-1, 1) * 32767).astype("<i2").tobytes())
            return buf.getvalue()
    except Exception as e:
        print(f"XTTS generation error: {e}")
        return None


def generate_speech(text: str) -> bool:
    """Compatibility file API; use exactly the configured voice."""
    data, suffix = tts_to_bytes(text)
    if not data:
        return False
    Path(f"temp_jarvis_speech{suffix}").write_bytes(data)
    return True


def _audio_envelope(data, suffix):
    """Use decoded PCM, never a synthetic oscillation posing as audio energy."""
    if suffix == ".wav":
        return _wav_envelope(data)
    try:
        import io
        import wave
        sound = pygame.mixer.Sound(file=io.BytesIO(data))
        rate, format_bits, channels = pygame.mixer.get_init()
        if format_bits != -16:
            return None
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(sound.get_raw())
        return _wav_envelope(output.getvalue())
    except Exception:
        return None  # A state animation is OK; fabricated sound levels are not.


def _play_audio_bytes(data: bytes, suffix: str = ".mp3", cancel_event=None) -> bool:
    """Play from memory; keep the stream alive until the mixer unloads it."""
    import io
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return False
    stream = io.BytesIO(data)
    try:
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        env = _audio_envelope(data, suffix)
        pygame.mixer.music.load(stream)
        if cancel.is_set():
            return False
        pygame.mixer.music.play()
        _record_audio_start()
        return _playback_pump(env, cancel_event=cancel)
    except Exception as e:
        print(f"Playback error: {e}")
        return False
    finally:
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
        stream.close()


# озвучиваем ответ вслух
@_settings_activity
def speak(text: str, cancel_event=None):
    """Speak using the chosen voice. A new command owns resetting interruption."""
    cancel = _state.PipelineCancellation(cancel_event)
    response = text if isinstance(text, Response) else None
    notification = getattr(_speech_context, "notification", False)
    # A stopped project can still return useful evidence. Display/archive it,
    # but never revive cancelled speech. Other cancelled replies stay silent.
    if response and response.report:
        ui_msg("jarvis", response.display_text)
        if not notification:
            remember_report(response)
    if cancel.is_set():
        return
    engine = _effective_tts_engine()
    written = str(text or "").strip()
    text = _clean_tts_text(response.speech if response else written)
    _state.last_spoken_text = text
    if not notification:
        _state.last_response_text = written
    print(f"Jarvis: {text}")
    jarvis_logger.info(f"[SPEAK] {text!r}")

    ui_state("speaking")
    if not (response and response.report) and written != "Секунду, обрабатываю, сэр.":
        ui_msg("jarvis", response.display_text if response else written)

    try:
        if not text or not any(ch.isalnum() for ch in text):
            return
        if len(text) > 600:
            chunks = SpeechChunks()
            def parts():
                yield from chunks.feed(text)
                yield from chunks.finish()
            speak_streaming(parts(), cancel_event=cancel)
            if not notification:
                _state.last_response_text = written
            return
        cached = _TTS_INSTANT_CACHE.get(text.strip())
        if cached and os.path.exists(cached):
            jarvis_logger.debug("[SPEAK] → instant cache")
            completed = _play_cached_file(cached, cancel_event=cancel)
            import jarvis_dashboard as dashboard
            dashboard.service("tts", engine=engine, status="ready" if completed else "error",
                              detail="" if completed else "Воспроизведение не завершено")
            return
        _mark_speaking()
        ui_sub("синтезирую голос…")
        data, suffix = tts_to_bytes(text, engine=engine)
        if data and not cancel.is_set():
            ui_sub("говорю…")
            _play_audio_bytes(data, suffix, cancel_event=cancel)
    finally:
        _set_done_speaking()


def speak_notification(text: str):
    """Start a new timer/reminder utterance without clearing global interruption.

    An old barge-in cannot suppress this notification, but a subsequent set()
    (even if the global flag is still true) cancels it. Callers serialize speech
    as before; this helper does not arbitrate concurrent mixer ownership.
    Ordinary response continuations must keep using speak()/their response token.
    """
    previous = getattr(_speech_context, "notification", False)
    _speech_context.notification = True
    try:
        return speak(text, cancel_event=_state.PipelineCancellation.for_notification())
    finally:
        _speech_context.notification = previous


@_settings_activity
def speak_streaming(sentences_iter, cancel_event=None):
    """Streaming TTS pipeline: generate + play sentences concurrently.

    Each run latches cancellation independently of later global interrupt resets.
    Queue waits are interruptible; errors and completion have a separate channel
    which cannot be blocked by a full audio queue. The producer closes its own
    iterator. Arbitrary blocking next()/native synthesis cannot be killed; after
    cancellation their eventual result is discarded, never played or enqueued.
    """
    cancel = _state.PipelineCancellation(cancel_event)
    if cancel.is_set():
        return
    engine = _effective_tts_engine()  # Freeze auto for the whole response.
    ui_state("speaking")
    jarvis_logger.info("[SPEAK:stream] start")
    audio_queue: queue.Queue = queue.Queue(maxsize=3)
    terminal = queue.Queue(maxsize=1)
    producer_done = threading.Event()
    spoken_parts = []

    @_settings_activity
    def producer():
        iterator = None
        error = None
        fences = SpeechFences()
        try:
            iterator = iter(sentences_iter)
            while not cancel.is_set():
                try:
                    sentence = next(iterator)
                except StopIteration:
                    break
                if cancel.is_set():
                    break
                sentence = _clean_tts_text(fences.feed(sentence + " "))
                if not sentence or not re.search(r'[A-Za-zА-Яа-яЁё0-9]', sentence):
                    continue
                data, suffix = tts_to_bytes(sentence, engine=engine)
                if not data:
                    jarvis_logger.error(f"[SPEAK:stream] TTS отказал, фраза пропущена: {sentence[:60]!r}")
                    continue
                while not cancel.is_set():
                    try:
                        audio_queue.put((sentence, data, suffix), timeout=_PIPELINE_POLL)
                        break
                    except queue.Full:
                        continue
        except BaseException as exc:
            error = exc
        finally:
            try:
                close = getattr(iterator, "close", None)
                if close:
                    close()
            except BaseException as exc:
                error = error or exc
            finally:
                terminal.put_nowait(("error" if error is not None else "end", error))
                producer_done.set()

    prod_thread = threading.Thread(target=producer, name="jarvis-tts", daemon=True)
    try:
        _mark_speaking()
        ui_sub("синтезирую голос…")
        prod_thread.start()
        waiting_since = time.monotonic()
        while not cancel.is_set():
            try:
                item = audio_queue.get(timeout=_PIPELINE_POLL)
            except queue.Empty:
                if producer_done.is_set():
                    # get() may time out just before the final put + done. Once
                    # done is observed no more audio can arrive; drain that last
                    # item before consuming the terminal status (including errors).
                    try:
                        item = audio_queue.get_nowait()
                    except queue.Empty:
                        kind, payload = terminal.get_nowait()
                        if kind == "error":
                            raise payload
                        break
                else:
                    if time.monotonic() - waiting_since >= _TTS_QUEUE_TIMEOUT:
                        raise TimeoutError("TTS producer не выдал аудио в пределах таймаута")
                    continue
            if cancel.is_set():
                break
            sentence, data, suffix = item
            ui_sub("говорю…")
            completed = _play_audio_bytes(data, suffix, cancel_event=cancel)
            if not completed:
                break
            spoken_parts.append(sentence)
            waiting_since = time.monotonic()
    finally:
        cancel.set()
        if prod_thread.ident is not None:
            prod_thread.join(timeout=0.2)
            if prod_thread.is_alive():
                jarvis_logger.warning("[SPEAK:stream] отменено; ожидается выход из native/iterator вызова")
        while True:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break
        if spoken_parts:
            _state.last_spoken_text = " ".join(spoken_parts)
            if not getattr(_speech_context, "notification", False):
                _state.last_response_text = _state.last_spoken_text
        _set_done_speaking()
        jarvis_logger.info("[SPEAK:stream] done")
