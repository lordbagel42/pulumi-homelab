from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass
class Config:
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_STATE_DIR", "state")))
    voice_socket: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_VOICE_SOCKET", "/var/lib/codex-phone/state/control.sock")))
    models_dir: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_MODELS_DIR", "/var/lib/codex-phone/models")))
    huddle_config: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_HUDDLE_CONFIG", "/var/lib/codex-phone/state/huddle-operator.json")))
    home_assistant_config: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_HOME_ASSISTANT_CONFIG", "/var/lib/codex-phone/home-assistant.json")))
    web_dir: Path = field(default_factory=lambda: Path(os.environ.get("PHONE_PLATFORM_WEB_DIR", str(Path(__file__).resolve().parent.parent / "web"))))
    route_reload: bool = field(default_factory=lambda: os.environ.get("PHONE_PLATFORM_ROUTE_RELOAD", "0") == "1")
    phone_ip: str = field(default_factory=lambda: os.environ.get("PHONE_PLATFORM_PHONE_IP", "192.168.0.197"))
    host: str = field(default_factory=lambda: os.environ.get("PHONE_PLATFORM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("PHONE_PLATFORM_PORT", "8088")))

    # Opt in only after provisioning Speech Engine and updating the PBX route.
    speech_engine_host: str = field(default_factory=lambda: os.environ.get("PHONE_PLATFORM_SPEECH_ENGINE_HOST", "127.0.0.1"))
    speech_engine_port: int = field(default_factory=lambda: int(os.environ.get("PHONE_PLATFORM_SPEECH_ENGINE_PORT", "0")))
    speech_engine_peers: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.environ.get("PHONE_PLATFORM_SPEECH_ENGINE_PEERS", "127.0.0.1").split(",") if x.strip()))
