"""Application-owned post-write checks. Fixed argv, no model-authored shell.

Behavior tests execute project code with user privileges in modify mode; this is
NOT a sandbox. inspect never calls this runner. Benchmark injects its own trusted
AST verifier and does not execute generated project code.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from jarvis_agent_context import clip_utf8
from jarvis_fileops import checked_path, read_project_bytes
from jarvis_project_checks import iter_files

_RUNNER_SLOT = threading.BoundedSemaphore(1)


def discover_tests(root, cancel, *, seconds=1.5):
    """Freeze pre-existing test inputs, never trust a command from README/JSON."""
    deadline = time.monotonic() + seconds
    files, frozen, notes = [], {}, []
    total = 0
    python_styles = set()
    try:
        for path in iter_files(root):
            if cancel.is_set() or time.monotonic() >= deadline or len(files) >= 128:
                notes.append('Поиск тестов ограничен временем/объёмом; набор может быть неполным.')
                break
            relative = path.relative_to(root).as_posix()
            is_python = path.name.startswith('test_') and path.suffix == '.py'
            is_node = path.name.endswith(('.test.js', '.test.mjs', '.test.cjs'))
            config = path.name in {'conftest.py', 'pytest.ini', 'pyproject.toml', 'setup.cfg', 'package.json'}
            if not (is_python or is_node or config):
                continue
            raw = read_project_bytes(root, path, max_bytes=min(128 * 1024, 2 * 1024 * 1024 - total))
            total += len(raw)
            frozen[relative] = hashlib.sha256(raw).hexdigest()
            if not (is_python or is_node):
                continue
            files.append(relative)
            text = raw.decode('utf-8', errors='replace')
            if is_python:
                python_styles.add('unittest' if re.search(r'\b(?:import|from)\s+unittest\b', text) else 'pytest')
    except (ValueError, OSError) as exc:
        notes.append('Поиск тестов неполный: ' + type(exc).__name__)
    runners = []
    pyfiles = [p for p in files if p.endswith('.py')]
    if pyfiles:
        if python_styles == {'unittest'}:
            # Discover each known parent explicitly (including non-package dirs).
            for parent in sorted({str(Path(p).parent) for p in pyfiles}):
                runners.append({'kind': 'unittest', 'argv': [sys.executable, '-B', '-m', 'unittest',
                                'discover', '-s', parent, '-p', 'test_*.py']})
        else:
            runners.append({'kind': 'pytest', 'argv': [sys.executable, '-B', '-m', 'pytest',
                            '-q', '-p', 'no:cacheprovider', '--', *sorted(pyfiles)]})
    jsfiles = sorted(p for p in files if not p.endswith('.py'))
    if jsfiles:
        node = shutil.which('node')
        if node:
            runners.append({'kind': 'node-test', 'argv': [node, '--test', '--test-reporter=tap',
                            *[str(checked_path(root, p)) for p in jsfiles]]})
        else:
            notes.append('Найдены Node-тесты, но node недоступен.')
    if not files:
        notes.append('Тесты не найдены в поддерживаемом формате (test_*.py / *.test.js,mjs,cjs).')
    return {'runners': runners[:8], 'frozen': frozen, 'notes': notes,
            'complete': not notes and len(runners) <= 8, 'test_files': files}


def _terminate(proc):
    try:
        if os.name == 'nt':
            # Only the PID just spawned by this runner, never an image-name kill.
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True,
                           timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if proc.poll() is None:
            proc.kill()


def run_argv(root, argv, cancel, deadline):
    """Bounded output and interruptible wait. Does not install dependencies."""
    if cancel.is_set() or time.monotonic() >= deadline:
        return {'exit': None, 'output': 'Проверка прервана до запуска.', 'stopped': True}
    if not _RUNNER_SLOT.acquire(blocking=False):
        return {'exit': None, 'output': 'Предыдущая проверка ещё удерживает поток вывода.', 'stopped': True}
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('OPENROUTER_', 'TELEGRAM_', 'JARVIS_', 'LM_STUDIO_', 'OLLAMA_')):
            env.pop(key)
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    try:
        proc = subprocess.Popen(argv, cwd=checked_path(root, '.'), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **flags)
    except Exception:
        _RUNNER_SLOT.release()
        raise
    data, overflow = bytearray(), threading.Event()
    def read():
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                left = max(0, 128 * 1024 - len(data))
                data.extend(chunk[:left])
                if len(chunk) > left:
                    overflow.set()
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.wait()  # EOF alone must not release a still-running check.
            finally:
                _RUNNER_SLOT.release()
    reader = threading.Thread(target=read, daemon=True, name='jarvis-check-output')
    try:
        reader.start()
    except Exception:
        _terminate(proc)
        proc.stdout.close()
        _RUNNER_SLOT.release()
        raise
    stopped = False
    try:
        while proc.poll() is None:
            if cancel.is_set() or time.monotonic() >= deadline or overflow.is_set():
                stopped = True
                _terminate(proc)
                break
            time.sleep(0.05)
        proc.wait(timeout=3)
    finally:
        if proc.poll() is None:
            _terminate(proc)
        reader.join(timeout=1)
        if not reader.is_alive():
            proc.stdout.close()
        else:
            stopped = True  # Slot remains owned until the outstanding reader exits.
    text = data.decode('utf-8', errors='replace')
    stopped = stopped or cancel.is_set() or overflow.is_set()
    if stopped:
        text += '\nПроверка остановлена: отмена, лимит времени или вывода.'
    if len(text.encode('utf-8')) > 5000:
        text = clip_utf8(text, 2400) + '\n[конец вывода]\n' + text.encode('utf-8')[-2400:].decode('utf-8', errors='ignore')
    return {'exit': proc.returncode, 'output': text, 'stopped': stopped}


def test_count(kind, output):
    pattern = {'unittest': r'Ran\s+(\d+)\s+tests?', 'pytest': r'(\d+)\s+passed',
               'node-test': r'#\s*pass\s+(\d+)'}[kind]
    matches = re.findall(pattern, output)
    count = int(matches[-1]) if matches else 0
    if kind == 'unittest':
        skipped = re.findall(r'\bskipped=(\d+)', output)
        count -= int(skipped[-1]) if skipped else 0
    return max(0, count)


def verify_project(root, changed, plan, cancel, deadline):
    checks, notes = [], list(plan['notes'])
    deadline = min(deadline, time.monotonic() + 25)
    try:
        snapshots = {}
        for relative in sorted(set(changed)):
            if cancel.is_set() or time.monotonic() >= deadline:
                return {'status': 'interrupted', 'checks': checks, 'notes': ['Проверка прервана/истёк лимит.']}
            raw = read_project_bytes(root, relative, max_bytes=8 * 1024 * 1024)
            snapshots[relative] = hashlib.sha256(raw).hexdigest()
            kind = Path(relative).suffix.casefold()
            try:
                if kind == '.py':
                    compile(raw, relative, 'exec', dont_inherit=True)
                elif kind == '.json':
                    json.loads(raw)
                elif kind in {'.js', '.cjs', '.mjs'} and shutil.which('node'):
                    result = run_argv(root, [shutil.which('node'), '--check', str(checked_path(root, relative))], cancel, deadline)
                    checks.append({**result, 'kind': 'syntax'})
                    if result['stopped']:
                        return {'status': 'interrupted', 'checks': checks, 'notes': notes}
                    continue
                else:
                    notes.append('Автопроверка синтаксиса не поддерживает: ' + relative)
                    continue
                checks.append({'kind': 'syntax', 'exit': 0, 'output': relative + ': синтаксис корректен, без исполнения.'})
            except (SyntaxError, ValueError, RecursionError) as exc:
                checks.append({'kind': 'syntax', 'exit': 1, 'output': relative + ': ' + str(exc)})
        if any(c['exit'] != 0 for c in checks):
            return {'status': 'failed', 'checks': checks, 'notes': notes}
        for relative, expected in plan['frozen'].items():
            if hashlib.sha256(read_project_bytes(root, relative, max_bytes=128 * 1024)).hexdigest() != expected:
                return {'status': 'unavailable', 'checks': checks,
                        'notes': notes + ['Тесты/конфигурация изменены после начала задачи: нужна независимая проверка.']}
        if plan['runners']:
            current = discover_tests(root, cancel)
            if current['frozen'] != plan['frozen'] or not current['complete']:
                return {'status': 'unavailable', 'checks': checks, 'notes': notes +
                        ['Набор тестов/конфигурация изменились или не подтверждены: нужна независимая проверка.']}
        for test in plan['runners']:
            if cancel.is_set() or time.monotonic() >= deadline:
                return {'status': 'interrupted', 'checks': checks, 'notes': notes + ['Проверка прервана/истёк лимит.']}
            result = run_argv(root, test['argv'], cancel, min(deadline, time.monotonic() + 15))
            count = test_count(test['kind'], result['output'])
            checks.append({**result, 'kind': 'behavior', 'runner': test['kind'], 'tests': count})
            if result['stopped'] or result['exit'] != 0 or count == 0:
                return {'status': 'failed', 'checks': checks, 'notes': notes +
                        (['Нулевой exit без подтверждённого числа тестов не считается успехом.'] if count == 0 else [])}
        for relative, digest in {**plan['frozen'], **snapshots}.items():
            if hashlib.sha256(read_project_bytes(root, relative, max_bytes=8 * 1024 * 1024)).hexdigest() != digest:
                return {'status': 'failed', 'checks': checks, 'notes': notes + ['Файлы изменились во время проверки: ' + relative]}
        if plan['runners']:
            current = discover_tests(root, cancel)
            if current['frozen'] != plan['frozen'] or not current['complete']:
                return {'status': 'failed', 'checks': checks, 'notes': notes + ['Набор тестов изменился во время проверки.']}
        passed = bool(plan['runners'] and plan['complete'] and not notes)
        return {'status': 'passed' if passed else 'unavailable', 'checks': checks, 'notes': notes}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {'status': 'failed', 'checks': checks, 'notes': notes + ['Проверка не завершена: ' + type(exc).__name__]}
