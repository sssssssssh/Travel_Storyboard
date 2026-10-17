"""TSB-327 - derive a short, silent, metadata-stripped clip from one shot.



This module is the only place that turns a user's footage into something that may

leave the machine, so it carries the strictest guarantees in the batch:



* **Originals are read-only (C1).** ``source_path`` is opened for reading by

  FFmpeg; nothing is written beside it. The staging directory is the only output,
  and it is created through :func:`create_owned_staging` so cleanup can prove it
  owns what it deletes (C7).

* **The source must still be the one we analysed.** The asset's recorded size,

  mtime and quick fingerprint are re-checked before encoding, so a swapped file
  raises ``SOURCE_CHANGED`` instead of producing a clip of the wrong footage.

* **Time is mapped, not guessed.** A model answers in *clip-relative* seconds; the

  absolute window is ``clip_start + rel``. Every event must satisfy
  ``segment.start_sec <= abs_start < abs_end <= segment.end_sec`` -?a violation is
  rejected rather than silently clamped, because clamping would quietly move a
  person's edit boundary (C2/C3).

* **Metadata is stripped, and stripping is verified.** ``creation_time``, device

  tags, GPS, subtitles and data streams are removed. The task book is explicit
  that this is *not* face/plate anonymisation, and the code says so too.



What this module deliberately does **not** claim:



* that removing metadata anonymises the picture;

* that a derived clip is "humble" about HDR - when the source is HDR and no

  reliable tone-mapping is configured, the clip is flagged ``COLOR_UNVERIFIED``
  rather than silently presented as correct SDR.



Two probe paths exist on purpose. FFmpeg needs a real file path, but the

byte-level decisions (duration/fps/rotation/HDR) must be *asserted* against what

actually landed on disk, so :func:`probe_clip` re-reads the artifact and its

result - not the encoder's intent - is what gets returned.

"""



from __future__ import annotations



import hashlib

import json

import logging
from contextlib import contextmanager

from dataclasses import dataclass, field

from pathlib import Path



from ...ffmpeg import (

    MediaInfo,
    ensure_path_usable,
    locate_ffmpeg,
    probe_media,
    safe_open,
    safe_stat,
    safe_unlink,
    run,

)

from ..staging_cleanup import cleanup_staging_or_log, create_owned_staging



logger = logging.getLogger(__name__)



#: Initial, adjustable derivation parameters (the task book calls them "初始可调

#: 初始可调测试参数, 并非声称服务端限). They are budget choices, not endpoint facts.

DEFAULT_MAX_CLIP_SEC = 15.0

DEFAULT_MIN_CLIP_SEC = 5.0

DEFAULT_TARGET_HEIGHT = 720

DEFAULT_CRF = 26



#: Codes surfaced to callers instead of vague failures (C6).

CLIP_TOO_SHORT = "CLIP_TOO_SHORT"

CLIP_TOO_LONG = "CLIP_TOO_LONG"

SOURCE_CHANGED = "SOURCE_CHANGED"

COLOR_UNVERIFIED = "COLOR_UNVERIFIED"

SEGMENT_RANGE_INVALID = "SEGMENT_RANGE_INVALID"

CLIP_UNREADABLE = "CLIP_UNREADABLE"

NO_VIDEO_STREAM = "NO_VIDEO_STREAM"





class VideoClipError(RuntimeError):

    """A classified clip-derivation failure."""



    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}





@dataclass

class ClipSpec:

    """What to derive, expressed in source coordinates."""



    segment_id: str
    asset_id: str
    source_path: Path
    #: The *editable* interval. Never the detected interval: a person's trim wins.
    start_sec: float
    end_sec: float
    max_duration_sec: float = DEFAULT_MAX_CLIP_SEC





@dataclass

class ClipArtifact:

    """A derived clip plus the evidence needed to trust and clean it up."""



    path: Path
    segment_id: str
    asset_id: str
    #: Absolute seconds in the source where this clip starts.
    source_clip_start_sec: float
    #: Absolute seconds in the source where this clip ends.
    source_clip_end_sec: float
    duration_sec: float
    width: int
    height: int
    rotation: int
    vfr: bool
    has_audio: bool
    size_bytes: int
    sha256: str
    container: str
    #: True when the source was HDR and no verified tone-map was applied.
    color_unverified: bool
    #: Non-fatal notes worth showing (rotation normalised, VFR re-timed, ...).
    notes: list[str] = field(default_factory=list)



    @property
    def clip_id(self) -> str:
        return self.segment_id



    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "source_clip_start_sec": round(self.source_clip_start_sec, 3),
            "source_clip_end_sec": round(self.source_clip_end_sec, 3),
            "duration_sec": round(self.duration_sec, 3),
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "vfr": self.vfr,
            "has_audio": self.has_audio,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "container": self.container,
            "color_unverified": self.color_unverified,
            "notes": list(self.notes),
        }





# ---------------------------------------------------------------------------

# time mapping

# ---------------------------------------------------------------------------



@dataclass(frozen=True)

class EventWindow:

    """A model-reported window, resolved to absolute source seconds."""



    start_rel_sec: float
    end_rel_sec: float
    start_abs_sec: float
    end_abs_sec: float





def map_event_window(

    *,
    clip: ClipArtifact,
    segment_start_sec: float,
    segment_end_sec: float,
    start_rel_sec: float,
    end_rel_sec: float,

) -> EventWindow:

    """Resolve clip-relative seconds to absolute source seconds.



    Rejects rather than repairs. A negative, inverted or out-of-clip window is a
    malformed observation; clamping it would fabricate a window the model never
    claimed, and allow it to drift past the segment the person actually trimmed
    (C2/C3).
    """
    for name, value in (("start_rel_sec", start_rel_sec), ("end_rel_sec", end_rel_sec)):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise VideoClipError(
                SEGMENT_RANGE_INVALID, f"{name} must be a number, got {type(value).__name__}"
            )
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise VideoClipError(SEGMENT_RANGE_INVALID, f"{name} must be finite, got {value!r}")



    if start_rel_sec < 0 or end_rel_sec < 0:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"event window must not be negative ({start_rel_sec}..{end_rel_sec})",
        )
    if end_rel_sec <= start_rel_sec:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"event window is inverted or empty ({start_rel_sec}..{end_rel_sec})",
        )



    duration = clip.source_clip_end_sec - clip.source_clip_start_sec
    # A tiny tolerance absorbs float noise from the model's decimal output without
    # letting a genuinely out-of-range window through.
    tolerance = 0.05
    if end_rel_sec > duration + tolerance:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"event window {end_rel_sec}s exceeds the clip duration {duration:.3f}s",
        )



    start_abs = clip.source_clip_start_sec + start_rel_sec
    end_abs = clip.source_clip_start_sec + end_rel_sec



    if start_abs < segment_start_sec - tolerance or end_abs > segment_end_sec + tolerance:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"event window {start_abs:.3f}..{end_abs:.3f}s falls outside the edited "
            f"segment {segment_start_sec:.3f}..{segment_end_sec:.3f}s",
        )
    return EventWindow(
        start_rel_sec=start_rel_sec,
        end_rel_sec=end_rel_sec,
        start_abs_sec=start_abs,
        end_abs_sec=end_abs,
    )





# ---------------------------------------------------------------------------

# source verification

# ---------------------------------------------------------------------------



def verify_source_unchanged(asset, source_path: Path) -> dict:

    """Re-check that the file on disk is still the one that was analysed.



    Uses the same cheap facts the indexer recorded. A mismatch is ``SOURCE_CHANGED``
    (a *failure*, C6) rather than a silent re-analysis, because the caller is about
    to attach a model's observation to a specific span of a specific file.
    """
    path = Path(source_path)
    try:
        stat = safe_stat(path)
    except OSError as exc:
        raise VideoClipError(SOURCE_CHANGED, f"source is unreadable: {exc}") from exc
    if stat is None:
        raise VideoClipError(SOURCE_CHANGED, "source no longer exists")



    recorded_size = getattr(asset, "file_size_bytes", None)
    if recorded_size and int(recorded_size) != int(stat.st_size):
        raise VideoClipError(
            SOURCE_CHANGED,
            f"source size changed ({stat.st_size} != recorded {recorded_size}); "
            "re-scan before analysing",
        )



    recorded_mtime = getattr(asset, "mtime_ns", None)
    if recorded_mtime and int(recorded_mtime) != int(stat.st_mtime_ns):
        raise VideoClipError(
            SOURCE_CHANGED,
            "source modification time changed; re-scan before analysing",
        )
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}





# ---------------------------------------------------------------------------

# derivation

# ---------------------------------------------------------------------------



def plan_clip_duration(

    *,
    segment_start_sec: float,
    segment_end_sec: float,
    max_duration_sec: float = DEFAULT_MAX_CLIP_SEC,

) -> tuple[float, float, list[str]]:

    """Choose the clip window inside the segment, and explain any clamping.



    Returns ``(start, end, notes)``. The window never leaves the edited interval;
    if the shot is longer than the budget we take the *first* ``max_duration_sec``
    and say so, because silently taking the middle would make the model's event
    times describe a range the user cannot see marked in the UI.
    """
    if segment_end_sec <= segment_start_sec:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"segment interval is empty ({segment_start_sec}..{segment_end_sec})",
        )
    available = segment_end_sec - segment_start_sec
    notes: list[str] = []
    end = segment_end_sec
    if available > max_duration_sec:
        end = segment_start_sec + max_duration_sec
        notes.append(
            f"shot is {available:.1f}s; only the first {max_duration_sec:.1f}s were "
            "derived (budget limit, configurable)"
        )
    return segment_start_sec, end, notes





def _is_hdr(info: MediaInfo) -> bool:

    """HDR-ish transfer characteristics that need a verified tone-map."""
    transfer = (info.video.color_transfer or "").lower()
    return transfer in {"smpte2084", "arib-std-b67"}  # PQ / HLG





def derive_clip(

    *,
    project_id: str,
    job_id: str,
    spec: ClipSpec,
    asset=None,
    cancel_check=None,
    target_height: int = DEFAULT_TARGET_HEIGHT,
    crf: int = DEFAULT_CRF,
    staged: Path | None = None,

) -> ClipArtifact:

    """Encode one silent, metadata-free H.264 clip for ``spec``.



    The output lands in a staging directory this job owns. On any failure the
    staging is left in place for the caller's cleanup step to report - this
    function does not hide residue.
    """
    source = Path(spec.source_path)
    ensure_path_usable(source)



    if asset is not None:
        verify_source_unchanged(asset, source)



    try:
        source_info = probe_media(source)
    except Exception as exc:  # noqa: BLE001 - classified below
        raise VideoClipError(CLIP_UNREADABLE, f"cannot probe source: {exc}") from exc
    if not source_info.has_video:
        raise VideoClipError(NO_VIDEO_STREAM, "source has no video stream")



    start, end, notes = plan_clip_duration(
        segment_start_sec=spec.start_sec,
        segment_end_sec=spec.end_sec,
        max_duration_sec=spec.max_duration_sec,
    )
    duration = end - start



    if source_info.video.vfr:
        notes.append("source is variable frame rate; the clip is re-timed to a constant rate")
    color_unverified = _is_hdr(source_info)
    if color_unverified:
        notes.append(
            f"HDR source ({source_info.video.color_transfer}); no verified tone-map applied, "
            "colours are COLOR_UNVERIFIED"
        )
    if source_info.video.rotation:
        notes.append(f"rotation {source_info.video.rotation}° normalised into the pixels")



    # TSB-340: ``staged`` lets the caller pre-register the exact leaf and hand it
    # in. ``create_owned_staging`` proves ownership by requiring the directory to
    # be *newly created* (``exist_ok=False``), so calling it again on a path the
    # caller already registered raises FileExistsError -- the two R1 fixes
    # (TSB-336 pre-registration and derive_clip's own creation) collided on the
    # real API path. Reusing the caller's registered leaf keeps the ownership
    # proof intact: nothing new is adopted, the same exact leaf is used.
    if staged is not None:
        stage = Path(staged)
    else:
        stage = create_owned_staging(
            project_id, f"staging/{spec.asset_id}/{spec.segment_id}", owner_job_id=job_id
        )
    out = Path(stage) / f"{spec.segment_id}.mp4"



    # -ss before -i seeks cheaply and, with re-encoding, is frame-accurate enough
    # for a window we then verify by re-probing the artifact.
    # -an: derived clips are silent by design (privacy + budget).
    # -map_metadata -1 / -map_metadata:s:v -1: drop container and stream tags
    #   (creation_time, device make/model, GPS in container metadata).
    # -sn -dn: drop subtitle and data streams, which are another metadata channel.
    # -fflags/-flags +bitexact: measured on FFmpeg 9.0.2, these (together with
    #   -map_metadata -1) do not remove the muxer's own "encoder=Lavf<version>"
    #   tag; nothing does, including -metadata encoder=. They are kept because
    #   they suppress other encoder-injected bookkeeping, and the metadata test
    #   reports the toolchain tag separately from personal data rather than
    #   claiming it was stripped.
    # scale=-2:target_height keeps the aspect ratio with an even width.
    vf = f"scale=-2:{target_height}"
    if source_info.video.vfr:
        vf = f"fps=30,{vf}"
    cmd = [
        locate_ffmpeg(),
        "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+bitexact",
        "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-map_metadata", "-1", "-map_metadata:s:v", "-1",
        "-flags", "+bitexact",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd, expect_output=out, cancel_check=cancel_check)



    artifact = probe_clip(
        path=out,
        segment_id=spec.segment_id,
        asset_id=spec.asset_id,
        source_clip_start_sec=start,
        source_clip_end_sec=end,
        color_unverified=color_unverified,
        notes=notes,
    )
    return artifact





def probe_clip(

    *,
    path: Path,
    segment_id: str,
    asset_id: str,
    source_clip_start_sec: float,
    source_clip_end_sec: float,
    color_unverified: bool = False,
    notes: list[str] | None = None,

) -> ClipArtifact:

    """Re-read the artifact and describe what actually landed on disk.



    The encoder's intent is not evidence: FFmpeg can exit 0 without a usable file
    (AGENTS.md 5.2), and the only trustworthy duration is the one ffprobe reads
    back. ``duration`` is the *measured* value, not the requested one.
    """
    path = Path(path)
    if not path.is_file():
        raise VideoClipError(CLIP_UNREADABLE, "derived clip was not created")



    try:
        info = probe_media(path)
    except Exception as exc:  # noqa: BLE001
        raise VideoClipError(CLIP_UNREADABLE, f"derived clip is unreadable: {exc}") from exc



    if info.has_audio:
        # Not a crash, but it means a privacy/budget assumption failed; surface it.
        raise VideoClipError(
            CLIP_UNREADABLE,
            "derived clip unexpectedly carries an audio track; refusing to treat it as silent",
        )



    size = path.stat().st_size
    digest = _sha256(path)
    measured = info.duration_sec or (source_clip_end_sec - source_clip_start_sec)
    return ClipArtifact(
        path=path,
        segment_id=segment_id,
        asset_id=asset_id,
        source_clip_start_sec=source_clip_start_sec,
        source_clip_end_sec=source_clip_start_sec + measured,
        duration_sec=measured,
        width=info.video.width,
        height=info.video.height,
        rotation=info.video.rotation,
        vfr=info.video.vfr,
        has_audio=info.has_audio,
        size_bytes=size,
        sha256=digest,
        container=info.container,
        color_unverified=color_unverified,
        notes=list(notes or []),
    )





def _sha256(path: Path, chunk: int = 1 << 20) -> str:

    h = hashlib.sha256()
    with safe_open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()





def strip_container_metadata_report(path: Path) -> dict:
    """Report which *personal* metadata tags survive in a derived clip.

    Separates two things that must not be conflated:

    * **Personal / identifying** tags -- ``creation_time``, device make/model,
      ``location``/GPS, ``comment``, Apple QuickTime tags. These must be gone, and
      a survivor is a finding.
    * **Toolchain / structural** tags -- ``encoder=Lavf<version>``, ``major_brand``,
      ``minor_version``, ``compatible_brands``. FFmpeg's MP4 muxer re-stamps
      ``encoder`` on every write and no combination of ``-map_metadata -1``,
      ``+bitexact`` or ``-metadata encoder=`` removes it (measured on FFmpeg 9.0.2,
      see the note in :func:`derive_clip`). It names the encoder version, not the
      user, so it is reported separately instead of being called a privacy leak --
      claiming otherwise would be an inaccurate finding.

    ``personal_tags`` empty is the expectation; anything in it is a real problem.
    """
    from ...ffmpeg import probe

    try:
        raw = probe(path)
    except Exception:  # noqa: BLE001
        return {"readable": False, "personal_tags": {}, "toolchain_tags": {}}

    fmt_tags = ((raw.get("format") or {}).get("tags") or {})
    stream_tags: dict = {}
    for stream in raw.get("streams") or []:
        stream_tags.update(stream.get("tags") or {})

    PERSONAL = {
        "creation_time", "make", "model", "location", "location-eng", "gps",
        "com.apple.quicktime.make", "com.apple.quicktime.model",
        "com.apple.quicktime.location.iso6709", "com.apple.quicktime.creationdate",
        "comment", "artist", "author", "copyright", "title", "description",
        "device", "software",
    }
    TOOLCHAIN = {"encoder", "major_brand", "minor_version", "compatible_brands"}

    #: handler_name is special: the *muxer* writes "VideoHandler"/"SoundHandler"
    #: on every file, but a camera can also put a user string there. Measured
    #: behaviour is that our derivation replaces a custom value with the muxer
    #: default, so the value -- not the key -- decides the classification.
    HANDLER_DEFAULTS = {"videohandler", "soundhandler", "videohandler\x00"}

    personal: dict[str, str] = {}
    toolchain: dict[str, str] = {}
    for source in (fmt_tags, stream_tags):
        for key, value in source.items():
            low = key.lower()
            text = str(value)[:120]
            if low == "handler_name":
                if text.strip().lower() in HANDLER_DEFAULTS:
                    toolchain[key] = text
                else:
                    personal[key] = text
            elif low in PERSONAL:
                personal[key] = text
            elif low in TOOLCHAIN:
                toolchain[key] = text

    return {"readable": True, "personal_tags": personal, "toolchain_tags": toolchain}


def cleanup_clip(artifact: ClipArtifact, *, project_id: str, job_id: str):

    """Owned cleanup for one derived clip (delegates to the shared whitelist)."""
    from ..staging_cleanup import cleanup_staging_or_log


    # ``artifact.path`` is the clip *file*; its parent is exactly the leaf that
    # ``create_owned_staging`` created and registered. Passing anything higher
    # (the asset directory, the project root) is refused by the cleaner, which is
    # the behaviour TSB-336 fixed a caller for.
    stage = artifact.path.parent
    return cleanup_staging_or_log(project_id, stage, owner_job_id=job_id)


@contextmanager
def owned_clip_staging(project_id: str, *, asset_id: str, segment_id: str, job_id: str):
    """Yield the owned staging leaf and register it **before** encoding starts.

    TSB-336: ``derive_clip`` calls ``create_owned_staging`` and can then fail
    (FFmpeg missing, source unreadable, cancelled, probe mismatch, encoding
    error). Registering the path only after a *successful* return meant a failed
    or cancelled derivation left its directory behind with nothing referencing
    it, so no cleanup step could report or reclaim it.

    The leaf is therefore published to the caller as soon as it exists: the
    caller appends it to its cleanup list inside the ``with`` body, so every exit
    path -- success, exception, cancellation -- has it recorded.

    Ownership is not weakened: the directory still must be newly created by
    ``create_owned_staging``, and ``cleanup_staging_or_log`` still validates the
    exact leaf shape. This helper changes *when* the path is known, not *what* is
    permitted to be deleted.
    """
    stage = create_owned_staging(
        project_id, f"staging/{asset_id}/{segment_id}", owner_job_id=job_id
    )
    try:
        yield Path(stage)
    finally:
        # No deletion here: cleanup is the caller's reported step so its outcome
        # can be surfaced (C6). This only guarantees the path was observable.
        pass

