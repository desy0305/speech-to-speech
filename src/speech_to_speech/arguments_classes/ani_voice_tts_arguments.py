from dataclasses import dataclass, field


@dataclass
class AniVoiceTTSHandlerArguments:
    ani_voice_api_url: str = field(
        default="http://ani-voice-api:8000",
        metadata={
            "help": "Base URL for the Ani Voice API sidecar. Default is 'http://ani-voice-api:8000'."
        },
    )
    ani_voice_style: str = field(
        default="F5",
        metadata={"help": "Ani Voice voice_style value. Default is 'F5'."},
    )
    ani_voice_speed: float = field(
        default=1.6,
        metadata={"help": "Ani Voice synthesis speed. Default is 1.6."},
    )
    ani_voice_timeout_s: float = field(
        default=120.0,
        metadata={"help": "Ani Voice HTTP stream timeout in seconds. Default is 120."},
    )
    ani_voice_blocksize: int = field(
        default=512,
        metadata={"help": "Audio chunk size in samples for streaming output. Default is 512."},
    )
