"""Small initial speech units, then longer phrases; never split inside a word.

The caller decides whether streaming is authorized (no tool actions). The full
model reply is kept separately and never reconstructed from spoken fragments.
"""
import re
import time

from jarvis_actions import needs_action_buffer

CAPABILITY_REPLY = (
    "Помогу с компьютером и вашими проектами, сэр. "
    "Открываю приложения, ищу информацию, веду задачи и напоминания. "
    "Могу проверить код, подготовить письмо или сообщение. Для почты и Telegram нужно подключение.")


def capability_reply(text):
    t = re.sub(r"\s+", " ", (text or "").casefold()).strip(" .!?«»")
    return CAPABILITY_REPLY if t in {"что ты умеешь", "что ты можешь", "твои возможности", "расскажи о своих возможностях"} else None


def can_stream_reply(text):
    # Plain explanations need no tool schemas. Keep action/negation/hypothetical
    # vocabulary conservative even after removing the explanatory leading verb.
    t = re.sub(r"^(?:(?:расскажи|объясни|почему|зачем|что\s+(?:такое|значит|означает))[,\s:]+)+",
               "", (text or "").strip(), flags=re.I)
    return not needs_action_buffer(t)


class SpeechChunks:
    def __init__(self, clock=time.monotonic):
        self.buffer = ""
        self.first = True
        self.clock = clock
        self.started = None

    def feed(self, delta):
        if not self.buffer:
            self.started = self.clock()
            delta = delta.lstrip()
        self.buffer += delta
        result = []
        while self.buffer:
            # A punctuation boundary is useful immediately, including a comma
            # inside a long capabilities list. Decimal numbers are not split.
            marks = r"[.!?;:,]" if self.first else r"[.!?]"
            boundary = re.search(marks + r"(?:[»\"])?(?=\s)", self.buffer)
            spaces = list(re.finditer(r"\s+", self.buffer))
            words = 3 if self.first else 14
            age = self.clock() - self.started
            cut = boundary.end() if boundary else 0
            if len(spaces) >= words:
                limit = spaces[words - 1].start()
                cut = min(cut, limit) if cut else limit
            elif age >= (0.18 if self.first else 0.8) and spaces:
                cut = cut or spaces[-1].start()
            if not cut or not re.search(r"\w", self.buffer[:cut]):
                break
            result.append(self.buffer[:cut].strip())
            self.buffer = self.buffer[cut:].lstrip()
            self.first = False
            self.started = self.clock()
        return result

    def finish(self):
        tail, self.buffer = self.buffer.strip(), ""
        return [tail] if tail else []
