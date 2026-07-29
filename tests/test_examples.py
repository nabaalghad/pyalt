"""End-to-end front-end test: every example program in examples/ must parse."""

import glob
import os
import unittest

from compiler.lexer import Lexer
from compiler.parser import Parser

EXAMPLES_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")
EXPECTED_EXAMPLES = 13  # 5 spec + fastwords/textkit/mlprep + 07/08/09 + csv tools


class ExampleTests(unittest.TestCase):
    def test_all_examples_parse(self):
        files = sorted(glob.glob(os.path.join(EXAMPLES_DIR, "*.pya")))
        self.assertEqual(len(files), EXPECTED_EXAMPLES)
        for path in files:
            with self.subTest(example=os.path.basename(path)):
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                tokens = Lexer(src, path).tokenize()
                module = Parser(tokens, path, src).parse_module()
                self.assertTrue(module.body, f"{path} parsed to an empty module")


if __name__ == "__main__":
    unittest.main()
