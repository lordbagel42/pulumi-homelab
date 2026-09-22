"""Slack user-session adapter. rooms.join is PRIVATE, not the Calls API.

Observed protocol references (not an official Slack compatibility promise):
https://github.com/deployor/hq-fishbowl/blob/c1619f146c71c88474ac047baaa1532f0e0f270a/huddleUtils.js
https://github.com/v1ctorio/dj-hakkun/blob/53d94eba15c1f9a9b9c04ae51addbb150edbea88/hakkun-slack-expert/userbot.py
Only the protocol shape is used here; implementations are independent.
"""

import json
import logging
import re
import time

import aiohttp

from .huddles import Huddle


def user_summary(user):
    if (not isinstance(user, dict) or not re.fullmatch(r"[UW][A-Z0-9]{8,20}", user.get("id", ""))
            or user.get("deleted") or user.get("is_bot") or user.get("is_app_user")
            or user.get("is_invited_user") or user.get("is_profile_only")):
        return None
    profile = user.get("profile") or {}
    def clean(value):
        return " ".join(str(value or "").split())[:100]
    return {"id": user["id"], "name": clean(profile.get("real_name") or user.get("real_name") or user.get("name")),
            "username": clean(user.get("name"))}


class SlackError(RuntimeError):
    def __init__(self, method, code, retry_after=0):
        # Whitelist characters before including a server-supplied error in logs.
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "unexpected_response"
        self.retry_after = retry_after
        super().__init__(f"Slack {method}: {self.code}")


def chime_credentials(payload, expected_room):
    """Validate the native room before exposing credentials to the media SDK."""
    call = payload.get("call")
    if not isinstance(call, dict) or call.get("call_id") != expected_room:
        raise SlackError("rooms.join", "room_changed_during_join")
    data = call.get("free_willy")
    if not isinstance(data, dict):
        raise SlackError("rooms.join", "unsupported_media_backend")
    meeting, attendee = data.get("meeting"), data.get("attendee")
    if not isinstance(meeting, dict) or not isinstance(attendee, dict):
        raise SlackError("rooms.join", "missing_chime_credentials")
    placement = meeting.get("MediaPlacement")
    if not isinstance(placement, dict):
        raise SlackError("rooms.join", "missing_chime_placement")
    for container, fields in ((meeting, ("MeetingId",)),
                              (attendee, ("AttendeeId", "JoinToken")),
                              (placement, ("AudioHostUrl", "SignalingUrl", "TurnControlUrl"))):
        if any(not isinstance(container.get(key), str) or not container[key] for key in fields):
            raise SlackError("rooms.join", "missing_chime_credentials")
    # Return a projection; never forward cookies, Slack tokens or other response
    # content to Chromium. No credentials are persisted or logged.
    return {"meeting": meeting, "attendee": attendee}


class Slack:
    def __init__(self, config, http):
        self.config, self.http = config, http
        self.next_allowed = {}
        self.user_id = None
        self.leave_supported = True

    async def api(self, method, **params):
        if method not in {"auth.test", "conversations.info", "conversations.history", "rooms.join",
                          "client.getWebSocketURL", "screenhero.rooms.info", "users.info",
                          "search.enterprise", "search.team", "conversations.open",
                          "rooms.notifyMember", "rooms.leave"}:
            raise ValueError("Unsupported Slack operation")
        delay = self.next_allowed.get(method, 0) - time.monotonic()
        if delay > 0:
            raise SlackError(method, "ratelimited", retry_after=delay)
        form = aiohttp.FormData(default_to_multipart=True)
        form.add_field("token", self.config.client_token)
        if getattr(self.config, "enterprise_id", None):
            params.setdefault("team_id", self.config.team_id)
        for key, value in params.items():
            if value is not None:
                form.add_field(key, json.dumps(value) if isinstance(value, (bool, list, dict)) else str(value))
        async with self.http.post(
            f"https://{self.config.workspace}.slack.com/api/{method}", data=form,
            headers={"Cookie": self.config.cookie, "Accept": "application/json",
                     "User-Agent": "HuddlePhone/1.0"}, allow_redirects=False,
        ) as response:
            if response.status == 429:
                try:
                    delay = max(1, float(response.headers.get("Retry-After", "60")))
                except ValueError:
                    delay = 60
                self.next_allowed[method] = time.monotonic() + delay
                raise SlackError(method, "ratelimited", retry_after=delay)
            if response.status != 200:
                raise SlackError(method, "http_error")
            # Bound response size. A history page is metadata, not a file export.
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise SlackError(method, "response_too_large")
            try:
                result = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise SlackError(method, "invalid_json") from None
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise SlackError(method, result.get("error") if isinstance(result, dict) else None)
            return result

    async def authenticate(self):
        identity = await self.api("auth.test")
        enterprise = getattr(self.config, "enterprise_id", None)
        enterprise_session = bool(enterprise and identity.get("team_id") == enterprise
                                  and identity.get("enterprise_id") == enterprise)
        if enterprise and identity.get("enterprise_id") != enterprise:
            raise ValueError("SLACK_ENTERPRISE_ID does not match the user session organization")
        if identity.get("team_id") != self.config.team_id and not enterprise_session:
            raise ValueError("SLACK_TEAM_ID does not match the user session workspace")
        self.user_id = identity.get("user_id")
        channel = (await self.api("conversations.info", channel=self.config.channel_id)).get("channel", {})
        if enterprise_session and channel.get("context_team_id") != self.config.team_id:
            raise ValueError("SLACK_TEAM_ID does not match the configured channel's workspace")
        if not channel.get("is_member") or channel.get("is_archived"):
            raise ValueError("Your selfbot user must be a member of the configured, unarchived channel")

    async def history(self, **params):
        return await self.api("conversations.history", channel=self.config.channel_id, **params)

    async def join(self, huddle):
        # No retries: rooms.join has a side effect and may already have succeeded
        # when an HTTP response is lost. Caller verifies the huddle just before.
        response = await self.api("rooms.join", id=huddle.room_id, channel_id=huddle.channel_id,
                                  regions=self.config.region, multidevice=True)
        return chime_credentials(response, huddle.room_id)

    async def room_info(self, huddle):
        response = await self.api("screenhero.rooms.info", room=huddle.room_id)
        return response.get("room")

    async def caller_name(self, user_id):
        """Resolve the actual inviter's Slack name without retaining their profile."""
        if not isinstance(user_id, str) or not re.fullmatch(r"[UW][A-Z0-9]{8,20}", user_id):
            raise ValueError("Invalid Slack caller ID")
        user = (await self.api("users.info", user=user_id)).get("user")
        if not isinstance(user, dict) or user.get("id") != user_id:
            raise SlackError("users.info", "unexpected_user")
        profile = user.get("profile")
        if not isinstance(profile, dict):
            profile = {}
        for value in (profile.get("display_name"), profile.get("real_name"),
                      user.get("real_name"), user.get("name")):
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:100]
        return "Slack huddle"

    async def user(self, user_id):
        if not isinstance(user_id, str) or not re.fullmatch(r"[UW][A-Z0-9]{8,20}", user_id):
            raise ValueError("Use a Slack member ID such as U0123456789")
        result = user_summary((await self.api("users.info", user=user_id)).get("user"))
        if not result or result["id"] != user_id or user_id == self.user_id:
            raise ValueError("Choose an active human Slack member other than the bridge account")
        return result

    async def search_users(self, query):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100:
            raise ValueError("Enter a name or Slack member ID between 2 and 100 characters")
        query = query.strip().strip("<@>")
        if re.fullmatch(r"[UW][A-Z0-9]{8,20}", query):
            return {"users": [await self.user(query)], "more": False}
        result = await self.api("search.enterprise" if self.config.enterprise_id else "search.team",
                                query={"type": "and", "clauses": [
                                    {"type": "is", "value": "user"},
                                    {"type": "fuzzy_name_and_email", "value": query}]},
                                count=6, include_bots=False, include_deleted=0)
        users = [u for item in result.get("items", []) if (u := user_summary(item)) and u["id"] != self.user_id]
        return {"users": users[:5], "more": result.get("num_found", len(users)) > 5}

    async def start_huddle(self, user_id):
        # Current Slack web client: joinRoom(channelId) creates/joins that DM's
        # native huddle. Never retry this mutation after an ambiguous failure.
        channel = (await self.api("conversations.open", users=user_id)).get("channel", {}).get("id", "")
        if not re.fullmatch(r"D[A-Z0-9]+", channel):
            raise SlackError("conversations.open", "unexpected_channel")
        response = await self.api("rooms.join", channel_id=channel, regions=self.config.region, multidevice=True)
        room = response.get("call", {}).get("call_id", "")
        if not re.fullmatch(r"R[A-Z0-9]+", room):
            raise SlackError("rooms.join", "unexpected_room")
        huddle = Huddle(self.config.team_id, channel, room, time.time(), False, "", "outgoing")
        return huddle, chime_credentials(response, room)

    async def invite(self, huddle, user_id):
        # Starting a DM huddle can already invite its member. In particular,
        # notifyMember can reject a second notification after they accepted.
        room = await self.room_info(huddle)
        if not isinstance(room, dict) or room.get("id") != huddle.room_id:
            raise SlackError("screenhero.rooms.info", "unexpected_room")
        if room.get("has_ended") is True:
            return "room-ended"
        participants = room.get("participants", [])
        if isinstance(participants, list) and user_id in participants:
            return "already-present"
        pending = room.get("pending_invitees", {})
        if isinstance(pending, (dict, list)) and user_id in pending:
            return "already-invited"
        statuses = room.get("last_invite_status_by_user", {})
        if isinstance(statuses, dict) and statuses.get(user_id) in {"accepted", "pending", "joining_soon"}:
            return "already-invited"
        await self.api("rooms.notifyMember", channel_id=huddle.channel_id, user_id=user_id)
        return "sent"

    async def leave(self, huddle, credentials):
        if not self.leave_supported:
            return
        try:
            await self.api("rooms.leave", channel_id=huddle.channel_id, call_id=huddle.room_id,
                           attendee_id=credentials["attendee"]["AttendeeId"], reason="user_initiated")
        except SlackError as error:
            if error.code == "feature_not_enabled":
                # Slack rolls out explicit leave independently of native Chime.
                # On older workspaces, SDK disconnect removes participation;
                # confirmed by rooms.info after the real outgoing call test.
                self.leave_supported = False
                logging.getLogger("huddle-phone").info("Slack explicit leave is unavailable; native Chime disconnect handles departure")
                return
            if error.code not in {"channel_not_found", "room_not_found", "invalid_channel_id", "attendee_not_found"}:
                raise
