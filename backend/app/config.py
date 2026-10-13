"""Application configuration via environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Bump when the indexing / detection algorithm changes in a way that
# invalidates previously stored analysis results.
ANALYSIS_VERSION = "2.0.2"
APP_VERSION = "2.0.2"

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
