import unittest
import os
import tempfile
from pathlib import Path
from unittest import mock

from hwo_adapter import parse, validate
import hwo_runner
import workflow_state
import task_manager
import workgraph


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

    def test_hwo_steps_create_instance_tasks_and_emit_lifecycle(self):
        source = "-> inspect the project\n-> run verification\n"
        steps = hwo_runner.parse_hwo(source)
        events = []
        def collect(batch):
            events.extend(batch if isinstance(batch, list) else [batch])
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                run = workflow_state.new_run("hwo", "task.hwo")
                mapping = hwo_runner._prepare_workflow_tasks(
                    steps, run["runId"], tmp)
                task_manager.detach_active_tasks(cwd=tmp)
                ctx = hwo_runner.HwoCtx(
                    deps=object(), session={}, run_state=run,
                    run_id=run["runId"], workflow_tasks=mapping,
                    events_cb=collect,
                )
                with mock.patch.object(
                        hwo_runner, "_run_task_group",
                        return_value={"ok": True, "msg": "done", "outputs": {}}):
                    result = hwo_runner.run_sequence(steps, ctx)
                self.assertTrue(result["ok"])
                tasks = [
                    workgraph.get_step(
                        entry["workId"], entry["taskId"], cwd=tmp)
                    for entry in mapping.values()
                ]
                self.assertEqual(len(tasks), 2)
                self.assertTrue(all(t["status"] == "completed" for t in tasks))
                self.assertTrue(all(not t["session_only"] for t in tasks))
                self.assertTrue(all(
                    t["metadata"].get("scopeType") == "hwo-run"
                    for t in tasks))
                self.assertEqual([e["type"] for e in events], [
                    "step_started", "step_started",
                    "step_completed", "step_completed",
                ])
                self.assertTrue(all(e["runId"] == run["runId"] for e in events))
                self.assertEqual(
                    [e["stepId"] for e in events], ["0", "1", "0", "1"])
            finally:
                os.chdir(old)


if __name__ == "__main__":
    unittest.main()
