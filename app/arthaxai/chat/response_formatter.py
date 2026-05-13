from __future__ import annotations

import re

from app.arthaxai.chat.neplish import detect_response_language_mode


# ── small text helpers ────────────────────────────────────────────────────────

def _strip_summary_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().startswith("summary:"):
        return text.split(":", 1)[1].strip()
    return text


def _normalize_line(line: str) -> str:
    return str(line or "").strip()


def _is_title_noise(line: str) -> bool:
    """
    Returns True for lines that are just the AI name / generic title.
    These are stripped because the frontend already shows the sender name
    above every assistant bubble — keeping them here creates duplication.
    """
    normalized = re.sub(r"^#+\s*", "", str(line or "").strip()).strip().lower().rstrip(":")
    return normalized in {
        "arthax ai",
        "arthax business ai",
        "arthax personal ai",
        "arthax business balance",
        "arthax business summary",
        "arthax personal balance",
        "arthax personal summary",
        "arthax personal finance ai",
        "arthax business ai assistant",
    }


def _strip_leading_title_noise(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and _is_title_noise(trimmed[0]):
        trimmed = trimmed[1:]
    return trimmed


def _is_supporting_header(line: str) -> bool:
    normalized = str(line or "").strip().lower().rstrip(":")
    return normalized in {
        "supporting details",
        "supporting evidence",
        "details",
        "evidence",
    }


def _extract_money_highlight(text: str) -> str | None:
    match = re.search(r"(NPR\s?[\d,]+(?:\.\d{2})?)", str(text or ""), re.IGNORECASE)
    if match:
        return match.group(1).replace("  ", " ").strip()
    return None


def _normalize_money_display(text: str) -> str:
    return re.sub(r"\b(NPR\s?[\d,]+)\.00\b", r"\1", str(text or ""))


def _dedupe_preserving_order(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        normalized = re.sub(r"\s+", " ", str(line or "").strip()).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(str(line or "").strip())
    return unique


def _localized_supporting_details_label(reply: str) -> str:
    mode = detect_response_language_mode(reply)
    if mode == "nepali":
        return "सहयोगी विवरण"
    if mode == "neplish":
        return "Supporting kura"
    return "Supporting details"


def _localized_keep_in_mind_label(reply: str) -> str:
    mode = detect_response_language_mode(reply)
    if mode == "nepali":
        return "ध्यान दिनुहोस्"
    if mode == "neplish":
        return "Dhyan dinuhos"
    return "Keep in mind"


# ── shared core formatter ─────────────────────────────────────────────────────

def _format_assistant_reply(reply: str) -> str:
    """
    Unified formatter used by both personal and business pipelines.

    Structure of the output:
        **{highlighted NPR amount}**  ← if an NPR amount is found in the summary
        {direct answer / explainer sentence}
        **Supporting details**
        - bullet
        - bullet
        **Keep in mind**
        {reminder text}

    Key rules:
    - NO ### heading — the frontend already shows the sender name above the bubble.
    - The direct answer (from "Summary:" line) is shown ONCE, bold if it contains
      an NPR amount, plain otherwise.  The old code had a bug where it was appended
      twice when no amount was found; that is fixed here.
    - "Supporting details" / "Supporting evidence" header lines from the LLM are
      stripped and re-emitted as a bold label so they look consistent.
    - Up to 6 supporting bullet lines are kept.
    """
    normalized = str(reply or "").strip()
    if not normalized:
        return normalized

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    lines = _strip_leading_title_noise(lines)

    if not lines:
        return normalized

    if len(lines) == 1:
        return _normalize_money_display(lines[0])

    # ── extract structural pieces ─────────────────────────────────────────
    # title_line: first line that ends with ":" but isn't a "Supporting details" header
    title_line = (
        lines[0]
        if lines[0].endswith(":") and not _is_supporting_header(lines[0])
        else None
    )
    # summary_line: last line that starts with "Summary:"
    summary_line = next(
        (line for line in reversed(lines) if line.lower().startswith("summary:")),
        None,
    )
    keep_in_mind_lines = [
        line for line in lines if line.lower().startswith("keep in mind")
    ]

    # ── build direct answer ───────────────────────────────────────────────
    # Prefer the Summary: line; fall back to first non-bullet content line.
    direct_answer = _strip_summary_prefix(summary_line or "")
    if not direct_answer:
        content_lines = [line for line in lines if line != title_line]
        first_detail = next(
            (line for line in content_lines if line.startswith("- ")),
            content_lines[0] if content_lines else "",
        )
        direct_answer = (
            first_detail[2:].strip() if first_detail.startswith("- ") else first_detail.strip()
        )

    # ── money highlight ───────────────────────────────────────────────────
    highlighted_amount = _extract_money_highlight(direct_answer)

    # ── explainer: first plain (non-bullet, non-title, non-summary) line ─
    explainer_line = next(
        (
            line
            for line in lines
            if line not in {title_line, summary_line}
            and not _is_supporting_header(line)
            and not line.startswith("- ")
            and not line.lower().startswith("keep in mind")
            and not line.lower().startswith("breakdown:")
        ),
        "",
    )

    # ── supporting bullets ────────────────────────────────────────────────
    supporting_lines: list[str] = []
    for line in lines:
        if line == title_line:
            continue
        if summary_line and line == summary_line:
            continue
        if _is_supporting_header(line):
            continue
        if line == explainer_line:
            continue
        if line.lower().startswith("keep in mind"):
            continue
        if line.lower().startswith("breakdown:"):
            continue
        supporting_lines.append(_normalize_line(line))

    supporting_lines = _dedupe_preserving_order(supporting_lines)[:6]

    # ── assemble output ───────────────────────────────────────────────────
    parts: list[str] = []

    # Primary answer line — bold amount if found, otherwise bold full answer
    if highlighted_amount:
        parts.append(f"**{highlighted_amount}**")
        # Show the full sentence as explainer only if it adds more than just the amount
        if direct_answer and direct_answer.strip() != highlighted_amount:
            parts.append(direct_answer)
    else:
        # FIX for Bug 2: append direct_answer ONCE as bold — do NOT append it
        # again as plain text. The old code had an elif that re-added it.
        parts.append(f"**{direct_answer}**")

    # Explainer sentence (skip if same as direct_answer to avoid duplication)
    if explainer_line and explainer_line != direct_answer:
        parts.append(explainer_line)

    # Supporting bullets
    if supporting_lines:
        parts.append(f"**{_localized_supporting_details_label(normalized)}**\n" + "\n".join(supporting_lines))

    # Keep in mind reminder
    if keep_in_mind_lines:
        reminder = keep_in_mind_lines[0]
        reminder = re.sub(r"^keep in mind:\s*", "", reminder, flags=re.IGNORECASE).strip()
        if reminder:
            parts.append(f"**{_localized_keep_in_mind_label(normalized)}**\n{reminder}")

    return _normalize_money_display("\n\n".join(parts))


# ── public API ────────────────────────────────────────────────────────────────

def format_business_reply_like_personal(reply: str) -> str:
    """
    Formats a business AI reply for display in the chat bubble.

    Previously this function prepended '### arthaX Business AI' to the output.
    That heading is now REMOVED because BusinessChatScreen.tsx already renders
    'arthaX Business AI' as the sender label above every assistant bubble
    (line 1269). Keeping the ### heading caused it to appear twice.

    All formatting logic is delegated to _format_assistant_reply().
    """
    return _format_assistant_reply(reply)


def format_personal_reply_like_business_structure(reply: str) -> str:
    """
    Formats a personal AI reply for display in the chat bubble.

    Previously this function prepended '### arthaX AI' / '### arthaX Personal Balance'
    to the output.  That heading is now REMOVED because PersonalChatScreen.tsx already
    renders 'arthaX AI' as the sender label above every assistant bubble (line 1667).

    All formatting logic is delegated to _format_assistant_reply().
    """
    return _format_assistant_reply(reply)


def format_reply_like_personal(reply: str) -> str:
    """
    Lightweight formatter used for simple / non-structured replies.
    Strips title noise and returns clean plain text.
    """
    normalized = str(reply or "").strip()
    if not normalized:
        return normalized

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    lines = _strip_leading_title_noise(lines)
    if len(lines) <= 1:
        return _normalize_money_display(lines[0] if lines else normalized)

    title_line = (
        lines[0]
        if lines[0].endswith(":") and not _is_supporting_header(lines[0])
        else None
    )
    summary_line = next(
        (line for line in reversed(lines) if line.lower().startswith("summary:")),
        None,
    )

    direct_answer = _strip_summary_prefix(summary_line or "")
    if not direct_answer:
        content_lines = [line for line in lines if line != title_line]
        first_detail = next(
            (line for line in content_lines if line.startswith("- ")),
            content_lines[0] if content_lines else "",
        )
        direct_answer = (
            first_detail[2:].strip() if first_detail.startswith("- ") else first_detail.strip()
        )

    supporting_lines: list[str] = []
    for line in lines:
        if line == title_line:
            continue
        if summary_line and line == summary_line:
            continue
        if _is_supporting_header(line):
            continue
        supporting_lines.append(line)

    supporting_lines = supporting_lines[:6]

    if not supporting_lines:
        return _normalize_money_display(direct_answer)

    return _normalize_money_display(
        "\n\n".join(
            [direct_answer, f"{_localized_supporting_details_label(normalized)}:\n" + "\n".join(supporting_lines)]
        )
    )
