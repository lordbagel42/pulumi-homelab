"""Per-route speech and operator settings; cloud media formats stay fixed."""
import json
import math

from pydantic import BaseModel, ConfigDict, Field, model_validator
from elevenlabs.types import (AsrConversationalConfig, BaseTurnConfig,
                              ConversationConfigInput, TtsConversationalConfigInput)


class OperatorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine_id: str = Field(default="", pattern=r"^(seng_[\w-]+)?$", max_length=100)
    voice_id: str = Field(default="", pattern=r"^[\w.-]*$", max_length=100)
    model_id: str = Field(default="", pattern=r"^[\w.-]*$", max_length=100)
    language: str = Field(default="", pattern=r"^[\w-]*$", max_length=30)
    greeting: str = Field(default="", max_length=2000)
    personality: str = Field(default="", max_length=8000)
    speed: float | None = Field(default=None, ge=.7, le=1.2)
    stability: float | None = Field(default=None, ge=0, le=1)
    similarity_boost: float | None = Field(default=None, ge=0, le=1)
    tts: dict = Field(default_factory=dict)
    asr: dict = Field(default_factory=dict)
    turn: dict = Field(default_factory=dict)
    conversation: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_options(self):
        groups = {"tts": TtsConversationalConfigInput, "asr": AsrConversationalConfig,
                  "turn": BaseTurnConfig, "conversation": ConversationConfigInput}
        reserved = {"tts": {"voice_id", "model_id", "speed", "stability", "similarity_boost", "agent_output_audio_format"},
                    "asr": {"user_input_audio_format"},
                    "conversation": {"client_events", "text_only", "file_input", "monitoring_enabled", "monitoring_events"}}
        for name, schema in groups.items():
            value = getattr(self, name)
            if set(value) - schema.model_fields.keys() or set(value) & reserved.get(name, set()):
                raise ValueError(f"Unsupported or reserved {name} option")
            if len(json.dumps(value, allow_nan=False)) > 12000:
                raise ValueError(f"{name} options are too large")
            schema.model_validate(value, strict=True)
        for key in ("turn_timeout", "initial_wait_time", "silence_end_call_timeout"):
            value = self.turn.get(key)
            if value is not None and (type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 86400):
                raise ValueError("Turn timeouts must be between 0 and 86400 seconds")
        duration = self.conversation.get("max_duration_seconds")
        if duration is not None and (type(duration) is not int or not 60 <= duration <= 86400):
            raise ValueError("Call duration must be between 60 and 86400 seconds")
        for section, key, allowed in [
            (self.turn, "turn_eagerness", {"patient", "normal", "eager"}),
            (self.turn, "spelling_patience", {"auto", "off"}),
            (self.tts, "text_normalisation_type", {"system_prompt", "elevenlabs"}),
        ]:
            if key in section and (not isinstance(section[key], str) or section[key] not in allowed):
                raise ValueError(f"Unsupported {key}")
        return self


def profile_for(store, route):
    record = store.get("routes", route, {})
    return OperatorProfile.model_validate(record.get("speech_engine") or {}).model_dump()


def operator_instructions(store, route, call_id=None, cli="/opt/phone-platform/current/bin/switchboard", owner_id=""):
    profile = profile_for(store, route)
    instructions = profile["personality"].strip()
    if route == "slack-operator":
        instructions += (f"\nYou are the owner's Slack phone operator. Use the Switchboard CLI at {cli}. "
            "Commands: huddle lookup QUERY; uuid; huddle dial USER_ID --request-id REQUEST_ID; "
            "huddle status REQUEST_ID; huddle cancel REQUEST_ID. Never print credentials. "
            "Resolve ambiguous names by asking the owner. Start a huddle only when requested. "
            "Never repeat a dial after an uncertain result; check its status instead. ")
        if owner_id:
            instructions += f"The owner's Slack member ID is {owner_id}. "
        if call_id:
            instructions += ("You are on a live handset call. Ask clarification with phone_ask_user. "
                f"To connect it, use huddle dial USER_ID --request-id REQUEST_ID --mode operator --operator-call-id {call_id}. "
                "Never use ring mode. Do not place a second dial. Stop when the call ends. ")
    return instructions.strip()
