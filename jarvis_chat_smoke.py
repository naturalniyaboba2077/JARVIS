"""Opt-in local-model dialogue check. Synthetic memory, no audio or tools.

python -B jarvis_chat_smoke.py
Calls only the configured loopback LM Studio model; no fallback or model reload.
Printed samples need human review: an assertion cannot establish that a joke is funny.
"""
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlparse


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    import jarvis
    import jarvis_llm as llm
    import jarvis_chat_memory as chat
    if urlparse(llm.LM_STUDIO_URL).hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise SystemExit('Smoke-check accepts loopback LM Studio only')
    with tempfile.TemporaryDirectory(prefix='jarvis-chat-smoke-') as directory:
        fixture = chat.ChatMemory(Path(directory) / 'memory.sqlite3')
        with patch.object(chat, 'memory', fixture), patch.object(jarvis, 'SESSION_MEMORY', True), \
             patch.object(jarvis, 'load_memory', return_value={}), \
             patch.object(jarvis, 'get_obsidian_memory', return_value=''):
            outputs = []
            for question in ('Расскажи прикол', 'Это не смешно',
                             'Если ты будешь так выебываться, то сделаю из тебя помощника в колл-центре',
                             'Расскажи ещё один анекдот'):
                with fixture.turn(question, persist=True):
                    messages = jarvis._build_messages(question)
                    started = time.perf_counter()
                    response = ''.join(llm._lmstudio_deltas(messages, max_tokens=200, timeout=45))
                    assert response.strip(), 'Local model returned no answer'
                    fixture.capture(response)
                    outputs.append(response)
                    print(f'USER: {question}\nJARVIS ({time.perf_counter() - started:.2f}s): {response}\n', flush=True)
            assert len(set(outputs)) == len(outputs), 'Exact repeated answer'
            restored = chat.ChatMemory(fixture.path)
            assert len(restored.context('продолжим', persist=True)) == 8, 'Restart lost synthetic dialogue'
            print('PASS: four distinct replies, dialogue survives restart. No real conversation or tools used.')


if __name__ == '__main__':
    main()
