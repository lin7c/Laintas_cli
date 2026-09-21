"""D3 (bughunt): concurrent writers must not drop run records.

save_run / cache_set / cache_delete did an unlocked read-modify-write of
the whole workflow_runs.json / hwg_cache.json. Concurrent writers (HWG
frontier threads, two CLI processes) silently overwrote each other — 200
concurrent saves left 37 on disk. The fix serializes the RMW with a
per-path thread RLock plus an flock sidecar file (the session_lifecycle
.guard pattern), reentrant per-thread so checkpoint->save_run nesting
shares the outer hold.
"""
import os
import subprocess
import sys
import tempfile
import threading
import unittest

import workflow_state


class _Chdir:
    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(self.path)
        return self

    def __exit__(self, *exc):
        os.chdir(self.old)


def _run(i):
    return {"runId": f"run-{i}", "kind": "hwo", "status": "running",
            "source": "x.hwo", "inputs": {}, "currentNode": None}


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_thread_saves_lose_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            n_threads, n_saves = 8, 25

            def worker(i):
                for k in range(n_saves):
                    workflow_state.save_run(
                        {**_run(f"{i}-{k}"), "runId": f"run-{i}-{k}"})

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            saved = set((workflow_state._read_json(
                workflow_state._runs_path(), {"runs": {}}).get("runs") or {}))
            expected = {f"run-{i}-{k}" for i in range(n_threads)
                        for k in range(n_saves)}
            self.assertEqual(saved, expected,
                             f"lost {len(expected - saved)} records")

    def test_concurrent_cache_sets_lose_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):

            def worker(i):
                for k in range(25):
                    workflow_state.cache_set(f"k{i}-{k}", {"v": k})

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            entries = set((workflow_state._read_json(
                workflow_state._cache_path(), {"entries": {}}).get("entries") or {}))
            expected = {f"k{i}-{k}" for i in range(6) for k in range(25)}
            self.assertEqual(entries, expected)

    def test_nested_save_run_does_not_deadlock(self):
        # checkpoint() calls save_run(); save_run holds the store lock.
        # Reentrancy per-thread is what makes this safe.
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            run = workflow_state.new_run("hwo", "x.hwo")
            done = []

            def nested():
                workflow_state.checkpoint(run, "inner", {})
                done.append(True)

            t = threading.Thread(target=nested)
            t.start()
            t.join(timeout=10)
            self.assertTrue(done, "nested save_run under lock deadlocked")

    def test_cross_process_saves_lose_nothing(self):
        code = (
            "import sys, threading\n"
            "sys.path.insert(0, '/root/laintas_cli')\n"
            "import workflow_state as ws\n"
            "def worker(i):\n"
            "    for k in range(50):\n"
            "        ws.save_run({'runId': f'p{sys.argv[1]}-{i}-{k}', 'kind': 'hwo',\n"
            "                     'status': 'running', 'source': 'x.hwo',\n"
            "                     'inputs': {}, 'currentNode': None})\n"
            "ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]\n"
            "for t in ts: t.start()\n"
            "for t in ts: t.join()\n"
        )
        with tempfile.TemporaryDirectory() as tmp, _Chdir(tmp):
            procs = [subprocess.Popen([sys.executable, "-c", code, tag])
                     for tag in ("1", "2")]
            for p in procs:
                p.wait(timeout=60)
                self.assertEqual(p.returncode, 0)
            saved = set((workflow_state._read_json(
                workflow_state._runs_path(), {"runs": {}}).get("runs") or {}))
            expected = {f"p{p}-{i}-{k}" for p in ("1", "2")
                        for i in range(4) for k in range(50)}
            self.assertEqual(saved, expected,
                             f"cross-process writers lost "
                             f"{len(expected - saved)} records")



class StoreLockErrorPropagationTests(unittest.TestCase):
    def test_oserror_in_body_is_not_masked(self):
        # The lock's soft-fail handler used to wrap the yield, so a write
        # error inside the body re-yielded and surfaced as
        # "generator didn't stop after throw()".
        import tempfile
        from pathlib import Path
        import workflow_state
        path = Path(tempfile.mkdtemp()) / "runs.json"
        with self.assertRaises(OSError):
            with workflow_state._store_lock(path):
                raise OSError("disk full")
        # And the lock is released: a second acquisition does not hang.
        with workflow_state._store_lock(path):
            pass

if __name__ == "__main__":
    unittest.main()
