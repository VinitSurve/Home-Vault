"""macOS User Notification Center integration for HomeVault.

Sends native macOS notifications for upload completions, delivery receipts,
and attention-required events using osascript.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def send_notification(
    title: str,
    message: str,
    subtitle: Optional[str] = None,
) -> bool:
    """Display a native macOS notification banner.

    Args:
        title: Notification header (e.g. "HomeVault").
        message: Notification body text.
        subtitle: Optional subtitle text.

    Returns:
        True if notification was posted, False if skipped or failed.
    """
    logger.info("[Notification] %s: %s%s", title, f"({subtitle}) " if subtitle else "", message)

    if platform.system() != "Darwin":
        return False

    # Escape quotes for AppleScript
    def escape_applescript(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    safe_title = escape_applescript(title)
    safe_message = escape_applescript(message)

    script_parts = [f'display notification "{safe_message}" with title "{safe_title}"']
    if subtitle:
        script_parts.append(f'subtitle "{escape_applescript(subtitle)}"')

    script = " ".join(script_parts)

    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return True
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("Failed to invoke osascript for notification: %s", e)
        return False
