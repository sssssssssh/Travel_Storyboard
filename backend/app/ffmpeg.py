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
    """Raised when an ffmpeg/ffprobe call fails.

    ``code`` is a stable, machine-readable identifier so a failure can be
    classified without parsing prose (TSB-241). Callers that surface errors to
    users should map it to a message rather than showing raw stderr.
    """

    #: Default classification for an unspecified external-tool failure.
    DEFAULT_CODE = "FFMPEG_FAILED"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.DEFAULT_CODE


# Stable error codes shared by the media boundary (TSB-241).
ERR_PATH_TOO_LONG = "PATH_TOO_LONG"
ERR_OUTPUT_NOT_CREATED = "OUTPUT_NOT_CREATED"
ERR_MEDIA_UNREADABLE = "MEDIA_UNREADABLE"
ERR_EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
ERR_COMMAND_FAILED = "COMMAND_FAILED"


def path_exceeds_limit(path: str | Path) -> bool:
    """True when *path* is long enough that Windows may refuse it.

    Windows enforces MAX_PATH (260) unless ``LongPathsEnabled`` is set AND every
    participating process opts in. Python and FFmpeg disagree in that state, so
    callers treat this as a hard, reportable condition rather than letting
    FFmpeg write a file Python cannot see.
    """
    if os.name != "nt":
        return False
    return len(str(path)) >= 260


def ensure_path_usable(path: str | Path) -> None:
    """Raise a classified error when *path* is too long for a safe round trip."""
    if path_exceeds_limit(path):
        raise FFmpegError(
            f"路径过长（{len(str(path))} 字符，Windows 上限 260）：{path}。"
            "请把项目输出工作区放到更短的目录（例如 D:\\TSB）。",
            code=ERR_PATH_TOO_LONG,
        )


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


def safe_stat(path: str | Path):
    """``Path.stat`` that transparently retries through the extended-length form.

    Python's own ``Path.stat()`` obeys MAX_PATH when ``LongPathsEnabled`` is off,
    so a source file that FFmpeg reads happily can look missing to the indexer.
    Retrying with ``\\\\?\\`` makes deep source paths behave like shallow ones.

    Only a *missing path* triggers the retry. ``PermissionError`` and other real
    failures propagate unchanged, so a locked file is still reported as
    ``source_unavailable`` rather than being mistaken for a long-path problem.
    """
    target = Path(path)
    try:
        return target.stat()
    except FileNotFoundError:
        if os.name != "nt":
            raise
        return os.stat(long_path(target))


def safe_open(path: str | Path, mode: str = "rb"):
    """``open`` that falls back to the extended-length form on Windows.

    Only a missing path triggers the fallback; permission errors propagate.
    """
    try:
        return open(path, mode)
    except FileNotFoundError:
        if os.name != "nt":
            raise
        return open(long_path(path), mode)


def safe_is_file(path: str | Path) -> bool:
    """``Path.is_file`` that is not defeated by a long path."""
    target = Path(path)
    try:
        if target.is_file():
            return True
    except OSError:
        # Permission problems are "not a readable file" for our purposes.
        pass
    if os.name != "nt":
        return False
    try:
        return os.path.isfile(long_path(target))
    except (OSError, ValueError):
        return False


def safe_mkdir(path: str | Path) -> None:
    """Create a directory (and parents) without hitting MAX_PATH.

    ``Path.mkdir(parents=True)`` walks the path itself and fails past 260 chars
    even when the target is a normal video/JPEG output directory.
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        os.makedirs(long_path(target), exist_ok=True)


def safe_unlink(path: str | Path) -> None:
    """Remove a file, tolerating >MAX_PATH paths. Missing files are ignored."""
    target = Path(path)
    try:
        target.unlink(missing_ok=True)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        extended = long_path(target)
        try:
            os.unlink(extended)
        except FileNotFoundError:
            pass


def safe_image_open(path: str | Path):
    """Open an image with Pillow, tolerating >MAX_PATH paths.

    Pillow calls ``builtins.open`` internally, which obeys the Windows MAX_PATH
    limit — so a JPEG this app just wrote can be invisible to it. Read the bytes
    ourselves and hand Pillow a buffer instead.
    """
    import io as _io

    from PIL import Image

    target = Path(path)
    try:
        with target.open("rb") as handle:
            return Image.open(_io.BytesIO(handle.read()))
    except (FileNotFoundError, OSError):
        if os.name != "nt":
            raise
        with open(long_path(target), "rb") as handle:
            return Image.open(_io.BytesIO(handle.read()))


def safe_replace(src: str | Path, dst: str | Path) -> None:
    """Move *src* onto *dst*, tolerating >MAX_PATH paths on either side."""
    source, target = Path(src), Path(dst)
    try:
        source.replace(target)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        os.replace(long_path(source), long_path(target))


def safe_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to a possibly >MAX_PATH path."""
    target = Path(path)
    try:
        target.write_text(text, encoding=encoding)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        with open(long_path(target), "w", encoding=encoding, newline="") as handle:
            handle.write(text)


def run(
    cmd: list[str],
    timeout: int = 3600,
    *,
    expect_output: str | Path | None = None,
    expect_pattern: str | None = None,
) -> subprocess.CompletedProcess:
    """Run an external command, raising a classified ``FFmpegError`` on failure.

    FFmpeg can exit **0** while writing nothing. Two known causes are handled at
    this boundary so they never surface later as a confusing
    ``FileNotFoundError`` from Pillow or OpenCV:

    * the ``image2`` muxer refuses a filename with no ``%03d`` pattern;
    * on Windows a path past ``MAX_PATH`` can be written by FFmpeg yet stay
      invisible to Python's ``os.path``.

    Pass ``expect_output`` for a single file, or ``expect_pattern`` (a glob) for
    a frame sequence, and the result is verified before returning.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"Executable not found: {cmd[0]}", code=ERR_EXECUTABLE_MISSING
        ) from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        preview = " ".join(cmd[:6])
        raise FFmpegError(
            f"Command failed ({proc.returncode}): {preview} ...\n{stderr}",
            code=ERR_COMMAND_FAILED,
        )

    if expect_output is not None:
        target = Path(expect_output)
        if not target.is_file() and not Path(long_path(target)).is_file():
            stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            if path_exceeds_limit(target):
                raise FFmpegError(
                    f"输出路径过长，FFmpeg 退出码为 0 但 Python 读不到文件：{target}\n{stderr}",
                    code=ERR_PATH_TOO_LONG,
                )
            raise FFmpegError(
                f"Command reported success but wrote no output: {target}\n{stderr}",
                code=ERR_OUTPUT_NOT_CREATED,
            )
    elif expect_pattern is not None:
        # Frame sequences: verify at least one file matched.
        import glob as _glob

        matches = _glob.glob(expect_pattern)
        if not matches:
            directory = Path(expect_pattern).parent
            # glob() itself can miss >MAX_PATH output; retry via the prefix form.
            if directory.is_dir():
                matches = [str(p) for p in directory.iterdir() if p.is_file()]
            if not matches:
                stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
                code = (
                    ERR_PATH_TOO_LONG
                    if path_exceeds_limit(expect_pattern)
                    else ERR_OUTPUT_NOT_CREATED
                )
                raise FFmpegError(
                    f"Command reported success but produced no frames matching "
                    f"{expect_pattern}\n{stderr}",
                    code=code,
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
        raise FFmpegError(
            f"Invalid ffprobe JSON for {source}", code=ERR_MEDIA_UNREADABLE
        ) from exc


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
    safe_mkdir(out.parent)
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
    run(cmd, expect_output=out)
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
    safe_mkdir(out.parent)
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
    run(cmd, expect_output=out)
    return out
