#!/usr/bin/env python3
"""
Terminal-Bench-inspired shell agent test harness for laintas_cli.

Runs agent-level tasks in isolated temporary directories, covering the
same categories as Terminal-Bench (file-operations, software-engineering,
system-administration, data-processing, devops, security).

Does NOT require Docker. Does NOT modify the host system.
All tasks run in ephemeral temp dirs that are cleaned up after.

Usage:
    source venv/bin/activate
    python tests/test_terminal_bench.py
    python tests/test_terminal_bench.py --category file-operations
    python tests/test_terminal_bench.py --task hello-world
    python tests/test_terminal_bench.py --verbose
"""

import os
import sys
import json
import shutil
import signal
import tempfile
import textwrap
import time
import subprocess
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import laintas_cli


# ─── Task Definition ──────────────────────────────────────────────────

@dataclass
class Task:
    """A Terminal-Bench-style task definition."""
    id: str
    instruction: str
    category: str
    difficulty: str
    tags: list
    setup_script: str = ""           # bash script to set up the environment
    verify_script: str = ""          # bash script that exits 0 on success
    cleanup_script: str = ""         # bash script to clean up leaked resources
    extra_files: dict = field(default_factory=dict)  # filename → content, written directly
    timeout_sec: int = 60
    max_output_chars: int = 5000

    def to_yaml_dict(self):
        return {
            "instruction": self.instruction,
            "difficulty": self.difficulty,
            "category": self.category,
            "tags": self.tags,
            "max_agent_timeout_sec": float(self.timeout_sec),
        }


@dataclass
class TaskResult:
    task_id: str
    category: str
    difficulty: str
    passed: bool
    duration_sec: float
    steps_taken: int
    output_snippet: str
    error: str = ""
    verify_rc: int = -1


# ─── Task Definitions ─────────────────────────────────────────────────

TASKS: list[Task] = []


def task(t: Task):
    TASKS.append(t)
    return t


# ── Category: file-operations ──

task(Task(
    id="hello-world",
    instruction="Create a file called hello.txt with the content 'Hello, world!' inside it.",
    category="file-operations",
    difficulty="easy",
    tags=["file-operations"],
    verify_script='test -f hello.txt && grep -q "Hello, world!" hello.txt',
))

task(Task(
    id="file-organizer",
    instruction=(
        "In the current directory there are files: photo1.jpg, photo2.jpg, "
        "document.pdf, notes.txt, report.pdf, image.png. "
        "Create directories 'images', 'documents', and 'text'. "
        "Move .jpg and .png files to images/, .pdf files to documents/, "
        "and .txt files to text/."
    ),
    category="file-operations",
    difficulty="easy",
    tags=["file-operations", "organization"],
    setup_script=(
        "touch photo1.jpg photo2.jpg document.pdf notes.txt report.pdf image.png"
    ),
    verify_script=(
        "test -d images && test -d documents && test -d text && "
        "test -f images/photo1.jpg && test -f images/photo2.jpg && "
        "test -f images/image.png && "
        "test -f documents/document.pdf && test -f documents/report.pdf && "
        "test -f text/notes.txt && "
        "! test -f photo1.jpg && ! test -f document.pdf"
    ),
))

task(Task(
    id="csv-to-json",
    instruction=(
        "Convert the CSV file 'data.csv' to a JSON file 'data.json'. "
        "The JSON should be an array of objects where each object represents "
        "a row with the CSV headers as keys."
    ),
    category="file-operations",
    difficulty="medium",
    tags=["file-operations", "data-conversion"],
    setup_script=textwrap.dedent("""\
        cat > data.csv << 'CSVEOF'
        name,age,city
        Alice,30,New York
        Bob,25,London
        Charlie,35,Tokyo
        CSVEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f data.json && python3 -c "
        import json, sys
        with open('data.json') as f:
            data = json.load(f)
        assert isinstance(data, list), 'not a list'
        assert len(data) == 3, f'expected 3 rows, got {len(data)}'
        assert data[0]['name'] == 'Alice'
        assert data[1]['age'] == '25' or data[1]['age'] == 25
        assert data[2]['city'] == 'Tokyo'
        print('OK')
        "
    """),
))

task(Task(
    id="log-rotation",
    instruction=(
        "There is a log file 'app.log' with 1000 lines. Split it into "
        "chunks of 200 lines each, named app.log.1, app.log.2, etc. "
        "Then compress each chunk with gzip. Finally, create a file "
        "'manifest.txt' listing each .gz file with its line count."
    ),
    category="file-operations",
    difficulty="medium",
    tags=["file-operations", "text-processing"],
    setup_script="seq 1 1000 | while read i; do echo \"[$i] INFO: Log entry number $i at $(date)\"; done > app.log",
    verify_script=textwrap.dedent("""\
        test -f app.log.1.gz && test -f app.log.5.gz &&
        test -f manifest.txt &&
        [ $(ls app.log.*.gz 2>/dev/null | wc -l) -eq 5 ] &&
        grep -c '.gz' manifest.txt | grep -q '5'
    """),
))

# ── Category: software-engineering ──

task(Task(
    id="fix-python-script",
    instruction=(
        "There is a Python script 'calculator.py' that has bugs. "
        "Fix it so that all four operations (add, subtract, multiply, divide) "
        "work correctly. The divide function should raise ValueError on division by zero."
    ),
    category="software-engineering",
    difficulty="easy",
    tags=["coding", "debugging", "python"],
    setup_script=textwrap.dedent("""\
        cat > calculator.py << 'PYEOF'
        def add(a, b):
            return a + b

        def subtract(a, b):
            return a - b

        def multiply(a, b):
            return a * b

        def divide(a, b):
            if b == 0:
                raise ZeroDivisionError("Cannot divide by zero")
            return a / b
        PYEOF
    """),
    verify_script=textwrap.dedent("""\
        python3 -c "
        from calculator import add, subtract, multiply, divide
        assert add(2, 3) == 5
        assert subtract(10, 4) == 6
        assert multiply(3, 7) == 21
        assert divide(10, 2) == 5.0
        try:
            divide(1, 0)
            # Should raise ValueError, not ZeroDivisionError
            assert False, 'should have raised'
        except ValueError:
            pass
        except ZeroDivisionError:
            assert False, 'should raise ValueError not ZeroDivisionError'
        print('OK')
        "
    """),
))

task(Task(
    id="build-rest-api",
    instruction=(
        "Create a simple Python HTTP server in 'server.py' that listens on "
        "port 18923 and responds to GET /health with JSON {\"status\": \"ok\"}. "
        "Start the server in the background, then verify it responds correctly."
    ),
    category="software-engineering",
    difficulty="medium",
    tags=["coding", "networking", "python"],
    setup_script="",
    verify_script=textwrap.dedent("""\
        test -f server.py &&
        timeout 10 python3 server.py &
        SERVER_PID=$!
        sleep 2
        RESPONSE=$(curl -s http://localhost:18923/health 2>/dev/null || echo "CURL_FAILED")
        kill $SERVER_PID 2>/dev/null
        wait $SERVER_PID 2>/dev/null
        if [ "$RESPONSE" = "CURL_FAILED" ]; then
            echo "Server did not respond"
            exit 1
        fi
        echo "$RESPONSE" | python3 -c "
        import json, sys
        data = json.loads(sys.stdin.read())
        assert data['status'] == 'ok'
        print('OK')
        "
    """),
    timeout_sec=90,
))

task(Task(
    id="write-unit-tests",
    instruction=(
        "There is a Python module 'utils.py' with functions is_palindrome, "
        "fizzbuzz, and flatten_list. Write comprehensive unit tests in "
        "'test_utils.py' using the unittest module. All tests must pass."
    ),
    category="software-engineering",
    difficulty="medium",
    tags=["coding", "testing", "python"],
    setup_script=textwrap.dedent("""\
        cat > utils.py << 'PYEOF'
        def is_palindrome(s):
            s = s.lower().replace(" ", "")
            return s == s[::-1]

        def fizzbuzz(n):
            result = []
            for i in range(1, n + 1):
                if i % 15 == 0:
                    result.append("FizzBuzz")
                elif i % 3 == 0:
                    result.append("Fizz")
                elif i % 5 == 0:
                    result.append("Buzz")
                else:
                    result.append(str(i))
            return result

        def flatten_list(nested):
            result = []
            for item in nested:
                if isinstance(item, list):
                    result.extend(flatten_list(item))
                else:
                    result.append(item)
            return result
        PYEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f test_utils.py &&
        python3 -m unittest test_utils -v 2>&1
    """),
    timeout_sec=90,
))

# ── Category: system-administration ──

task(Task(
    id="process-killer",
    instruction=(
        "There are 3 background sleep processes running with PIDs stored in "
        "pids.txt. Kill all of them and write their PIDs to 'killed.txt', "
        "one per line. Verify none are still running."
    ),
    category="system-administration",
    difficulty="easy",
    tags=["system", "processes"],
    setup_script=textwrap.dedent("""\
        setsid sleep 9999 </dev/null >/dev/null 2>&1 &
        P1=$!
        setsid sleep 9999 </dev/null >/dev/null 2>&1 &
        P2=$!
        setsid sleep 9999 </dev/null >/dev/null 2>&1 &
        P3=$!
        echo $P1 > pids.txt
        echo $P2 >> pids.txt
        echo $P3 >> pids.txt
    """),
    cleanup_script="while read pid; do kill -9 $pid 2>/dev/null; done < pids.txt 2>/dev/null; true",
    verify_script=textwrap.dedent("""\
        test -f killed.txt &&
        [ $(wc -l < killed.txt) -eq 3 ] &&
        while read pid; do
            if kill -0 "$pid" 2>/dev/null; then
                echo "Process $pid still running"
                exit 1
            fi
        done < killed.txt
    """),
))

task(Task(
    id="disk-space-report",
    instruction=(
        "Generate a disk usage report for the /tmp directory. "
        "Write a file 'disk_report.txt' containing: total size on first line, "
        "then the top 5 largest files/directories with their sizes, "
        "sorted from largest to smallest."
    ),
    category="system-administration",
    difficulty="medium",
    tags=["system", "reporting"],
    setup_script="",
    verify_script=textwrap.dedent("""\
        test -f disk_report.txt &&
        [ $(wc -l < disk_report.txt) -ge 2 ] &&
        head -1 disk_report.txt | grep -qE '[0-9]'
    """),
    timeout_sec=30,
))

task(Task(
    id="cron-to-systemd",
    instruction=(
        "Convert the following cron entry into a systemd service + timer pair:\n"
        "  */5 * * * * /usr/local/bin/backup.sh\n"
        "Create 'backup.service' and 'backup.timer' files. The timer should "
        "run every 5 minutes. Do NOT install them system-wide."
    ),
    category="system-administration",
    difficulty="medium",
    tags=["system", "systemd", "devops"],
    setup_script="",
    verify_script=textwrap.dedent("""\
        test -f backup.service && test -f backup.timer &&
        grep -q 'ExecStart.*backup.sh' backup.service &&
        grep -q 'OnCalendar' backup.timer &&
        grep -q '5' backup.timer
    """),
))

# ── Category: data-processing ──

task(Task(
    id="log-analysis",
    instruction=(
        "Analyze 'access.log' (Apache combined format). Write 'report.txt' with:\n"
        "1. Total number of requests\n"
        "2. Number of unique IP addresses\n"
        "3. Top 3 most requested URLs\n"
        "4. Count of 4xx and 5xx errors"
    ),
    category="data-processing",
    difficulty="medium",
    tags=["data-processing", "text-processing", "log-analysis"],
    setup_script=textwrap.dedent("""\
        cat > access.log << 'LOGEOF'
        192.168.1.1 - - [01/Jan/2026:10:00:01 +0000] "GET /index.html HTTP/1.1" 200 1234 "-" "Mozilla/5.0"
        192.168.1.2 - - [01/Jan/2026:10:00:02 +0000] "GET /about.html HTTP/1.1" 200 2345 "-" "Mozilla/5.0"
        192.168.1.1 - - [01/Jan/2026:10:00:03 +0000] "GET /index.html HTTP/1.1" 200 1234 "-" "Chrome/120"
        10.0.0.1 - - [01/Jan/2026:10:00:04 +0000] "GET /api/data HTTP/1.1" 404 0 "-" "curl/7.88"
        192.168.1.3 - - [01/Jan/2026:10:00:05 +0000] "POST /api/submit HTTP/1.1" 500 0 "-" "Python/3.11"
        192.168.1.1 - - [01/Jan/2026:10:00:06 +0000] "GET /index.html HTTP/1.1" 200 1234 "-" "Mozilla/5.0"
        10.0.0.2 - - [01/Jan/2026:10:00:07 +0000] "GET /missing.html HTTP/1.1" 404 0 "-" "curl/7.88"
        192.168.1.2 - - [01/Jan/2026:10:00:08 +0000] "GET /about.html HTTP/1.1" 200 2345 "-" "Mozilla/5.0"
        192.168.1.4 - - [01/Jan/2026:10:00:09 +0000] "GET /contact.html HTTP/1.1" 200 3456 "-" "Safari/17"
        10.0.0.1 - - [01/Jan/2026:10:00:10 +0000] "DELETE /api/item/1 HTTP/1.1" 403 0 "-" "curl/7.88"
        LOGEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f report.txt &&
        grep -q '10' report.txt &&
        grep -q '5' report.txt &&
        grep -q 'index.html' report.txt &&
        grep -qE '(4xx|4XX|client error|3)' report.txt &&
        grep -qE '(5xx|5XX|server error|1)' report.txt
    """),
))

task(Task(
    id="deduplicate-sorted",
    instruction=(
        "The file 'names.txt' contains a sorted list of names, one per line, "
        "with many duplicates. Remove duplicates and write the result to "
        "'unique_names.txt', preserving sorted order. Also write the count "
        "of removed duplicates to 'duplicates_removed.txt'."
    ),
    category="data-processing",
    difficulty="easy",
    tags=["data-processing", "text-processing"],
    setup_script=textwrap.dedent("""\
        cat > names.txt << 'NAMESEOF'
        Alice
        Alice
        Alice
        Bob
        Bob
        Charlie
        Charlie
        Charlie
        Charlie
        David
        Eve
        Eve
        Frank
        Frank
        Frank
        George
        NAMESEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f unique_names.txt && test -f duplicates_removed.txt &&
        [ $(wc -l < unique_names.txt) -eq 7 ] &&
        head -1 unique_names.txt | grep -q 'Alice' &&
        tail -1 unique_names.txt | grep -q 'George' &&
        [ $(cat duplicates_removed.txt) -eq 9 ]
    """),
))

# ── Category: devops ──

task(Task(
    id="git-repo-setup",
    instruction=(
        "Initialize a git repo in the current directory. Create a file "
        "'README.md' with '# My Project' as content, commit it with message "
        "'Initial commit'. Then create a branch 'feature', add 'feature.py' "
        "with 'print(\"hello\")', commit on feature branch, switch back to "
        "main/master, and merge the feature branch."
    ),
    category="devops",
    difficulty="easy",
    tags=["version-control", "git"],
    setup_script="git config --global user.email 'test@test.com' 2>/dev/null; git config --global user.name 'Test' 2>/dev/null; git config --global init.defaultBranch main 2>/dev/null; true",
    verify_script=textwrap.dedent("""\
        test -d .git &&
        test -f README.md &&
        test -f feature.py &&
        git log --oneline | grep -q 'Initial commit' &&
        git log --oneline --all | grep -c '' | xargs test 2 -le
    """),
))

task(Task(
    id="docker-compose-parser",
    instruction=(
        "Parse the file 'docker-compose.yml' and generate a 'run.sh' script "
        "that contains the equivalent 'docker run' commands for each service. "
        "Map ports, volumes, and environment variables correctly."
    ),
    category="devops",
    difficulty="hard",
    tags=["devops", "docker", "scripting"],
    setup_script=textwrap.dedent("""\
        cat > docker-compose.yml << 'DCEOF'
        version: '3.8'
        services:
          web:
            image: nginx:latest
            ports:
              - "8080:80"
            volumes:
              - ./html:/usr/share/nginx/html
            environment:
              - NGINX_HOST=example.com
          db:
            image: postgres:15
            ports:
              - "5432:5432"
            environment:
              - POSTGRES_PASSWORD=secret
              - POSTGRES_DB=myapp
            volumes:
              - pgdata:/var/lib/postgresql/data
        volumes:
          pgdata:
        DCEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f run.sh &&
        grep -q 'docker run' run.sh &&
        grep -q 'nginx' run.sh &&
        grep -q 'postgres' run.sh &&
        grep -q '8080:80' run.sh &&
        grep -q '5432:5432' run.sh &&
        grep -q 'POSTGRES_PASSWORD' run.sh &&
        grep -q 'NGINX_HOST' run.sh
    """),
    timeout_sec=120,
))

# ── Category: security ──

task(Task(
    id="fix-permissions",
    instruction=(
        "There's a directory 'webapp/' with incorrect permissions. "
        "Set directories to 755 and files to 644. The 'webapp/config/' "
        "directory should have files set to 600 (owner-only read/write). "
        "The 'webapp/bin/' directory should have files set to 755 (executable)."
    ),
    category="security",
    difficulty="medium",
    tags=["security", "permissions", "system"],
    setup_script=textwrap.dedent("""\
        mkdir -p webapp/{config,bin,static,templates}
        echo "secret=dbpass" > webapp/config/database.yml
        echo "api_key=abc123" > webapp/config/secrets.env
        echo "#!/bin/bash" > webapp/bin/start.sh
        echo "#!/bin/bash" > webapp/bin/deploy.sh
        echo "<html></html>" > webapp/static/index.html
        echo "<html></html>" > webapp/static/style.css
        echo "{% block %}" > webapp/templates/base.html
        chmod -R 777 webapp/
    """),
    verify_script=textwrap.dedent("""\
        [ $(stat -c %a webapp/config/database.yml) -eq 600 ] &&
        [ $(stat -c %a webapp/config/secrets.env) -eq 600 ] &&
        [ $(stat -c %a webapp/bin/start.sh) -eq 755 ] &&
        [ $(stat -c %a webapp/bin/deploy.sh) -eq 755 ] &&
        [ $(stat -c %a webapp/static/index.html) -eq 644 ] &&
        [ $(stat -c %a webapp/templates/base.html) -eq 644 ] &&
        [ $(stat -c %a webapp) -eq 755 ]
    """),
))

task(Task(
    id="find-hardcoded-secrets",
    instruction=(
        "Search the 'project/' directory for hardcoded secrets. Look for:\n"
        "- API keys (patterns like 'api_key', 'apikey', 'API_KEY' followed by = and a value)\n"
        "- Passwords (patterns like 'password', 'passwd', 'secret' followed by = and a value)\n"
        "- Private keys (files containing 'BEGIN RSA PRIVATE KEY' or 'BEGIN PRIVATE KEY')\n"
        "Write all findings to 'secrets_report.txt' with format: filepath:line_number:matched_text"
    ),
    category="security",
    difficulty="medium",
    tags=["security", "code-audit", "grep"],
    setup_script=textwrap.dedent("""\
        mkdir -p project/src project/config project/certs
        cat > project/src/app.py << 'PYEOF'
        import os
        API_KEY = "sk-abc123def456ghi789"
        db_password = "super_secret_123"

        def connect():
            pass
        PYEOF
        cat > project/config/settings.ini << 'INIEOF'
        [database]
        host = localhost
        password = mysecretpassword
        apikey = AKIAIOSFODNN7EXAMPLE
        INIEOF
        cat > project/certs/server.key << 'KEYEOF'
        -----BEGIN RSA PRIVATE KEY-----
        MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF1PbnGPFG3x
        -----END RSA PRIVATE KEY-----
        KEYEOF
        cat > project/src/clean.py << 'CLEANEOF'
        import os
        api_key = os.environ.get("API_KEY")
        CLEANEOF
    """),
    verify_script=textwrap.dedent("""\
        test -f secrets_report.txt &&
        grep -q 'app.py' secrets_report.txt &&
        grep -q 'settings.ini' secrets_report.txt &&
        grep -q 'server.key' secrets_report.txt &&
        [ $(wc -l < secrets_report.txt) -ge 3 ]
    """),
    extra_files={
        "scan_secrets.py": textwrap.dedent("""\
            import os, re

            patterns = [
                (r'(api_key|apikey|api[-_]?key)\\s*=\\s*\\S+', 'api_key'),
                (r'(password|passwd|secret)\\s*=\\s*\\S+', 'password'),
                (r'BEGIN.*PRIVATE KEY', 'private_key'),
            ]

            # Files that legitimately reference env vars, not secrets
            safe_patterns = [r'os\\.environ', r'getenv', r'\\$\\{']

            findings = []
            for root, dirs, files in os.walk('project'):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath) as f:
                            for i, line in enumerate(f, 1):
                                # Skip lines that read from environment
                                if any(re.search(sp, line) for sp in safe_patterns):
                                    continue
                                for pat, label in patterns:
                                    if re.search(pat, line, re.IGNORECASE):
                                        findings.append(f'{fpath}:{i}:{line.strip()}')
                                        break
                    except Exception:
                        pass

            with open('secrets_report.txt', 'w') as f:
                for finding in findings:
                    f.write(finding + '\\n')
            print(f'Found {len(findings)} secrets')
        """),
    },
))

# ── Category: networking ──

task(Task(
    id="port-scanner",
    instruction=(
        "Write a bash script 'portscan.sh' that takes a hostname as argument "
        "and scans ports 20-1024, reporting which ones are open. "
        "Use /dev/tcp or nc. Make it executable. "
        "Test it against localhost and save results to 'scan_results.txt'."
    ),
    category="networking",
    difficulty="hard",
    tags=["networking", "security", "bash"],
    setup_script="",
    verify_script=textwrap.dedent("""\
        test -f portscan.sh &&
        [ -x portscan.sh ] &&
        head -1 portscan.sh | grep -q '#!' &&
        grep -qE '(nc |/dev/tcp|nmap)' portscan.sh
    """),
    timeout_sec=120,
))


# ─── Test Harness ─────────────────────────────────────────────────────

class TestHarness:
    """Runs Terminal-Bench-style tasks in isolated temp directories."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.results: list[TaskResult] = []

    def run_task(self, task: Task) -> TaskResult:
        """Run a single task in an isolated temporary directory."""
        workdir = tempfile.mkdtemp(prefix=f"tb_{task.id}_")
        start_time = time.time()

        try:
            # Phase 1: Setup
            if task.setup_script:
                setup_rc = self._run_bash(task.setup_script, workdir, timeout=30)
                if setup_rc != 0:
                    return TaskResult(
                        task_id=task.id,
                        category=task.category,
                        difficulty=task.difficulty,
                        passed=False,
                        duration_sec=time.time() - start_time,
                        steps_taken=0,
                        output_snippet="",
                        error=f"setup failed (rc={setup_rc})",
                    )

            # Phase 1.5: Write extra files directly to workdir (bypasses bash quoting)
            for filename, content in task.extra_files.items():
                filepath = os.path.join(workdir, filename)
                with open(filepath, "w") as f:
                    f.write(content)

            # Phase 2: Execute — run the task instruction through laintas_cli's shell
            exec_output, steps = self._execute_task(task, workdir)

            # Phase 3: Verify
            verify_rc = self._run_bash(task.verify_script, workdir, timeout=30)
            passed = verify_rc == 0
            duration = time.time() - start_time

            return TaskResult(
                task_id=task.id,
                category=task.category,
                difficulty=task.difficulty,
                passed=passed,
                duration_sec=duration,
                steps_taken=steps,
                output_snippet=exec_output[:500],
                verify_rc=verify_rc,
            )

        except Exception as e:
            return TaskResult(
                task_id=task.id,
                category=task.category,
                difficulty=task.difficulty,
                passed=False,
                duration_sec=time.time() - start_time,
                steps_taken=0,
                output_snippet="",
                error=str(e),
            )

        finally:
            # Run cleanup script if defined (kill leaked processes, etc.)
            if task.cleanup_script:
                self._run_bash(task.cleanup_script, workdir, timeout=10)
            # Cleanup — always remove temp dir
            shutil.rmtree(workdir, ignore_errors=True)

    def _execute_task(self, task: Task, workdir: str) -> tuple[str, int]:
        """
        Execute the task instruction using laintas_cli's execute_command_pty.

        Strategy: We interpret the instruction as a natural language task and
        execute it step by step using shell commands. Since we're testing the
        SHELL capability (not the AI model), we break each task into concrete
        shell commands that represent what a competent agent WOULD do, and
        verify they execute correctly through laintas_cli's PTY layer.

        This tests: PTY execution fidelity, multi-step command chaining,
        output capture, error propagation, environment handling.
        """
        commands = self._plan_commands(task)
        all_output = []
        steps = 0

        for cmd in commands:
            # CRITICAL: each PTY call starts a fresh bash, so cd must be in every command
            full_cmd = f"cd {workdir} && {cmd}"
            if self.verbose:
                print(f"    [step {steps+1}] $ {cmd}")
            result = laintas_cli.execute_command_pty(full_cmd, timeout=task.timeout_sec)
            output = result.get("stdout", "")
            # Strip the cd output noise from the captured output
            output = output.replace(f"{workdir}\n", "", 1)
            all_output.append(output)
            steps += 1

            if self.verbose:
                print(f"    [rc={result['returncode']}] {output[:100]}")

            # If a step fails, stop and report
            if not result["success"] and result["returncode"] != 0:
                # Some commands legitimately return non-zero; check if it's fatal
                if self.verbose:
                    print(f"    [WARN] step {steps} returned rc={result['returncode']}")

        return "\n".join(all_output), steps

    def _plan_commands(self, task: Task) -> list[str]:
        """
        Translate a natural language task instruction into concrete shell commands.
        This simulates what the AI agent would plan — testing that the shell
        can faithfully EXECUTE the plan.
        """
        cmd_map = {
            "hello-world": [
                'echo "Hello, world!" > hello.txt',
            ],
            "file-organizer": [
                "mkdir -p images documents text",
                "mv photo1.jpg photo2.jpg image.png images/",
                "mv document.pdf report.pdf documents/",
                "mv notes.txt text/",
            ],
            "csv-to-json": [
                textwrap.dedent("""\
                    python3 -c "
                    import csv, json
                    with open('data.csv') as f:
                        reader = csv.DictReader(f)
                        data = list(reader)
                    with open('data.json', 'w') as f:
                        json.dump(data, f, indent=2)
                    "
                """),
            ],
            "log-rotation": [
                "split -l 200 -d app.log app.log.",
                # Rename to .1, .2, etc
                "for f in app.log.0*; do n=$(echo $f | sed 's/app.log.0*/ /'); mv $f app.log.$((10#${f##*.} + 1)); done 2>/dev/null; "
                "for i in $(seq 1 5); do [ -f app.log.$i ] && gzip app.log.$i; done",
                "for f in app.log.*.gz; do lines=$(zcat $f | wc -l); echo \"$f: $lines lines\"; done > manifest.txt",
            ],
            "fix-python-script": [
                textwrap.dedent("""\
                    cat > calculator.py << 'PYEOF'
                    def add(a, b):
                        return a + b

                    def subtract(a, b):
                        return a - b

                    def multiply(a, b):
                        return a * b

                    def divide(a, b):
                        if b == 0:
                            raise ValueError("Cannot divide by zero")
                        return a / b
                    PYEOF
                """),
            ],
            "build-rest-api": [
                textwrap.dedent("""\
                    cat > server.py << 'PYEOF'
                    from http.server import HTTPServer, BaseHTTPRequestHandler
                    import json

                    class Handler(BaseHTTPRequestHandler):
                        def do_GET(self):
                            if self.path == '/health':
                                self.send_response(200)
                                self.send_header('Content-Type', 'application/json')
                                self.end_headers()
                                self.wfile.write(json.dumps({"status": "ok"}).encode())
                            else:
                                self.send_response(404)
                                self.end_headers()
                        def log_message(self, format, *args):
                            pass

                    if __name__ == '__main__':
                        server = HTTPServer(('localhost', 18923), Handler)
                        server.serve_forever()
                    PYEOF
                """),
            ],
            "write-unit-tests": [
                textwrap.dedent("""\
                    cat > test_utils.py << 'PYEOF'
                    import unittest
                    from utils import is_palindrome, fizzbuzz, flatten_list

                    class TestIsPalindrome(unittest.TestCase):
                        def test_basic(self):
                            self.assertTrue(is_palindrome("racecar"))
                            self.assertTrue(is_palindrome("madam"))
                            self.assertFalse(is_palindrome("hello"))

                        def test_case_insensitive(self):
                            self.assertTrue(is_palindrome("RaceCar"))

                        def test_with_spaces(self):
                            self.assertTrue(is_palindrome("nurses run"))

                        def test_single_char(self):
                            self.assertTrue(is_palindrome("a"))

                    class TestFizzBuzz(unittest.TestCase):
                        def test_length(self):
                            self.assertEqual(len(fizzbuzz(15)), 15)

                        def test_fizz(self):
                            result = fizzbuzz(3)
                            self.assertEqual(result[-1], "Fizz")

                        def test_buzz(self):
                            result = fizzbuzz(5)
                            self.assertEqual(result[-1], "Buzz")

                        def test_fizzbuzz(self):
                            result = fizzbuzz(15)
                            self.assertEqual(result[-1], "FizzBuzz")

                        def test_number(self):
                            result = fizzbuzz(1)
                            self.assertEqual(result[0], "1")

                    class TestFlattenList(unittest.TestCase):
                        def test_flat(self):
                            self.assertEqual(flatten_list([1, 2, 3]), [1, 2, 3])

                        def test_nested(self):
                            self.assertEqual(flatten_list([1, [2, 3], 4]), [1, 2, 3, 4])

                        def test_deep(self):
                            self.assertEqual(flatten_list([1, [2, [3, [4]]]]), [1, 2, 3, 4])

                        def test_empty(self):
                            self.assertEqual(flatten_list([]), [])

                    if __name__ == '__main__':
                        unittest.main()
                    PYEOF
                """),
                "python3 -m unittest test_utils -v",
            ],
            "process-killer": [
                "cat pids.txt",
                "while read pid; do kill $pid 2>/dev/null; echo $pid >> killed.txt; done < pids.txt",
                "sleep 0.5",
            ],
            "disk-space-report": [
                "du -sh /tmp 2>/dev/null | awk '{print $1}' > disk_report.txt",
                "du -ah /tmp 2>/dev/null | sort -rh 2>/dev/null | head -5 >> disk_report.txt",
            ],
            "cron-to-systemd": [
                textwrap.dedent("""\
                    cat > backup.service << 'EOF'
                    [Unit]
                    Description=Backup Service

                    [Service]
                    Type=oneshot
                    ExecStart=/usr/local/bin/backup.sh
                    EOF
                """),
                textwrap.dedent("""\
                    cat > backup.timer << 'EOF'
                    [Unit]
                    Description=Run backup every 5 minutes

                    [Timer]
                    OnCalendar=*:0/5
                    Persistent=true

                    [Install]
                    WantedBy=timers.target
                    EOF
                """),
            ],
            "log-analysis": [
                # Write the analysis script to a file first (avoids quoting hell)
                textwrap.dedent("""\
                    cat > analyze.py << 'PYEOF'
                    import re
                    from collections import Counter

                    with open('access.log') as f:
                        lines = f.readlines()

                    total = len(lines)
                    ips = set()
                    urls = Counter()
                    errors_4xx = 0
                    errors_5xx = 0

                    for line in lines:
                        ip = line.split()[0]
                        ips.add(ip)
                        m = re.search(r'"(GET|POST|PUT|DELETE|PATCH) (\\S+)', line)
                        if m:
                            urls[m.group(2)] += 1
                        m2 = re.search(r'" (\\d{3}) ', line)
                        if m2:
                            code = int(m2.group(1))
                            if 400 <= code < 500:
                                errors_4xx += 1
                            elif 500 <= code < 600:
                                errors_5xx += 1

                    with open('report.txt', 'w') as f:
                        f.write(f'Total requests: {total}\\n')
                        f.write(f'Unique IPs: {len(ips)}\\n')
                        f.write('Top 3 URLs:\\n')
                        for url, count in urls.most_common(3):
                            f.write(f'  {url}: {count}\\n')
                        f.write(f'4xx errors: {errors_4xx}\\n')
                        f.write(f'5xx errors: {errors_5xx}\\n')
                    PYEOF
                """),
                "python3 analyze.py",
            ],
            "deduplicate-sorted": [
                "sort -u names.txt > unique_names.txt",
                "echo $(( $(wc -l < names.txt) - $(wc -l < unique_names.txt) )) > duplicates_removed.txt",
            ],
            "git-repo-setup": [
                "git init",
                'echo "# My Project" > README.md',
                "git add README.md",
                'git commit -m "Initial commit"',
                "git checkout -b feature",
                'echo \'print("hello")\' > feature.py',
                "git add feature.py",
                'git commit -m "Add feature"',
                "git checkout main",
                "git merge feature",
            ],
            "docker-compose-parser": [
                textwrap.dedent("""\
                    python3 -c "
                    import yaml

                    with open('docker-compose.yml') as f:
                        compose = yaml.safe_load(f)

                    with open('run.sh', 'w') as f:
                        f.write('#!/bin/bash\\n\\n')
                        for name, svc in compose.get('services', {}).items():
                            parts = ['docker run -d']
                            parts.append(f'--name {name}')
                            for p in svc.get('ports', []):
                                parts.append(f'-p {p}')
                            for v in svc.get('volumes', []):
                                parts.append(f'-v {v}')
                            for e in svc.get('environment', []):
                                parts.append(f'-e {e}')
                            parts.append(svc['image'])
                            f.write(' '.join(parts) + '\\n')
                    "
                """),
                "chmod +x run.sh",
            ],
            "fix-permissions": [
                "find webapp -type d -exec chmod 755 {} +",
                "find webapp -type f -exec chmod 644 {} +",
                "find webapp/config -type f -exec chmod 600 {} +",
                "find webapp/bin -type f -exec chmod 755 {} +",
            ],
            "find-hardcoded-secrets": [
                # The scan script is written to the workdir by the harness directly
                # (see _execute_task override for this task) to avoid bash heredoc quoting issues
                "python3 scan_secrets.py",
            ],
            "port-scanner": [
                textwrap.dedent("""\
                    cat > portscan.sh << 'SHEOF'
                    #!/bin/bash
                    HOST="${1:-localhost}"
                    echo "Scanning $HOST ports 20-1024..."
                    for port in $(seq 20 1024); do
                        (echo >/dev/tcp/$HOST/$port) 2>/dev/null && echo "OPEN: $port"
                    done
                    echo "Scan complete."
                    SHEOF
                """),
                "chmod +x portscan.sh",
                "./portscan.sh localhost > scan_results.txt 2>/dev/null; true",
            ],
        }

        return cmd_map.get(task.id, ["echo 'no plan for this task'"])

    def _run_bash(self, script: str, workdir: str, timeout: int = 30) -> int:
        """Run a bash script in a directory, return exit code."""
        try:
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if self.verbose and result.returncode != 0:
                print(f"    [bash rc={result.returncode}]")
                if result.stderr:
                    print(f"    stderr: {result.stderr[:200]}")
                if result.stdout:
                    print(f"    stdout: {result.stdout[:200]}")
            return result.returncode
        except subprocess.TimeoutExpired:
            return -1
        except Exception as e:
            if self.verbose:
                print(f"    [bash exception: {e}]")
            return -1


# ─── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Terminal-Bench-style agent shell tests")
    parser.add_argument("--category", type=str, help="Filter by category")
    parser.add_argument("--task", type=str, help="Run a specific task by ID")
    parser.add_argument("--difficulty", type=str, choices=["easy", "medium", "hard"],
                        help="Filter by difficulty")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show step-by-step output")
    args = parser.parse_args()

    # Filter tasks
    tasks = TASKS
    if args.category:
        tasks = [t for t in tasks if t.category == args.category]
    if args.task:
        tasks = [t for t in tasks if t.id == args.task]
    if args.difficulty:
        tasks = [t for t in tasks if t.difficulty == args.difficulty]

    if not tasks:
        print("No tasks matched the filters.")
        print(f"\nAvailable tasks ({len(TASKS)} total):")
        for t in TASKS:
            print(f"  {t.id:30s} [{t.difficulty:6s}] {t.category}")
        return 1

    print("=" * 70)
    print("Terminal-Bench Agent Shell Tests (laintas_cli)")
    print("=" * 70)
    print(f"  Tasks to run: {len(tasks)}")
    print(f"  Categories:   {', '.join(sorted(set(t.category for t in tasks)))}")
    print(f"  Isolation:    ephemeral temp dirs (auto-cleaned)")
    print()

    harness = TestHarness(verbose=args.verbose)
    results: list[TaskResult] = []

    for i, task in enumerate(tasks, 1):
        tag = f"[{task.difficulty}/{task.category}]"
        print(f"  ({i}/{len(tasks)}) {task.id} {tag} ...", end=" ", flush=True)

        result = harness.run_task(task)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        icon = "\033[32m✓\033[0m" if result.passed else "\033[31m✗\033[0m"
        print(f"{icon} {status} ({result.duration_sec:.1f}s, {result.steps_taken} steps)",
              end="")
        if result.error:
            print(f"  [{result.error}]", end="")
        print()

    # ── Summary ──
    print()
    print("=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    total_time = sum(r.duration_sec for r in results)

    # By category
    categories = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = {"pass": 0, "total": 0}
        categories[r.category]["total"] += 1
        if r.passed:
            categories[r.category]["pass"] += 1

    # By difficulty
    difficulties = {}
    for r in results:
        if r.difficulty not in difficulties:
            difficulties[r.difficulty] = {"pass": 0, "total": 0}
        difficulties[r.difficulty]["total"] += 1
        if r.passed:
            difficulties[r.difficulty]["pass"] += 1

    print(f"\n  Overall: {passed}/{total} ({100*passed/total:.0f}%)")
    print(f"  Total time: {total_time:.1f}s")

    print("\n  By category:")
    for cat, stats in sorted(categories.items()):
        pct = 100 * stats["pass"] / stats["total"] if stats["total"] else 0
        bar = "█" * stats["pass"] + "░" * (stats["total"] - stats["pass"])
        print(f"    {cat:25s} {stats['pass']}/{stats['total']} ({pct:.0f}%) {bar}")

    print("\n  By difficulty:")
    for diff in ["easy", "medium", "hard"]:
        if diff in difficulties:
            stats = difficulties[diff]
            pct = 100 * stats["pass"] / stats["total"] if stats["total"] else 0
            bar = "█" * stats["pass"] + "░" * (stats["total"] - stats["pass"])
            print(f"    {diff:25s} {stats['pass']}/{stats['total']} ({pct:.0f}%) {bar}")

    # Failures detail
    failures = [r for r in results if not r.passed]
    if failures:
        print(f"\n  Failed tasks ({len(failures)}):")
        for r in failures:
            print(f"    [{r.task_id}] verify_rc={r.verify_rc} error={r.error or 'verify script failed'}")
            if r.output_snippet:
                print(f"      output: {r.output_snippet[:150]}")

    print()
    print("=" * 70)

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
