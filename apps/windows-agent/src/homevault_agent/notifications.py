"""Windows notification integration for HomeVault.

Provides logging and safe user notification for upload completion,
delivery receipts, and attention-required events on Windows.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def send_notification(
    title: str,
    message: str,
    subtitle: Optional[str] = None,
) -> bool:
    """Log and optionally display a notification on Windows.

    Args:
        title: Notification header (e.g. "HomeVault").
        message: Notification body text.
        subtitle: Optional subtitle text.

    Returns:
        True if notification was logged/displayed.
    """
    header = f"{title} ({subtitle})" if subtitle else title
    logger.info("[Notification] %s: %s", header, message)
    return True
