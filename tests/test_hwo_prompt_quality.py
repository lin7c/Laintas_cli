import unittest

from hwo_adapter import parse, validate


class HwoPromptQualityTests(unittest.TestCase):
    def test_body_rejects_in_call_as_variable_reference(self):
        ast = parse("""@line [in(prompt: string)]

#architect# {
  -> Read in(prompt) and write contract.md.
}
""")
        errors = validate(ast)
        self.assertTrue(any("body text uses in(prompt)" in e for e in errors), errors)

    def test_recommended_input_binding_is_valid(self):
        ast = parse("""@line [in(prompt: string)]

#architect# [in(prompt = $input.prompt), out(contract: file)] {
  -> Read $self.prompt and write contract.md.
  -> agent_return({ "contract": "contract.md" })
}
""")
        self.assertEqual(validate(ast), [])


if __name__ == "__main__":
    unittest.main()
