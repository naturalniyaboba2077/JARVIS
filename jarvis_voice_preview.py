"""Render a synthetic voice preview without microphone access or playback.

python -B jarvis_voice_preview.py --engine edge
The result is an approximation, not a recording of the original character.
Edge sends this fixed, non-personal sentence to Microsoft's speech service.
"""
import argparse
import io
from pathlib import Path
import time


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("edge", "piper", "xtts"), default="edge")
    args = parser.parse_args()
    import soundfile as sf
    import jarvis_tts as tts
    phrase = "Добрый вечер, сэр. Я на связи. Проверю указанный проект и доложу о результате."
    started = time.perf_counter()
    data, suffix = tts.tts_to_bytes(phrase, engine=args.engine)
    if not data or suffix not in {".wav", ".mp3"}:
        raise SystemExit("Не удалось синтезировать образец; голос не проверен")
    with sf.SoundFile(io.BytesIO(data)) as audio:
        duration = len(audio) / audio.samplerate
        if not 1 < duration < 45:
            raise SystemExit("Некорректная длительность образца")
    output = Path(__file__).resolve().parent / "voice_samples" / f"jarvis-{args.engine}-preview{suffix}"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(data)
    print(f"Синтезированный образец: {output}\nАудио: {duration:.1f} с; холодный синтез: {time.perf_counter()-started:.1f} с")


if __name__ == "__main__":
    main()
