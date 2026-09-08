"""Opt-in first-playback check: synthetic text, local LLM, virtual audio sink.

No microphone, personal memory, UI startup, shell tools, or speaker playback.
Edge receives only the public synthetic phrases below. Cache generation writes
normal application voice-cache files. Not a microphone-to-ear measurement.
"""
import os
os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["JARVIS_OVERLAY"] = "off"
import sys
import time
from urllib.parse import urlparse
from unittest.mock import patch


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import jarvis
    import jarvis_llm as llm
    import jarvis_tts as tts
    import jarvis_state as state
    from jarvis_speech_chunks import CAPABILITY_REPLY
    if urlparse(llm.LM_STUDIO_URL).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("Only loopback LM Studio is permitted")
    # Materialize the new profile's help cache, including on the first install.
    started = time.perf_counter()
    with patch.object(tts, "INSTANT_PHRASES", [CAPABILITY_REPLY]):
        tts.prewarm_tts_cache()
    print(f"HELP cache preparation: {time.perf_counter()-started:.2f}s", flush=True)
    assert CAPABILITY_REPLY in tts._TTS_INSTANT_CACHE, "Help cache not generated"
    real_pump = tts._playback_pump
    # Playback is real SDL decoding on a virtual sink, stopped immediately at
    # mixer start. This is NOT a claim about physical speaker/acoustic latency.
    def end_playback(*args, **kwargs):
        tts.pygame.mixer.music.stop()
        state.interrupt_event.set()
        return False
    with patch.object(tts, "_playback_pump", side_effect=end_playback), \
            patch.object(jarvis, "load_memory", return_value={}), \
            patch.object(jarvis, "SESSION_MEMORY", False), \
            patch.object(jarvis, "log_interaction"), \
            patch.object(jarvis, "conversation_history", []), \
            patch.object(llm, "LLM_ENGINE", "lmstudio"), \
            patch.object(llm, "OPENROUTER_API_KEY", None), \
            patch.object(llm, "_ollama_deltas", side_effect=AssertionError("No fallback in this measurement")):
        for prompt in ["Что ты умеешь?", "Объясни кратко, почему небо голубое.",
                       "Объясни кратко, почему трава зелёная."]:
            state.interrupt_event.clear()
            state.last_llm_ttft_ms = 0
            jarvis.process_with_llm_streaming(prompt)
            print(f"FIRST PLAYBACK: {state.last_audio_start_ms:.0f} ms; "
                  f"MODEL TTFT: {state.last_llm_ttft_ms:.0f} ms; {prompt}", flush=True)
            assert state.last_audio_start_ms > 0, "No playback occurred"
        # One complete answer with real playback timing on the silent SDL sink.
        # Verify that producing and playing overlap without dropping fragments.
        state.interrupt_event.clear()
        with patch.object(tts, '_playback_pump', wraps=real_pump) as player:
            answer = jarvis.process_with_llm_streaming(
                'Ответь одним коротким предложением: почему листья зелёные?')
        assert ' '.join(state.last_spoken_text.split()) == ' '.join(answer.split()), 'Spoken fragments differ from reply'
        print(f'FULL VIRTUAL PLAYBACK: {player.call_count} fragments, no words lost', flush=True)
    state.interrupt_event.clear()


if __name__ == "__main__":
    main()
