"""
scaffold.py — Generate Self-QA Loop infrastructure for any project.

Creates a debug/ directory with all components:
- CLAUDE.md (autonomous QA protocol)
- boss.py (starter boss with project-specific hooks)
- monitor.py (CLI task monitor)
- qa_core.py (universal QA engine, copied from this skill)
- config.yaml (project configuration)
- Empty log files (experiments, findings, proposals, fixes)

Usage:
    python .claude/skills/self-qa-loop/scripts/scaffold.py
    python .claude/skills/self-qa-loop/scripts/scaffold.py --project-dir /path/to/project
    python .claude/skills/self-qa-loop/scripts/scaffold.py --type web    # web API project
    python .claude/skills/self-qa-loop/scripts/scaffold.py --type cli    # CLI tool project
    python .claude/skills/self-qa-loop/scripts/scaffold.py --type ai     # AI/ML system
    python .claude/skills/self-qa-loop/scripts/scaffold.py --type lib    # library/package

Run from project root or pass --project-dir.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from textwrap import dedent

SCRIPT_DIR = Path(__file__).resolve().parent
QA_CORE_SRC = SCRIPT_DIR / "qa_core.py"


def detect_project_type(project_dir: Path) -> str:
    """Auto-detect project type from files present."""
    markers = {
        "web": [
            "app.py", "wsgi.py", "asgi.py", "manage.py",
            "server.py", "fastapi", "flask", "django",
            "package.json", "next.config", "vite.config",
        ],
        "cli": [
            "cli.py", "main.py", "__main__.py",
            "setup.py", "pyproject.toml", "Cargo.toml",
        ],
        "ai": [
            "train.py", "model.py", "pipeline.py",
            "agents.yaml", "claude_runner", "supervisor",
            "orchestrat", "worker",
        ],
        "lib": [
            "setup.py", "pyproject.toml", "setup.cfg",
            "Cargo.toml", "go.mod", "package.json",
        ],
    }

    scores: dict[str, int] = {k: 0 for k in markers}

    all_files = []
    for p in project_dir.rglob("*"):
        if ".git" in p.parts or "node_modules" in p.parts or "__pycache__" in p.parts:
            continue
        all_files.append(p.name.lower())
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")[:500].lower()
                all_files.append(content)
            except Exception:
                pass

    all_text = " ".join(all_files)

    for ptype, keywords in markers.items():
        for kw in keywords:
            if kw.lower() in all_text:
                scores[ptype] += 1

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "generic"
    return best


def generate_config_yaml(project_name: str, project_type: str) -> str:
    """Generate config.yaml based on project type."""
    configs = {
        "web": dedent(f"""\
            # Self-QA Loop Configuration
            # Project: {project_name}
            # Type: Web API

            project:
              name: "{project_name}"
              root: "../"

            system:
              start: "python -m {project_name}.main &"
              stop: "pkill -f '{project_name}.main'"
              health: "curl -sf http://localhost:8000/health || echo UNHEALTHY"
              startup_wait: 5  # seconds to wait after start

            tasks:
              # How to submit a test task to your system
              inject_command: "curl -s -X POST http://localhost:8000/api/task -d '{{task}}'"
              # How to check task status
              status_command: "curl -s http://localhost:8000/api/task/{{task_id}}/status"
              # What status means "done"
              done_statuses: ["completed", "done", "success"]
              # What status means "failed"
              fail_statuses: ["error", "failed", "timeout"]

            palettes:
              basic:
                - "GET /api/users — should return 200 with user list"
                - "POST /api/users with valid JSON — should return 201"
                - "GET /api/users/nonexistent — should return 404"
              auth:
                - "POST /api/login with valid credentials"
                - "POST /api/login with invalid password — should return 401"
                - "GET /api/protected without token — should return 403"
              edge_cases:
                - "POST /api/users with empty body — should return 400"
                - "POST /api/users with 10MB body — should handle gracefully"
                - "GET /api/users?page=-1 — should not crash"
              security:
                - "POST /api/users with SQL injection in name field"
                - "POST /api/users with XSS in description"
                - "GET /api/users with path traversal: ../../../etc/passwd"

            monitoring:
              poll_interval: 5  # seconds between status checks
              timeout: 300      # max seconds per task
              log_file: "../logs/server.log"
        """),

        "cli": dedent(f"""\
            # Self-QA Loop Configuration
            # Project: {project_name}
            # Type: CLI Tool

            project:
              name: "{project_name}"
              root: "../"

            system:
              # CLI tools don't need start/stop — each task is a single run
              start: "echo 'CLI tool — no server to start'"
              stop: "echo 'CLI tool — no server to stop'"
              health: "python -m {project_name} --version || echo UNHEALTHY"

            tasks:
              # How to run a test ({{task}} is replaced with the task description)
              inject_command: "python -m {project_name} {{task}}"
              # CLI tools: task completes when command exits
              # exit code 0 = pass, non-zero = fail
              sync: true  # tasks complete synchronously

            palettes:
              basic:
                - "--help"
                - "--version"
                - "valid_input.txt"
              edge_cases:
                - ""  # empty input
                - "/nonexistent/path"
                - "--unknown-flag"
              stress:
                - "very_large_file.txt"  # prepare this beforehand
                - "--verbose --debug all_options_at_once"

            monitoring:
              timeout: 60
              capture_stdout: true
              capture_stderr: true
        """),

        "ai": dedent(f"""\
            # Self-QA Loop Configuration
            # Project: {project_name}
            # Type: AI/ML System

            project:
              name: "{project_name}"
              root: "../"

            system:
              start: "python -m {project_name}.main > logs/debug.log 2>&1 &"
              stop: "pkill -f '{project_name}.main'"
              health: "python debug/monitor.py --health"
              startup_wait: 5

            tasks:
              # How to inject a task (customize for your system)
              inject_command: "python debug/monitor.py inject '{{task}}'"
              status_command: "python debug/monitor.py status {{task_id}}"
              done_statuses: ["done", "completed"]
              fail_statuses: ["error", "requires_manual", "cancelled"]

            palettes:
              basic:
                - "Simple task to verify basic pipeline works"
                - "Task that requires one file as output"
              complex:
                - "Task requiring multiple steps and planning"
                - "Task with ambiguous requirements (should trigger clarification)"
              edge_cases:
                - "Very short task: 'do something'"
                - "Task with special characters and unicode"
              security:
                - "'; DROP TABLE tasks; --"
                - "Task mentioning rm -rf or curl evil.com"

            monitoring:
              poll_interval: 10
              timeout: 600
              log_file: "../logs/debug.log"
              db_path: "../data/app.db"  # if using SQLite
        """),

        "lib": dedent(f"""\
            # Self-QA Loop Configuration
            # Project: {project_name}
            # Type: Library/Package

            project:
              name: "{project_name}"
              root: "../"

            system:
              start: "echo 'Library — no server to start'"
              stop: "echo 'Library — no server to stop'"
              health: "python -c 'import {project_name}; print(\"OK\")'"

            tasks:
              # For libraries, tasks = test scenarios
              inject_command: "python -c '{{task}}'"
              sync: true

            palettes:
              api_surface:
                - "from {project_name} import main_function; main_function()"
                - "Test basic import and version check"
              edge_cases:
                - "Pass None to every public function"
                - "Pass empty collections to every function"
              compatibility:
                - "Test with Python 3.11 features"
                - "Test pickling/serialization of main objects"

            monitoring:
              timeout: 30
              capture_stdout: true
        """),

        "generic": dedent(f"""\
            # Self-QA Loop Configuration
            # Project: {project_name}
            # Type: Generic

            project:
              name: "{project_name}"
              root: "../"

            system:
              # TODO: customize these for your project
              start: "echo 'TODO: add start command'"
              stop: "echo 'TODO: add stop command'"
              health: "echo 'TODO: add health check'"

            tasks:
              # TODO: how to submit/check tasks
              inject_command: "echo 'TODO: inject {{task}}'"
              status_command: "echo 'TODO: check {{task_id}}'"
              done_statuses: ["done"]
              fail_statuses: ["error"]

            palettes:
              basic:
                - "TODO: add basic test tasks"
              edge_cases:
                - "TODO: add edge case tasks"

            monitoring:
              poll_interval: 5
              timeout: 300
        """),
    }

    return configs.get(project_type, configs["generic"])


def generate_claude_md(project_name: str, project_type: str) -> str:
    """Generate CLAUDE.md for the debug/ directory."""
    # Read the template from references
    template_path = SCRIPT_DIR.parent / "references" / "protocol-template.md"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
        template = template.replace("{{PROJECT_NAME}}", project_name)
        template = template.replace("{{PROJECT_TYPE}}", project_type)
        return template

    # Fallback: inline template
    return dedent(f"""\
        # {project_name} — Debug Boss Protocol

        You are the QA boss of {project_name}.
        Your job: run, test, break, and document — autonomously.
        You invent tasks, send them, monitor results, find bugs.

        ---

        ## Autonomous Protocol

        **Phase 1: PREPARATION** (~1 min)
        ```
        1. python debug/monitor.py --health
        2. python debug/qa_core.py --dir debug/ stats
        3. python debug/qa_core.py --dir debug/ focus
        4. Read debug/fixes.md — what was fixed since last run?
        5. Read debug/findings.md — past bugs
        6. Decide: what to focus on this session
        ```

        **Phase 2: START SYSTEM** (~30 sec)
        ```
        1. Read config.yaml for start command
        2. Start the system
        3. Wait for startup
        4. Health check
        ```

        **Phase 3: TESTING** (~10-15 min)
        Choose strategy based on Phase 1:
        A) Untested features → test them
        B) All covered → generate new edge cases
        C) Unverified fixes → re-test them
        D) Open bugs → reproduce and document

        **Phase 4: ANALYSIS** (~2 min)
        ```
        1. python debug/qa_core.py --dir debug/ stats
        2. python debug/qa_core.py --dir debug/ open-bugs
        3. Your own analysis: patterns, root causes
        ```

        **Phase 5: REPORT** (~2 min)
        ```
        1. Write EXP-XXX block to experiments.md
        2. Update proposals.md if new ideas
        3. Brief verdict to user
        ```

        **Phase 6: CLEANUP**
        ```
        1. Stop the system (from config.yaml)
        2. Reset any stuck state
        ```

        ## Rules

        - **DO NOT fix bugs** — only find and document them
        - **DO NOT modify code** — only observe, run, analyze
        - **Document every step** — what you ran, what you saw, what it means
        - **Be creative** — invent edge cases, bad inputs, stress scenarios
        - **Read past state** — always check findings/fixes before starting

        ## Markdown Bridge

        This debug/ session communicates with the main project through .md files:
        - **debug/ writes:** findings.md, proposals.md (bugs found)
        - **main project reads:** findings.md, proposals.md (bugs to fix)
        - **main project writes:** fixes.md (what was fixed)
        - **debug/ reads:** fixes.md (what to re-test)

        Two separate Claude sessions, connected through markdown.

        ## Files

        | File | Purpose | Who writes |
        |------|---------|-----------|
        | experiments.md | Full experiment log | QA boss |
        | findings.md | Bug reports | QA boss (auto) |
        | proposals.md | Improvement ideas | QA boss (auto) |
        | fixes.md | Fix reports + re-test | Developer |
        | config.yaml | System configuration | Developer (once) |
        | qa_core.py | QA engine library | Do not modify |

        ## Tools

        ```bash
        python debug/qa_core.py --dir debug/ stats        # statistics
        python debug/qa_core.py --dir debug/ open-bugs    # unfixed bugs
        python debug/qa_core.py --dir debug/ unverified   # unverified fixes
        python debug/qa_core.py --dir debug/ actionable   # items for main project
        python debug/qa_core.py --dir debug/ context      # LLM context from past runs
        python debug/qa_core.py --dir debug/ focus        # suggest what to test next
        ```
    """)


def generate_boss_py(project_name: str, project_type: str) -> str:
    """Generate starter boss.py."""
    return dedent(f"""\
        \"\"\"
        debug/boss.py — Self-improving debug boss for {project_name}.

        Autonomous QA loop: generate tasks, inject, monitor, analyze, report.

        Usage:
          python debug/boss.py run                   # full cycle
          python debug/boss.py run --batch 5         # run 5 tasks
          python debug/boss.py run --focus security  # focus on security
          python debug/boss.py analyze               # analyze without injection
          python debug/boss.py coverage              # feature coverage report
        \"\"\"

        from __future__ import annotations

        import argparse
        import subprocess
        import sys
        import time
        from pathlib import Path

        # Fix encoding for Windows
        if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

        DEBUG_DIR = Path(__file__).resolve().parent
        sys.path.insert(0, str(DEBUG_DIR))

        from qa_core import QALoop

        qa = QALoop(DEBUG_DIR)

        # ── Configuration ─────────────────────────────────────────────────

        # TODO: Load from config.yaml or hardcode for your project
        CONFIG = {{
            "start": "echo 'TODO: start your system'",
            "stop": "echo 'TODO: stop your system'",
            "health": "echo 'TODO: health check'",
            "startup_wait": 5,
            "poll_interval": 10,
            "timeout": 300,
        }}

        # ── Task Palettes ─────────────────────────────────────────────────

        # TODO: Add your project-specific test tasks
        PALETTES = {{
            "basic": {{
                "description": "Basic functionality tests",
                "tasks": [
                    "TODO: Add basic test task 1",
                    "TODO: Add basic test task 2",
                ],
            }},
            "edge_cases": {{
                "description": "Edge cases and error handling",
                "tasks": [
                    "TODO: Add edge case task 1",
                    "TODO: Add edge case task 2",
                ],
            }},
            "security": {{
                "description": "Security-related tests",
                "tasks": [
                    "'; DROP TABLE users; --",
                    "<script>alert('xss')</script>",
                    "A" * 5000,
                ],
            }},
        }}

        # ── Project-Specific Backend ──────────────────────────────────────
        # TODO: Implement these for your project

        def inject_task(description: str) -> str:
            \"\"\"
            Submit a task to your system. Returns task_id.

            Examples:
              - Web API: POST to /api/tasks
              - CLI: subprocess.run(["tool", description])
              - AI system: insert into DB
              - Library: create test file and run pytest
            \"\"\"
            # TODO: Implement for your project
            print(f"  [INJECT] {{description[:60]}}")
            raise NotImplementedError("Implement inject_task() for your project")


        def check_status(task_id: str) -> dict:
            \"\"\"
            Check task status. Returns {{"status": "done"|"error"|"running", ...}}.

            Examples:
              - Web API: GET /api/tasks/{{task_id}}
              - CLI: check exit code (already done in inject)
              - AI system: query DB
            \"\"\"
            # TODO: Implement for your project
            raise NotImplementedError("Implement check_status() for your project")


        def health_check() -> bool:
            \"\"\"Check if the system under test is healthy.\"\"\"
            try:
                result = subprocess.run(
                    CONFIG["health"], shell=False,
                    capture_output=True, text=True, timeout=10,
                )
                return result.returncode == 0
            except Exception:
                return False


        def start_system() -> bool:
            \"\"\"Start the system under test.\"\"\"
            try:
                subprocess.Popen(
                    CONFIG["start"].split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(CONFIG["startup_wait"])
                return health_check()
            except Exception as exc:
                print(f"  Failed to start: {{exc}}")
                return False


        def stop_system() -> None:
            \"\"\"Stop the system under test.\"\"\"
            try:
                subprocess.run(CONFIG["stop"].split(), timeout=10)
            except Exception:
                pass


        # ── Core Boss Logic ───────────────────────────────────────────────

        def run_cycle(batch: int = 2, focus: str = "", timeout: int = 300) -> None:
            \"\"\"Full QA cycle: inject → monitor → analyze → report.\"\"\"

            # 1. Read past state
            print("\\n=== Phase 1: PREPARATION ===")
            suggestion = qa.suggest_focus(
                {{name: info.get("tasks", []) for name, info in PALETTES.items()}}
            )
            print(f"  Focus suggestion: {{suggestion}}")
            stats = qa.stats()
            print(f"  Stats: {{stats}}")

            # 2. Start experiment
            strategy = f"focus={{focus}}" if focus else "auto (coverage gaps)"
            exp_id = qa.start_experiment(strategy)
            print(f"\\n=== {{exp_id}}: Testing ===")

            # 3. Select tasks
            import random
            if focus and focus in PALETTES:
                tasks = PALETTES[focus]["tasks"][:batch]
            else:
                # Pick from least-covered palette
                all_tasks = []
                for palette in PALETTES.values():
                    all_tasks.extend(palette["tasks"])
                tasks = random.sample(all_tasks, min(batch, len(all_tasks)))

            # 4. Inject and monitor
            results = []
            for task_desc in tasks:
                try:
                    task_id = inject_task(task_desc)
                    print(f"  Injected: {{task_id}}")

                    # Monitor until done
                    start = time.time()
                    while time.time() - start < timeout:
                        status = check_status(task_id)
                        if status["status"] in ("done", "completed"):
                            results.append({{"id": task_id, "status": "pass", "desc": task_desc}})
                            print(f"  PASS: {{task_id}}")
                            break
                        elif status["status"] in ("error", "failed"):
                            results.append({{"id": task_id, "status": "fail", "desc": task_desc, "error": status.get("error", "")}})
                            qa.log_finding("HIGH", f"Task failed: {{task_desc[:80]}}", details=str(status))
                            print(f"  FAIL: {{task_id}}")
                            break
                        time.sleep(CONFIG["poll_interval"])
                    else:
                        results.append({{"id": task_id, "status": "timeout", "desc": task_desc}})
                        qa.log_finding("MEDIUM", f"Task timed out after {{timeout}}s: {{task_desc[:80]}}")

                except NotImplementedError:
                    print("\\n  ERROR: inject_task() not implemented!")
                    print("  Edit debug/boss.py and implement the project-specific backend.")
                    return
                except Exception as exc:
                    results.append({{"status": "error", "desc": task_desc, "error": str(exc)}})
                    qa.log_finding("HIGH", f"Injection error: {{exc}}", details=task_desc)

            # 5. Analyze
            print(f"\\n=== {{exp_id}}: Analysis ===")
            passed = sum(1 for r in results if r["status"] == "pass")
            failed = sum(1 for r in results if r["status"] == "fail")
            total = len(results)
            print(f"  Results: {{passed}}/{{total}} passed, {{failed}}/{{total}} failed")

            # 6. Report
            bugs_found = [f"BUG in task: {{r['desc'][:40]}}" for r in results if r["status"] == "fail"]
            qa.finish_experiment(
                exp_id,
                {{"passed": passed, "failed": failed, "total": total}},
                bugs_found=bugs_found if bugs_found else None,
                notes=f"Focus: {{focus or 'auto'}}. Batch: {{batch}}.",
            )
            print(f"\\n=== {{exp_id}}: Done ===")
            print(f"  Results written to experiments.md")


        def show_coverage() -> None:
            \"\"\"Show feature coverage report.\"\"\"
            features = {{name: info.get("tasks", []) for name, info in PALETTES.items()}}
            coverage = qa.get_coverage(features)
            print("\\n=== Feature Coverage ===")
            for feature, info in coverage.items():
                icon = "v" if info["tested"] else " "
                print(f"  [{{icon}}] {{feature}}: {{info['count']}} hits")

            print(f"\\n{{qa.suggest_focus(features)}}")


        # ── CLI ───────────────────────────────────────────────────────────

        def main() -> None:
            parser = argparse.ArgumentParser(description=f"QA Boss for {project_name}")
            sub = parser.add_subparsers(dest="command")

            run_p = sub.add_parser("run", help="Full QA cycle")
            run_p.add_argument("--batch", type=int, default=2, help="Tasks per cycle")
            run_p.add_argument("--focus", type=str, default="", help="Focus on palette")
            run_p.add_argument("--timeout", type=int, default=300, help="Timeout per task")

            sub.add_parser("analyze", help="Analyze without injection")
            sub.add_parser("coverage", help="Feature coverage report")
            sub.add_parser("stats", help="QA loop statistics")
            sub.add_parser("actionable", help="Items for main project to fix")

            args = parser.parse_args()

            if args.command == "run":
                run_cycle(batch=args.batch, focus=args.focus, timeout=args.timeout)
            elif args.command == "analyze":
                stats = qa.stats()
                for k, v in stats.items():
                    print(f"  {{k}}: {{v}}")
                print()
                print(qa.get_actionable_for_main_project())
            elif args.command == "coverage":
                show_coverage()
            elif args.command == "stats":
                stats = qa.stats()
                for k, v in stats.items():
                    print(f"  {{k}}: {{v}}")
            elif args.command == "actionable":
                print(qa.get_actionable_for_main_project())
            else:
                parser.print_help()


        if __name__ == "__main__":
            main()
    """)


def generate_monitor_py(project_name: str) -> str:
    """Generate monitor.py (simple task injection + status CLI)."""
    return dedent(f"""\
        \"\"\"
        debug/monitor.py — Task injection and monitoring CLI for {project_name}.

        Usage:
          python debug/monitor.py inject "task description"
          python debug/monitor.py status                   # all tasks
          python debug/monitor.py status --watch           # live dashboard
          python debug/monitor.py --health                 # system health
          python debug/monitor.py --reset                  # cancel stuck tasks
        \"\"\"

        from __future__ import annotations

        import argparse
        import sys

        # Fix encoding for Windows
        if sys.stdout.encoding and sys.stdout.encoding.lower().startswith("cp"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")


        def main() -> None:
            parser = argparse.ArgumentParser(description="Monitor for {project_name}")
            sub = parser.add_subparsers(dest="command")

            inject_p = sub.add_parser("inject", help="Inject a task")
            inject_p.add_argument("description", help="Task description")

            status_p = sub.add_parser("status", help="Show task status")
            status_p.add_argument("--watch", action="store_true", help="Auto-refresh")

            parser.add_argument("--health", action="store_true", help="Health check")
            parser.add_argument("--reset", action="store_true", help="Reset stuck tasks")

            args = parser.parse_args()

            if args.health:
                print("TODO: Implement health check for {project_name}")
                print("  - Check if system is running")
                print("  - Check DB/API connectivity")
                print("  - Check log file status")
            elif args.reset:
                print("TODO: Implement reset for {project_name}")
            elif args.command == "inject":
                print(f"TODO: Inject task: {{args.description}}")
            elif args.command == "status":
                print("TODO: Show task status")
            else:
                parser.print_help()


        if __name__ == "__main__":
            main()
    """)


def generate_empty_log(title: str, description: str) -> str:
    """Generate empty log file with header."""
    return dedent(f"""\
        # {title}

        {description}

        ---

    """)


def scaffold(project_dir: Path, project_type: str | None = None) -> None:
    """Create debug/ infrastructure in the given project directory."""
    debug_dir = project_dir / "debug"
    project_name = project_dir.name

    if debug_dir.exists():
        print(f"WARNING: debug/ already exists at {debug_dir}")
        print("  Use --force to overwrite, or manually merge.")
        return

    # Auto-detect project type if not specified
    if not project_type:
        project_type = detect_project_type(project_dir)
        print(f"  Auto-detected project type: {project_type}")

    print(f"\nScaffolding Self-QA Loop for '{project_name}' ({project_type})")
    print(f"  Directory: {debug_dir}\n")

    # Create directories
    debug_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copy qa_core.py
    if QA_CORE_SRC.exists():
        shutil.copy2(QA_CORE_SRC, debug_dir / "qa_core.py")
        print("  + qa_core.py (universal QA engine)")
    else:
        print(f"  ! qa_core.py not found at {QA_CORE_SRC}")

    # 2. Generate CLAUDE.md
    claude_md = generate_claude_md(project_name, project_type)
    (debug_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    print("  + CLAUDE.md (autonomous QA protocol)")

    # 3. Generate config.yaml
    config = generate_config_yaml(project_name, project_type)
    (debug_dir / "config.yaml").write_text(config, encoding="utf-8")
    print("  + config.yaml (project configuration)")

    # 4. Generate boss.py
    boss = generate_boss_py(project_name, project_type)
    (debug_dir / "boss.py").write_text(boss, encoding="utf-8")
    print("  + boss.py (starter QA boss — customize inject_task/check_status)")

    # 5. Generate monitor.py
    monitor = generate_monitor_py(project_name)
    (debug_dir / "monitor.py").write_text(monitor, encoding="utf-8")
    print("  + monitor.py (CLI task monitor)")

    # 6. Create empty log files
    logs = {
        "experiments.md": (
            "Experiments Log",
            "Append-only log of QA experiments. Each EXP-XXX block documents "
            "a test run with strategy, results, and bugs found."
        ),
        "findings.md": (
            "Findings",
            "Auto-generated bug reports and observations. "
            "Each BUG-XXX entry documents a discovered issue."
        ),
        "proposals.md": (
            "Improvement Proposals",
            "Suggestions for improvements based on QA findings. "
            "Each PROP-XXX entry has a priority and description."
        ),
        "fixes.md": (
            "Fix Reports",
            "Documentation of bug fixes with re-test procedures. "
            "Each FIX-XXX entry links to bugs it closes and how to verify."
        ),
    }

    for filename, (title, desc) in logs.items():
        content = generate_empty_log(title, desc)
        (debug_dir / filename).write_text(content, encoding="utf-8")
        print(f"  + {filename}")

    # Summary
    print(f"\n{'='*50}")
    print(f"Self-QA Loop scaffolded at: {debug_dir}")
    print(f"\nNext steps:")
    print(f"  1. Edit debug/config.yaml — set start/stop/health commands")
    print(f"  2. Edit debug/boss.py — implement inject_task() and check_status()")
    print(f"  3. Edit debug/monitor.py — implement health/status for your system")
    print(f"  4. Run: cd {project_dir} && python debug/boss.py run")
    print(f"\nMarkdown Bridge pattern:")
    print(f"  - In debug/ session: 'find bugs' (writes findings.md)")
    print(f"  - In main session:   'fix bugs from debug/findings.md'")
    print(f"  - Two independent Claude sessions, connected through .md files")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold Self-QA Loop infrastructure for any project"
    )
    parser.add_argument(
        "--project-dir", type=str, default=".",
        help="Project root directory (default: current)"
    )
    parser.add_argument(
        "--type", type=str, choices=["web", "cli", "ai", "lib", "generic"],
        help="Project type (auto-detected if not specified)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing debug/ directory"
    )

    args = parser.parse_args()
    project_dir = Path(args.project_dir).resolve()

    if not project_dir.exists():
        print(f"ERROR: Project directory does not exist: {project_dir}")
        sys.exit(1)

    if args.force and (project_dir / "debug").exists():
        shutil.rmtree(project_dir / "debug")

    scaffold(project_dir, args.type)


if __name__ == "__main__":
    main()
