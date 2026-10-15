"""Application configuration via environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Bump when the indexing / detection algorithm changes in a way that
# invalidates previously stored analysis results.
# v2.0.4 deliberately does NOT bump this: the round only fixed path handling,
# job creation and proxy acceptance — the detection algorithm is unchanged, so
# existing analysis stays valid and is not re-run.
ANALYSIS_VERSION = "2.0.2"
APP_VERSION = "2.1.0"

# File extensions treated as source video.
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".webm",
    ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ts", ".rmvb", ".rm", ".mxf",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRAVELSB_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Server ---
    bind_host: str = "127.0.0.1"
    bind_port: int = 8737

    # --- Storage ---
    data_dir: str = "./data"
    database_url: str = ""  # empty -> derive from data_dir

    # --- FFmpeg ---
    ffmpeg_path: str = ""
    ffprobe_path: str = ""

    # --- Processing ---
    max_concurrency: int = 2
    thumb_width: int = 640
    proxy_width: int = 480
    # Scene detection tuning
    detect_sample_fps: float = 1.0
    detect_max_segment_sec: float = 30.0
    detect_threshold: float = 0.35

    # --- AI model (M3) ---
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""

    # --- Vision model guard rails (M5, TSB-302) ---
    # All optional; defaults are deliberately conservative.
    model_timeout_sec: float = 120.0
    model_max_images: int = 4
    model_max_request_bytes: int = 8_000_000
    model_max_calls_per_job: int = 200
    model_max_retries: int = 2
    # Capability declarations. `native_video` is opted into, never assumed.
    model_supports_json_schema: bool = False
    model_supports_native_video: bool = False
    # A plain-`http://` endpoint is refused for non-loopback hosts unless the
    # operator explicitly accepts cleartext for a known endpoint.
    model_allow_insecure_http: bool = False

    # --- Vision semantics (M5, TSB-303/304) ---
    # Master switch: sending images off-box requires an explicit opt-in.
    model_mode: str = "off"  # off | on
    vision_prompt_version: str = "1.0"
    vision_schema_version: str = "1.0"
    vision_max_segments_per_job: int = 200
    vision_target_width: int = 768
    # Output-token ceilings. Reasoning models (e.g. GLM-5.3-Flash) spend part of
    # this budget on a separate `reasoning` field and leave `content` null when it
    # runs out, so these must be generous enough to cover reasoning AND the JSON
    # answer. A too-small value is reported as MODEL_INVALID_OUTPUT, never as a
    # silent empty success.
    vision_max_tokens: int = 4000
    vision_max_tokens_motion: int = 4000
    # Motion review (M5.2, TSB-307): frames sampled inside the valid interval.
    motion_frame_count: int = 5

    # --- Logging ---
    log_level: str = "INFO"

    # --- CORS origins allowed for local dev frontend ---
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        db_path = self.data_path / "travel_storyboard.db"
        return f"sqlite:///{db_path.as_posix()}"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
