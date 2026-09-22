from dataclasses import dataclass, field
import os
import re


def required(env, key, pattern=None):
    value = env.get(key, "").strip()
    if not value or (pattern and not re.fullmatch(pattern, value)):
        raise ValueError(f"Set a valid {key} in huddle-phone/.env")
    if "\r" in value or "\n" in value:
        raise ValueError(f"{key} must be one line")
    return value


def integer(env, key, default, minimum, maximum):
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def workspace(env):
    value = required(env, "SLACK_WORKSPACE").lower()
    if value.endswith(".slack.com"):
        value = value[:-len(".slack.com")]
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.enterprise)?", value):
        raise ValueError("SLACK_WORKSPACE must be a Slack workspace or Enterprise Grid hostname")
    return value


@dataclass(frozen=True)
class Config:
    team_id: str
    channel_id: str
    workspace: str
    client_token: str = field(repr=False)
    cookie: str = field(repr=False)
    dry_run: bool
    state_path: str
    chromium: str
    region: str
    ami_host: str
    ami_port: int
    ami_user: str
    ami_secret: str = field(repr=False)
    phone_channel: str
    ring_seconds: int
    max_call_seconds: int
    max_age_seconds: int
    poll_seconds: int
    http_port: int
    audio_port: int
    enterprise_id: str | None = None
    incoming_invites: bool = True
    operator_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        dry = env.get("DRY_RUN", "true").lower()
        if dry not in ("true", "false"):
            raise ValueError("DRY_RUN must be true or false")
        invitations = env.get("INCOMING_INVITES", "true").lower()
        if invitations not in ("true", "false"):
            raise ValueError("INCOMING_INVITES must be true or false")
        cookie = required(env, "SLACK_COOKIE")
        if not re.search(r"(?:^|;\s*)d=xoxd-[^;\s]+", cookie):
            raise ValueError("SLACK_COOKIE must contain your Slack d=xoxd-... session cookie")
        return cls(
            team_id=required(env, "SLACK_TEAM_ID", r"T[A-Z0-9]+"),
            channel_id=required(env, "SLACK_CHANNEL_ID", r"[CG][A-Z0-9]+"),
            workspace=workspace(env),
            client_token=required(env, "SLACK_CLIENT_TOKEN", r"xoxc-[^\s]+"),
            cookie=cookie, dry_run=dry == "true",
            state_path=env.get("STATE_PATH", "/state/huddles.sqlite3"),
            chromium=env.get("CHROMIUM_PATH", "/usr/bin/chromium"),
            region=required({"CHIME_REGION": env.get("CHIME_REGION", "us-east-2")},
                            "CHIME_REGION", r"[a-z]{2}(?:-[a-z]+)+-\d"),
            ami_host=env.get("AMI_HOST", "127.0.0.1"),
            ami_port=integer(env, "AMI_PORT", 5038, 1024, 65535),
            ami_user=required({"AMI_USER": env.get("AMI_USER", "huddle-phone")}, "AMI_USER", r"[a-zA-Z0-9_-]+"),
            ami_secret=env.get("AMI_SECRET", "") if dry == "true" else required(env, "AMI_SECRET"),
            phone_channel=required({"PHONE_CHANNEL": env.get("PHONE_CHANNEL", "SCCP/6738")},
                                   "PHONE_CHANNEL", r"(?:SCCP|PJSIP)/[A-Za-z0-9_-]+"),
            ring_seconds=integer(env, "RING_SECONDS", 30, 5, 90),
            max_call_seconds=integer(env, "MAX_CALL_SECONDS", 3600, 60, 14400),
            max_age_seconds=integer(env, "MAX_HUDDLE_AGE_SECONDS", 120, 15, 600),
            poll_seconds=integer(env, "POLL_SECONDS", 5, 5, 300),
            http_port=integer(env, "HTTP_PORT", 8099, 1024, 65535),
            audio_port=integer(env, "AUDIO_PORT", 9094, 1024, 65535),
            enterprise_id=(required(env, "SLACK_ENTERPRISE_ID", r"E[A-Z0-9]+")
                           if env.get("SLACK_ENTERPRISE_ID") else None),
            incoming_invites=invitations == "true",
            operator_secret=(required(env, "OPERATOR_SECRET", r"[A-Za-z0-9_-]{32,128}")
                             if env.get("OPERATOR_SECRET") else ""),
        )
