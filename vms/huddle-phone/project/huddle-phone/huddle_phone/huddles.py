"""Normalize Slack's public huddle-thread metadata; never infer a channel
from a user's huddle status alone.
"""

from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class Huddle:
    team_id: str
    channel_id: str
    room_id: str
    started_at: float
    ended: bool
    thread_ts: str
    invitation_ts: str = ""
    invited_by: str = ""

    @property
    def key(self) -> str:
        key = f"{self.team_id}:{self.channel_id}:{self.room_id}"
        return f"{key}:invite:{self.invitation_ts}" if self.invitation_ts else key


def from_invitation(event: dict, *, team_id: str, enterprise_id: str | None = None) -> Huddle | None:
    """A targeted event received on this user's authenticated Slack socket.

    Freshness belongs to the invitation, even if the room started hours ago.
    Do not retain the prejoin credentials Slack includes in free_willy.
    """
    allowed_teams = {value for value in (team_id, enterprise_id) if value}
    if (event.get("type") != "huddle_invite" or not isinstance(event.get("team_id"), str)
            or event.get("team_id") not in allowed_teams):
        return None
    for field, pattern in (("channel_id", r"[CDG][A-Z0-9]+"), ("call_id", r"R[A-Z0-9]+"),
                           ("sender_user_id", r"[UW][A-Z0-9]+"), ("event_ts", r"\d+\.\d+")):
        if not isinstance(event.get(field), str) or not re.fullmatch(pattern, event[field]):
            return None
    timestamp = float(event["event_ts"])
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return Huddle(team_id, event["channel_id"], event["call_id"], timestamp, False, "",
                  event["event_ts"], event["sender_user_id"])


def active_room(room: dict, huddle: Huddle) -> bool:
    """Check the exact invited room without needing access to its chat history."""
    if not isinstance(room, dict) or room.get("id") != huddle.room_id or room.get("call_family") != "huddle":
        return False
    channels = room.get("channels")
    if room.get("has_ended") is not False or not isinstance(channels, list) or huddle.channel_id not in channels:
        return False
    try:
        ended_at = float(room.get("date_end", 0))
    except (ValueError, TypeError, OverflowError):
        return False
    return math.isfinite(ended_at) and ended_at == 0


def from_message(message: dict, *, team_id: str, channel_id: str) -> Huddle | None:
    """Parse either a history item or a message/message_changed event.

    The caller supplies the verified workspace and requested history channel.
    Explicit conflicting channel IDs are rejected, even if room.channels lists
    the target channel (a huddle can be shared with multiple channels).
    """
    if message.get("type") != "message":
        return None
    if message.get("channel", channel_id) != channel_id:
        return None
    if message.get("team", team_id) != team_id:
        return None
    if message.get("subtype") == "message_changed":
        message = message.get("message", {})
        if not isinstance(message, dict):
            return None
    if message.get("subtype") != "huddle_thread":
        return None
    if message.get("channel", channel_id) != channel_id:
        return None
    if message.get("team", team_id) != team_id:
        return None
    room = message.get("room")
    if not isinstance(room, dict) or room.get("call_family") != "huddle":
        return None
    room_id = room.get("id", "")
    if not isinstance(room_id, str) or not re.fullmatch(r"R[A-Z0-9]+", room_id):
        return None
    thread_ts = message.get("ts", "")
    if not isinstance(thread_ts, str) or not re.fullmatch(r"\d+\.\d+", thread_ts):
        return None
    try:
        started_at = float(room.get("date_start") or thread_ts)
        ended_at = float(room.get("date_end") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(started_at) or started_at <= 0 or not math.isfinite(ended_at):
        return None
    # Missing state is not sufficient evidence to place a telephone call.
    if not isinstance(room.get("has_ended"), bool):
        return None
    return Huddle(team_id, channel_id, room_id, started_at,
                  room["has_ended"] is True or ended_at > 0, thread_ts)
