"""FFmpeg / FFprobe wrapper.

All external calls use list-style argv (no shell string concatenation) so
Windows paths with spaces, CJK characters, emoji, etc. are handled safely.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings

_COMMON_WIN_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
]


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe call fails."""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _find_common(name: str) -> str | None:
    exe = f"{name}.exe"
    for directory in _COMMON_WIN_DIRS:
        candidate = Path(directory) / exe
        if candidate.exists():
            return str(candidate)
    return None


def locate_ffmpeg() -> str:
    settings = get_settings()
    if settings.ffmpeg_path:
        return settings.ffmpeg_path
    env = os.environ.get("FFMPEG_PATH")
    if env:
        return env
    return _which("ffmpeg") or _find_common("ffmpeg") or "ffmpeg"


def locate_ffprobe() -> str:
    settings = get_settings()
    if settings.ffprobe_path:
        return settings.ffprobe_path
    env = os.environ.get("FFPROBE_PATH")
    if env:
        return env
    return _which("ffprobe") or _find_common("ffprobe") or "ffprobe"


def _resolve_exe(name: str) -> str:
    return locate_ffmpeg() if name == "ffmpeg" else locate_ffprobe()


def _exe_available(exe: str) -> bool:
    if not exe:
        return False
    if Path(exe).exists():
        return True
    return _which(exe) is not None


def ffmpeg_available() -> str | None:
    p = locate_ffmpeg()
    return p if _exe_available(p) else None


def ffprobe_available() -> str | None:
    p = locate_ffprobe()
    return p if _exe_available(p) else None


def long_path(path: str | Path) -> str:
    r"""Return a form of *path* that survives Windows' MAX_PATH limit.

    Windows still enforces a 260-character limit unless the ``LongPathsEnabled``
    policy is on. FFmpeg is a native program and happily writes to a longer
    path, but Python's own ``os.path`` calls then fail to see the file — which
    used to look like "ffmpeg succeeded but produced nothing".

    Prefixing with ``\\?\`` (or ``\\?\UNC\`` for network shares) opts into the
    extended-length API for both sides, so a deep workspace keeps working.
    """
    text = str(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    try:
        absolute = os.path.abspath(text)
    except (OSError, ValueError):
        return text
    if absolute.startswith("\\\\"):  # UNC share: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


def path_length_warning(path: str | Path) -> str | None:
    """A user-facing hint when a path is close to (or past) Windows' limit."""
    if os.name != "nt":
        return None
    length = len(str(path))
    if length < 240:
        return None
    return (
        f"路径长度 {length} 接近或超过 Windows 的 260 字符上限，"
        "建议把项目输出工作区放到更短的目录（例如 D:\\TSB）。"
    )


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run an external command, raising FFmpegError on non-zero exit.

    FFmpeg can exit **0** while writing nothing — the ``image2`` muxer refuses a
    filename with no ``%03d`` pattern, and on Windows a path past ``MAX_PATH``
    can be written by FFmpeg yet be invisible to Python. Both used to surface
    much later as a confusing ``FileNotFoundError`` from Pillow, so a
    single-image output is verified here.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FFmpegError(f"Executable not found: {cmd[0]}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        preview = " ".join(cmd[:6])
        raise FFmpegError(f"Command failed ({proc.returncode}): {preview} ...\n{stderr}")

    # Verify a single-image output actually landed. Retry through the
    # extended-length path form: FFmpeg may have written a >MAX_PATH file that
    # a plain Python stat cannot see.
    raw_last = str(cmd[-1]) if cmd else ""
    if raw_last.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) and "%" not in raw_last:
        if not Path(raw_last).is_file() and not Path(long_path(raw_last)).is_file():
            stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            raise FFmpegError(
                f"Command reported success but wrote no image: {raw_last}\n{stderr}"
            )
    return proc


# --- Probe data structures -------------------------------------------------

@dataclass
class VideoStreamInfo:
    width: int = 0
    height: int = 0
    rotation: int = 0
    codec: str = ""
    pixel_format: str = ""
    fps_num: int = 0
    fps_den: int = 0
    vfr: bool = False
    duration_sec: float = 0.0
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""


@dataclass
class MediaInfo:
    duration_sec: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    audio_codec: str = ""
    container: str = ""
    video: VideoStreamInfo = field(default_factory=VideoStreamInfo)


def _parse_rational(value: str | None) -> tuple[int, int]:
    """Parse an ffprobe rational like '30000/1001' into (num, den)."""
    if not value or "/" not in value:
        return (0, 0)
    try:
        num_s, den_s = value.split("/", 1)
        return int(num_s), int(den_s)
    except ValueError:
        return (0, 0)


def _parse_rotation(stream: dict[str, Any]) -> int:
    # Prefer side-data Display Matrix rotation.
    for side in stream.get("side_data_list") or []:
        if side.get("side_data_type") == "Display Matrix":
            try:
                rotation = int(round(float(side.get("rotation", 0))))
            except (TypeError, ValueError):
                rotation = 0
            return ((-rotation) % 360) if rotation else 0
    tags = stream.get("tags") or {}
    raw = tags.get("rotate", "0")
    try:
        return int(float(raw)) % 360
    except (TypeError, ValueError):
        return 0


def probe(source: str | Path) -> dict[str, Any]:
    """Return raw ffprobe JSON for a media file."""
    cmd = [
        locate_ffprobe(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        long_path(source),
    ]
    proc = run(cmd)
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"Invalid ffprobe JSON for {source}") from exc


def probe_media(source: str | Path) -> MediaInfo:
    """Probe a media file and normalize the result into a MediaInfo."""
    data = probe(source)
    info = MediaInfo()
    fmt = data.get("format") or {}
    info.container = fmt.get("format_name", "")
    try:
        info.duration_sec = float(fmt.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        info.duration_sec = 0.0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not info.has_video:
            info.has_video = True
            v = info.video
            v.width = int(stream.get("width", 0) or 0)
            v.height = int(stream.get("height", 0) or 0)
            v.codec = stream.get("codec_name", "")
            v.pixel_format = stream.get("pix_fmt", "")
            v.rotation = _parse_rotation(stream)
            avg = stream.get("avg_frame_rate", "")
            rfr = stream.get("r_frame_rate", "")
            v.fps_num, v.fps_den = _parse_rational(avg)
            v.vfr = bool(avg) and bool(rfr) and avg != rfr
            try:
                v.duration_sec = float(stream.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                v.duration_sec = 0.0
            v.color_primaries = stream.get("color_primaries", "") or ""
            v.color_transfer = stream.get("color_transfer", "") or ""
            v.color_space = stream.get("color_space", "") or ""
        elif codec_type == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name", "")

    # Prefer the video stream's duration when available.
    if info.video.duration_sec:
        info.duration_sec = info.video.duration_sec
    return info


# --- Media operations ------------------------------------------------------

def extract_frame(
    source: str | Path,
    time_sec: float,
    out_path: str | Path,
    width: int | None = None,
) -> Path:
    """Extract a single frame at an exact-ish time to a JPEG file."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-accurate_seek",
        "-ss", f"{time_sec:.3f}",
        "-i", long_path(source),
        "-frames:v", "1",
    ]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    # `-update 1` is required: without it the image2 muxer refuses a filename
    # that has no `%03d` pattern, prints a warning, and exits 0 without writing.
    cmd += ["-q:v", "3", "-update", "1", long_path(out)]
    run(cmd)
    return out


def extract_preview(
    source: str | Path,
    start_sec: float,
    duration_sec: float,
    out_path: str | Path,
    width: int | None = None,
    crf: int = 28,
    keep_audio: bool = False,
) -> Path:
    """Generate a low-bitrate proxy clip (used for dynamic review in M3+)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", long_path(source),
        "-t", f"{duration_sec:.3f}",
    ]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    if not keep_audio:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        long_path(out),
    ]
    run(cmd)
    return out
