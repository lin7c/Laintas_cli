"""Exercise deletion through a running prompt_toolkit application."""
import asyncio
import unittest
from unittest import mock

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

import laintas_cli


class ResumePickerDeleteTests(unittest.TestCase):
    def test_delete_refreshes_same_application_and_empty_state_stays_open(self):
        rows = [{"id": name, "timestamp": ts, "kind": "checkpoint",
                 "chat_history": [{"role": "assistant", "content": name}]}
                for name, ts in (("first", 1), ("second", 2))]
        apps, errors = [], []
        application = laintas_cli.Application

        with create_pipe_input() as pipe:
            def make_app(**kwargs):
                app = application(**kwargs, input=pipe, output=DummyOutput())
                apps.append(app)

                def rendered():
                    control = app.layout.container.children[-1].content
                    return str(control.text())

                async def settle(predicate, timeout=2.0):
                    """Poll until predicate holds; fixed sleeps race the app."""
                    deadline = asyncio.get_event_loop().time() + timeout
                    while asyncio.get_event_loop().time() < deadline:
                        try:
                            if predicate():
                                return True
                        except Exception:
                            pass
                        await asyncio.sleep(.05)
                    return predicate()

                async def exercise():
                    try:
                        await asyncio.sleep(.3)
                        # Rows render newest-first, so "second" is the initial
                        # selection without any navigation. Delete it in place.
                        pipe.send_text("x")
                        ok = await settle(lambda: [r["id"] for r in rows] == ["first"]
                                          and "first" in rendered()
                                          and "second" not in rendered())
                        self.assertFalse(app.is_done)
                        self.assertTrue(ok, f"delete did not refresh: rows={rows}")
                        pipe.send_text("x")  # clamped selection deletes the remaining row
                        ok = await settle(lambda: rows == []
                                          and "No saved sessions remain" in rendered())
                        self.assertFalse(app.is_done)
                        self.assertTrue(ok, f"second delete did not refresh: rows={rows}")
                        pipe.send_text("x\r")  # empty actions cannot close the UI
                        await settle(lambda: True, .3)
                        self.assertFalse(app.is_done)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        pipe.send_text("q")

                app.pre_run_callables.append(lambda: app.create_background_task(exercise()))
                return app

            with mock.patch.object(laintas_cli, "Application", side_effect=make_app), \
                    mock.patch.object(laintas_cli, "_resume_choices", side_effect=lambda _: list(rows)), \
                    mock.patch.object(laintas_cli, "delete_resume_state",
                                      side_effect=lambda cwd, item: rows.remove(item)):
                result = laintas_cli.show_resume_picker("/unused")
        if errors:
            raise errors[0]
        self.assertIsNone(result)
        self.assertEqual(len(apps), 1)

    def test_delete_exception_is_shown_without_leaving_picker(self):
        row = {"id": "first", "timestamp": 1, "chat_history": []}

        def interact(items, **kwargs):
            update = kwargs["on_action"]("delete", 0)
            self.assertIn("Could not delete", update["hint"])
            self.assertEqual(len(update["items"]), 1)
            self.assertIsNone(kwargs["on_action"]("details", 0))
            return (None, -1)

        with mock.patch.object(laintas_cli, "_resume_choices", return_value=[row]), \
                mock.patch.object(laintas_cli, "delete_resume_state", side_effect=OSError("denied")), \
                mock.patch.object(laintas_cli, "select_dialog", side_effect=interact) as picker:
            self.assertIsNone(laintas_cli.show_resume_picker("/unused"))
        self.assertEqual(picker.call_count, 1)
