/* Opt-in, headless visual fixtures. Never connects to the live Jarvis bridge.
 * Requires Playwright via NODE_PATH or a dev install; Edge executable via env.
 * node ui/preview.cjs -> ignored ui/previews/*.png + geometry report.
 */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

(async () => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.JARVIS_PREVIEW_BROWSER || undefined });
  const output = path.join(__dirname, 'previews'); fs.mkdirSync(output, { recursive: true });
  const reports = [], errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => errors.push(error.message));
    await page.route(/^https?:/, route => route.abort());
    await page.addInitScript(() => {
      const fixture = { ready: true, state: 'idle', pending: null, timers: [], activities: [], services: {}, compact: false,
        configured: { llm: 'lmstudio', stt: 'whisper', tts: 'piper', cloud_key_set: false },
        microphone: { enabled: true, ready: true, error: '' },
        phase: { state: 'idle', text: '', elapsed_seconds: 0 },
        followup: { mode: 'smart', remaining_seconds: 0, seconds: 60 } };
      window.previewFixture = fixture;
      window.pywebview = { api: {
        runtime_status: async () => fixture,
        audio_levels: async () => ({ input: 0, output: 0, output_available: false }),
        list_microphones: async () => [{ index: 1, name: 'USB-микрофон · пример' }],
        get_settings: async () => ({ JARVIS_LLM: 'lmstudio', TTS_ENGINE: 'piper', JARVIS_VOICE_STYLE: 'calm',
          JARVIS_MIC_INDEX: '1', SESSION_MEMORY: 'off', JARVIS_OVERLAY: 'on',
          JARVIS_FOLLOWUP_MODE: 'smart', JARVIS_FOLLOWUP_WINDOW: '60' }),
        set_compact_mode: async enabled => ({ ok: true, compact: enabled }),
        save_settings: async () => ({ ok: false, message: 'Это демонстрация, настройки не сохраняются.' }),
        send_command: async () => false,
      } };
    });
    await page.goto(pathToFileURL(path.join(__dirname, 'index.html')).href);
    await page.waitForFunction(() => document.querySelector('#stLabel').textContent === 'Готов к команде');
    await page.evaluate(() => {
      const label = document.createElement('span'); label.textContent = 'Предпросмотр · пример данных';
      label.style.cssText = 'font-size:10px;color:#9caab9;margin-left:12px;pointer-events:none';
      document.querySelector('.brand').after(label);
    });
    async function capture(name, settings = false) {
      const result = await page.evaluate(settingsOpen => {
        const scope = settingsOpen ? document.querySelector('#settingsPanel') : document.querySelector('#jvRoot');
        const overflow = [...scope.querySelectorAll('input,select,textarea,button')].filter(el => {
          if (!el.getClientRects().length || el.closest('[hidden]') || (!settingsOpen && el.closest('#settingsPanel'))) return false;
          const r = el.getBoundingClientRect(); return r.width > 0 && (r.left < -1 || r.right > innerWidth + 1);
        }).map(el => el.id || el.dataset.key);
        const grip = document.querySelector('.grip').getBoundingClientRect();
        const controls = document.querySelector('.window-controls').getBoundingClientRect();
        const choice = document.querySelector('.project-choice-path');
        const question = document.querySelector('#confirmation');
        const footer = question.querySelector('.card-actions');
        const projectTargetVisible = !choice || question.hidden ||
          (choice.getBoundingClientRect().top >= question.getBoundingClientRect().top &&
           choice.getBoundingClientRect().bottom <= footer.getBoundingClientRect().top);
        return { width: innerWidth, height: innerHeight, documentOverflow: document.documentElement.scrollWidth > innerWidth,
          dragOverlapsButtons: grip.right > controls.left + 1, projectTargetVisible, overflow };
      }, settings);
      reports.push({ name, ...result });
      await page.screenshot({ path: path.join(output, name + '.png'), animations: 'disabled' });
    }
    await capture('01-empty');
    await page.evaluate(() => {
      window.jvAddMsg('user', 'Проверь проект «Пример». Найди ошибки и объясни, как их исправить.');
      window.jvAddMsg('jarvis', 'Начинаю проверку проекта. Сначала изучу структуру и исходные файлы.');
      window.previewFixture.state = 'thinking';
      window.previewFixture.phase = { state: 'thinking', text: 'Читаю исходные файлы проекта…', elapsed_seconds: 12 };
      window.jvSetState('thinking'); window.jvSetSub('Читаю исходные файлы проекта…');
      window.jvConnected();
    });
    await page.waitForFunction(() => document.querySelector('#taskTime').textContent === '00:12');
    await capture('02-working');
    await page.locator('#tbSettings').click();
    await page.waitForFunction(() => !document.querySelector('#saveSettings').disabled);
    await capture('03-settings', true);
    await page.setViewportSize({ width: 460, height: 740 });
    await capture('04-settings-narrow', true);
    await page.locator('#settingsClose').click();
    await capture('05-conversation-narrow');
    await page.setViewportSize({ width: 1040, height: 740 });
    await capture('06-working-default');
    await page.evaluate(() => {
      window.previewFixture.pending = { id: 'fixture-project', kind: 'project', query: 'Пример', mode: 'inspect',
        task: 'Проверь проект Пример и оцени архитектуру', expires_at: Date.now() / 1000 + 120,
        choices: [{ id: 'fixture-choice', name: 'Пример проекта', path: 'C:/Примеры/Разработка/Пример проекта' }] };
      window.jvConnected();
    });
    await page.waitForFunction(() => document.querySelector('#confirmTitle').textContent === 'Это тот проект?');
    await capture('08-project-choice');
    await page.setViewportSize({ width: 460, height: 740 });
    await capture('09-project-choice-narrow');
    await page.evaluate(() => {
      window.previewFixture.pending = { ...window.previewFixture.pending, id: 'fixture-multiple',
        choices: [...window.previewFixture.pending.choices, { id: 'fixture-second', name: 'Пример проекта',
          path: 'C:/Примеры/Архив/Длинное название папки/Пример проекта' }] };
      window.jvConnected();
    });
    await page.waitForFunction(() => document.querySelector('#projectChoices').children.length === 2);
    await capture('10-project-choices-narrow');
    const lastChoice = page.locator('#projectChoices button').last();
    await lastChoice.scrollIntoViewIfNeeded();
    const lastChoiceReachable = await lastChoice.evaluate(button => {
      const r = button.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return !button.disabled && (hit === button || button.contains(hit));
    });
    if (!lastChoiceReachable) errors.push('Last project choice is obscured after scrolling.');
    await page.evaluate(() => { window.previewFixture.pending = null; window.jvConnected(); });
    await page.waitForFunction(() => document.querySelector('#confirmation').hidden);
    await page.setViewportSize({ width: 1040, height: 740 });
    await page.locator('#modeToggle').click();
    await page.setViewportSize({ width: 540, height: 360 });
    await capture('07-compact');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    const reduced = await page.locator('#taskCaption').evaluate(el => getComputedStyle(el).animationName);
    console.log(JSON.stringify({ reports, errors, reducedMotionAnimation: reduced, output }, null, 2));
    if (errors.length || reports.some(r => r.documentOverflow || r.dragOverlapsButtons || !r.projectTargetVisible || r.overflow.length) || reduced !== 'none') process.exitCode = 1;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
