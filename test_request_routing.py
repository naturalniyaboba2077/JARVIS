"""Request fidelity regressions. Run through run_tests.py; all side effects mocked."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

import jarvis
import jarvis_config as config
import jarvis_llm as llm
import jarvis_tts as tts
import project_agent as agent
from jarvis_actions import Action
from jarvis_requests import action_mismatch, project_request


class RequestTests(unittest.TestCase):
    def setUp(self):
        jarvis._state.interrupt_event.clear()

    def test_reported_bad_transcription_asks_without_model(self):
        with patch.object(jarvis, 'get_lmstudio_client', side_effect=AssertionError('No model')), patch.object(jarvis._feat, 'open_latest_download') as latest:
            result = jarvis.handle_local_feature_command('травей работу моего проекта учет оборудования который находится в папке документа')
        self.assertIn('Не уверен', result)
        latest.assert_not_called()

    def test_natural_inspection_extracts_project_and_location(self):
        r = project_request('Проверь работу моего проекта Учёт оборудования, который находится в папке Документы.')
        self.assertEqual((r.project, r.mode, r.location), ('Учёт оборудования', 'inspect', 'Documents'))

    def test_inspection_without_colon(self):
        self.assertEqual(project_request('изучи проект Demo').project, 'Demo')

    def test_compound_request_keeps_target_and_modification(self):
        r = project_request('проверь проект Demo в папке Документы и исправь ошибки')
        self.assertEqual((r.project, r.mode, r.location), ('Demo', 'modify', 'Documents'))

    def test_desktop_location(self):
        r = project_request('проверь проект Demo на рабочем столе')
        self.assertEqual((r.project, r.location), ('Demo', 'Desktop'))

    def test_no_change_task_remains_read_only(self):
        self.assertEqual(project_request('проверь проект Demo: не меняй файлы').mode, 'inspect')

    def test_explicit_modification_keeps_task(self):
        r = project_request('доработай проект Demo: добавь кнопку')
        self.assertEqual((r.project, r.mode), ('Demo', 'modify'))
        self.assertIn('добавь кнопку', r.task)

    def test_vague_modification_asks(self):
        self.assertTrue(project_request('поработай над проектом Demo').clarification)

    def test_quoted_project(self):
        self.assertEqual(project_request('проверь проект «Учёт оборудования»').project, 'Учёт оборудования')

    def test_discussion_does_not_invoke_project(self):
        for text in ('расскажи о проекте Demo', 'если я попрошу проверить проект Demo, сможешь?', 'не меняй проект Demo'):
            self.assertIsNone(project_request(text), text)

    def test_history_keeps_its_route(self):
        self.assertIsNone(project_request('откати последние правки в проекте Demo'))

    def test_project_does_not_authorize_any_generic_tool(self):
        for name, args in [('FILE:LATEST', ()), ('CMD', ('Get-Process',)), ('OPEN', ('calc',)), ('TYPE', ('hello',))]:
            self.assertTrue(action_mismatch([Action(name, args)], 'проверь проект Demo'))

    def test_last_download_requires_explicit_request(self):
        self.assertTrue(action_mismatch([Action('FILE:LATEST')], 'открой документ Отчет'))
        self.assertIsNone(action_mismatch([Action('FILE:LATEST')], 'открой последнюю загрузку'))

    def test_last_file_question_does_not_open_download(self):
        for text in ('найди последний скачанный файл', 'какой файл в последней загрузке', 'открой последний файл проекта'):
            self.assertTrue(action_mismatch([Action('FILE:LATEST')], text), text)

    def test_explicit_downloaded_file_opens(self):
        self.assertIsNone(action_mismatch([Action('FILE:LATEST')], 'открой последний скачанный файл'))

    def test_wrong_application_rejected(self):
        with patch.object(jarvis, 'execute_system_command') as execute:
            result = jarvis.parse_and_execute_tags('[OPEN:calc]', 'открой браузер')
        execute.assert_not_called()
        self.assertIn('не совпадает', result)

    def test_whole_plan_checked_before_first_effect(self):
        with patch.object(jarvis, 'execute_system_command') as execute, patch.object(jarvis._feat, 'open_latest_download') as latest:
            jarvis.parse_and_execute_tags('[OPEN:browser][FILE:LATEST]', 'открой браузер')
        execute.assert_not_called()
        latest.assert_not_called()

    def test_typing_and_browser_positive_controls(self):
        for request, action in [('открой браузер', Action('OPEN', ('browser',))), ('открой ютуб', Action('OPEN', ('youtube.com',))), ('пиши привет', Action('TYPE', ('привет',))), ('поставь таймер на минуту', Action('TIMER', (60, '')))]:
            self.assertIsNone(action_mismatch([action], request), request)

    def test_mail_read_does_not_authorize_send(self):
        self.assertTrue(action_mismatch([Action('MAIL:SEND', ('demo@example.com', 'x', 'y'))], 'проверь почту'))

    def test_file_target_is_grounded(self):
        self.assertTrue(action_mismatch([Action('FILE:OPEN', ('C:/tmp/wrong.zip',))], 'открой файл отчет.txt'))
        self.assertIsNone(action_mismatch([Action('FILE:OPEN', ('C:/tmp/отчет.txt',))], 'открой файл отчет.txt'))

    def test_project_backend_respects_lmstudio(self):
        with patch.object(jarvis, 'LLM_ENGINE', 'lmstudio'), patch.object(jarvis._project_agent, '_resolve_project', return_value=Path('C:/demo')), patch.object(jarvis, 'get_lmstudio_client', return_value='LOCAL'), patch.object(jarvis, 'get_openrouter_client', side_effect=AssertionError('No cloud')), patch.object(jarvis._project_agent, 'run_project_agent', return_value='report') as run:
            self.assertEqual(jarvis.handle_local_feature_command('проверь проект Demo'), 'report')
        self.assertEqual(run.call_args.args[0], 'LOCAL')
        self.assertEqual(run.call_args.kwargs['mode'], 'inspect')

    def test_non_streaming_path_uses_same_router_without_cloud_key(self):
        with patch.object(jarvis, '_build_messages', return_value=[]), patch.object(jarvis, 'OPENROUTER_API_KEY', None), patch.object(jarvis, '_llm_deltas', return_value=iter(['Локальный ответ'])) as route, patch.object(jarvis, 'conversation_history', []):
            self.assertEqual(jarvis.process_with_llm('привет'), 'Локальный ответ')
        route.assert_called_once()

    def test_non_streaming_empty_response_never_claims_success(self):
        with patch.object(jarvis, '_build_messages', return_value=[]), patch.object(jarvis, '_llm_deltas', return_value=iter([])):
            self.assertIn('пустой ответ', jarvis.process_with_llm('привет'))


class ProjectScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'Учёт_оборудования'
        self.project.mkdir()
        (self.project / 'app.py').write_text('x=1\n', encoding='utf-8')
        self.env = patch.dict(os.environ, {'JARVIS_PROJECT_ROOTS': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        jarvis._state.interrupt_event.clear()

    def test_yo_and_separator_normalization(self):
        self.assertEqual(agent._resolve_project('учет оборудования'), self.project)

    def test_duplicate_name_requires_choice(self):
        (self.root / 'учет оборудования').mkdir()
        with self.assertRaisesRegex(ValueError, 'несколько'):
            agent._resolve_project('учет оборудования')

    def test_read_mode_excludes_write_schema(self):
        self.assertNotIn('write_file', [t['function']['name'] for t in agent._tools('inspect')])

    def test_read_mode_rejects_forged_write_tool(self):
        result = agent._execute(self.project, 'write_file', {'path': 'app.py', 'content': 'bad'}, mode='inspect')
        self.assertIn('не выполнялась', result)
        self.assertEqual((self.project/'app.py').read_text(), 'x=1\n')

    def test_read_mode_never_starts_shell(self):
        with patch.object(agent.subprocess, 'run', side_effect=AssertionError('No shell')):
            result = agent._execute(self.project, 'run_command', {'command': 'python -c "print(1)"'}, mode='inspect')
        self.assertIn('заблокирована', result)

    def test_read_mode_compile_does_not_execute(self):
        (self.project/'app.py').write_text('raise RuntimeError("must not execute")\n')
        self.assertIn('exit=0', agent._execute(self.project, 'run_command', {'command': 'compile'}, mode='inspect'))
        self.assertFalse((self.project/'__pycache__').exists())

    def test_no_tools_report_does_not_claim_code_inspected(self):
        client = Mock()
        client.chat.completions.create.return_value.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content='Ответ модели', tool_calls=[]))]
        # This case specifically covers zero reads; automatic preparation has
        # its own integration assertions in test_project_workflow.
        with patch.object(agent, '_prepare_project', return_value=('', [], {'runners': [], 'notes': []})):
            result = agent.run_project_agent(client, 'mock', str(self.project), 'проверь', mode='inspect')
        self.assertIn('содержимое кода модель не проверила', result)
        self.assertIn('тесты приложения не запускались', result)

    def test_cancellation_before_tools(self):
        client = Mock()
        cancel = threading.Event()
        cancel.set()
        result = agent.run_project_agent(client, 'mock', str(self.project), 'проверь', mode='inspect', cancel_event=cancel)
        client.chat.completions.create.assert_not_called()
        self.assertIn('прервана', result)


class ModelVoiceTests(unittest.TestCase):
    def test_errors_do_not_leak_credentials(self):
        message = llm._failure_reason('lmstudio', RuntimeError('No models loaded secret-token-123'))
        self.assertIn('не загружена', message)
        self.assertNotIn('secret', message)

    def test_failure_summary_preserves_each_backend(self):
        error = llm.LLMUnavailable(['LM Studio: модель не загружена', 'Ollama: истёк срок'])
        self.assertIn('LM Studio', str(error))
        self.assertIn('Ollama', str(error))

    def test_remote_server_is_never_autoloaded(self):
        with patch.object(llm, 'LLM_ENGINE', 'lmstudio'), patch.object(llm, 'LM_STUDIO_AUTOLOAD', True), patch.object(llm, 'LM_STUDIO_URL', 'https://example.com/v1'), patch.object(llm.subprocess, 'run') as run, patch.object(llm, 'get_lmstudio_client') as client:
            llm.warmup_lmstudio()
        run.assert_not_called()
        client.assert_not_called()

    def test_working_model_is_not_loaded_twice(self):
        with patch.object(llm, 'LLM_ENGINE', 'lmstudio'), patch.object(llm, 'LM_STUDIO_AUTOLOAD', True), patch.object(llm, 'LM_STUDIO_URL', 'http://127.0.0.1:1234/v1'), patch.object(llm, 'get_lmstudio_client', return_value=Mock()), patch.object(llm.subprocess, 'run') as run:
            llm.warmup_lmstudio()
        run.assert_not_called()

    def test_model_timeout_does_not_load_another_instance(self):
        client = Mock()
        client.chat.completions.create.side_effect = TimeoutError('timed out')
        with patch.object(llm, 'LLM_ENGINE', 'lmstudio'), patch.object(llm, 'LM_STUDIO_AUTOLOAD', True), patch.object(llm, 'LM_STUDIO_URL', 'http://127.0.0.1:1234/v1'), patch.object(llm, 'get_lmstudio_client', return_value=client), patch.object(llm.subprocess, 'run') as run:
            llm.warmup_lmstudio()
        run.assert_not_called()

    def test_absent_model_is_loaded_then_checked(self):
        client = Mock()
        client.chat.completions.create.side_effect = [RuntimeError('No models loaded'), Mock()]
        with patch.object(llm, 'LLM_ENGINE', 'lmstudio'), patch.object(llm, 'LM_STUDIO_AUTOLOAD', True), patch.object(llm, 'LM_STUDIO_URL', 'http://127.0.0.1:1234/v1'), patch.object(llm, 'get_lmstudio_client', return_value=client), patch.object(llm.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            llm.warmup_lmstudio()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ['load', llm.LM_STUDIO_MODEL])
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_edge_prosody_is_used_by_both_synthesis_paths(self):
        async def stream():
            yield {'type': 'audio', 'data': b'audio'}
        async def save(path):
            pass
        communicate = Mock(return_value=types.SimpleNamespace(stream=stream, save=save))
        with patch.object(tts, 'VOICE_STYLE', 'neutral'), patch.object(tts, 'edge_tts', types.SimpleNamespace(Communicate=communicate)), patch.object(tts, 'EDGE_RATE', '-5%'), patch.object(tts, 'EDGE_PITCH', '-10Hz'):
            self.assertEqual(tts._edge_tts_to_bytes('Привет'), b'audio')
            self.assertTrue(tts._run_edge_tts_sync('Привет', 'unused.mp3'))
        self.assertEqual(communicate.call_count, 2)
        for call in communicate.call_args_list:
            self.assertEqual(call.kwargs, {'rate': '-5%', 'pitch': '-10Hz'})

    def test_edge_cache_identity_changes_with_prosody(self):
        first = tts._edge_voice_key()
        with patch.object(tts, 'EDGE_RATE', '+20%'):
            self.assertNotEqual(first, tts._edge_voice_key())
        with patch.object(tts, 'EDGE_PITCH', '+20Hz'):
            self.assertNotEqual(first, tts._edge_voice_key())

    def test_cloud_cache_warmup_does_not_block_command_loop(self):
        with patch.object(tts, '_effective_tts_engine', return_value='edge'), patch.object(tts, 'prewarm_tts_cache') as warm, patch.object(tts.threading, 'Thread') as thread:
            tts.start_tts_cache_warmup()
        warm.assert_not_called()
        thread.return_value.start.assert_called_once()
        self.assertEqual(thread.call_args.kwargs['target'], warm)
        self.assertTrue(thread.call_args.kwargs['daemon'])

    def test_cache_stops_after_first_synthesis_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tts, '_TTS_CACHE_DIR', Path(directory)), patch.object(tts, '_effective_tts_engine', return_value='edge'), patch.object(tts, 'tts_to_bytes', return_value=(None, None)) as synth:
            tts.prewarm_tts_cache()
        synth.assert_called_once()

    def test_new_settings_validate_before_any_write(self):
        invalid = [{'EDGE_RATE': '-100%'}, {'EDGE_PITCH': 'low'}, {'LM_STUDIO_CONTEXT': '8192.5'}]
        for updates in invalid:
            with patch.object(config, '_read_config_snapshot', return_value=({}, None)), patch.object(config, '_atomic_write_config_bytes') as write:
                self.assertFalse(config._write_config_file(updates)[0])
            write.assert_not_called()

    def test_valid_settings_remain_importable(self):
        with patch.object(config, '_read_config_snapshot', return_value=({}, None)), patch.object(config, '_atomic_write_config_bytes') as write:
            self.assertTrue(config._write_config_file({'EDGE_RATE': '-5%', 'EDGE_PITCH': '-10Hz', 'LM_STUDIO_CONTEXT': '8192.0', 'XTTS_LANGUAGE': 'RU'})[0])
        saved = json.loads(write.call_args.args[1])
        self.assertEqual(int(saved['LM_STUDIO_CONTEXT']), 8192)
        self.assertEqual(saved['XTTS_LANGUAGE'], 'ru')

    def test_voice_key_changes_with_reference_speed_and_language(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(tts, 'JARVIS_DIR', Path(directory)):
            ref = Path(directory)/'jarvis_sample.wav'
            ref.write_bytes(b'voice-a')
            first = tts._xtts_voice_key()
            ref.write_bytes(b'voice-b')
            self.assertNotEqual(first, tts._xtts_voice_key())
            second = tts._xtts_voice_key()
            with patch.object(tts, 'XTTS_SPEED', 0.95):
                self.assertNotEqual(second, tts._xtts_voice_key())
            with patch.object(tts, 'XTTS_LANGUAGE', 'en'):
                self.assertNotEqual(second, tts._xtts_voice_key())


if __name__ == '__main__':
    unittest.main(verbosity=2)
