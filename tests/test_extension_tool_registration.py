"""An extension's capability must be able to reach the MODEL, not just the user.

`register_command` gives a slash command, which only a human can type. Until an
extension calls `register_tool`, the model is never offered it and does not know
it exists -- which is what happened to one shipped extension: it declared
`toolPrefix`, wrote a TOOL_SPEC, called `register_tool(name, fn, spec)`, and the
call raised TypeError into its own `except Exception: pass`. The tool was absent
from every request the extension was ever installed for.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extension_runtime  # noqa: E402
from tools import Tool  # noqa: E402


SPEC = {
    "description": "look up a dependency path",
    "parameters": {
        "type": "object",
        "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
        "required": ["src", "dst"],
    },
}


def _ctx(runtime):
    return extension_runtime.ExtensionContext(
        name="demo", console=mock.Mock(), backend=None, cwd=".", _runtime=runtime)


class PlainFormTests(unittest.TestCase):
    """(name, handler, spec) -- the form an extension can write without
    importing the CLI's internals."""

    def setUp(self):
        self.runtime = mock.Mock()
        self.ctx = _ctx(self.runtime)

    def _registered(self) -> Tool:
        self.assertTrue(self.runtime.register_tool.called,
                        "nothing reached the registry")
        owner, tool = self.runtime.register_tool.call_args[0]
        self.assertEqual(owner, "demo")
        return tool

    def test_plain_form_registers_instead_of_raising(self):
        self.ctx.register_tool("lookup", lambda src, dst: f"{src}->{dst}", SPEC)
        tool = self._registered()
        self.assertEqual(tool.name, "lookup")
        self.assertEqual(tool.description, "look up a dependency path")
        self.assertEqual(tool.schema, SPEC["parameters"])

    def test_handler_receives_schema_properties_as_kwargs(self):
        self.ctx.register_tool("lookup", lambda src, dst: f"{src}->{dst}", SPEC)
        out = self._registered().invoke({"src": "a", "dst": "b"}, None)
        self.assertEqual(out, {"ok": True, "result": "a->b"})

    def test_bad_arguments_return_an_error_not_a_crash(self):
        self.ctx.register_tool("lookup", lambda src, dst: src, SPEC)
        out = self._registered().invoke({"wrong": 1}, None)
        self.assertFalse(out["ok"])
        self.assertIn("invalid parameters", out["error"])

    def test_a_raising_handler_is_reported_not_propagated(self):
        def boom(src, dst):
            raise RuntimeError("index missing")
        self.ctx.register_tool("lookup", boom, SPEC)
        out = self._registered().invoke({"src": "a", "dst": "b"}, None)
        self.assertFalse(out["ok"])
        self.assertIn("index missing", out["error"])

    def test_missing_spec_still_yields_a_valid_schema(self):
        self.ctx.register_tool("bare", lambda: "ok")
        self.assertEqual(self._registered().schema,
                         {"type": "object", "properties": {}})


class ToolFormTests(unittest.TestCase):
    """The original single-Tool form must keep working unchanged."""

    def setUp(self):
        self.runtime = mock.Mock()
        self.ctx = _ctx(self.runtime)

    def test_tool_object_is_passed_through(self):
        tool = Tool(name="x", description="d", schema={}, invoke=lambda p, c: {})
        self.ctx.register_tool(tool)
        self.assertIs(self.runtime.register_tool.call_args[0][1], tool)

    def test_mixing_the_two_forms_is_rejected(self):
        tool = Tool(name="x", description="d", schema={}, invoke=lambda p, c: {})
        with self.assertRaises(TypeError):
            self.ctx.register_tool(tool, lambda: 1, SPEC)

    def test_a_non_callable_handler_is_rejected(self):
        with self.assertRaises(TypeError):
            self.ctx.register_tool("lookup", "not-callable", SPEC)



class ErrorAttributionTests(unittest.TestCase):
    """A TypeError from the handler's BODY is a bug in the extension, not bad
    arguments from the model. Conflating them is how that registration
    failure spent its whole life looking like a compatibility downgrade."""

    def setUp(self):
        self.runtime = mock.Mock()
        self.ctx = _ctx(self.runtime)

    def _tool(self, fn):
        self.ctx.register_tool("t", fn, SPEC)
        return self.runtime.register_tool.call_args[0][1]

    def test_type_error_inside_the_body_is_not_called_a_parameter_error(self):
        def bad_body(src, dst):
            return 1 + "not a number"       # TypeError from the body
        out = self._tool(bad_body).invoke({"src": "a", "dst": "b"}, None)
        self.assertFalse(out["ok"])
        self.assertNotIn("invalid parameters", out["error"])
        self.assertIn("TypeError", out["error"])

    def test_wrong_argument_names_are_still_called_a_parameter_error(self):
        out = self._tool(lambda src, dst: src).invoke({"nope": 1}, None)
        self.assertFalse(out["ok"])
        self.assertIn("invalid parameters", out["error"])

if __name__ == "__main__":
    unittest.main()
