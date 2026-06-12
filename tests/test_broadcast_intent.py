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
    # 2026-04-11 PR #4 additions: Korean naturalness gaps that bb6c4d0 missed
    # Auxiliary verb 줘/주세요 with separating space (Korean orthography norm)
    "채널에 공지해 줘",
    "본문에 올려 주세요",
    "본문에 안내해 주세요",
    # ~하고 connector form (chained imperative — final clause has no 해/해줘)
    "기능 추가했으니까 스레드 요약하고 채널 본문 공지하고",  # 원 사용자 보고
    "채널에 공유하고",
    "본문에 브리핑하고",
    # "본문" as channel-equivalent token (Slack: "본문" = channel root vs thread)
    "본문에 공지해줘",
    "본문에도 알려줘",
    # Trailing confirmation/agreement filler after the imperative — caught
    # operationally on 2026-04-11 SOM Slack ("@SOM Agent 채널 본문 공지해 ok?")
    "채널 본문 공지해 ok?",
    "본문에 공지해 응?",
    "채널에 공유해 좋아?",
    "본문에 안내해 thanks",
    "채널에 공지해 plz",
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
    # 2026-04-11 PR #4: false-positive guards for the new ~하고 alternation.
    # Random `~하고` endings with non-broadcast stems must NOT trigger.
    "운동을 좋아하고",  # 좋아 not in stem list
    "산책하고 와",  # 산책 not in stem list, also doesn't end with 하고
    # Channel + 하고 but in narrative / past form (REFERENTIAL_HINT catches)
    "그가 채널에 공지하고 떠났다",
    "어제 채널에 공지하고 끝냈어",
    # Last clause is not the broadcast verb
    "그래서 채널에 공지하고 끝낼게",
    # 본문 token with question form (not imperative)
    "본문 어디 있어?",
    "본문 길이 제한이 얼마야?",
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


# -----------------------------------------------------------------------------
# Source-level guard — TOOLS.md must NOT advertise non-existent LLM tools.
# Background: companio's `message`, `cron`, and `share_to_channel` are NOT
# Claude-CLI-callable tools (cli.py:418 explicitly notes "MessageSender cannot
# be injected into Claude CLI subprocess"). Older docs claimed they were, which
# caused the LLM to hallucinate explanations like "Gateway needs restart" when
# it tried to call them and failed. PR #4 cleaned up the docs. This guard
# locks the cleanup so a future doc edit can't silently regress.
# -----------------------------------------------------------------------------


def test_tools_md_does_not_advertise_phantom_llm_tools() -> None:
    """`templates/TOOLS.md` must not contain the legacy "companio-Specific Tools"
    section heading or claim that `message`/`cron`/`share_to_channel` are
    LLM-callable. The honest version explains they are NOT injected."""
    from pathlib import Path

    tools_md = (
        Path(__file__).parent.parent
        / "companio"
        / "templates"
        / "TOOLS.md"
    )
    text = tools_md.read_text(encoding="utf-8")

    # Forbidden phrases — these were the misleading claims
    forbidden = [
        "## companio-Specific Tools",
        "Two additional tools are injected by companio",
    ]
    for phrase in forbidden:
        assert phrase not in text, (
            f"TOOLS.md still contains the misleading legacy phrase {phrase!r}. "
            "This caused LLM hallucinations like 'Gateway needs restart'. "
            "Replace with the honest 'What companio does NOT inject' section "
            "introduced in PR #4."
        )

    # Required honesty markers — the new section must be present
    required = [
        "There are no companio-specific LLM-callable tools",
        "Channel actions",
    ]
    for phrase in required:
        assert phrase in text, (
            f"TOOLS.md is missing the required honesty marker {phrase!r}. "
            "PR #4's cleanup of phantom tool claims has been partially "
            "reverted."
        )
