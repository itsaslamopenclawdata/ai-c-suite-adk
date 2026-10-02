"""ADK function tools available to the executive agents.

ADK 2.x allows ``tools`` and ``output_schema`` on the same agent -- tools run
during the reasoning loop, and structure is enforced only on the final answer.
That combination is what makes the anti-hallucination design work here: an
agent can *look up* the source lines it intends to cite, then must return a
schema that requires those citations.

The document for the current run is held in a context variable rather than
passed as a tool argument, so an agent cannot reach into another run's
document.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import List, Optional

logger = logging.getLogger(__name__)

# Set per run by the engine before the ADK Runner is invoked.
_current_document: ContextVar[Optional[List[str]]] = ContextVar(
    "current_document", default=None
)
_current_filename: ContextVar[str] = ContextVar("current_filename", default="")


def set_document(numbered_lines: Optional[List[str]], filename: str = "") -> None:
    """Bind the source document for the current run."""
    _current_document.set(numbered_lines)
    _current_filename.set(filename)


def clear_document() -> None:
    _current_document.set(None)
    _current_filename.set("")


def lookup_source_lines(start_line: int, end_line: int) -> dict:
    """Return the exact text of a line range from the source document.

    Use this to verify a passage before you cite it. Citing a line you have
    not read is how errors get into an executive briefing.

    Args:
        start_line: First line number to read (1-based, inclusive).
        end_line: Last line number to read (inclusive). Keep ranges under 40
            lines; call the tool again for another range.

    Returns:
        A dict with the requested text, or an error explaining why the range
        is not valid.
    """
    lines = _current_document.get()
    if not lines:
        return {
            "error": "No source document was supplied for this task.",
            "guidance": (
                "You have nothing to cite. Report what you cannot determine in "
                "open_questions instead of asserting it."
            ),
        }

    total = len(lines)
    if start_line < 1 or end_line < start_line:
        return {"error": f"Invalid range {start_line}-{end_line}.", "line_count": total}
    if start_line > total:
        return {
            "error": f"Line {start_line} is past the end of the document.",
            "line_count": total,
            "guidance": f"This document has {total} lines. Do not cite beyond that.",
        }

    end = min(end_line, total, start_line + 39)
    return {
        "filename": _current_filename.get(),
        "start_line": start_line,
        "end_line": end,
        "line_count": total,
        "text": "\n".join(lines[start_line - 1 : end]),
    }


def document_outline() -> dict:
    """Return the document's size and its markdown headings.

    Call this first to orient yourself, then use lookup_source_lines to read
    the sections that matter to your domain.
    """
    lines = _current_document.get()
    if not lines:
        return {"error": "No source document was supplied for this task."}

    headings = []
    for index, raw in enumerate(lines, start=1):
        # Lines arrive as "L 12| ## Heading" -- strip the numbering prefix.
        text = raw.split("|", 1)[1].strip() if "|" in raw else raw.strip()
        if text.startswith("#"):
            headings.append({"line": index, "heading": text.lstrip("# ").strip()})

    return {
        "filename": _current_filename.get(),
        "line_count": len(lines),
        "headings": headings or [{"line": 1, "heading": "(no markdown headings found)"}],
    }


EXECUTIVE_TOOLS = [lookup_source_lines, document_outline]
