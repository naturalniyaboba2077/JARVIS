import unittest
from model_compare import INVENTORY, check_fixture, missing_item_finding, report_complete, raw_chatml_prompt
from evaluate_results import tag_checks, median
from compact_prompt_probe import compact_messages
from workflow_cases import DISCOUNT, AVERAGE, check_holdout


class HoldoutVerifierTests(unittest.TestCase):
    def test_discount_correct_and_wrong(self):
        self.assertLess(check_holdout('discount_large_fix', DISCOUNT)['passed'], 4)
        fixed = DISCOUNT.replace('price - percent', 'price * (1 - percent / 100)')
        self.assertEqual(check_holdout('discount_large_fix', fixed)['passed'], 4)

    def test_average_empty_and_nonempty(self):
        self.assertFalse(check_holdout('average_fix', AVERAGE)['verified'])
        for expression in ('sum(values) / len(values) if values else 0', 'sum(values) / (len(values) or 1)'):
            fixed = 'def average(values):\n    return ' + expression
            self.assertEqual(check_holdout('average_fix', fixed)['passed'], 4)

    def test_rejects_non_interpretable_code_without_execution(self):
        for body in ("__import__('os').system('no')", 'values.__class__', '10 ** 10000', '1e100'):
            with self.subTest(body=body):
                self.assertFalse(check_holdout('average_fix', 'def average(values):\n    return ' + body)['verified'])
        self.assertFalse(check_holdout('average_fix', 'import os\n' + AVERAGE)['verified'])

    def test_if_guard_and_unrelated_prefix(self):
        source = '# preserve\n' * 1000 + 'def average(values):\n    if not values:\n        return 0\n    return sum(values) / len(values)\n'
        self.assertEqual(check_holdout('average_fix', source)['passed'], 4)


class FixtureVerifierTests(unittest.TestCase):
    def test_original_has_known_bug(self):
        self.assertEqual(check_fixture(INVENTORY)["passed"], 1)

    def test_correct_fix(self):
        fixed = INVENTORY.replace('item["price"]', 'item["price"] * item["quantity"]')
        self.assertEqual(check_fixture(fixed)["passed"], 3)

    def test_get_variant(self):
        fixed = INVENTORY.replace('item["price"]', 'item.get("price", 0) * item.get("quantity", 0)')
        self.assertEqual(check_fixture(fixed)["passed"], 3)

    def test_import_is_not_executed(self):
        value = check_fixture("import os\n" + INVENTORY)
        self.assertFalse(value["verified"])

    def test_arbitrary_call_is_not_executed(self):
        value = check_fixture("def total_value(items):\n    return __import__('os').system('no')\n")
        self.assertFalse(value["verified"])

    def test_property_access_is_not_executed(self):
        value = check_fixture("def total_value(items):\n    return items.__class__.__base__.__subclasses__()\n")
        self.assertFalse(value["verified"])

    def test_loop_is_not_executed(self):
        value = check_fixture("def total_value(items):\n    while True: pass\n")
        self.assertFalse(value["verified"])

    def test_syntax_error(self):
        self.assertFalse(check_fixture("def broken(")["verified"])


class ReportVerifierTests(unittest.TestCase):
    def test_stop_does_not_make_partial_report_complete(self):
        requests = [{"finish_reason": "stop", "content": "готово", "error": None}]
        self.assertFalse(report_complete("Полная проверка не завершена: лимит шагов", requests))
        self.assertFalse(report_complete("<tool_call>read_file</tool_call>", requests))
        self.assertTrue(report_complete("Обнаружены две ошибки", requests))

    def test_invented_correct_behavior_is_not_a_finding(self):
        self.assertFalse(missing_item_finding("find_equipment при отсутствии совпадения возвращает None"))
        self.assertTrue(missing_item_finding("find_equipment выбрасывает StopIteration вместо None"))


class RegradeTests(unittest.TestCase):
    def test_compact_probe_preserves_history_and_source(self):
        source = [{"role": "system", "content": "long"},
                  {"role": "user", "content": "графит"},
                  {"role": "assistant", "content": "запомнил"}]
        compact = compact_messages(source)
        self.assertEqual(compact[1:], source[1:])
        self.assertEqual(source[0]["content"], "long")
        self.assertNotEqual(compact[0], source[0])

    def test_find_folds_yo_like_jarvis_discovery(self):
        row = {"id": "file_find", "checks": {"correct_argument": False},
               "parsed_actions": [{"name": "FILE:FIND", "args": ["отчёт.txt"]}]}
        self.assertTrue(tag_checks(row)["correct_argument"])
        self.assertFalse(row["checks"]["correct_argument"])

    def test_find_rejects_different_name(self):
        row = {"id": "file_find", "checks": {},
               "parsed_actions": [{"name": "FILE:FIND", "args": ["бюджет.txt"]}]}
        self.assertFalse(tag_checks(row)["correct_argument"])

    def test_search_keeps_requested_version(self):
        row = {"id": "web_search", "checks": {"correct_argument": True},
               "parsed_actions": [{"name": "SEARCH", "args": ["GPT Astra"]}]}
        self.assertFalse(tag_checks(row)["correct_argument"])
        row["parsed_actions"][0]["args"] = ["GPT Astra 6 характеристики"]
        self.assertTrue(tag_checks(row)["correct_argument"])

    def test_unknown_latency_is_not_zero(self):
        self.assertIsNone(median([{"time": None}], "time"))
        self.assertEqual(median([{"time": None}, {"time": 2}, {"time": 4}], "time"), 3)


class RawPromptTests(unittest.TestCase):
    def test_keeps_history_and_closes_thinking(self):
        result = raw_chatml_prompt([{"role": "user", "content": "Привет"},
                                   {"role": "assistant", "content": "Слушаю"}])
        self.assertIn("<|im_start|>user\nПривет<|im_end|>", result)
        self.assertIn("<|im_start|>assistant\nСлушаю<|im_end|>", result)
        self.assertTrue(result.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"))

    def test_no_tool_transport(self):
        with self.assertRaises(ValueError):
            raw_chatml_prompt([{"role": "tool", "content": "result"}])


if __name__ == "__main__":
    unittest.main()
