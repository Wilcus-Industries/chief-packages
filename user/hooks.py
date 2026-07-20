"""Boot-loaded hook: put the owner's standing facts in every turn's prompt.

``data/memory/User.md`` holds the handful of facts about the owner that are
true on every turn — name, age, where they live, what they do. They are not
memories: there is nothing to recall, no relevance to judge, no search to run.
So they are not stored as memory facts; they are simply always present.

The read is per-turn and unconditional, which is affordable precisely because
the file is small. The system prompt is reassembled from scratch each turn and
never persisted, so a ``pre_turn`` contribution costs its own length once per
request — it does not accumulate across a conversation. (``session_start``
would be wrong here: its text lands in the first turn's system message only,
and the standing facts would silently vanish from turn two onward.)

Import-light on purpose: stdlib only, no chief imports, so boot pays nothing.
"""

from pathlib import Path
from typing import Any

USER_PATH = Path("data/memory/User.md")

# Prefaces the file so the block reads as owner facts rather than instructions —
# the contribution is rendered inside a <hook source="user"> block.
HEADER = "Standing facts about your owner (always true, no recall needed):"


def read_user(user_path: Path = USER_PATH) -> str:
    """The owner's ``User.md`` text, read fresh, stripped.

    Never raises: an absent, unreadable, or non-UTF-8 file is a no-op (empty
    string), so a hand-edited file can never break a turn. The hook runner
    would contain the failure anyway; returning empty keeps it silent, since a
    missing file is the ordinary pre-seed state and not an error.
    """
    try:
        return user_path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def register(context: Any, hooks: Any) -> None:
    """Register the per-turn contribution. Called by the boot hook loader."""

    @hooks.pre_turn
    async def owner_facts(_turn: Any) -> str | None:
        text = read_user()
        return f"{HEADER}\n\n{text}" if text else None
