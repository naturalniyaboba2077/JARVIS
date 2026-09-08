"""Helper regression only; real LM Studio tool calls are tested separately."""
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined, UndefinedError


MACRO = Path(__file__).with_name("gemma_format_type_argument.jinja").read_text(encoding="utf-8")


class GemmaTemplateTests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(undefined=StrictUndefined)
        self.call = '{{ format_type_argument(value) }}'

    def test_original_undefined_macro_fails(self):
        with self.assertRaises(UndefinedError):
            self.env.from_string(self.call).render(value="string")

    def test_scalar_schema_type(self):
        self.assertEqual(self.env.from_string(MACRO + self.call).render(value="string"),
                         '<|"|>STRING<|"|>')

    def test_nullable_type_array(self):
        self.assertEqual(self.env.from_string(MACRO + self.call).render(value=["integer", "null"]),
                         '[<|"|>INTEGER<|"|>,<|"|>NULL<|"|>]')


if __name__ == "__main__":
    unittest.main()
