"""Source-level grep guard for Slack inbound metadata.

WO-P1-01 §3-10: the ack reaction lifecycle relies on `metadata['message_ts']`
being populated for every inbound that originates from a real Slack event. If
a refactor ever removes that field, the entire 👀 ack feature silently breaks
(reactions are scoped to a specific message timestamp). This grep keeps us
honest at parse time so the regression is caught in CI before deployment.
"""

from __future__ import annotations

import re
from pathlib import Path

SLACK_PY = Path(__file__).parent.parent / "companio" / "channels" / "slack.py"


def test_slack_app_mention_populates_message_ts():
    source = SLACK_PY.read_text(encoding="utf-8")
    assert '"message_ts"' in source, (
        "slack.py no longer references the message_ts metadata key — the ack "
        "reaction lifecycle depends on it. Restore the field in _on_app_mention."
    )
    assert re.search(
        r'"message_ts"\s*:\s*event(?:\.get)?\s*\(\s*["\']ts',
        source,
    ), (
        "_on_app_mention / _on_message must populate metadata['message_ts'] "
        "from event['ts'] (or event.get('ts'))."
    )
