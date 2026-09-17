#!/usr/bin/env python3
"""Compare a git baseline and optional pre-fix snapshot with current compaction.

Offline mode measures local preprocessing, request count, and prompt size using
a deterministic stub; it does NOT measure model latency or semantic quality.
--live uses the configured CLI account and models on synthetic data only.
"""
import argparse
import ast
import copy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_loop as loop
import laintas_cli as cli

SUMMARY = "## Goal\nFix service\n## Progress\nBlocked\n## Next Steps\nRepair\n## Critical Context\nTest failed"
FUNCTIONS = {"_llm_summarize", "_llm_review_summary", "_serialize_thread_msg",
             "_summarize_head_in_chunks", "_compact_thread_messages"}


def git_source(ref, path):
    return subprocess.check_output(["git", "show", f"{ref}:{path}"], text=True)


def version(source, adapter_source, effort, pipeline=False):
    namespace = dict(vars(loop))
    adapter = {"__file__": str(Path(loop.ctxpol.__file__).resolve().parent / "adapter.py")}
    exec(compile(adapter_source, "<baseline-adapter>", "exec"), adapter)
    namespace["ctxpol"] = SimpleNamespace(**{
        **vars(loop.ctxpol), "truncate_tool_output": adapter["truncate_tool_output"]})
    namespace["get_runtime_config"] = lambda key: (
        effort if key == "compact_review_effort" else loop.get_runtime_config(key))
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<baseline-loop>", "exec"), namespace)
    return namespace["_compact_thread_messages" if pipeline else "_summarize_head_in_chunks"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="HEAD")
    parser.add_argument("--before-source", type=Path)
    parser.add_argument("--before-adapter", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--pipeline", action="store_true", help="Measure full forced compaction including pruning")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    loop.reset_runtime_config()
    loop.set_runtime_config("compact_chunk_tokens", 4000)
    loop.set_runtime_config("mem_extract_on_compact", False)
    variants = [("committed", version(git_source(args.baseline, "agent_loop.py"),
                                     git_source(args.baseline, "context_policy/adapter.py"), "auto", args.pipeline))]
    if args.before_source:
        variants.append(("before_fix", version(args.before_source.read_text(),
                                              args.before_adapter.read_text(), "none", args.pipeline)))
    variants.append(("fixed", loop._compact_thread_messages if args.pipeline else loop._summarize_head_in_chunks))
    session = cli.load_session() if args.live else {}
    if args.live and not session:
        raise SystemExit("Live benchmark requires an existing CLI session")
    head = [{"role": "user", "content":
             "Fix /srv/service/worker.py on port 7319. Do not deploy without my approval. "
             "If tests fail, repair them and rerun. Preserve exact error and path."}]
    for i in range(1 if args.live else 12):
        head.extend([
            {"role": "assistant", "tool_calls": [{"id": str(i), "function": {
                "name": "shell", "arguments": '{"command":"pytest /srv/service/test_worker.py"}'}}]},
            {"role": "tool", "name": "shell", "tool_call_id": str(i), "content":
             "Collecting tests; execution is not finished.\n" +
             "Loading module, no final result yet.\n" * (700 if args.live else 3000) +
             "\nImportError: missing fastcodec in /srv/service/worker.py\nFAILED: exit 7. No deployment performed."},
        ])
    results = []
    for name, summarize in variants:
        records = []
        event = threading.Event()
        def backend(**kwargs):
            start = time.perf_counter()
            response = cli.call_backend_stream(**kwargs) if args.live else {"reply": SUMMARY}
            elapsed = time.perf_counter() - start
            record = {"kind": kwargs["task_kind"], "seconds": round(elapsed, 3),
                      "input_tokens_estimate": loop.tokenizer.count_tokens(
                          kwargs["system_prompt"] + "\n" + kwargs["message"]),
                      "source_present": "<source-transcript>" in kwargs["message"],
                      "error": bool(response.get("error"))}
            records.append(record)
            if args.live:
                print(json.dumps({"variant": name, **record}), flush=True)
            if response.get("error"):
                raise RuntimeError("Backend request failed: " + str(response.get("reply", ""))[:500])
            return response
        timer = threading.Timer(300, event.set) if args.live else None
        if timer:
            timer.start()
        start = time.perf_counter()
        try:
            if args.pipeline:
                messages = copy.deepcopy(head) + [
                    {"role": "user", "content": "Inspect the failure."},
                    {"role": "assistant", "content": "I will inspect it."},
                    {"role": "user", "content": "Continue; still do not deploy."}]
                state = {"_thread_messages": messages}
                summarize(messages, SimpleNamespace(call_backend=backend), session, "EN", state,
                          force=True, interrupt_event=event)
                summary = state.get("_thread_summary")
            else:
                summary = summarize(SimpleNamespace(call_backend=backend), session, head,
                                    None, "EN", "compaction-benchmark", event)
        finally:
            if timer:
                timer.cancel()
                timer.join()
        result = {"variant": name, "seconds": round(time.perf_counter() - start, 4),
                  "calls": len(records), "input_tokens_estimate": sum(
                      r["input_tokens_estimate"] for r in records),
                  "valid_summary": loop._valid_structured_summary(summary or "", "EN"),
                  "requests": records}
        if args.live:
            result["summary"] = summary
            result["fact_markers"] = {marker: marker.lower() in (summary or "").lower()
                                      for marker in ["7319", "approval", "ImportError", "fastcodec",
                                                     "/srv/service/worker.py", "exit 7"]}
        results.append(result)
        args.output.write_text(json.dumps({"live": args.live, "pipeline": args.pipeline, "synthetic": True,
            "baseline": args.baseline, "results": results}, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k not in {"requests", "summary"}}), flush=True)
        if not result["valid_summary"] or any(r["error"] for r in records):
            raise SystemExit("Benchmark failed; failed requests are not performance measurements")


if __name__ == "__main__":
    main()
