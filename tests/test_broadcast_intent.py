"""Unit tests for the broadcast intent detector (`_detect_broadcast_intent`).

Covers 04-plan.md §1.2 — the token + imperative-ending gate replaces the old
`_BROADCAST_TRIGGER_PATTERN` regex to cut false positives. Positive cases
verify every matching lane (standalone, Korean imperative, English bigram).
Negative cases lock the anti-false-positive gates (referential hints,
question forms, off-topic text).
"""

from __future__ import annotations

import pytest

from companio.core.loop import _BROADCAST_MARKER, _detect_broadcast_intent

# -----------------------------------------------------------------------------
# Positive — user is explicitly asking the bot to broadcast its reply
# -----------------------------------------------------------------------------

POSITIVE_CASES = [
    # Korean: channel + verb + imperative ending
    "채널에도 공유해",
    "채널 본문에도 브리핑해",  # 원 버그 케이스
    "이 결과 채널에 공지해줘",
    "요약해서 채널에 올려줘",
    "채널에 방송해줘",
    "채널에 브리핑 부탁드려",
    "이거 채널에 공유해주세요",
    # Korean standalone phrases
    "팀에 알려줘",
    "모두에게 알려주세요",
    "다른 사람도 볼 수 있게 해줘",
    "@channel 공지",
    "@here 긴급",
    "방송해주세요",
    "팀에 공지해주세요",
    # English — strong bigram
    "please broadcast this to the channel",
    "share with the channel",
    "announce to everyone in the channel",
    "post this to the channel now",
    "Broadcast to the team channel please",
]


@pytest.mark.parametrize("phrase", POSITIVE_CASES)
def test_positive_phrases_detected(phrase: str) -> None:
    assert _detect_broadcast_intent(phrase), (
        f"phrase {phrase!r} should match broadcast intent"
    )


# -----------------------------------------------------------------------------
# Negative — user is talking ABOUT broadcasts, asking questions, or unrelated
# -----------------------------------------------------------------------------

NEGATIVE_CASES = [
    # Trivially unrelated
    "",
    "hello world",
    "버그 수정해줘",
    "can you fix the bug?",
    "이 코드 설명해줘",
    # Referential — past tense / observation, not a request
    "어제 #general 채널에 공지 올라왔어?",
    "이 채널에 PR 공지가 떴는데 뭔지 알아?",
    "announce 채널에서 broadcast 알림 못 받았어",
    "'채널 공지' 기능 어떻게 써?",
    "broadcast API 문서 어디 있어?",
    "채널 주제가 뭐였지?",
    "채널 공지 떴어?",
    # Question forms (not imperative)
    "채널에 공유된 적 있어?",
    # Share-but-not-channel — "공유" appears but target is not a channel
    "이 코드 공유 폴더에 넣어줘",
    "파일 공유 링크 뽑아줘",
    # English — single weak keyword, no bigram
    "i saw the broadcast yesterday",
    "announce button is broken",
    "channel mapping is confusing",
]


@pytest.mark.parametrize("phrase", NEGATIVE_CASES)
def test_negative_phrases_not_detected(phrase: str) -> None:
    assert not _detect_broadcast_intent(phrase), (
        f"phrase {phrase!r} should NOT match broadcast intent"
    )


def test_none_content_safe() -> None:
    # Defensive: callers may pass None/empty. Must not raise.
    assert _detect_broadcast_intent(None) is False  # type: ignore[arg-type]
    assert _detect_broadcast_intent("") is False
    assert _detect_broadcast_intent("   ") is False


# -----------------------------------------------------------------------------
# Marker exact-string lock — prevents silent UX drift
# -----------------------------------------------------------------------------


def test_broadcast_marker_exact_string_lock() -> None:
    assert _BROADCAST_MARKER == "\n\n_📢 채널에도 공유되었습니다_"
