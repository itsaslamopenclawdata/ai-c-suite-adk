"""Static preflight checks for the deploy scripts and Cloud Build config.

Deliberately Python rather than shell. The previous shell version used a
heredoc inside `$(...)`, which bash 3.2 (the macOS default) cannot parse, and
`grep -P`, which BSD grep does not support -- so on macOS the check failed,
hit `|| true`, and reported success. A check that can only pass is worse than
no check at all.

    python3 tests/preflight_lint.py [repo_root]

Exit code 0 if everything passes, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PASS = "\033[1;32m✓\033[0m"
FAIL = "\033[1;31m✗\033[0m"

failures: list[str] = []


def ok(label: str) -> None:
    print(f"  {PASS} {label}")


def bad(label: str, details: list[str]) -> None:
    print(f"  {FAIL} {label}")
    for d in details[:8]:
        print(f"      {d}")
    failures.append(label)


def shell_scripts(root: Path) -> list[Path]:
    return sorted(
        p for p in list(root.glob("deploy/*.sh")) + list(root.glob("tests/*.sh"))
    )


def strip_comments(text: str) -> list[tuple[int, str]]:
    """Return (line_number, line) for lines that are not pure comments."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        out.append((i, line))
    return out


# --------------------------------------------------------------------------
# Portability: these scripts must run on bash 3.2 and BSD userland
# --------------------------------------------------------------------------

PATTERNS = [
    (
        "no associative arrays (declare -A)",
        re.compile(r"declare\s+-A\b"),
        "bash 4+ only",
    ),
    (
        "no ${var^^} / ${var,,} case expansion",
        re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(\^\^|,,)"),
        "bash 4+ only",
    ),
    (
        "no mapfile / readarray",
        re.compile(r"\b(mapfile|readarray)\b"),
        "bash 4+ only",
    ),
    (
        "no &>> append redirect",
        re.compile(r"&>>"),
        "bash 4+ only",
    ),
    (
        "no grep -P",
        re.compile(r"\bgrep\b[^|;\n]*\s-[A-Za-z]*P\b"),
        "BSD grep on macOS has no -P; it fails and any `|| true` hides it",
    ),
    (
        "no sed -i without a backup suffix",
        re.compile(r"\bsed\s+-i\s+(?!\.|'')"),
        "BSD sed requires -i '' ; GNU sed requires -i with no argument",
    ),
    (
        "no unbraced variable touching a non-ASCII character",
        re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]"),
        "bash 3.2 reads the multi-byte char as part of the variable name",
    ),
]

# A heredoc opened inside $( ... ) -- bash 3.2 cannot parse this.
HEREDOC_IN_SUBST = re.compile(r"\$\([^)]*<<-?\s*'?[A-Za-z_]")


def lint_shell(root: Path) -> None:
    print("\n-- Portability lint (bash 3.2 + BSD userland) --")
    scripts = shell_scripts(root)
    if not scripts:
        bad("found shell scripts to lint", ["no deploy/*.sh or tests/*.sh"])
        return

    for label, pattern, why in PATTERNS:
        hits = []
        for path in scripts:
            for num, line in strip_comments(path.read_text(encoding="utf-8")):
                if pattern.search(line):
                    hits.append(f"{path.name}:{num}: {line.strip()[:90]}  ({why})")
        ok(label) if not hits else bad(label, hits)

    hits = []
    for path in scripts:
        for num, line in strip_comments(path.read_text(encoding="utf-8")):
            if HEREDOC_IN_SUBST.search(line):
                hits.append(
                    f"{path.name}:{num}: {line.strip()[:90]}  "
                    "(bash 3.2 cannot parse a heredoc inside $(...); "
                    "put the script in its own file)"
                )
    ok("no heredoc inside $(...)") if not hits else bad("no heredoc inside $(...)", hits)


# --------------------------------------------------------------------------
# Cloud Build config
# --------------------------------------------------------------------------

# Cloud Build resolves these when it parses the file, before substitutions are
# applied, so a ${_VAR} in any of them fails at submit time.
ENUM_FIELDS = (
    "machineType", "logging", "substitutionOption", "requestedVerifyOption",
    "logStreamingOption", "defaultLogsBucketBehavior", "sourceProvenanceHash",
    "status", "pool",
)


def lint_cloudbuild(path: Path) -> None:
    print("\n-- Cloud Build config --")
    if not path.is_file():
        bad("cloudbuild.yaml exists", [str(path)])
        return

    raw = path.read_text(encoding="utf-8")
    text = "\n".join(
        l for l in raw.splitlines() if not l.lstrip().startswith("#")
    )

    problems = []
    for field in ENUM_FIELDS:
        for m in re.finditer(rf"^\s*{field}\s*:\s*(.+?)\s*$", text, re.M):
            if "${" in m.group(1):
                problems.append(
                    f"{field}: substitution '{m.group(1)}' in an enum-typed field"
                )

    used = set(re.findall(r"\$\{(_[A-Z0-9_]+)\}", text))
    block = re.search(r"^substitutions:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    declared = (
        set(re.findall(r"^\s+(_[A-Z0-9_]+)\s*:", block.group(1), re.M)) if block else set()
    )
    for name in sorted(used - declared):
        problems.append(f"{name} is used but not declared under substitutions:")

    ok(f"substitutions used: {', '.join(sorted(used)) or 'none'}")

    try:
        import yaml

        doc = yaml.safe_load(raw)
        if not doc.get("steps"):
            problems.append("no build steps defined")
        ok(f"steps: {len(doc.get('steps', []))}, images: {len(doc.get('images', []))}")
    except ImportError:
        ok("PyYAML not installed; skipped structural parse")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"YAML parse error: {exc}")

    ok("no substitutions in enum-typed fields") if not problems else bad(
        "cloudbuild.yaml is valid", problems
    )


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    lint_shell(root)
    lint_cloudbuild(root / "deploy" / "cloudbuild.yaml")

    if failures:
        print(f"\n\033[1;31m{len(failures)} preflight check(s) failed.\033[0m")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
