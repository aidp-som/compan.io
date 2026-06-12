"""MCP server for cross-channel Slack message posting.

Runs as a stdio MCP server, spawned by Claude CLI via .mcp.json.
Provides two tools: list_channels and send_to_channel.

Environment variables:
  CROSSPOST_TOKEN       — Slack bot token (xoxb-...)
  CROSSPOST_CONFIG_PATH — Path to JSON config file with channel routes
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from mcp.server.fastmcp import FastMCP

MENTION_PATTERN = re.compile(r"<!(?:here|channel|everyone)>|@here|@channel|@everyone")

mcp = FastMCP("companio-crosspost")

_config: dict = {}
_token: str = ""


def _load_config() -> None:
    global _config, _token
    _token = os.environ.get("CROSSPOST_TOKEN", "")
    config_path = os.environ.get("CROSSPOST_CONFIG_PATH", "")
    if config_path and Path(config_path).exists():
        _config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    else:
        _config = {}


def _slack_api(method: str, payload: dict) -> dict:
    """Call a Slack Web API method."""
    data = json.dumps(payload).encode()
    req = Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "Authorization": f"Bearer {_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                return {"ok": False, "error_code": result.get("error", "unknown")}
            return result
    except HTTPError as e:
        if e.code == 429:
            retry_after = int(e.headers.get("Retry-After", "30"))
            return {"ok": False, "error_code": "rate_limited", "retry_after": retry_after}
        return {"ok": False, "error_code": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "error_code": str(e)}


def _slack_post(channel_id: str, text: str, blocks: list[dict] | None = None) -> dict:
    """Post a message to a Slack channel."""
    payload: dict = {"channel": channel_id, "text": text}
    if blocks:
        payload["blocks"] = blocks
    return _slack_api("chat.postMessage", payload)


def _sanitize_message(text: str) -> str:
    """Strip dangerous mentions from message text."""
    return MENTION_PATTERN.sub("", text).strip()


@mcp.tool()
def list_channels() -> dict:
    """전송 가능한 채널 목록을 반환합니다. send_to_channel 호출 전에 이 도구로 유효한 target_name을 확인하세요."""
    _load_config()
    routes = _config.get("routes", {})
    channels = [
        {"name": name, "description": route.get("description", "")}
        for name, route in routes.items()
    ]
    return {"channels": channels}


@mcp.tool()
def send_to_channel(target_name: str, message: str, dry_run: bool = True) -> dict:
    """지정한 채널에 메시지를 전송합니다.

    Args:
        target_name: list_channels()에서 확인한 채널 논리명 (예: "검수요청")
        message: 전송할 메시지 본문
        dry_run: True면 미리보기만 반환 (기본값). False면 실제 전송.

    반드시 사용자에게 "X 채널에 보내겠습니다" 확인을 받은 후 dry_run=False로 호출하세요.
    """
    _load_config()
    routes = _config.get("routes", {})
    route = routes.get(target_name)

    if not route:
        available = ", ".join(routes.keys()) if routes else "(없음)"
        return {
            "ok": False,
            "error_code": "unknown_target",
            "message": f"'{target_name}'은(는) 등록된 채널이 아닙니다. 사용 가능: {available}",
        }

    sanitized = _sanitize_message(message)
    if not sanitized:
        return {"ok": False, "error_code": "empty_message", "message": "메시지가 비어있습니다."}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "target": target_name,
            "description": route.get("description", ""),
            "preview": sanitized[:500],
            "message": f"[미리보기] '{target_name}' 채널에 전송될 내용입니다. 실제 전송하려면 dry_run=False로 다시 호출하세요.",
        }

    if not _token:
        return {"ok": False, "error_code": "no_token", "message": "CROSSPOST_TOKEN이 설정되지 않았습니다."}

    result = _slack_post(route["channel_id"], sanitized)
    if result["ok"]:
        return {
            "ok": True,
            "dry_run": False,
            "target": target_name,
            "ts": result["ts"],
            "message": f"'{target_name}' 채널에 메시지를 전송했습니다.",
        }
    return {
        "ok": False,
        "error_code": result.get("error_code", "unknown"),
        "retry_after": result.get("retry_after"),
        "message": f"전송 실패: {result.get('error_code')}",
    }


@mcp.tool()
def invite_to_channel(channel_id: str, user_ids: list[str]) -> dict:
    """Slack 채널에 사용자를 초대합니다.

    Args:
        channel_id: 초대할 채널 ID (예: "C0B5BGXRQ2F"). Slack에서 채널 상세 > 하단에서 확인 가능.
        user_ids: 초대할 사용자 ID 목록 (예: ["U0B2ES9AE30", "U0B2LL2QD8C"])

    공개/비공개 채널 모두 지원. 비공개 채널은 봇이 이미 멤버여야 합니다.
    필요 scope: channels:write.invites (공개), groups:write.invites (비공개)
    """
    _load_config()
    if not _token:
        return {"ok": False, "error_code": "no_token", "message": "CROSSPOST_TOKEN이 설정되지 않았습니다."}

    if not channel_id or not user_ids:
        return {"ok": False, "error_code": "invalid_args", "message": "channel_id와 user_ids가 필요합니다."}

    result = _slack_api("conversations.invite", {
        "channel": channel_id,
        "users": ",".join(user_ids),
    })

    if result.get("ok"):
        return {
            "ok": True,
            "channel": channel_id,
            "invited": user_ids,
            "message": f"{len(user_ids)}명을 채널에 초대했습니다.",
        }

    error = result.get("error_code", "unknown")
    error_messages = {
        "already_in_channel": "이미 채널에 참여 중인 사용자입니다.",
        "channel_not_found": "채널을 찾을 수 없습니다. ID를 확인하세요.",
        "not_in_channel": "봇이 해당 채널의 멤버가 아닙니다. 먼저 봇을 초대해주세요.",
        "cant_invite_self": "봇 자신은 초대할 수 없습니다.",
        "missing_scope": "채널 초대 권한(channels:write.invites 또는 groups:write.invites)이 없습니다.",
    }
    return {
        "ok": False,
        "error_code": error,
        "message": error_messages.get(error, f"초대 실패: {error}"),
    }


if __name__ == "__main__":
    mcp.run()
