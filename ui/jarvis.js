/* Jarvis desktop UI. All model/tool text stays text; only the Python bridge acts. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var root = $('jvRoot'), state = 'idle', pending = null, status = null;
  var connected = false, compact = false, settingsLoaded = false, pendingBusy = null;
  var backendWasReady = false;
  var settingsStatusSignature = '';
  var settingsNoticeMessage = '';
  var activitySignature = '', timerNodes = new Map(), levels = { input: 0, output: 0 };
  var labels = { idle: 'Готов к команде', listening: 'Слушаю', thinking: 'Обрабатываю', speaking: 'Отвечаю' };
  var subtitles = { idle: 'Скажите «Джарвис» или напишите команду', listening: 'Можно говорить',
    thinking: 'Готовлю ответ', speaking: 'Ответ появится в диалоге' };
  var customSub = '', settingsTab = 'general', restoreFocus = null, settingsBaseline = {}, loadingSettings = false;
  var busyButtons = new WeakSet();
  var phaseStarted = Date.now();
  var reducedMotion = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  function api() { return (window.pywebview && window.pywebview.api) || null; }
  function ready() { return connected && status && status.ready; }
  function startupText() {
    return status && status.startup ? status.startup.message : 'Запускаю системы · можно набрать команду';
  }
  function notice(text, error) {
    $('notice').textContent = text || '';
    $('notice').hidden = !text;
    $('notice').classList.toggle('error', !!error);
    if ($('settingsPanel').classList.contains('open') && text) {
      $('settingsNote').textContent = text;
      $('settingsNote').classList.toggle('error', !!error);
    }
  }
  async function call(name) {
    var a = api(), args = Array.prototype.slice.call(arguments, 1);
    if (!a || typeof a[name] !== 'function') throw new Error('Нет связи с приложением. Команда не выполнена.');
    var timeout;
    try {
      return await Promise.race([Promise.resolve(a[name].apply(a, args)),
        new Promise(function (_, reject) { timeout = setTimeout(function () {
          reject(new Error('Приложение не ответило вовремя. Проверьте результат перед повтором.'));
        }, 15000); })]);
    } finally { clearTimeout(timeout); }
  }
  async function action(button, fn) {
    busyButtons.add(button);
    button.disabled = true;
    try {
      var result = await fn();
      if (result && result.ok === false) throw new Error(result.message || 'Действие не выполнено');
      if (result && result.message) notice(result.message);
      return result;
    } catch (error) { notice(error.message || String(error), true); return null; }
    finally {
      busyButtons.delete(button);
      var windowControl = ['tbMin', 'tbMax', 'tbClose'].includes(button.id);
      button.disabled = (windowControl ? !connected : !ready()) || button.id === 'saveSettings' && !settingsLoaded;
    }
  }
  function element(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function setState(value) {
    if (!labels[value]) return;
    state = value;
    customSub = '';
    phaseStarted = Date.now();
    renderState();
  }
  function setText(id, value) {
    if ($(id).textContent !== value) $(id).textContent = value;
  }
  function renderState() {
    var mic = status && status.microphone;
    var followup = status && status.followup;
    var idleHint = state === 'idle' && followup && followup.remaining_seconds > 0
      ? 'Можно без обращения ещё ' + followup.remaining_seconds + ' с' + (followup.mode === 'smart' ? ' · проверяю обращение' : '')
      : subtitles[state];
    var shownState = pending ? 'confirmation' : state;
    if ($('core').dataset.state !== shownState) $('core').dataset.state = shownState;
    setText('stLabel', pending ? 'Нужно подтверждение' : !connected ? 'Нет подключения'
      : status && !status.ready ? (status.startup && status.startup.error ? 'Не удалось запуститься' : 'Запускаюсь') : labels[state]);
    setText('stSub', pending ? (pending.kind === 'project' ? 'Выберите проект кнопкой или ответьте голосом' : 'Проверьте получателя и полный текст') : !connected
      ? 'Откройте Jarvis через приложение' : status && !status.ready ? startupText() : mic && !mic.ready ? 'Доступен текстовый ввод · микрофон недоступен'
        : mic && !mic.enabled ? 'Распознавание на паузе · текстовый ввод доступен' : customSub || idleHint);
    renderTaskStatus();
  }
  function renderTaskStatus() {
    var starting = connected && status && !status.ready;
    var startupFailed = starting && status.startup && status.startup.error;
    var busy = connected && !pending && !startupFailed && (starting || ['thinking', 'speaking', 'listening'].includes(state));
    var phase = status && status.phase;
    var caption = !connected ? 'Нет связи с приложением' : pending ? (pending.kind === 'project' ? 'Уточняю, какой проект вы имели в виду' : 'Ожидаю подтверждение отправки')
      : starting ? startupText() : busy ? customSub || {
        thinking: 'Обрабатываю запрос', speaking: 'Озвучиваю ответ', listening: 'Слушаю команду'
      }[state] : 'На связи · готов к следующей задаче';
    var shown = !connected || startupFailed ? 'offline' : pending ? 'confirmation' : starting ? 'thinking' : busy ? state : 'idle';
    if ($('taskStatus').dataset.state !== shown) $('taskStatus').dataset.state = shown;
    setText('taskCaption', caption);
    var elapsed = starting && status.startup ? Math.floor(status.startup.elapsed_seconds) : phase && phase.state === state && phase.text === customSub
      ? phase.elapsed_seconds : Math.floor((Date.now() - phaseStarted) / 1000);
    setText('taskTime', busy && elapsed > 0 ? duration(elapsed) : '');
    $('taskCaption').title = caption;
    root.dataset.state = shown;
  }
  function appendText(node, text) {
    node.appendChild(document.createTextNode(String(text)));
  }
  function renderMessageText(target, text) {
    // Fenced code is formatted without interpreting HTML, Markdown links or tags.
    var parts = String(text).split(/```[^\n]*\n([\s\S]*?)```/g);
    parts.forEach(function (part, index) {
      var node = element(index % 2 ? 'pre' : 'div', index % 2 ? '' : 'message-text');
      appendText(node, part);
      target.appendChild(node);
    });
  }
  function addMsg(who, text) {
    if (!text) return;
    $('emptyState').hidden = true;
    var log = $('log'), atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
    var d = element('article', 'msg ' + (who === 'user' ? 'user' : 'jarvis'));
    d.appendChild(element('div', 'who', who === 'user' ? 'Вы' : 'Jarvis'));
    renderMessageText(d, text);
    log.appendChild(d);
    while (log.querySelectorAll('.msg').length > 200) log.querySelector('.msg').remove();
    if (atBottom || who === 'user') log.scrollTop = log.scrollHeight;
    $('compactLine').textContent = String(text).slice(0, 600);
  }
  window.jvSetState = setState;
  window.jvSetSub = function (text) {
    if (customSub !== (text || '')) phaseStarted = Date.now();
    customSub = text || ''; renderState();
  };
  window.jvAddMsg = addMsg;
  var streamMessages = new Map();
  window.jvStream = function (id, text, done) {
    var record = streamMessages.get(id), log = $('log');
    var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
    if (!record) {
      if (!text) return;
      $('emptyState').hidden = true;
      var node = element('article', 'msg jarvis');
      node.appendChild(element('div', 'who', 'Jarvis'));
      var body = element('div', 'message-text'); node.appendChild(body); log.appendChild(node);
      record = { node: node, body: body }; streamMessages.set(id, record);
      while (log.querySelectorAll('.msg').length > 200) log.querySelector('.msg').remove();
      // Bound abandoned generations too (e.g. application interrupted mid-stream).
      while (streamMessages.size > 8) streamMessages.delete(streamMessages.keys().next().value);
    }
    record.node.setAttribute('aria-busy', String(!done));
    if (record.body.textContent !== text) record.body.textContent = text;
    if (done) {
      // Same safe code renderer as a non-streaming reply; no literal fences in
      // the final message, no HTML interpretation and no second message node.
      record.body.replaceChildren();
      renderMessageText(record.body, text);
      streamMessages.delete(id);
    }
    if (atBottom) log.scrollTop = log.scrollHeight;
    setText('compactLine', String(text).slice(0, 600));
  };
  window.jvLatency = function (stage, value) {
    var el = $('lat-' + stage); if (el) el.textContent = value;
  };
  window.jvClearLat = function () {
    ['stt', 'llm', 'tts', 'sum'].forEach(function (key) { if ($('lat-' + key)) $('lat-' + key).textContent = '—'; });
  };
  function diagnosticText(data) {
    var lines = [data.summary || 'Диагностика получена', '', 'Технические сведения:'];
    Object.keys(data).filter(function (key) { return key !== 'summary'; }).forEach(function (key) {
      lines.push(key + ': ' + String(data[key]));
    });
    $('diagBox').textContent = lines.join('\n');
    $('diagnosticsDetails').open = true;
  }
  window.jvDiagnostics = diagnosticText;

  function setConnected(value) {
    connected = value;
    ['sendBtn', 'micToggle', 'stopBtn', 'modeToggle', 'saveSettings', 'diagBtn',
      'telegramCodeBtn', 'telegramLoginBtn', 'telegramStatusBtn', 'listenTest', 'voiceTest'].forEach(function (id) {
      if ($(id)) $(id).disabled = !ready() || busyButtons.has($(id)) || id === 'saveSettings' && !settingsLoaded;
    });
    renderState();
  }
  async function mode(enabled) {
    var result = await call('set_compact_mode', enabled);
    if (!result.ok) throw new Error(result.message);
    compact = result.compact;
    root.classList.toggle('compact', compact);
    $('modeToggle').setAttribute('aria-pressed', String(compact));
    $('modeToggle').setAttribute('aria-label', compact ? 'Развернуть диалог' : 'Компактный режим');
    $('modeToggle').title = compact ? 'Развернуть диалог' : 'Компактный режим';
    return result;
  }
  $('modeToggle').onclick = function () { action(this, function () { return mode(!compact); }); };
  $('activityToggle').onclick = function () {
    var hidden = $('workspace').classList.toggle('activity-hidden');
    this.setAttribute('aria-expanded', String(!hidden));
  };
  $('micToggle').onclick = function () {
    var enabled = status ? status.microphone.enabled : true;
    action(this, async function () {
      var result = await call('set_microphone_enabled', !enabled);
      if (result.ok && status) { status.microphone.enabled = result.enabled; renderMic(); }
      return result;
    });
  };
  function renderMic() {
    var enabled = status.microphone.enabled, button = $('micToggle');
    button.classList.toggle('mic-muted', !enabled);
    button.setAttribute('aria-pressed', String(!enabled));
    button.setAttribute('aria-label', enabled ? 'Приостановить распознавание' : 'Возобновить распознавание');
    button.disabled = !ready() || !status.microphone.ready || busyButtons.has(button);
    $('micTestStatus').textContent = !status.microphone.ready ? status.microphone.error || 'Микрофон ещё не готов'
      : enabled ? 'Вход открыт. Индикатор сверху показывает уровень звука.'
        : 'Программная пауза: звук отбрасывается, устройство остаётся открытым.';
    renderState();
  }
  $('stopBtn').onclick = function () { action(this, function () { return call('stop'); }); };
  [['micToggle', 'Микрофон'], ['stopBtn', 'Стоп'], ['sendBtn', 'Отправить']].forEach(function (pair) {
    $(pair[0]).appendChild(element('span', 'button-caption', pair[1]));
  });
  $('composer').onsubmit = function (event) {
    event.preventDefault();
    var text = $('cmd').value.trim();
    if (!text || !connected || $('sendBtn').disabled) return;
    const projectQuestionId = pending?.kind === 'project' ? pending.id : null;
    action($('sendBtn'), async function () {
      const result = projectQuestionId === null ? await call('send_command', text)
        : await call('send_command', text, projectQuestionId);
      if (result === false) throw new Error('Команда не принята');
      if ($('cmd').value.trim() === text) { $('cmd').value = ''; $('cmd').style.height = ''; }
      $('cmd').focus();
    });
  };
  $('cmd').onkeydown = function (event) {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault(); $('composer').requestSubmit();
    }
  };
  $('cmd').oninput = function () { this.style.height = '38px'; this.style.height = Math.min(110, this.scrollHeight) + 'px'; };
  document.querySelectorAll('[data-prompt]').forEach(function (button) {
    button.onclick = function () { $('cmd').value = button.dataset.prompt; $('cmd').focus(); };
  });

  function renderPending(value) {
    const changed = pending?.id !== value?.id;
    if (changed) pendingBusy = null;
    pending = value;
    root.classList.toggle('choosing-project', pending?.kind === 'project');
    $('confirmation').hidden = !pending;
    if (!pending) { renderState(); return; }
    const project = pending.kind === 'project';
    const choices = project ? (pending.choices ?? []) : [];
    if (changed) {
      $('confirmation').dataset.kind = pending.kind;
      $('confirmation').setAttribute('aria-label', project ? 'Выбор проекта' : 'Подтверждение отправки');
      $('confirmTitle').textContent = project ? (choices.length === 1 ? 'Это тот проект?' : 'Какой проект вы имели в виду?')
        : pending.kind === 'email' ? 'Письмо готово к отправке' : 'Сообщение в Telegram';
      $('confirmRecipient').textContent = project ? 'Вы искали: ' + pending.query : 'Кому: ' + pending.recipient;
      $('confirmSubject').textContent = project ? (pending.mode === 'inspect' ? 'Проверка без изменений файлов' : 'Изменения по исходному поручению')
        : pending.subject ? 'Тема: ' + pending.subject : '';
      $('confirmBody').textContent = project ? 'Исходное поручение: ' + pending.task : pending.body;
      $('projectChoices').hidden = !project;
      $('projectChoices').replaceChildren();
      const requestId = pending.id;
      choices.forEach((choice, index) => {
        const card = element('div', 'project-choice');
        card.appendChild(element('strong', '', (index + 1) + '. ' + choice.name));
        card.appendChild(element('div', 'project-choice-path', choice.path));
        if (choices.length > 1) {
          const button = element('button', 'btn confirm-send', 'Выбрать этот проект');
          button.setAttribute('aria-label', 'Выбрать проект ' + (index + 1) + ': ' + choice.name);
          button.onclick = () => confirmProject(requestId, choice.id, true, button);
          card.appendChild(button);
        }
        $('projectChoices').appendChild(card);
      });
      $('projectAnswerHint').hidden = !project;
      $('projectAnswerHint').textContent = choices.length === 1
        ? 'Можно сказать: «это тот проект» или «не тот». После подтверждения продолжу поручение.'
        : 'Можно сказать номер: «первый», «второй»… Или «не тот», если ни один не подходит.';
      $('confirmSend').hidden = project && choices.length !== 1;
      $('confirmSend').textContent = project ? 'Да, этот проект' : 'Отправить';
      $('confirmCancel').textContent = project ? (choices.length > 1 ? 'Ни один' : 'Не тот') : 'Отменить';
      if (compact) action($('modeToggle'), function () { return mode(false); });
    }
    const seconds = Math.max(0, Math.ceil(pending.expires_at - Date.now() / 1000));
    $('confirmMeta').textContent = pendingBusy === pending.id ? 'Подтверждение принято. Ожидаю результат…'
      : seconds ? 'Подтверждение действует ещё ' + seconds + ' с' : project ? 'Срок истёк. Повторите поручение.' : 'Срок истёк. Отправка недоступна.';
    $('confirmSend').disabled = !ready() || !seconds || pendingBusy === pending.id;
    $('confirmCancel').disabled = !ready() || !seconds;
    $('projectChoices').querySelectorAll('button').forEach(button => {
      button.disabled = !ready() || !seconds || pendingBusy === pending.id;
    });
    renderState();
  }
  /** Send only opaque IDs. Never accept a browser-edited path/task as authority. */
  async function confirmProject(requestId, choiceId, approved, button) {
    if (!pending || pending.kind !== 'project' || pending.id !== requestId || !ready()
        || pending.expires_at <= Date.now() / 1000 || approved && pendingBusy === requestId) return;
    if (approved) pendingBusy = requestId;
    renderPending(pending);
    try {
      await action(button, async () => {
        const result = await call('confirm_project', requestId, choiceId, approved);
        if (result?.ok === false && pendingBusy === requestId) pendingBusy = null;
        // On uncertain transport failure, keep approval disabled: a delayed RPC
        // may have succeeded. Rejection stays available; replacement resets it.
        return result;
      });
    } finally { renderPending(pending); }
  }
  function confirm(approved) {
    var current = pending;
    if (!current || approved && pendingBusy === current.id) return;
    if (current.kind === 'project') {
      confirmProject(current.id, approved ? current.choices?.[0]?.id ?? null : null, approved,
        approved ? $('confirmSend') : $('confirmCancel'));
      return;
    }
    if (approved) pendingBusy = current.id;
    renderPending(current);
    action(approved ? $('confirmSend') : $('confirmCancel'), async function () {
      var result = await call('confirm_send', current.kind, current.id, approved);
      if (!result.ok && pendingBusy === current.id) pendingBusy = null;
      return result;
    }).then(function () { renderPending(pending); });
  }
  $('confirmSend').onclick = function () { confirm(true); };
  $('confirmCancel').onclick = function () { confirm(false); };

  function duration(seconds) {
    seconds = Math.max(0, Math.ceil(seconds));
    var hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60;
    return (hours ? String(hours).padStart(2, '0') + ':' : '') + String(minutes).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  }
  function renderTimers(timers) {
    var ids = new Set(timers.map(function (timer) { return timer.id; }));
    timerNodes.forEach(function (node, id) { if (!ids.has(id)) { node.remove(); timerNodes.delete(id); } });
    timers.forEach(function (timer) {
      var node = timerNodes.get(timer.id);
      if (!node) {
        node = element('article', 'activity-card');
        node.appendChild(element('h3', '', timer.label));
        node.appendChild(element('div', 'timer-value'));
        var button = element('button', 'text-btn', 'Отменить таймер');
        button.onclick = function () { action(button, function () { return call('cancel_timer', timer.id); }); };
        node.appendChild(button); $('timers').prepend(node); timerNodes.set(timer.id, node);
      }
      node.querySelector('.timer-value').textContent = timer.status === 'running' ? duration(timer.remaining)
        : timer.status === 'completed' ? 'Завершён' : 'Отменён';
      node.querySelector('button').hidden = timer.status !== 'running';
    });
  }
  async function preview(id, changes) {
    var result = await call(changes ? 'preview_change' : 'preview_file', id);
    if (!result.ok) throw new Error(result.message);
    $('previewTitle').textContent = result.title;
    $('previewText').textContent = result.text || '';
    if (result.truncated) $('previewText').appendChild(document.createTextNode('\n\n…Показаны первые 100 КБ.'));
    $('previewText').hidden = !!result.image;
    $('previewImage').hidden = !result.image;
    if (result.image && /^data:image\/(png|jpeg);base64,/.test(result.image)) $('previewImage').src = result.image;
    else $('previewImage').removeAttribute('src');
    $('preview').showModal(); $('previewClose').focus();
  }
  $('previewClose').onclick = function () { $('preview').close(); };
  function renderActivity(items) {
    var signature = JSON.stringify(items);
    if (signature === activitySignature) return;
    activitySignature = signature;
    $('activityList').replaceChildren();
    items.forEach(function (item) {
      var card = element('article', 'activity-card');
      card.appendChild(element('h3', '', item.title));
      card.appendChild(element('p', '', item.filename || item.detail));
      card.appendChild(element('span', 'card-time', new Date(item.at * 1000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })));
      if (item.kind === 'file') {
        var actions = element('div', 'card-actions'), open = element('button', 'text-btn', 'Открыть файл');
        open.onclick = function () { action(open, function () { return preview(item.id); }); };
        actions.appendChild(open);
        if (item.project && item.seq != null) {
          var diff = element('button', 'text-btn', 'Посмотреть правку');
          diff.onclick = function () { action(diff, function () { return preview(item.id, true); }); };
          actions.appendChild(diff);
          var undo = element('button', 'text-btn', 'Отменить правку');
          undo.onclick = function () { action(undo, function () { return call('undo_change', item.id); }); };
          actions.appendChild(undo);
        }
        card.appendChild(actions);
      }
      $('activityList').appendChild(card);
    });
  }
  function renderStatus(data) {
    if (typeof data.first_audio_ms === 'number') setText('lat-first', data.first_audio_ms > 0 ? (data.first_audio_ms / 1000).toFixed(2) + ' с' : '—');
    var becameReady = data.ready && !backendWasReady;
    backendWasReady = !!data.ready;
    status = data;
    setConnected(connected);
    if (becameReady && $('settingsPanel').classList.contains('open') && !settingsLoaded) loadSettings();
    renderMic(); renderPending(data.pending); renderTimers(data.timers || []); renderActivity(data.activities || []);
    $('activityEmpty').hidden = !!((data.activities || []).length || (data.timers || []).length);
    var llm = data.services.llm, tts = data.services.tts, failed = Object.values(data.services).some(function (s) { return s.status === 'error'; });
    $('connection').dataset.status = failed ? 'error' : 'ready';
    $('connection').textContent = llm ? (['local', 'lmstudio'].includes(llm.engine) ? 'Локальная модель' : 'Облачная модель') + ' · ' + llm.model
      : 'Режим: ' + ({ local: 'Ollama с резервом', lmstudio: 'LM Studio с резервом', cloud: 'облачный с резервом' }[data.configured.llm] || data.configured.llm);
    if (failed) $('connection').textContent += ' · есть ошибка';
    if (!data.ready) {
      $('connection').dataset.status = data.startup && data.startup.error ? 'error' : 'working';
      $('connection').textContent = startupText();
    }
    $('engineStatus').textContent = Object.keys(data.services).length ? Object.entries(data.services).map(function (entry) {
      var service = entry[1], name = { stt: 'Распознавание', tts: 'Голос', llm: 'Ответы' }[entry[0]] || entry[0];
      return name + ': ' + service.engine + (service.model ? ' / ' + service.model : '') + ' — '
        + ({ ready: 'последний вызов успешен', working: 'обработка', error: 'ошибка', unknown: 'не проверено' }[service.status] || service.status)
        + (service.detail ? '\n' + service.detail : '');
    }).join('\n\n') : 'Движки ещё не использовались в этой сессии. Настройка не означает доступность.';
    if (tts && tts.status === 'error') $('voiceTestStatus').textContent = tts.detail;
    if (data.settings) {
      var settingsStatus = data.settings;
      var signature = JSON.stringify(settingsStatus);
      if (signature !== settingsStatusSignature) {
        settingsStatusSignature = signature;
        var dirty = Object.keys(collectSettings()).length > 0;
        if (!dirty || settingsStatus.state === 'error') {
          $('settingsNote').textContent = settingsStatus.message + (dirty ? ' Есть также несохранённые изменения.' : '');
          $('settingsNote').classList.toggle('error', settingsStatus.state === 'error');
        }
        if (settingsNoticeMessage && $('notice').textContent === settingsNoticeMessage) {
          $('notice').textContent = settingsStatus.message;
          $('notice').classList.toggle('error', settingsStatus.state === 'error');
          settingsNoticeMessage = settingsStatus.message;
        }
      }
      if (['pending', 'applying', 'error'].includes(settingsStatus.state)) {
        ['voiceTest', 'telegramCodeBtn', 'telegramLoginBtn'].forEach(function (id) { $(id).disabled = true; });
      }
    }
    if (labels[data.state] && state !== data.state) { state = data.state; phaseStarted = Date.now(); customSub = ''; }
    if (data.phase && data.phase.state === state) customSub = data.phase.text || '';
    renderState();
  }

  // Keep existing configuration keys and authorization operations, group by task.
  var groups = [
    ['general', 'Основные', 'Изменения применяются без перезапуска. Текущий ответ завершится прежним голосом и моделью.'],
    ['integrations', 'Подключения', 'Telegram и облачные сервисы. Ключи хранятся только в локальной конфигурации.'],
    ['advanced', 'Дополнительно', 'Модели, голос и время ожидания. Если всё работает, менять эти параметры не нужно.']
  ];
  var basicKeys = ['JARVIS_LLM', 'TTS_ENGINE', 'JARVIS_VOICE_STYLE', 'JARVIS_MIC_INDEX', 'JARVIS_OVERLAY', 'SESSION_MEMORY', 'JARVIS_FOLLOWUP_MODE', 'JARVIS_FOLLOWUP_WINDOW'];
  var voiceKeys = ['STT_ENGINE', 'WHISPER_MODEL', 'TTS_ENGINE', 'PIPER_VOICE', 'EDGE_VOICE', 'EDGE_RATE', 'EDGE_PITCH', 'JARVIS_MIC_INDEX', 'XTTS_SPEED', 'XTTS_LANGUAGE'];
  var modelKeys = ['JARVIS_LLM', 'OLLAMA_MODEL', 'OPENROUTER_MODEL', 'OPENROUTER_FREE_MODEL', 'LM_STUDIO_URL', 'LM_STUDIO_MODEL', 'LM_STUDIO_CODE_MODEL', 'LM_STUDIO_AUTOLOAD', 'LM_STUDIO_GPU', 'LM_STUDIO_CONTEXT'];
  var projectKeys = ['JARVIS_PROJECT_ROOTS', 'OPENROUTER_AGENT_MODEL'];
  var fieldLabels = {
    JARVIS_LLM: 'Основной режим ответов', OLLAMA_MODEL: 'Модель Ollama', OPENROUTER_MODEL: 'Модель OpenRouter',
    LM_STUDIO_URL: 'Адрес LM Studio', LM_STUDIO_MODEL: 'Модель LM Studio', LM_STUDIO_CODE_MODEL: 'Модель LM Studio для кода',
    JARVIS_LLM_DEADLINE_LM_STUDIO: 'Ожидание LM Studio, с',
    OPENROUTER_FREE_MODEL: 'Резервная облачная модель', OPENROUTER_AGENT_MODEL: 'Проектный агент в облачном режиме',
    JARVIS_PROJECT_ROOTS: 'Папки для проектов и файлов', SESSION_MEMORY: 'Память между запусками',
    JARVIS_LLM_DEADLINE: 'Ожидание локальной модели, с', JARVIS_LLM_DEADLINE_CLOUD: 'Ожидание облачной модели, с',
    JARVIS_LLM_GEN_BUDGET: 'Бюджет генерации, с', STT_ENGINE: 'Распознавание речи', WHISPER_MODEL: 'Размер модели Whisper',
    TTS_ENGINE: 'Голос', PIPER_VOICE: 'Голос Piper', PIPER_LENGTH_SCALE: 'Темп Piper',
    PIPER_NOISE_SCALE: 'Выразительность Piper', PIPER_NOISE_W_SCALE: 'Плавность Piper', EDGE_VOICE: 'Голос Edge',
    JARVIS_PAUSE_THRESHOLD: 'Пауза до конца фразы, с', JARVIS_PHRASE_TIME_LIMIT: 'Максимальная фраза, с',
    JARVIS_WAKE_COMMAND_WINDOW: 'Ожидание после обращения, с', JARVIS_FOLLOWUP_MODE: 'Режим продолжения разговора',
    JARVIS_FOLLOWUP_WINDOW: 'Окно продолжения, с', JARVIS_SPEAK_COOLDOWN: 'Пауза после ответа, с',
    JARVIS_MIC_INDEX: 'Микрофон', JARVIS_OVERLAY: 'Индикатор речи при свёрнутом окне',
    OPENROUTER_API_KEY: 'Ключ OpenRouter · пустое поле сохраняет прежний', TELEGRAM_API_ID: 'Telegram API ID',
    TELEGRAM_API_HASH: 'Telegram API Hash · пустое поле сохраняет прежний', TELEGRAM_PHONE: 'Номер телефона'
  };
  var sections = {};
  groups.forEach(function (group, index) {
    var section = element('section', 'settings-section'); section.id = 'settings-' + group[0];
    section.setAttribute('role', 'tabpanel'); section.setAttribute('aria-labelledby', 'tab-' + group[0]);
    section.appendChild(element('p', 'settings-description', group[2])); sections[group[0]] = section;
    var button = element('button', '', group[1]); button.id = 'tab-' + group[0];
    var number = element('span', 'nav-index', '0' + (index + 1)); number.setAttribute('aria-hidden', 'true'); button.prepend(number);
    button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', section.id);
    button.onclick = function () { switchTab(group[0]); };
    $('settingsNav').appendChild(button);
  });
  var generalBlocks = {};
  [['voice', 'Ответы и голос', 'Как звучит и отвечает ваш помощник.'],
    ['conversation', 'Продолжение разговора', 'После ответа можно говорить без имени. В спорных случаях — уточнение.'],
    ['workspace', 'Рабочее пространство', 'Присутствие помощника на экране и память сессии.']].forEach(function (group) {
    var block = element('fieldset', 'settings-block'); block.appendChild(element('legend', '', group[1]));
    block.appendChild(element('p', 'settings-description', group[2]));
    sections.general.appendChild(block); generalBlocks[group[0]] = block;
  });
  var advancedGroups = {};
  [['voice', 'Тонкая настройка голоса и распознавания'], ['models', 'Модели и локальный сервер'],
    ['projects', 'Папки проектов и доступ'], ['timing', 'Время ожидания и продолжение диалога']].forEach(function (group) {
    var disclosure = element('details', 'advanced-group');
    disclosure.appendChild(element('summary', '', group[1]));
    var content = element('div', 'advanced-fields'); disclosure.appendChild(content);
    sections.advanced.appendChild(disclosure); advancedGroups[group[0]] = content;
  });
  advancedGroups.projects.appendChild(element('p', 'settings-description', 'Например, C:/ или отдельные папки через точку с запятой. Поиск идёт и во вложенных папках. Новые корни действуют со следующей задачи после применения. Права Windows остаются прежними; эти корни не ограничивают shell.'));
  Array.from($('settingsGrid').children).forEach(function (field) {
    if (field.classList.contains('telegram-auth')) { sections.integrations.appendChild(field); return; }
    var input = field.querySelector('[data-key]'); if (!input) return;
    var key = input.dataset.key;
    if (basicKeys.includes(key)) generalBlocks[key.startsWith('JARVIS_FOLLOWUP_') ? 'conversation'
      : ['JARVIS_OVERLAY', 'SESSION_MEMORY'].includes(key) ? 'workspace' : 'voice'].appendChild(field);
    else if (key === 'OPENROUTER_API_KEY') sections.integrations.appendChild(field);
    else {
      var group = voiceKeys.includes(key) || key.startsWith('PIPER_') ? 'voice' : modelKeys.includes(key) ? 'models'
        : projectKeys.includes(key) ? 'projects' : 'timing';
      advancedGroups[group].appendChild(field);
    }
  });
  groups.forEach(function (group) { $('settingsGrid').appendChild(sections[group[0]]); });
  document.querySelectorAll('.field').forEach(function (field, i) {
    var input = field.querySelector('input,select'), label = field.querySelector('label'); if (!input || !label) return;
    input.id = input.id || 'setting-' + i; label.htmlFor = input.id;
    if (fieldLabels[input.dataset.key]) label.textContent = fieldLabels[input.dataset.key];
    if (input.tagName === 'SELECT') Array.from(input.options).forEach(function (option) {
      var translations = { on: 'Включено', off: 'Выключено', auto: 'Автоматически', strict: 'Строгий', normal: 'Обычный' };
      if (input.dataset.key === 'JARVIS_LLM') option.textContent = { local: 'На компьютере · Ollama', lmstudio: 'На компьютере · LM Studio', cloud: 'В облаке · OpenRouter' }[option.value] || option.textContent;
      else if (input.dataset.key !== 'JARVIS_FOLLOWUP_MODE' && translations[option.value]) { var value = option.value; option.textContent = translations[value]; option.value = value; }
    });
  });
  var tests = element('div', 'telegram-actions');
  var listenTest = element('button', 'btn secondary', 'Проверить микрофон'); listenTest.id = 'listenTest';
  var voiceTest = element('button', 'btn secondary', 'Проверить голос'); voiceTest.id = 'voiceTest';
  sections.general.appendChild(element('p', 'settings-description', 'При недоступности модели используется настроенный резерв. Проверка голоса воспроизводит текущие настройки запущенного приложения.'));
  tests.append(listenTest, voiceTest); sections.general.appendChild(tests);
  var micTestStatus = element('p', 'settings-description'); micTestStatus.id = 'micTestStatus'; sections.general.appendChild(micTestStatus);
  var voiceTestStatus = element('p', 'settings-description'); voiceTestStatus.id = 'voiceTestStatus'; sections.general.appendChild(voiceTestStatus);
  listenTest.onclick = function () { action(this, function () { return call('listen_once'); }); };
  voiceTest.onclick = function () { action(this, function () { return call('test_voice'); }); };
  var engineStatus = element('pre', 'diag'); engineStatus.id = 'engineStatus'; sections.advanced.appendChild(engineStatus);
  var details = element('details', 'diagnostic-disclosure'); details.id = 'diagnosticsDetails';
  details.appendChild(element('summary', '', 'Задержки и техническая диагностика'));
  var lat = element('div', 'lat');
  [['first', 'До начала звука'], ['stt', 'Речь'], ['llm', 'Модель'], ['tts', 'Голос'], ['sum', 'Сумма']].forEach(function (pair) {
    var item = element('span', '', pair[1] + ' '), value = element('b', '', '—'); value.id = 'lat-' + pair[0];
    item.appendChild(value); lat.appendChild(item);
  });
  details.append(lat, $('diagBox')); sections.advanced.appendChild(details);
  function switchTab(id) {
    settingsTab = id;
    groups.forEach(function (group) {
      sections[group[0]].hidden = group[0] !== id;
      $('tab-' + group[0]).setAttribute('aria-selected', String(group[0] === id));
      $('tab-' + group[0]).tabIndex = group[0] === id ? 0 : -1;
    });
  }
  $('settingsNav').onkeydown = function (event) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    var index = groups.findIndex(function (group) { return group[0] === settingsTab; });
    index = event.key === 'Home' ? 0 : event.key === 'End' ? groups.length - 1
      : (index + (['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1) + groups.length) % groups.length;
    switchTab(groups[index][0]); $('tab-' + settingsTab).focus();
  };
  switchTab('general');

  function fillSettings(cfg) {
    document.querySelectorAll('[data-key]').forEach(function (el) {
      var overridden = (cfg._OVERRIDDEN_KEYS || []).includes(el.dataset.key);
      el.disabled = overridden;
      if (overridden) el.title = 'Задано переменной окружения при запуске';
    });
    document.querySelectorAll('[data-key]').forEach(function (el) {
      var k = el.dataset.key; if (k==='OPENROUTER_API_KEY'||k==='TELEGRAM_API_HASH') return;
      if (cfg[k] !== undefined) el.value = cfg[k];
      settingsBaseline[k] = el.value;
    });
    ['OPENROUTER_API_KEY', 'TELEGRAM_API_HASH'].forEach(function (key) {
      document.querySelector('[data-key="' + key + '"]').placeholder = cfg[key + '_SET'] ? 'Сохранён · оставить без изменения' : 'Не задан';
    });
    updateDirty();
  }
  function collectSettings() {
    var data = {};
    document.querySelectorAll('[data-key]').forEach(function (el) {
      var k = el.dataset.key; if ((k==='OPENROUTER_API_KEY'||k==='TELEGRAM_API_HASH') && !el.value) return;
      if (el.value !== settingsBaseline[k]) data[k] = el.value;
    });
    return data;
  }
  function updateDirty() {
    $('settingsDirty').hidden = Object.keys(collectSettings()).length === 0;
  }
  async function loadSettings() {
    if (!api() || settingsLoaded || loadingSettings) return;
    if (!ready()) { $('settingsNote').textContent = startupText(); return; }
    loadingSettings = true;
    $('saveSettings').disabled = true;
    try {
      var loaded = await Promise.all([call('list_microphones'), call('get_settings')]);
      var items = loaded[0]; $('micSelect').replaceChildren();
      var base = element('option', '', 'По умолчанию'); base.value = ''; $('micSelect').appendChild(base);
      items.forEach(function (mic) { var option = element('option', '', mic.index + ' · ' + mic.name); option.value = mic.index; $('micSelect').appendChild(option); });
      fillSettings(loaded[1]); settingsLoaded = true;
    } catch (error) { $('settingsNote').textContent = error.message; }
    finally { loadingSettings = false; $('saveSettings').disabled = !ready() || !settingsLoaded; }
  }
  function openSettings() {
    restoreFocus = document.activeElement;
    if (compact && connected) action($('modeToggle'), function () { return mode(false); });
    $('settingsPanel').classList.add('open'); $('settingsPanel').inert = false;
    $('settingsShade').hidden = false; root.classList.add('settings-visible');
    ['workspace', 'composer', 'activityToggle', 'modeToggle'].forEach(function (id) { $(id).inert = true; });
    loadSettings(); $('settingsClose').focus();
  }
  function closeSettings() {
    $('settingsPanel').classList.remove('open'); $('settingsPanel').inert = true;
    $('settingsShade').hidden = true; root.classList.remove('settings-visible');
    ['workspace', 'composer', 'activityToggle', 'modeToggle'].forEach(function (id) { $(id).inert = false; });
    if (restoreFocus) restoreFocus.focus();
  }
  $('tbSettings').onclick = openSettings; $('settingsClose').onclick = closeSettings;
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && !$('preview').open && $('settingsPanel').classList.contains('open')) closeSettings();
  });
  document.querySelectorAll('[data-key]').forEach(function (input) {
    input.addEventListener('input', updateDirty);
    input.addEventListener('change', function () { updateDirty(); $('settingsNote').textContent = 'Есть несохранённые изменения. Нажмите «Применить» — перезапуск не нужен.'; });
  });
  async function saveCurrent() {
    if (!settingsLoaded) throw new Error('Настройки ещё не загружены. Закройте и откройте их снова.');
    var a = api(); if (!a) throw new Error('Нет связи с приложением');
    var submitted = collectSettings();
    var result = await a.save_settings(submitted);
    $('settingsNote').textContent = result.message;
    if (!result.ok) throw new Error(result.message);
    settingsNoticeMessage = result.message;
    Object.keys(submitted).forEach(function (key) {
      if (!['OPENROUTER_API_KEY', 'TELEGRAM_API_HASH'].includes(key)) settingsBaseline[key] = String(submitted[key]);
    });
    ['OPENROUTER_API_KEY', 'TELEGRAM_API_HASH'].forEach(function (key) {
      var input = document.querySelector('[data-key="' + key + '"]');
      if (input.value === submitted[key]) input.value = '';
    });
    updateDirty();
    return result;
  }
  $('saveSettings').onclick = function () { action(this, saveCurrent); };
  $('saveSettings').textContent = 'Применить';
  $('diagBtn').onclick = function () { action(this, async function () {
    switchTab('advanced'); $('diagBox').textContent = 'Проверяю…';
    diagnosticText(await call('diagnostics'));
  }); };
  $('telegramStatusBtn').onclick = function () { action(this, async function () {
    var result = await call('telegram_status'); $('telegramStatus').textContent = result.message;
  }); };
  $('telegramCodeBtn').onclick = function () { action(this, async function () {
    await saveCurrent(); $('telegramStatus').textContent = 'Запрашиваю код…';
    var result = await call('telegram_send_code'); $('telegramStatus').textContent = result.message;
  }); };
  $('telegramLoginBtn').onclick = function () { action(this, async function () {
    await saveCurrent(); $('telegramStatus').textContent = 'Подключаю Telegram…';
    try {
      var result = await call('telegram_sign_in', $('telegramCode').value, $('telegramPassword').value);
      $('telegramStatus').textContent = result.message;
      if (result.authorized || result.needs_password) $('telegramCode').value = '';
    } finally { $('telegramPassword').value = ''; }
  }); };
  ['tbMin', 'tbClose'].forEach(function (id) {
    $(id).onclick = function () { action(this, function () { return call(id === 'tbMin' ? 'minimize' : 'close'); }); };
  });
  var maximized = false;
  $('tbMax').onclick = function () { action(this, async function () {
    if (compact) await mode(false);
    var ok = await call(maximized ? 'restore' : 'maximize');
    if (ok !== false) { maximized = !maximized; $('tbMax').title = maximized ? 'Восстановить' : 'Развернуть'; }
  }); };
  var statusBusy = false, audioBusy = false;
  async function pollStatus() {
    if (statusBusy || !api() || document.hidden) return;
    statusBusy = true;
    try {
      var data = await call('runtime_status'); setConnected(true); renderStatus(data);
    } catch (error) {
      setConnected(false); $('connection').dataset.status = 'error'; $('connection').textContent = 'Связь с приложением потеряна';
    } finally { statusBusy = false; }
  }
  async function pollAudio() {
    if (audioBusy || !connected || document.hidden) return;
    if (!['speaking', 'listening'].includes(state) && !$('settingsPanel').classList.contains('open')) {
      if ($('core').style.getPropertyValue('--level') !== '0') $('core').style.setProperty('--level', 0);
      return;
    }
    if (reducedMotion && reducedMotion.matches) { $('core').style.setProperty('--level', 0); return; }
    audioBusy = true;
    try {
      levels = await call('audio_levels');
      var value = state === 'speaking' ? (levels.output_available ? levels.output : 0) : levels.input;
      $('core').style.setProperty('--level', Math.max(0, Math.min(1, Number(value) || 0)));
    } catch (_) { $('core').style.setProperty('--level', 0); }
    finally { audioBusy = false; }
  }
  window.jvConnected = function () { settingsLoaded = false; pollStatus(); };
  window.addEventListener('pywebviewready', pollStatus);
  document.addEventListener('visibilitychange', function () { root.classList.toggle('page-hidden', document.hidden); if (!document.hidden) pollStatus(); });
  setConnected(false);
  $('connection').textContent = 'Предпросмотр · выполнение команд недоступно';
  setInterval(pollStatus, 1000); setInterval(pollAudio, 100);
  pollStatus();
})();
