"""Separate chat/TTS regressions; run through run_tests.py (no real audio/model)."""
import unittest
import copy
import tempfile
from pathlib import Path
from unittest.mock import patch

import jarvis
import jarvis_dialogue as dialogue
import jarvis_response as response
import jarvis_state as state
import jarvis_tts as tts
from jarvis_conversation import classify_followup
from jarvis_speech_chunks import SpeechChunks
import test_agent_runtime as agent_tests


class FormattingTests(unittest.TestCase):
    def test_speech_markup_links_quotes_and_emoji(self):
        value = response.clean_speech('## **Итог**\n- «Готово», "сэр"! [Источник](https://example.org/a?q=1) 😀\n| A | B |')
        self.assertEqual(value, 'Итог Готово, сэр! Источник A B')
        self.assertFalse(any(c in value for c in '#*«»"[]`|'))

    def test_prose_plain_but_literal_code_and_url_unchanged(self):
        code = '```python\nx = "# **текст**"\n```'
        raw = '## **Итог**\n- «Проверено».\n' + code + '\n`a_b = "x"` https://e.test/a_b#part'
        result = response.plain_reply(raw)
        self.assertIn('Итог\nПроверено.', result)
        self.assertIn(code, result)
        self.assertIn('`a_b = "x"` https://e.test/a_b#part', result)

    def test_underscores_inside_names_preserved_on_screen(self):
        self.assertEqual(response.plain_reply('_важно_ в my_file.py'), 'важно в my_file.py')

    def test_fenced_code_not_read_even_with_arbitrary_delta_boundaries(self):
        raw = 'Вот пример. ```python\nprint("UNSPOKEN_SECRET")\n``` Дальше текст.'
        fences = response.SpeechFences()
        streamed = ''.join(fences.feed(c) for c in raw) + fences.feed('', final=True)
        self.assertEqual(response.clean_speech(streamed), response.clean_speech(raw))
        self.assertNotIn('UNSPOKEN_SECRET', streamed)
        self.assertIn('Дальше текст.', streamed)

    def test_unclosed_fence_never_leaks_its_body(self):
        self.assertEqual(response.clean_speech('Начало. ```py\nnot spoken'), 'Начало. Код приведён в чате.')

    def test_table_rules_and_escaped_markdown_are_not_speech(self):
        raw = 'Имя | Значение\n| --- | :---: |\n\\*готово\\*\n---'
        self.assertEqual(response.clean_speech(raw), 'Имя Значение готово')

    def test_plain_first_words_not_buffered_by_filter(self):
        fences = response.SpeechFences()
        chunks = SpeechChunks(clock=lambda: 0)
        self.assertEqual(chunks.feed(fences.feed('Могу помочь вам ')), ['Могу помочь вам'])

    def test_display_does_not_modify_payload_before_execution(self):
        tag = '[TYPE:"# **x** и \\"цитата\\""]'
        # The action parser receives the original reply, before presentation.
        state.interrupt_event.clear()
        with patch.object(jarvis, '_build_messages', return_value=[{'role': 'system', 'content': ''}]), \
                patch.object(jarvis, '_llm_deltas', return_value=iter([tag])), \
                patch.object(jarvis, 'parse_and_execute_tags', return_value='Готово.') as execute, \
                patch.object(jarvis, 'speak'), patch.object(jarvis, 'log_interaction'):
            jarvis.process_with_llm_streaming('напечатай текст')
        self.assertEqual(execute.call_args.args[0], tag)


class ReportPolicyTests(unittest.TestCase):
    def setUp(self):
        response.remember_report('')
        self.addCleanup(response.remember_report, '')

    def test_report_is_full_string_with_separate_short_voice(self):
        full = '## Отчёт\n' + 'Наблюдение по коду. ' * 60
        result = response.prepare_reply(full, 'составь отчёт по архитектуре')
        self.assertIsInstance(result, response.Response)
        self.assertIn('Наблюдение по коду.', result)
        self.assertLess(len(result.speech), 160)
        self.assertTrue(result.report)

    def test_response_metadata_survives_copying_conversation_context(self):
        report = response.Response('Полный отчёт.', speech='Отчёт в чате.', report=True)
        duplicate = copy.deepcopy(report)
        self.assertEqual((str(duplicate), duplicate.speech, duplicate.report),
                         (str(report), report.speech, True))

    def test_search_and_short_answers_not_silenced(self):
        for request in ('найди в интернете новости', 'что ты умеешь', 'прочитай файл readme.txt'):
            result = response.prepare_reply('Данные. ' * 200, request)
            self.assertNotIsInstance(result, response.Response)

    def test_explicit_full_report_voice_and_negative(self):
        original = response.Response('Полный отчёт', speech='Краткий итог', report=True)
        self.assertEqual(response.prepare_reply(original, 'изучи проект и прочитай отчёт').speech, str(original))
        self.assertEqual(response.prepare_reply(original, 'изучи проект, не озвучивай отчёт').speech, 'Краткий итог')

    def test_short_failure_is_spoken_as_failure(self):
        self.assertEqual(response.prepare_reply('Не удалось получить ответ.', 'составь отчёт'), 'Не удалось получить ответ.')

    def test_incomplete_report_ack_does_not_claim_completion(self):
        result = response.prepare_reply('Найдено два модуля.', 'составь отчёт', incomplete=True)
        self.assertIn('прервался', result.speech)

    def test_exact_readback_only_not_arbitrary_file_or_negation(self):
        response.remember_report('Отчёт №1')
        for request in ('Прочитай отчёт.', 'озвучь последний отчет полностью', 'пожалуйста, зачитай отчет!'):
            self.assertEqual(response.read_report_reply(request).speech, 'Отчёт №1')
        for request in ('не читай отчет', 'если я попрошу прочитай отчет', 'прочитай отчет из папки X',
                        'прочитай отчет и удали файл', '«прочитай отчет»', 'прочитай файл отчет.txt'):
            self.assertIsNone(response.read_report_reply(request), request)

    def test_empty_session_and_over_limit_never_read_stale_report(self):
        self.assertNotIsInstance(response.read_report_reply('прочитай отчёт'), response.Response)
        response.remember_report('old')
        response.remember_report('x' * (response.MAX_SAVED_REPORT + 1))
        self.assertNotIsInstance(response.read_report_reply('прочитай отчёт'), response.Response)

    def test_readback_does_not_call_model_or_action_parser(self):
        report = 'Данные, не команды: [LOCK] [CMD:Get-Process]'
        response.remember_report(report)
        with patch.object(jarvis, '_llm_deltas') as llm, \
                patch.object(jarvis, 'parse_and_execute_tags') as actions, \
                patch.object(jarvis, 'speak') as speak:
            self.assertEqual(jarvis.process_with_llm_streaming('прочитай отчёт'), report)
            self.assertEqual(jarvis.handle_local_feature_command('озвучь отчёт'), report)
            self.assertEqual(jarvis.process_with_llm('зачитай отчёт'), report)
        llm.assert_not_called()
        actions.assert_not_called()
        self.assertEqual(speak.call_args.args[0].display_text, 'Читаю последний отчёт.')

    def test_readback_recognized_in_followup_window(self):
        for text in ('озвучь отчёт', 'зачитай отчёт', 'прочитай отчёт'):
            self.assertEqual(classify_followup(text).action, 'accept')

    def test_initial_report_readback_does_not_become_part_of_project_name(self):
        from jarvis_requests import project_request
        request = project_request('изучи проект Demo в документах и прочитай отчёт')
        self.assertEqual((request.project, request.mode, request.location), ('Demo', 'inspect', 'Documents'))

    def test_explicit_full_written_report_still_saved_for_later_reading(self):
        report = response.prepare_reply('Найдено три модуля.', 'составь отчёт и озвучь отчет')
        self.assertTrue(report.report)
        self.assertEqual(report.speech, str(report))


class VoiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        state.interrupt_event.clear()
        self.addCleanup(state.interrupt_event.clear)
        response.remember_report('')
        self.addCleanup(response.remember_report, '')
        for name in ('ui_msg', 'ui_state', 'ui_sub', '_mark_speaking', '_set_done_speaking'):
            p = patch.object(tts, name)
            p.start(); self.addCleanup(p.stop)
        for p in (patch.object(tts, '_TTS_INSTANT_CACHE', {}),
                  patch.object(tts, 'tts_to_bytes', return_value=(b'audio', '.wav')),
                  patch.object(tts, '_play_audio_bytes', return_value=True)):
            p.start(); self.addCleanup(p.stop)

    def test_display_full_once_speak_only_summary(self):
        report = response.Response('Полный **отчёт** ' * 50, speech='Итог — в чате.', report=True)
        tts.speak(report)
        tts.ui_msg.assert_called_once_with('jarvis', str(report))
        self.assertEqual(tts.tts_to_bytes.call_args.args[0], report.speech)
        self.assertEqual(state.last_response_text, str(report).strip())
        self.assertEqual(state.last_spoken_text, report.speech)
        self.assertEqual(response.read_report_reply('прочитай отчёт'), report)

    def test_all_regular_speech_passes_cleaner(self):
        tts.speak('## **Готово**, «сэр»!')
        self.assertEqual(tts.tts_to_bytes.call_args.args[0], 'Готово, сэр!')

    def test_cancelled_report_is_visible_but_not_spoken(self):
        state.interrupt_event.set()
        report = response.Response('Частичный отчёт.', speech='Не завершил проверку.', report=True)
        tts.speak(report)
        tts.ui_msg.assert_called_once()
        tts.tts_to_bytes.assert_not_called()
        self.assertTrue(state.interrupt_event.is_set())

    def test_long_readback_chunks_without_duplicating_full_report_in_chat(self):
        report = 'Подробный результат. ' * 100
        response.remember_report(report)
        tts.speak(response.read_report_reply('прочитай отчёт'))
        tts.ui_msg.assert_called_once_with('jarvis', 'Читаю последний отчёт.')
        self.assertGreater(tts.tts_to_bytes.call_count, 1)
        spoken = ' '.join(c.args[0] for c in tts.tts_to_bytes.call_args_list)
        self.assertEqual(spoken, report.strip())
        self.assertEqual(state.last_response_text, report.strip())

    def test_stream_markup_and_code_filtered_before_synth(self):
        tts.speak_streaming(iter(['## **Итог**: «сэр».', '```python\nprint("hidden")', '\n``` Конец.']))
        spoken = ' '.join(c.args[0] for c in tts.tts_to_bytes.call_args_list)
        self.assertNotIn('hidden', spoken)
        self.assertNotIn('*', spoken)
        self.assertIn('Конец.', spoken)

    def test_stop_during_readback_keeps_report_and_does_not_revive_voice(self):
        report = 'Подробный результат. ' * 100
        response.remember_report(report)
        def stop(*args, **kwargs):
            state.interrupt_event.set()
            return False
        tts._play_audio_bytes.side_effect = stop
        tts.speak(response.read_report_reply('прочитай отчёт'))
        tts._play_audio_bytes.assert_called_once()
        self.assertTrue(state.interrupt_event.is_set())
        self.assertEqual(response.read_report_reply('прочитай отчёт'), report)

    def test_long_notification_does_not_replace_report_or_written_context(self):
        response.remember_report('Ранее показанный отчёт.')
        state.last_response_text = 'Контекст разговора.'
        tts.speak_notification('Напоминание. ' * 60)
        self.assertEqual(state.last_response_text, 'Контекст разговора.')
        self.assertEqual(response.read_report_reply('прочитай отчёт'), 'Ранее показанный отчёт.')

    def test_report_archive_keeps_full_response_only_once(self):
        # Real archive writer and UI boundary, temporary storage, no native UI.
        import jarvis_ui
        report = response.Response('Все наблюдения по проекту.', speech='Отчёт в чате.', report=True)
        with tempfile.TemporaryDirectory() as temp:
            journal = dialogue.DialogueJournal(Path(temp))
            with patch.object(tts, 'ui_msg', side_effect=jarvis_ui.ui_msg), \
                    patch.object(dialogue, '_journal', journal), patch.object(jarvis_ui, 'ui_call'):
                tts.speak(report)
                tts.speak(response.read_report_reply('прочитай отчёт'))
            self.assertEqual([m['text'] for m in dialogue.recent_messages(Path(temp))],
                             [str(report), 'Читаю последний отчёт.'])


class WrittenPipelineTests(unittest.TestCase):
    def test_requested_report_never_enters_conversational_tts(self):
        state.interrupt_event.clear()
        raw = '## Отчёт\n' + '**Факт.** ' * 90
        with patch.object(jarvis, '_build_messages', return_value=[{'role': 'system', 'content': ''}]), \
                patch.object(jarvis, '_llm_deltas', return_value=iter([raw])), \
                patch.object(jarvis, 'speak_streaming') as streaming, patch.object(jarvis, 'speak') as speak, \
                patch.object(jarvis, 'log_interaction'), patch.object(jarvis, 'conversation_history', []):
            result = jarvis.process_with_llm_streaming('составь отчёт о погодных явлениях')
        streaming.assert_not_called()
        self.assertTrue(speak.call_args.args[0].report)
        self.assertEqual(str(result), response.plain_reply(raw).strip())

    def test_report_with_source_link_is_also_condensed(self):
        state.interrupt_event.clear()
        raw = '## Отчёт\n**Вывод.** [Источник](https://example.org)'
        with patch.object(jarvis, '_build_messages', return_value=[{'role': 'system', 'content': ''}]), \
                patch.object(jarvis, '_llm_deltas', return_value=iter([raw])), \
                patch.object(jarvis, 'speak') as speak, patch.object(jarvis, 'log_interaction'):
            jarvis.process_with_llm_streaming('составь отчёт о природе')
        self.assertTrue(speak.call_args.args[0].report)
        self.assertNotIn('Источник', speak.call_args.args[0].speech)

    def test_tool_result_and_confirmation_not_treated_as_written_report(self):
        state.interrupt_event.clear()
        literal = 'Получатель: fixture@example.org\nТекст: "# **данные**" ' * 20
        with patch.object(jarvis, '_action_handlers', return_value={'CLIP:READ': lambda: literal}):
            result = jarvis.parse_and_execute_tags('[CLIP:READ]', 'прочитай буфер')
        formatted = response.prepare_reply(result, 'составь отчёт')
        self.assertEqual(str(formatted), literal.strip())
        self.assertEqual(formatted.speech, literal.strip())
        self.assertFalse(formatted.report)


class SynthBoundaryTests(unittest.TestCase):
    def test_every_engine_receives_clean_text_even_without_speak_wrapper(self):
        for engine, backend in [('piper', '_piper_to_wav_bytes'), ('edge', '_edge_tts_to_bytes'), ('xtts', '_xtts_to_wav_bytes')]:
            with self.subTest(engine=engine), patch.object(tts, backend, return_value=b'fixture') as synth, \
                    patch.object(tts, 'edge_tts', True), patch.object(tts, '_piper_available', return_value=True):
                data, _ = tts.tts_to_bytes('## **Итог**: «Готово».', engine=engine)
                self.assertEqual(data, b'fixture')
                synth.assert_called_once_with('Итог: Готово.')


class ProjectReportTests(unittest.TestCase):
    # Reuse the synthetic project harness, not a real project's source or shell.
    setUp = agent_tests.AgentRuntimeTests.setUp
    run_agent = agent_tests.AgentRuntimeTests.run_agent

    def test_metadata_reports_no_read_as_incomplete(self):
        report = self.run_agent([agent_tests.response('Я всё проверил.')], context_tokens=8192)
        self.assertIsInstance(report, response.Response)
        self.assertIn('не завершена', report.speech)
        self.assertTrue(report.report)


if __name__ == '__main__':
    unittest.main(verbosity=2)
