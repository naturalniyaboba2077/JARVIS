/* DOM-only component checks. No browser navigation, resource loading or real bridge. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(__dirname, 'jarvis.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function fixture(t, overrides = {}, offline = false) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, calls = [], intervals = [];
  t.after(() => w.close());
  w.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
  w.fetch = () => { throw Error('Network forbidden'); };
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const data = {
    ready: true, state: 'idle', pending: null, timers: [], activities: [], services: {}, compact: false,
    configured: { llm: 'local', stt: 'whisper', tts: 'piper', cloud_key_set: false },
    microphone: { enabled: true, ready: true, error: '' },
  };
  const defaults = {
    runtime_status: async () => data, audio_levels: async () => ({ input: 0, output: 0 }),
    get_settings: async () => ({ JARVIS_LLM: 'lmstudio', TTS_ENGINE: 'auto', SESSION_MEMORY: 'off' }),
    list_microphones: async () => [{ index: 1, name: '<b>USB fixture</b>' }],
    set_compact_mode: async enabled => ({ ok: true, compact: enabled }),
    set_microphone_enabled: async enabled => ({ ok: true, enabled }),
    send_command: async text => { w.jvAddMsg('user', text); return true; },
    save_settings: async () => ({ ok: true, message: 'Настройки сохранены' }),
    confirm_send: async () => ({ ok: true }), cancel_timer: async () => ({ ok: true }),
    confirm_project: async () => ({ ok: true }),
    preview_file: async () => ({ ok: true, title: 'fixture.md', text: '<script>unsafe()</script>' }),
    preview_change: async () => ({ ok: true, title: 'Изменения', text: '-before\n+after' }),
    undo_change: async () => ({ ok: true }), stop: async () => ({ ok: true }),
    listen_once: async () => ({ ok: true }), test_voice: async () => ({ ok: true }),
    diagnostics: async () => ({ summary: 'Синтетическая диагностика', version: 'fixture' }),
    telegram_status: async () => ({ message: 'Не подключён' }),
    telegram_send_code: async () => ({ message: 'Код запрошен' }),
    telegram_sign_in: async () => ({ message: 'Подключён', authorized: true }),
    minimize: async () => true, maximize: async () => true, restore: async () => true, close: async () => true,
  };
  const bridge = Object.fromEntries(Object.entries({ ...defaults, ...overrides }).map(([name, fn]) => [name, (...args) => {
    calls.push({ name, args }); return fn(...args);
  }]));
  if (!offline) w.pywebview = { api: bridge };
  w.eval(script);
  return { w, data, calls, $: id => w.document.getElementById(id),
    poll: async () => { await settle(); await intervals.find(i => i.ms === 1000).fn(); await settle(); },
    audio: async () => { await settle(); await intervals.find(i => i.ms === 100).fn(); await settle(); } };
}

test('startup keeps draft and gates commands until the backend is ready', async t => {
  const f = fixture(t); f.data.ready = false;
  f.data.microphone.ready = false;
  f.data.startup = { message: 'Подключаю голос и инструменты', elapsed_seconds: 2, error: false };
  await settle();
  assert.equal(f.$('stLabel').textContent, 'Запускаюсь');
  assert.equal(f.$('taskCaption').textContent, f.data.startup.message);
  assert.equal(f.$('taskStatus').dataset.state, 'thinking');
  assert.equal(f.$('taskTime').textContent, '00:02');
  f.$('cmd').value = 'Моя команда';
  f.$('composer').requestSubmit(); await settle();
  assert.equal(f.$('cmd').value, 'Моя команда');
  assert.equal(f.calls.filter(c => c.name === 'send_command').length, 0);
  f.$('tbMax').click(); await settle();
  assert.equal(f.$('tbMax').disabled, false);
  assert.equal(f.calls.filter(c => c.name === 'maximize').length, 1);
  f.$('tbMax').click(); await settle();
  assert.equal(f.calls.filter(c => c.name === 'restore').length, 1);
  f.data.ready = true; await f.poll();
  assert.equal(f.$('sendBtn').disabled, false);
  assert.equal(f.$('cmd').value, 'Моя команда');
  assert.equal(f.calls.filter(c => c.name === 'send_command').length, 0); // never auto-replay
  f.$('composer').requestSubmit(); await settle();
  assert.equal(f.calls.filter(c => c.name === 'send_command').length, 1);
});

test('startup error is honest, static, and keeps controls blocked', async t => {
  const f = fixture(t); f.data.ready = false; f.data.state = 'thinking';
  f.data.startup = { error: true, message: 'Не удалось запустить помощника', elapsed_seconds: 5 };
  await settle();
  assert.equal(f.$('stLabel').textContent, 'Не удалось запуститься');
  assert.equal(f.$('taskStatus').dataset.state, 'offline');
  assert.equal(f.$('taskTime').textContent, '');
  assert.equal(f.$('sendBtn').disabled, true);
  assert.equal(f.$('connection').dataset.status, 'error');
});

test('settings opened during startup load only after readiness', async t => {
  const f = fixture(t); f.data.ready = false; await settle();
  f.$('tbSettings').click(); await settle();
  assert.equal(f.calls.filter(c => c.name === 'get_settings').length, 0);
  assert.equal(f.$('saveSettings').disabled, true);
  f.data.ready = true; await f.poll();
  assert.equal(f.calls.filter(c => c.name === 'get_settings').length, 1);
  assert.equal(f.$('saveSettings').disabled, false);
});

test('live settings status updates without reloading or losing another draft', async t => {
  const f = fixture(t); await settle();
  f.$('tbSettings').click(); await settle();
  f.data.settings = { state: 'pending', message: 'Сохранено. Дождусь ответа.', revision: 1 };
  await f.poll();
  assert.equal(f.$('settingsNote').textContent, f.data.settings.message);
  assert.equal(f.$('voiceTest').disabled, true);
  assert.equal(f.$('telegramCodeBtn').disabled, true);
  f.data.settings = { state: 'applied', message: 'Настройки применены без перезапуска.', revision: 1 };
  await f.poll();
  assert.equal(f.$('settingsNote').textContent, f.data.settings.message);
  assert.equal(f.$('voiceTest').disabled, false);
  const field = f.w.document.querySelector('[data-key="EDGE_RATE"]');
  field.value = '+7%'; field.dispatchEvent(new f.w.Event('change'));
  const draftNote = f.$('settingsNote').textContent;
  f.data.settings = { state: 'pending', message: 'Применяю другую настройку', revision: 2 };
  await f.poll();
  assert.equal(field.value, '+7%');
  assert.equal(f.$('settingsNote').textContent, draftNote);
  assert.equal(f.$('settingsDirty').hidden, false);
});

test('settings edits made during save RPC remain dirty', async t => {
  let resolve;
  const f = fixture(t, { save_settings: () => new Promise(r => { resolve = r; }) });
  await settle(); f.$('tbSettings').click(); await settle();
  const field = f.w.document.querySelector('[data-key="EDGE_RATE"]');
  field.value = '+7%'; field.dispatchEvent(new f.w.Event('change'));
  f.$('saveSettings').click(); await settle();
  field.value = '+8%'; field.dispatchEvent(new f.w.Event('change'));
  resolve({ ok: true, state: 'pending', message: 'Сохранено' }); await settle();
  assert.equal(field.value, '+8%');
  assert.equal(f.$('settingsDirty').hidden, false);
  assert.equal(f.calls.filter(c => c.name === 'save_settings')[0].args[0].EDGE_RATE, '+7%');
});

test('apply error is visible and supports retry without closing the app', async t => {
  const f = fixture(t); await settle(); f.$('tbSettings').click(); await settle();
  f.data.settings = { state: 'error', message: 'Не удалось открыть микрофон. Нажмите Применить повторно.', revision: 1 };
  await f.poll();
  assert.equal(f.$('settingsNote').textContent, f.data.settings.message);
  assert.equal(f.$('settingsNote').classList.contains('error'), true);
  assert.equal(f.$('saveSettings').disabled, false);
  assert.equal(f.$('saveSettings').textContent, 'Применить');
});

test('environment overrides are shown as locked fields without exposing secrets', async t => {
  const f = fixture(t, { get_settings: async () => ({ EDGE_RATE: '-5%', _OVERRIDDEN_KEYS: ['EDGE_RATE', 'OPENROUTER_API_KEY'] }) });
  await settle(); f.$('tbSettings').click(); await settle();
  assert.equal(f.w.document.querySelector('[data-key="EDGE_RATE"]').disabled, true);
  const secret = f.w.document.querySelector('[data-key="OPENROUTER_API_KEY"]');
  assert.equal(secret.disabled, true); assert.equal(secret.value, '');
});

test('task shimmer follows real phases, not invented progress', async t => {
  const f = fixture(t); await settle();
  assert.equal(f.$('taskStatus').dataset.state, 'idle');
  assert.equal(f.$('taskTime').textContent, '');
  f.data.state = 'thinking';
  f.data.phase = { state: 'thinking', text: 'Читаю файл <script>test</script>', elapsed_seconds: 12 };
  await f.poll();
  assert.equal(f.$('taskStatus').dataset.state, 'thinking');
  assert.equal(f.$('taskCaption').textContent, f.data.phase.text);
  assert.equal(f.$('taskCaption').querySelector('script'), null);
  assert.equal(f.$('taskTime').textContent, '00:12');
  f.data.state = 'idle'; f.data.phase = { state: 'idle', text: '', elapsed_seconds: 0 };
  await f.poll(); assert.equal(f.$('taskStatus').dataset.state, 'idle');
  assert.equal(f.$('taskTime').textContent, '');
  assert.doesNotMatch(f.$('taskCaption').textContent, /Читаю файл|успешно|выполнено/);
});

test('task status receives pushed phases immediately', async t => {
  const f = fixture(t); await settle();
  f.w.jvSetState('thinking'); f.w.jvSetSub('Ищу проект в разрешённых папках…');
  assert.equal(f.$('taskCaption').textContent, 'Ищу проект в разрешённых папках…');
  assert.equal(f.$('taskStatus').dataset.state, 'thinking');
  f.w.jvSetState('speaking'); f.w.jvSetSub('синтезирую голос…');
  assert.equal(f.$('taskCaption').textContent, 'синтезирую голос…');
});

test('confirmation and connection loss stop the task shimmer', async t => {
  const f = fixture(t); await settle();
  f.data.state = 'thinking'; f.data.pending = { kind: 'email', id: 'p1', recipient: 'fixture',
    body: 'Fixture', expires_at: Date.now() / 1000 + 60 };
  await f.poll(); assert.equal(f.$('taskStatus').dataset.state, 'confirmation');
  assert.equal(f.$('taskTime').textContent, '');
  f.w.pywebview.api.runtime_status = async () => { throw Error('offline fixture'); };
  await f.poll(); assert.equal(f.$('taskStatus').dataset.state, 'offline');
  assert.match(f.$('taskCaption').textContent, /Нет связи/);
});

test('settings group related controls and track unsaved changes without saving on close', async t => {
  const f = fixture(t); await settle(); f.$('tbSettings').click(); await settle();
  assert.equal(f.$('settings-general').querySelectorAll('fieldset.settings-block').length, 3);
  assert.equal(f.$('settingsShade').hidden, false);
  assert.equal(f.$('settingsDirty').hidden, true);
  const control = f.w.document.querySelector('[data-key="JARVIS_FOLLOWUP_WINDOW"]');
  control.value = '55'; control.dispatchEvent(new f.w.Event('input', { bubbles: true }));
  assert.equal(f.$('settingsDirty').hidden, false);
  f.$('settingsClose').click(); assert.equal(f.$('settingsShade').hidden, true);
  assert.equal(f.calls.filter(c => c.name === 'save_settings').length, 0);
  f.$('tbSettings').click(); await settle(); assert.equal(control.value, '55');
  f.$('saveSettings').click(); await settle(); assert.equal(f.$('settingsDirty').hidden, true);
});

test('follow-up hint uses backend deadline and disappears on expiry or mute', async t => {
  const f = fixture(t); await settle();
  f.data.followup = { mode: 'smart', seconds: 60, remaining_seconds: 59 };
  await f.poll(); assert.match(f.$('stSub').textContent, /59 с/);
  f.data.microphone.enabled = false;
  await f.poll(); assert.doesNotMatch(f.$('stSub').textContent, /59 с/);
  f.data.microphone.enabled = true; f.data.followup.remaining_seconds = 0;
  await f.poll(); assert.match(f.$('stSub').textContent, /Джарвис/);
  assert.doesNotMatch(f.w.document.body.textContent, /Чарльз|Charles/i);
  assert.equal(f.calls.filter(c => c.name === 'send_command').length, 0);
});

test('follow-up controls are accessible in basic settings', async t => {
  const f = fixture(t); await settle();
  for (const key of ['JARVIS_FOLLOWUP_MODE', 'JARVIS_FOLLOWUP_WINDOW']) {
    const field = f.w.document.querySelector('[data-key="' + key + '"]');
    assert.ok(field.closest('[data-settings-group="general"]') || field.closest('#settings-general'));
  }
  const select = f.w.document.querySelector('[data-key="JARVIS_FOLLOWUP_MODE"]');
  assert.ok([...select.options].some(o => o.value === 'smart' && o.textContent === 'С проверкой обращения'));
});

test('offline preview never fabricates success or runs commands', async t => {
  const f = fixture(t, {}, true);
  await settle();
  assert.equal(f.$('sendBtn').disabled, true);
  assert.equal(f.$('modeToggle').disabled, true);
  f.$('cmd').value = 'открой браузер';
  f.$('composer').dispatchEvent(new f.w.Event('submit', { cancelable: true }));
  assert.equal(f.calls.length, 0);
  assert.equal(f.w.document.querySelectorAll('.msg').length, 0);
});

test('settings grouped into three labelled tabs; all controls have labels', async t => {
  const f = fixture(t); await settle();
  assert.equal(f.w.document.querySelectorAll('[role="tab"]').length, 3);
  assert.equal(f.$('settings-general').querySelectorAll('[data-key]').length, 8);
  assert.equal(f.$('settings-advanced').querySelectorAll('details.advanced-group').length, 4);
  for (const field of f.w.document.querySelectorAll('.field input,.field select')) {
    assert.ok(f.w.document.querySelector(`label[for="${field.id}"]`), field.id);
  }
  assert.equal(f.$('settingsPanel').hasAttribute('inert'), true);
  f.$('tbSettings').click(); await settle();
  assert.equal(f.$('settingsPanel').inert, false);
  assert.equal(f.w.document.querySelector('[data-key="JARVIS_LLM"]').value, 'lmstudio');
  assert.equal(f.w.document.querySelector('[data-key="TTS_ENGINE"]').value, 'auto');
  assert.equal(f.w.document.querySelector('[data-key="SESSION_MEMORY"]').value, 'off');
  assert.equal(f.$('micSelect').querySelectorAll('b').length, 0);
  f.$('settingsClose').click(); assert.equal(f.$('settingsPanel').inert, true);
});

test('tab keyboard navigation selects the next settings section', async t => {
  const f = fixture(t); await settle(); f.$('tbSettings').click(); await settle();
  f.$('tab-general').focus();
  f.$('settingsNav').dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(f.$('tab-integrations').getAttribute('aria-selected'), 'true');
  assert.equal(f.$('settings-general').hidden, true);
});

test('model text and code fences remain text, not HTML', async t => {
  const f = fixture(t); await settle();
  f.w.jvAddMsg('jarvis', '<img src=x onerror=alert(1)>\n```html\n<script>unsafe()</script>```');
  assert.equal(f.$('log').querySelectorAll('img,script').length, 0);
  assert.equal(f.$('log').querySelectorAll('pre').length, 1);
  assert.ok(f.$('log').textContent.includes('<script>unsafe()</script>'));
});

test('text command is submitted once and shown once by the bridge', async t => {
  const f = fixture(t); await settle(); f.$('cmd').value = 'который час';
  f.$('composer').dispatchEvent(new f.w.Event('submit', { cancelable: true })); await settle();
  assert.equal(f.calls.filter(c => c.name === 'send_command').length, 1);
  assert.equal(f.w.document.querySelectorAll('.msg.user').length, 1);
  assert.equal(f.$('cmd').value, '');
});

test('polling does not re-enable a command button while RPC is in flight', async t => {
  let release;
  const f = fixture(t, { send_command: () => new Promise(resolve => { release = resolve; }) });
  await settle(); f.$('cmd').value = 'fixture';
  f.$('composer').dispatchEvent(new f.w.Event('submit', { cancelable: true }));
  await f.poll(); assert.equal(f.$('sendBtn').disabled, true);
  release(true); await settle(); assert.equal(f.$('sendBtn').disabled, false);
});

test('Enter submits, Shift+Enter preserves a multiline draft', async t => {
  const f = fixture(t); await settle();
  f.$('cmd').value = 'fixture';
  f.$('cmd').dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, cancelable: true }));
  await settle(); assert.equal(f.calls.filter(c => c.name === 'send_command').length, 0);
  f.$('cmd').dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Enter', cancelable: true }));
  await settle(); assert.equal(f.calls.filter(c => c.name === 'send_command').length, 1);
});

test('compact mode resizes through bridge and restores full dialog', async t => {
  const f = fixture(t); await settle(); f.$('modeToggle').click(); await settle();
  assert.ok(f.$('jvRoot').classList.contains('compact'));
  f.$('modeToggle').click(); await settle();
  assert.equal(f.$('jvRoot').classList.contains('compact'), false);
  assert.deepEqual(f.calls.filter(c => c.name === 'set_compact_mode').map(c => c.args[0]), [true, false]);
});

test('pending send displays full recipient and body, then submits opaque ID only', async t => {
  const f = fixture(t); await settle();
  f.data.pending = { id: 'fixture-id', kind: 'email', recipient: 'fixture@example.invalid',
    subject: 'Полная тема', body: '<b>Полный текст</b>'.repeat(50), expires_at: Date.now() / 1000 + 120 };
  await f.poll();
  assert.equal(f.$('confirmation').hidden, false);
  assert.equal(f.$('confirmBody').textContent, f.data.pending.body);
  assert.equal(f.$('confirmBody').querySelectorAll('b').length, 0);
  f.$('confirmSend').click(); f.$('confirmSend').click(); await settle(); await f.poll();
  assert.equal(f.calls.filter(c => c.name === 'confirm_send').length, 1);
  assert.deepEqual(f.calls.find(c => c.name === 'confirm_send').args, ['email', 'fixture-id', true]);
  assert.equal(f.$('confirmSend').disabled, true);
  f.$('confirmCancel').click(); await settle();
  assert.deepEqual(f.calls.filter(c => c.name === 'confirm_send')[1].args, ['email', 'fixture-id', false]);
});

test('replacement confirmation resets button state and uses new request ID', async t => {
  const f = fixture(t); await settle();
  f.data.pending = { id: 'old', kind: 'telegram', recipient: 'Fixture', body: 'one', expires_at: Date.now() / 1000 + 120 };
  await f.poll(); f.$('confirmSend').click(); await settle();
  f.data.pending = { ...f.data.pending, id: 'new', body: 'two' }; await f.poll();
  assert.equal(f.$('confirmSend').disabled, false);
  f.$('confirmCancel').click(); await settle();
  assert.deepEqual(f.calls.filter(c => c.name === 'confirm_send').at(-1).args, ['telegram', 'new', false]);
});

test('expired confirmation disables sending', async t => {
  const f = fixture(t); await settle();
  f.data.pending = { id: 'expired', kind: 'email', recipient: 'Fixture', body: 'fixture', expires_at: 1 };
  await f.poll(); assert.equal(f.$('confirmSend').disabled, true);
  assert.match(f.$('confirmMeta').textContent, /истёк/);
});

function projectQuestion(extra = {}) {
  return { id: 'project-question', kind: 'project', query: 'Пример', mode: 'inspect',
    task: 'Проверь проект Пример', expires_at: Date.now() / 1000 + 120,
    choices: [{ id: 'choice-one', name: 'Пример <img src=x>', path: 'C:/fixtures/Пример' }], ...extra };
}

test('project question shows target, full task, scope, and yes/no buttons', async t => {
  const f = fixture(t); f.data.pending = projectQuestion(); await settle();
  assert.equal(f.$('confirmTitle').textContent, 'Это тот проект?');
  assert.equal(f.$('confirmation').getAttribute('aria-label'), 'Выбор проекта');
  assert.match(f.$('projectChoices').textContent, /C:\/fixtures\/Пример/);
  assert.equal(f.$('projectChoices').querySelector('img'), null);
  assert.match(f.$('confirmBody').textContent, /Проверь проект Пример/);
  assert.equal(f.$('confirmSubject').textContent, 'Проверка без изменений файлов');
  assert.equal(f.$('confirmSend').textContent, 'Да, этот проект');
  assert.equal(f.$('confirmCancel').textContent, 'Не тот');
  assert.match(f.$('stSub').textContent, /проект/);
  assert.doesNotMatch(f.$('taskCaption').textContent, /отправк/);
  f.$('confirmSend').click(); f.$('confirmSend').click(); await settle();
  assert.deepEqual(f.calls.filter(c => c.name === 'confirm_project').map(c => c.args),
    [['project-question', 'choice-one', true]]);
  assert.equal(f.calls.filter(c => c.name === 'confirm_send' || c.name === 'send_command').length, 0);
  assert.equal(f.$('confirmSend').disabled, true);
  assert.equal(f.$('confirmCancel').disabled, false);
});

test('project rejection remains available after queued approval', async t => {
  const f = fixture(t); f.data.pending = projectQuestion(); await settle();
  f.$('confirmSend').click(); await settle(); f.$('confirmCancel').click(); await settle();
  assert.deepEqual(f.calls.filter(c => c.name === 'confirm_project').at(-1).args, ['project-question', null, false]);
});

test('multiple project choices are explicit and cannot submit generic yes', async t => {
  const f = fixture(t); f.data.pending = projectQuestion({ choices: [
    { id: 'a', name: 'Пример', path: 'C:/fixtures/one/Пример' },
    { id: 'b', name: 'Пример', path: 'C:/fixtures/two/Пример' }] });
  await settle();
  assert.equal(f.$('confirmSend').hidden, true);
  assert.equal(f.$('confirmCancel').textContent, 'Ни один');
  const buttons = f.$('projectChoices').querySelectorAll('button');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[1].getAttribute('aria-label'), 'Выбрать проект 2: Пример');
  buttons[1].click(); buttons[0].click(); await settle();
  assert.deepEqual(f.calls.filter(c => c.name === 'confirm_project').map(c => c.args), [['project-question', 'b', true]]);
  assert.equal(buttons[0].disabled, true);
  assert.equal(buttons[1].disabled, true);
});

test('project polling preserves choice node and keyboard focus', async t => {
  const f = fixture(t); f.data.pending = projectQuestion({ choices: [
    { id: 'a', name: 'Первый', path: 'C:/fixtures/one' }, { id: 'b', name: 'Второй', path: 'C:/fixtures/two' }] });
  await settle();
  const button = f.$('projectChoices').querySelector('button'); button.focus();
  await f.poll();
  assert.equal(f.$('projectChoices').querySelector('button'), button);
  assert.equal(f.w.document.activeElement, button);
});

test('expired project question disables all choice controls', async t => {
  const f = fixture(t); f.data.pending = projectQuestion({ expires_at: 1 }); await settle();
  assert.equal(f.$('confirmSend').disabled, true);
  assert.equal(f.$('confirmCancel').disabled, true);
  assert.match(f.$('confirmMeta').textContent, /Повторите поручение/);
  f.$('confirmSend').click(); await settle();
  assert.equal(f.calls.filter(c => c.name === 'confirm_project').length, 0);
});

test('project rejection replaces question and late approval response cannot disable new choice', async t => {
  let finish;
  const f = fixture(t, { confirm_project: () => new Promise(resolve => { finish = resolve; }) });
  f.data.pending = projectQuestion(); await settle(); f.$('confirmSend').click(); await settle();
  f.data.pending = projectQuestion({ id: 'replacement' }); await f.poll();
  finish({ ok: true }); await settle();
  assert.equal(f.$('confirmSend').disabled, false);
  f.$('confirmSend').click(); await settle();
  assert.equal(f.calls.filter(c => c.name === 'confirm_project').at(-1).args[0], 'replacement');
  finish({ ok: true }); await settle();
});

test('project transport failure never silently retries an uncertain approval', async t => {
  const f = fixture(t, { confirm_project: async () => { throw Error('synthetic disconnect'); } });
  f.data.pending = projectQuestion(); await settle(); f.$('confirmSend').click(); await settle();
  assert.match(f.$('notice').textContent, /synthetic disconnect/);
  assert.equal(f.$('confirmSend').disabled, true);
  assert.equal(f.$('confirmCancel').disabled, false);
  await f.poll();
  assert.equal(f.calls.filter(c => c.name === 'confirm_project').length, 1);
});

test('project validation error allows a corrected choice without losing the draft', async t => {
  const f = fixture(t, { confirm_project: async () => ({ ok: false, message: 'Выберите проект заново' }) });
  f.data.pending = projectQuestion(); await settle(); f.$('cmd').value = 'мой черновик';
  f.$('confirmSend').click(); await settle();
  assert.match(f.$('notice').textContent, /заново/);
  assert.equal(f.$('confirmSend').disabled, false);
  assert.equal(f.$('cmd').value, 'мой черновик');
});

test('typed project answer includes the visible question ID', async t => {
  const f = fixture(t); f.data.pending = projectQuestion(); await settle();
  f.$('cmd').value = 'это тот проект';
  f.$('composer').dispatchEvent(new f.w.Event('submit', { cancelable: true })); await settle();
  assert.deepEqual(f.calls.find(c => c.name === 'send_command').args, ['это тот проект', 'project-question']);
});

test('project question switches cleanly back to a send confirmation', async t => {
  const f = fixture(t); f.data.pending = projectQuestion(); await settle();
  f.data.pending = { id: 'email-replacement', kind: 'email', recipient: 'fixture@example.invalid',
    subject: 'Fixture', body: 'Full body', expires_at: Date.now() / 1000 + 120 };
  await f.poll();
  assert.equal(f.$('projectChoices').hidden, true);
  assert.equal(f.$('projectChoices').children.length, 0);
  assert.equal(f.$('confirmSend').textContent, 'Отправить');
  assert.equal(f.$('confirmSend').hidden, false);
  f.$('confirmSend').click(); await settle();
  assert.deepEqual(f.calls.find(c => c.name === 'confirm_send').args, ['email', 'email-replacement', true]);
  assert.equal(f.calls.filter(c => c.name === 'confirm_project').length, 0);
});

test('project question expands compact view and indicates modifications', async t => {
  const f = fixture(t); await settle(); f.$('modeToggle').click(); await settle();
  f.data.pending = projectQuestion({ mode: 'modify', task: 'Исправь проект Пример: добавь тесты' });
  await f.poll();
  assert.equal(f.$('jvRoot').classList.contains('compact'), false);
  assert.match(f.$('confirmSubject').textContent, /Изменения/);
  assert.match(f.$('confirmBody').textContent, /добавь тесты/);
});

test('pending send automatically expands compact mode for review', async t => {
  const f = fixture(t); await settle(); f.$('modeToggle').click(); await settle();
  f.data.pending = { id: 'p', kind: 'email', recipient: 'Fixture', body: 'Full text', expires_at: Date.now() / 1000 + 120 };
  await f.poll(); assert.equal(f.$('jvRoot').classList.contains('compact'), false);
});

test('microphone pause uses existing stream gate and shows honest state', async t => {
  const f = fixture(t); await settle(); f.$('micToggle').click(); await settle();
  assert.deepEqual(f.calls.find(c => c.name === 'set_microphone_enabled').args, [false]);
  assert.match(f.$('stSub').textContent, /паузе/);
  assert.match(f.$('micTestStatus').textContent, /устройство остаётся открытым/);
});

test('core amplitude comes from telemetry and stays still on silence', async t => {
  const f = fixture(t, { audio_levels: async () => ({ input: .62, output: 0, output_available: false }) });
  await settle(); f.w.jvSetState('listening');
  await f.audio(); assert.equal(f.$('core').style.getPropertyValue('--level'), '0.62');
});

test('file cards preview escaped text and queue only their own undo ID', async t => {
  const f = fixture(t); await settle();
  f.data.activities = [{ id: 'file-fixture', kind: 'file', title: 'Изменена заметка', filename: 'fixture.md',
    detail: 'fixture', at: 1, project: 'fixture-project', seq: 1 }];
  await f.poll(); const buttons = f.$('activityList').querySelectorAll('button');
  buttons[0].click(); await settle(); assert.equal(f.$('preview').open, true);
  assert.equal(f.$('previewText').querySelectorAll('script').length, 0);
  f.$('previewClose').click(); buttons[1].click(); await settle();
  assert.match(f.$('previewText').textContent, /-before/);
  f.$('previewClose').click(); buttons[2].click(); await settle();
  assert.deepEqual(f.calls.find(c => c.name === 'undo_change').args, ['file-fixture']);
});

test('timer countdown changes without replacing focused controls', async t => {
  const f = fixture(t); await settle();
  f.data.timers = [{ id: 'timer', label: 'Чай', status: 'running', remaining: 62 }];
  await f.poll(); const button = f.$('timers').querySelector('button'); button.focus();
  f.data.timers[0].remaining = 61; await f.poll();
  assert.equal(f.$('timers').querySelector('button'), button);
  assert.equal(f.$('timers').querySelector('.timer-value').textContent, '01:01');
  button.click(); await settle(); assert.deepEqual(f.calls.find(c => c.name === 'cancel_timer').args, ['timer']);
});

test('settings keep API secrets write-only and omit empty replacements', async t => {
  const f = fixture(t); await settle(); f.$('tbSettings').click(); await settle();
  f.$('saveSettings').click(); await settle();
  const data = f.calls.find(c => c.name === 'save_settings').args[0];
  assert.equal(Object.hasOwn(data, 'OPENROUTER_API_KEY'), false);
  assert.equal(Object.hasOwn(data, 'TELEGRAM_API_HASH'), false);
});

test('idle UI makes no audio RPC calls, speaking resumes telemetry', async t => {
  const f = fixture(t); await settle();
  for (let i = 0; i < 10; i++) await f.audio();
  assert.equal(f.calls.filter(c => c.name === 'audio_levels').length, 0);
  f.w.jvSetState('speaking'); await f.audio();
  assert.equal(f.calls.filter(c => c.name === 'audio_levels').length, 1);
  f.w.jvSetState('idle'); f.$('tbSettings').click(); await settle(); await f.audio();
  assert.equal(f.calls.filter(c => c.name === 'audio_levels').length, 2);
});

test('unchanged status does not replace the state heading', async t => {
  const f = fixture(t); await settle();
  const heading = f.$('stLabel').firstChild, subtitle = f.$('stSub').firstChild;
  await f.poll(); await f.poll();
  assert.equal(f.$('stLabel').firstChild, heading);
  assert.equal(f.$('stSub').firstChild, subtitle);
});

test('settings save only changed values, including the voice profile', async t => {
  const f = fixture(t); await settle(); f.$('tbSettings').click(); await settle();
  f.w.document.querySelector('[data-key="JARVIS_VOICE_STYLE"]').value = 'calm';
  f.$('saveSettings').click(); await settle();
  assert.deepEqual(Object.entries(f.calls.find(c => c.name === 'save_settings').args[0]), [['JARVIS_VOICE_STYLE', 'calm']]);
  f.$('saveSettings').click(); await settle();
  assert.equal(Object.keys(f.calls.filter(c => c.name === 'save_settings').at(-1).args[0]).length, 0);
});

test('settings cannot overwrite config before it has loaded', async t => {
  let release;
  const f = fixture(t, { get_settings: () => new Promise(resolve => { release = resolve; }) });
  await settle(); f.$('tbSettings').click(); await settle(); await f.poll();
  assert.equal(f.$('saveSettings').disabled, true);
  f.$('saveSettings').click();
  assert.equal(f.calls.filter(c => c.name === 'save_settings').length, 0);
  release({ JARVIS_LLM: 'lmstudio' }); await settle();
  assert.equal(f.$('saveSettings').disabled, false);
});

test('streaming reply updates one safe message and preserves the draft focus', async t => {
  const f = fixture(t); await settle(); f.$('cmd').focus(); f.$('cmd').value = 'draft';
  f.w.jvStream('1', 'Первые слова', false);
  const node = f.$('log').querySelector('.msg');
  assert.equal(node.getAttribute('aria-busy'), 'true');
  f.w.jvStream('1', 'Первые слова <script>bad()</script>', true);
  assert.equal(f.$('log').querySelectorAll('.msg').length, 1);
  assert.equal(f.$('log').querySelector('.msg'), node);
  assert.equal(node.getAttribute('aria-busy'), 'false');
  assert.equal(node.querySelectorAll('script').length, 0);
  assert.equal(f.w.document.activeElement, f.$('cmd'));
  assert.equal(f.$('cmd').value, 'draft');
});

test('audio animation changes transform, not per-frame height', () => {
  const css = fs.readFileSync(path.join(__dirname, 'jarvis.css'), 'utf8');
  assert.match(css, /transform: scaleY\(calc\(/);
  assert.doesNotMatch(css, /height: calc\(5px \+ var\(--level\)/);
});

test('finished stream renders code without speaking markup or interpreting HTML', async t => {
  const f = fixture(t); await settle();
  f.w.jvStream('report', 'Пример кода', false);
  f.w.jvStream('report', 'Пример\n```python\nx = "# **literal** <script>"\n```\nКонец.', true);
  const messages = f.$('log').querySelectorAll('.msg');
  assert.equal(messages.length, 1);
  assert.equal(messages[0].querySelector('pre').textContent, 'x = "# **literal** <script>"\n');
  assert.equal(messages[0].querySelectorAll('script').length, 0);
  assert.ok(!messages[0].textContent.includes('```'));
});

test('bridge failure keeps draft and reports an error without success prose', async t => {
  const f = fixture(t, { send_command: async () => { throw Error('Синтетическая ошибка'); } });
  await settle(); f.$('cmd').value = 'fixture';
  f.$('composer').dispatchEvent(new f.w.Event('submit', { cancelable: true })); await settle();
  assert.equal(f.$('cmd').value, 'fixture'); assert.match(f.$('notice').textContent, /ошибка/);
});

test('configured model does not claim a successful backend call', async t => {
  const f = fixture(t); await settle();
  assert.match(f.$('connection').textContent, /Режим:/);
  assert.match(f.$('engineStatus').textContent, /не использовались/);
  f.data.services.llm = { engine: 'lmstudio', model: 'fixture', status: 'ready' }; await f.poll();
  assert.match(f.$('connection').textContent, /Локальная модель/);
});

test('theme has readable text contrast and reduced-motion handling', () => {
  function luminance(hex) {
    const rgb = hex.match(/\w\w/g).map(v => parseInt(v, 16) / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
    return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
  }
  for (const [text, bg] of [['e8edf0', '101316'], ['a2aeb7', '171b1f'], ['13212a', '8cc8e8'], ['edc27b', '242119']]) {
    const values = [luminance(text), luminance(bg)].sort((a, b) => b - a);
    assert.ok((values[0] + .05) / (values[1] + .05) >= 4.5, `${text}/${bg}`);
  }
  assert.match(fs.readFileSync(path.join(__dirname, 'jarvis.css'), 'utf8'), /prefers-reduced-motion/);
});
