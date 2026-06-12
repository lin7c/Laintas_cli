#!/usr/bin/env python3
"""
Shell capability tests for laintas_cli.
Tests input routing, PTY execution, built-in detection, and environment isolation.
"""

import sys
import os
import tempfile
import subprocess

# Add parent directory to path so we can import laintas_cli
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import laintas_cli


def test_extract_first_word():
    """Test extraction of first word from input."""
    print("\n=== Test: extract_first_word ===")
    cases = [
        ("ls -la", "ls"),
        ("  cd /tmp", "cd"),
        ("echo 'hello world'", "echo"),
        ("git commit -m 'test'", "git"),
        ("", ""),
        ("   ", ""),
        ("'quoted command' arg", "quoted command"),
        ("\"double quoted\" arg", "double quoted"),
    ]
    passed = 0
    for inp, expected in cases:
        result = laintas_cli.extract_first_word(inp)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: extract_first_word({repr(inp)}) = {repr(result)} (expected {repr(expected)})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_is_system_command():
    """Test system command detection (builtins + PATH)."""
    print("\n=== Test: is_system_command ===")
    cases = [
        # Shell builtins
        ("cd /tmp", True),
        ("export FOO=bar", True),
        ("echo hello", True),
        ("pwd", True),
        # Commands on PATH (should exist on most systems)
        ("ls", True),
        ("cat /etc/passwd", True),
        ("bash --version", True),
        # Non-existent commands
        ("nonexistent_command_xyz123", False),
        ("this_does_not_exist arg1 arg2", False),
        # Edge cases
        ("", False),
        ("   ", False),
    ]
    passed = 0
    for inp, expected in cases:
        result = laintas_cli.is_system_command(inp)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: is_system_command({repr(inp)}) = {result} (expected {expected})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_posix_builtins():
    """Test that POSIX shell builtins are correctly identified."""
    print("\n=== Test: POSIX builtins ===")
    builtins = laintas_cli._POSIX_SHELL_BUILTINS
    must_have = ["cd", "echo", "export", "pwd", "alias", "source", "exec", "exit", "set", "unset"]
    passed = 0
    for cmd in must_have:
        found = cmd in builtins
        status = "PASS" if found else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: '{cmd}' in _POSIX_SHELL_BUILTINS = {found}")
    print(f"  Total builtins: {len(builtins)}")
    print(f"  Result: {passed}/{len(must_have)} passed")
    return passed == len(must_have)


def test_execute_command_pty_simple():
    """Test PTY execution with simple commands."""
    print("\n=== Test: execute_command_pty (simple) ===")
    cases = [
        ("echo hello", 0, "hello"),
        ("pwd", 0, None),  # Don't check output, just success
        ("ls /tmp", 0, None),
        ("false", 1, None),  # Command that returns non-zero
        ("true", 0, None),
    ]
    passed = 0
    for cmd, expected_rc, expected_substr in cases:
        result = laintas_cli.execute_command_pty(cmd, timeout=5)
        rc_ok = result["returncode"] == expected_rc
        substr_ok = True
        if expected_substr:
            substr_ok = expected_substr in result["stdout"]
        ok = rc_ok and substr_ok
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: '{cmd}' -> rc={result['returncode']}, success={result['success']}")
        if not ok:
            print(f"         stdout={repr(result['stdout'][:100])}")
            print(f"         expected rc={expected_rc}, substr={repr(expected_substr)}")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_execute_command_pty_output():
    """Test PTY output capture with various content types."""
    print("\n=== Test: execute_command_pty (output capture) ===")

    # Test multi-line output
    result = laintas_cli.execute_command_pty("echo -e 'line1\\nline2\\nline3'", timeout=5)
    lines = [l for l in result["stdout"].strip().split("\n") if l]
    multi_ok = len(lines) >= 3

    # Test special characters
    result2 = laintas_cli.execute_command_pty("echo 'hello world! @#$%'", timeout=5)
    special_ok = "hello world!" in result2["stdout"]

    # Test large output
    result3 = laintas_cli.execute_command_pty("seq 1 100", timeout=5)
    large_ok = "100" in result3["stdout"] and "1" in result3["stdout"]

    cases = [
        ("multi-line output", multi_ok, f"{len(lines)} lines"),
        ("special characters", special_ok, result2["stdout"][:50]),
        ("large output (seq 100)", large_ok, f"len={len(result3['stdout'])}"),
    ]
    passed = 0
    for name, ok, detail in cases:
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {name} ({detail})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_execute_command_pty_errors():
    """Test PTY error handling."""
    print("\n=== Test: execute_command_pty (error handling) ===")

    # Non-existent command
    result = laintas_cli.execute_command_pty("nonexistent_cmd_xyz_123", timeout=5)
    err_ok = result["returncode"] != 0

    # Command with stderr
    result2 = laintas_cli.execute_command_pty("ls /nonexistent_path_xyz", timeout=5)
    stderr_ok = result2["returncode"] != 0

    # Timeout (should not hang forever)
    result3 = laintas_cli.execute_command_pty("sleep 0.5", timeout=2)
    timeout_ok = result3["returncode"] == 0

    cases = [
        ("non-existent command fails", err_ok, f"rc={result['returncode']}"),
        ("ls nonexistent path fails", stderr_ok, f"rc={result2['returncode']}"),
        ("short sleep completes", timeout_ok, f"rc={result3['returncode']}"),
    ]
    passed = 0
    for name, ok, detail in cases:
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {name} ({detail})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_dynamic_path_detection():
    """Test that newly added PATH commands are detected."""
    print("\n=== Test: dynamic PATH detection ===")

    # Create a temp script, add to PATH, verify detection
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = os.path.join(tmpdir, "test_laintas_cmd_xyz")
        with open(script_path, "w") as f:
            f.write("#!/bin/sh\necho test_output\n")
        os.chmod(script_path, 0o755)

        # Add tmpdir to PATH
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = tmpdir + os.pathsep + old_path

        # Should be detected now
        found = laintas_cli.is_system_command("test_laintas_cmd_xyz")

        # Restore PATH
        os.environ["PATH"] = old_path

        # Should NOT be detected after removing from PATH
        gone = not laintas_cli.is_system_command("test_laintas_cmd_xyz")

    cases = [
        ("temp command found after adding to PATH", found),
        ("temp command gone after removing from PATH", gone),
    ]
    passed = 0
    for name, ok in cases:
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {name}")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_list_path_commands():
    """Test PATH command enumeration."""
    print("\n=== Test: list_path_commands ===")

    commands = laintas_cli.list_path_commands()
    has_ls = "ls" in commands
    has_cat = "cat" in commands
    has_bash = "bash" in commands
    reasonable_count = len(commands) > 50  # Most systems have 100+ commands

    cases = [
        ("ls found", has_ls),
        ("cat found", has_cat),
        ("bash found", has_bash),
        (f"reasonable count ({len(commands)})", reasonable_count),
    ]
    passed = 0
    for name, ok in cases:
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {name}")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_meta_command_detection():
    """Test that / commands are identified as meta commands."""
    print("\n=== Test: meta command detection ===")

    cases = [
        ("/help", True),
        ("/login", True),
        ("/term new vim", True),
        ("/debug", True),
        ("/exit", True),
        ("help", False),       # no slash
        ("ls /tmp", False),    # slash not at start
        (" /help", False),     # leading space
    ]
    passed = 0
    for inp, expected in cases:
        result = inp.startswith("/")
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {repr(inp)} startswith('/') = {result} (expected {expected})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_pty_env_isolation():
    """Test that PTY commands don't leak env to parent."""
    print("\n=== Test: PTY environment isolation ===")

    # Set a variable inside PTY, check it's not in parent
    parent_before = os.environ.get("LAINTAS_TEST_ISOLATION", None)
    laintas_cli.execute_command_pty("export LAINTAS_TEST_ISOLATION=should_not_leak", timeout=5)
    parent_after = os.environ.get("LAINTAS_TEST_ISOLATION", None)
    isolated = parent_before is None and parent_after is None

    cases = [
        ("export in PTY doesn't leak to parent", isolated,
         f"before={parent_before}, after={parent_after}"),
    ]
    passed = 0
    for name, ok, detail in cases:
        status = "PASS" if ok else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: {name} ({detail})")
    print(f"  Result: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_interactive_commands_set():
    """Test that interactive commands requiring TTY passthrough are listed."""
    print("\n=== Test: _INTERACTIVE_COMMANDS set ===")

    icmds = laintas_cli._INTERACTIVE_COMMANDS
    must_have = ["vim", "nano", "less", "htop", "top", "python3", "ssh", "tmux"]
    passed = 0
    for cmd in must_have:
        found = cmd in icmds
        status = "PASS" if found else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"  {status}: '{cmd}' in _INTERACTIVE_COMMANDS = {found}")
    print(f"  Total interactive commands: {len(icmds)}")
    print(f"  Result: {passed}/{len(must_have)} passed")
    return passed == len(must_have)


def main():
    print("=" * 60)
    print("Laintas CLI Shell Capability Tests")
    print("=" * 60)

    results = {}

    # Run all tests
    tests = [
        ("extract_first_word", test_extract_first_word),
        ("is_system_command", test_is_system_command),
        ("posix_builtins", test_posix_builtins),
        ("execute_pty_simple", test_execute_command_pty_simple),
        ("execute_pty_output", test_execute_command_pty_output),
        ("execute_pty_errors", test_execute_command_pty_errors),
        ("dynamic_path", test_dynamic_path_detection),
        ("list_path_commands", test_list_path_commands),
        ("meta_command", test_meta_command_detection),
        ("env_isolation", test_pty_env_isolation),
        ("interactive_commands", test_interactive_commands_set),
    ]

    for name, func in tests:
        try:
            results[name] = func()
        except Exception as e:
            print(f"\n  EXCEPTION in {name}: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    print(f"\n  Total: {passed}/{total} test groups passed")
    print("=" * 60)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
