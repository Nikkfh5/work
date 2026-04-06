"""
qa_core.py — Universal QA Loop engine.

Drop this file into any project's debug/ directory.
Provides append-only log management, ID tracking, coverage analysis,
and context building for LLM-powered test generation.

Does NOT depend on any project-specific code.
Does NOT import anything outside stdlib.

Usage:
    from qa_core import QALoop

    qa = QALoop(Path("debug/"))
    exp_id = qa.start_experiment("smoke test basic features")
    qa.log_finding("HIGH", "Server returns 500 on empty POST body")
    qa.log_proposal("HIGH", "Add input validation middleware")
    qa.finish_experiment(exp_id, {"passed": 3, "failed": 1, "total": 4})
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


class QALoop:
    """Core QA loop engine with append-only institutional memory."""

    def __init__(self, debug_dir: str | Path):
        self.debug_dir = Path(debug_dir)
        self.experiments_file = self.debug_dir / "experiments.md"
        self.findings_file = self.debug_dir / "findings.md"
        self.proposals_file = self.debug_dir / "proposals.md"
        self.fixes_file = self.debug_dir / "fixes.md"

    # ── ID Management ─────────────────────────────────────────────────

    def _next_id(self, prefix: str, filepath: Path) -> str:
        """Get next sequential ID (e.g., EXP-010, BUG-028)."""
        if not filepath.exists():
            return f"{prefix}-001"

        content = filepath.read_text(encoding="utf-8")
        pattern = rf"{prefix}-(\d{{3}})"
        matches = re.findall(pattern, content)

        if not matches:
            return f"{prefix}-001"

        max_num = max(int(m) for m in matches)
        return f"{prefix}-{max_num + 1:03d}"

    # ── Experiments ───────────────────────────────────────────────────

    def start_experiment(self, strategy: str) -> str:
        """Start a new experiment. Returns EXP-XXX id."""
        exp_id = self._next_id("EXP", self.experiments_file)
        ts = _now()

        block = (
            f"\n---\n\n"
            f"## {exp_id} — {ts}\n\n"
            f"**Strategy:** {strategy}\n\n"
            f"**Status:** IN PROGRESS\n\n"
        )
        _append(self.experiments_file, block)
        return exp_id

    def finish_experiment(
        self,
        exp_id: str,
        results: dict,
        bugs_found: list[str] | None = None,
        notes: str = "",
    ) -> None:
        """Finish an experiment with results summary."""
        ts = _now()

        passed = results.get("passed", 0)
        failed = results.get("failed", 0)
        total = results.get("total", passed + failed)
        cost = results.get("cost_usd", None)
        duration = results.get("duration_sec", None)

        lines = [
            f"\n### {exp_id} Results — {ts}\n",
            f"- **Passed:** {passed}/{total}",
            f"- **Failed:** {failed}/{total}",
        ]

        if cost is not None:
            lines.append(f"- **Cost:** ${cost:.4f}")
        if duration is not None:
            lines.append(f"- **Duration:** {duration}s")

        if bugs_found:
            lines.append(f"- **Bugs found:** {', '.join(bugs_found)}")

        if notes:
            lines.append(f"\n**Notes:**\n{notes}")

        lines.append(f"\n**Status:** DONE\n")
        _append(self.experiments_file, "\n".join(lines))

    # ── Findings ──────────────────────────────────────────────────────

    def log_finding(
        self,
        severity: str,
        description: str,
        details: str = "",
        tags: list[str] | None = None,
    ) -> str:
        """Log a finding (bug/issue). Returns BUG-XXX id."""
        bug_id = self._next_id("BUG", self.findings_file)
        ts = _now()

        tag_str = f" [{', '.join(tags)}]" if tags else ""

        block = (
            f"\n### {bug_id} [{severity}]{tag_str} — {ts}\n\n"
            f"{description}\n"
        )
        if details:
            block += f"\n**Details:**\n{details}\n"

        _append(self.findings_file, block)
        return bug_id

    def log_positive(self, description: str) -> None:
        """Log a positive finding (something that works well)."""
        ts = _now()
        block = f"\n### [OK] {ts}\n\n{description}\n"
        _append(self.findings_file, block)

    # ── Proposals ─────────────────────────────────────────────────────

    def log_proposal(
        self,
        priority: str,
        title: str,
        description: str = "",
        related_bugs: list[str] | None = None,
    ) -> str:
        """Log an improvement proposal. Returns PROP-XXX id."""
        prop_id = self._next_id("PROP", self.proposals_file)
        ts = _now()

        block = f"\n### {prop_id} [{priority}] — {ts}\n\n**{title}**\n"
        if description:
            block += f"\n{description}\n"
        if related_bugs:
            block += f"\nRelated: {', '.join(related_bugs)}\n"

        _append(self.proposals_file, block)
        return prop_id

    # ── Fixes ─────────────────────────────────────────────────────────

    def log_fix(
        self,
        bugs_closed: list[str],
        description: str,
        files_changed: list[str],
        retest_procedure: str,
        risks: str = "",
    ) -> str:
        """Log a fix report. Returns FIX-XXX id."""
        fix_id = self._next_id("FIX", self.fixes_file)
        ts = _now()

        block = (
            f"\n---\n\n"
            f"## {fix_id} — {ts}\n\n"
            f"**Bugs closed:** {', '.join(bugs_closed)}\n\n"
            f"**What changed:**\n{description}\n\n"
            f"**Files changed:**\n"
        )
        for f in files_changed:
            block += f"- `{f}`\n"

        block += (
            f"\n**Re-test procedure:**\n{retest_procedure}\n\n"
            f"**Verified:** NOT YET\n"
        )
        if risks:
            block += f"\n**Risks:**\n{risks}\n"

        _append(self.fixes_file, block)
        return fix_id

    # ── State Reading ─────────────────────────────────────────────────

    def get_findings(self) -> str:
        """Read all findings as text."""
        return _read(self.findings_file)

    def get_proposals(self) -> str:
        """Read all proposals as text."""
        return _read(self.proposals_file)

    def get_experiments(self) -> str:
        """Read all experiments as text."""
        return _read(self.experiments_file)

    def get_fixes(self) -> str:
        """Read all fixes as text."""
        return _read(self.fixes_file)

    def get_open_bugs(self) -> list[str]:
        """Get BUG IDs that are NOT mentioned in fixes.md."""
        findings = _read(self.findings_file)
        fixes = _read(self.fixes_file)

        found = set(re.findall(r"BUG-\d{3}", findings))
        fixed = set(re.findall(r"BUG-\d{3}", fixes))
        return sorted(found - fixed)

    def get_unverified_fixes(self) -> list[str]:
        """Get FIX IDs that have 'Verified: NOT YET'."""
        fixes = _read(self.fixes_file)
        unverified = []
        for match in re.finditer(r"(FIX-\d{3}).*?Verified:\s*(NOT YET|NO)", fixes, re.DOTALL):
            unverified.append(match.group(1))
        return unverified

    # ── Coverage ──────────────────────────────────────────────────────

    def get_coverage(self, features: dict[str, list[str]]) -> dict[str, dict]:
        """
        Check which features have been tested based on experiments.md content.

        Args:
            features: {"feature_name": ["keyword1", "keyword2"]}

        Returns:
            {"feature_name": {"tested": True/False, "count": N, "keywords_hit": [...]}}
        """
        experiments = _read(self.experiments_file)
        findings = _read(self.findings_file)
        all_text = experiments + findings

        result = {}
        for feature, keywords in features.items():
            hits = [kw for kw in keywords if kw.lower() in all_text.lower()]
            result[feature] = {
                "tested": len(hits) > 0,
                "count": len(hits),
                "keywords_hit": hits,
            }
        return result

    def suggest_focus(self, features: dict[str, list[str]]) -> str:
        """Suggest what to test next based on coverage gaps."""
        coverage = self.get_coverage(features)
        untested = [f for f, info in coverage.items() if not info["tested"]]
        open_bugs = self.get_open_bugs()
        unverified = self.get_unverified_fixes()

        parts = []
        if unverified:
            parts.append(
                f"PRIORITY 1: Re-test unverified fixes: {', '.join(unverified)}"
            )
        if open_bugs:
            parts.append(
                f"PRIORITY 2: Reproduce open bugs: {', '.join(open_bugs[:5])}"
            )
        if untested:
            parts.append(
                f"PRIORITY 3: Test untested features: {', '.join(untested)}"
            )
        if not parts:
            parts.append(
                "All features covered, all fixes verified. "
                "Generate novel edge-case tests."
            )
        return "\n".join(parts)

    # ── Context for LLM ───────────────────────────────────────────────

    def build_llm_context(self, max_chars: int = 4000) -> str:
        """
        Build context string from past runs for LLM test generation.

        Includes: open bugs, unverified fixes, recent findings,
        coverage gaps. Keeps within max_chars.
        """
        parts = []

        open_bugs = self.get_open_bugs()
        if open_bugs:
            parts.append(f"Open bugs: {', '.join(open_bugs)}")

        unverified = self.get_unverified_fixes()
        if unverified:
            parts.append(f"Unverified fixes: {', '.join(unverified)}")

        # Recent findings (last 1500 chars)
        findings = self.get_findings()
        if findings:
            parts.append(f"Recent findings:\n{findings[-1500:]}")

        # Recent proposals (last 500 chars)
        proposals = self.get_proposals()
        if proposals:
            parts.append(f"Proposals:\n{proposals[-500:]}")

        context = "\n\n".join(parts)
        if len(context) > max_chars:
            context = context[:max_chars] + "\n...(truncated)"
        return context

    # ── Markdown Bridge ───────────────────────────────────────────────

    def get_actionable_for_main_project(self) -> str:
        """
        Generate a summary of actionable items for the main project session.

        This is the 'markdown bridge' — debug/ session writes findings,
        main project session reads them and fixes bugs.
        Returns markdown suitable for a developer to act on.
        """
        parts = ["# Actionable Items from QA Loop\n"]

        # Unverified fixes — need re-test
        unverified = self.get_unverified_fixes()
        if unverified:
            parts.append("## Unverified Fixes (re-test needed)")
            fixes = self.get_fixes()
            for fix_id in unverified:
                # Extract re-test procedure
                match = re.search(
                    rf"{fix_id}.*?Re-test procedure:\n(.*?)(?:\n\*\*|\Z)",
                    fixes,
                    re.DOTALL,
                )
                if match:
                    parts.append(f"- **{fix_id}**: {match.group(1).strip()[:200]}")
                else:
                    parts.append(f"- **{fix_id}**: re-test procedure not found")

        # Open bugs — need fixing
        open_bugs = self.get_open_bugs()
        if open_bugs:
            parts.append("\n## Open Bugs (need fixing)")
            findings = self.get_findings()
            for bug_id in open_bugs[:10]:
                match = re.search(
                    rf"{bug_id}\s*\[(\w+)\].*?\n\n(.*?)(?:\n###|\Z)",
                    findings,
                    re.DOTALL,
                )
                if match:
                    severity = match.group(1)
                    desc = match.group(2).strip()[:200]
                    parts.append(f"- **{bug_id}** [{severity}]: {desc}")
                else:
                    parts.append(f"- **{bug_id}**: description not found")

        # Top proposals
        proposals = self.get_proposals()
        critical = re.findall(r"(PROP-\d{3})\s*\[CRITICAL\].*?\n\n\*\*(.*?)\*\*", proposals)
        high = re.findall(r"(PROP-\d{3})\s*\[HIGH\].*?\n\n\*\*(.*?)\*\*", proposals)
        if critical or high:
            parts.append("\n## Top Proposals")
            for prop_id, title in (critical + high)[:5]:
                parts.append(f"- **{prop_id}**: {title}")

        return "\n".join(parts)

    # ── Stats ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Get overall statistics."""
        findings = _read(self.findings_file)
        proposals = _read(self.proposals_file)
        experiments = _read(self.experiments_file)
        fixes = _read(self.fixes_file)

        return {
            "experiments": len(re.findall(r"EXP-\d{3}", experiments)),
            "bugs_found": len(set(re.findall(r"BUG-\d{3}", findings))),
            "bugs_open": len(self.get_open_bugs()),
            "proposals": len(set(re.findall(r"PROP-\d{3}", proposals))),
            "fixes": len(set(re.findall(r"FIX-\d{3}", fixes))),
            "fixes_unverified": len(self.get_unverified_fixes()),
        }


# ── Helpers (module-level, no dependencies) ────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _append(filepath: Path, text: str) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text)


def _read(filepath: Path) -> str:
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return ""


# ── CLI ────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI interface for qa_core."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="QA Loop Core — institutional memory manager")
    parser.add_argument("--dir", default=".", help="Debug directory (default: current)")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("stats", help="Show QA loop statistics")
    sub.add_parser("open-bugs", help="List open (unfixed) bugs")
    sub.add_parser("unverified", help="List unverified fixes")
    sub.add_parser("actionable", help="Generate actionable items for main project")
    sub.add_parser("context", help="Build LLM context from past runs")

    focus_p = sub.add_parser("focus", help="Suggest what to test next")
    focus_p.add_argument("--features", type=str, help="JSON file with features map")

    args = parser.parse_args()
    qa = QALoop(args.dir)

    if args.command == "stats":
        s = qa.stats()
        print(f"Experiments:       {s['experiments']}")
        print(f"Bugs found:        {s['bugs_found']}")
        print(f"Bugs open:         {s['bugs_open']}")
        print(f"Proposals:         {s['proposals']}")
        print(f"Fixes:             {s['fixes']}")
        print(f"Fixes unverified:  {s['fixes_unverified']}")

    elif args.command == "open-bugs":
        bugs = qa.get_open_bugs()
        if bugs:
            for b in bugs:
                print(b)
        else:
            print("No open bugs.")

    elif args.command == "unverified":
        fixes = qa.get_unverified_fixes()
        if fixes:
            for f in fixes:
                print(f)
        else:
            print("All fixes verified.")

    elif args.command == "actionable":
        print(qa.get_actionable_for_main_project())

    elif args.command == "context":
        print(qa.build_llm_context())

    elif args.command == "focus":
        features: dict[str, list[str]] = {}
        if args.features:
            with open(args.features) as f:
                features = json.load(f)
        print(qa.suggest_focus(features))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
