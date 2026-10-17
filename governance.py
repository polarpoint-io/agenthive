"""
governance.py - write-path policy: what a memory node is allowed to say,
and what it is allowed to skip.

Three small rules that all belong to the moment a node is written, before
it reaches storage or the review queue:

1. RESERVED_TAGS is a fixed vocabulary layered on top of the free-form
   tags a caller can already send. Nothing rejects an unreserved tag -
   this is a convention with teeth, not a schema. Its value is that the
   index layer (see retrieval.project_index) returns tags, so an agent
   can decide which bodies are worth fetching without reading any of
   them, and a reviewer can filter the queue by kind rather than by eye.

2. NEVER_AUTO_APPROVE is the guardrail that makes the vocabulary worth
   having. An auto-approve rule exists to skip review for things a team
   has learned it never rejects (see db.find_matching_rule). A node
   tagged security_alert or sensitive is the opposite case by
   definition, so rule matching is skipped for it entirely - including
   when the rule matched on agent_id rather than on a tag, which is the
   case a tag-only check would miss. Fail closed: an admin can still
   approve it by hand in a second, but nothing auto-approves it by
   accident.

3. strip_private() removes <private>...</private> blocks before the node
   is stored. This is NOT a replacement for the approval gate - the gate
   is a human reading the thing. It is a different control at a
   different moment: the gate catches a leaked secret after it is
   already in the database and rendered in an admin's browser, whereas
   this never writes it down. Both, not either.

   An unclosed <private> strips to the end of the text on purpose. The
   alternative - treating a malformed tag as literal text - stores
   exactly the content the author was trying to withhold, which is the
   wrong way round for a failure nobody will notice.
"""

import re

RESERVED_TAGS = frozenset({"decision", "bugfix", "security_alert", "sensitive"})

NEVER_AUTO_APPROVE = frozenset({"security_alert", "sensitive"})

_PRIVATE_BLOCK = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)
_PRIVATE_UNCLOSED = re.compile(r"<private>.*\Z", re.DOTALL | re.IGNORECASE)

REDACTION_MARKER = "[redacted]"


def blocks_auto_approve(tags) -> bool:
    """True when this node's tags forbid auto-approval, whatever rule
    would otherwise have matched it."""
    return bool(NEVER_AUTO_APPROVE & {str(t).strip().lower() for t in (tags or [])})


def strip_private(text: str) -> tuple:
    """Return (cleaned_text, blocks_removed).

    Leaves a visible marker rather than deleting silently: a reviewer
    seeing '[redacted]' knows something was withheld on purpose and can
    ask, where a seamless gap reads as the whole story."""
    if not text or "<private" not in text.lower():
        return text, 0

    cleaned, closed = _PRIVATE_BLOCK.subn(REDACTION_MARKER, text)
    cleaned, unclosed = _PRIVATE_UNCLOSED.subn(REDACTION_MARKER, cleaned)
    return cleaned, closed + unclosed
