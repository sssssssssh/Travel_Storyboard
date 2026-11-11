"""TSB-370-C: real native-video capability probe against the user's PAI-EAS.

The task book (§1.2) forbids inferring video support from an image call, so this
sends an actual minimal video and reports exactly what came back. The probe is
charged to the ``native_video`` bucket, because it is a real outbound attempt.

The clip is synthesised locally (a 2-second test pattern), never real footage, so
a probe that fails costs the user nothing sensitive.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.model_call_ledger import ModelCallLedger  # noqa: E402

RUN = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TMPDIR")
           or os.environ.get("TEMP") or "/tmp") / "tsb370-run"


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg
    exe = locate_ffmpeg()
    if not exe:
        raise SystemExit("ffmpeg not found")
    return str(exe)


def make_probe_clip(path: Path) -> None:
    """A 2-second, tiny, silent clip. Nothing from the user's library is used."""
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc=s=160x96:d=2:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-an", str(path)],
        check=True, capture_output=True,
    )


def main() -> None:
    s = get_settings()
    ledger = ModelCallLedger(RUN / "ledger.json")
    clip = RUN / "probe_clip.mp4"
    if not clip.exists():
        make_probe_clip(clip)
    data = clip.read_bytes()
    print(f"probe clip: {len(data)} bytes")

    body = json.dumps({
        "model": s.model_name,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe what moves in this video in one sentence."},
                {"type": "video_url",
                 "video_url": {"url": "data:video/mp4;base64,"
                               + base64.b64encode(data).decode()}},
            ],
        }],
        "max_tokens": 2000,
    }).encode()

    try:
        ledger.charge("native_video", "capability-probe", len(body),
                      note="native video capability probe")
    except Exception as exc:  # BudgetExhausted
        print("BLOCKED:", exc)
        return

    url = str(s.model_base_url).rstrip("/") + "/chat/completions"
    result: dict = {"probe": "video_url + base64 data URI"}
    try:
        with httpx.Client(trust_env=False, timeout=180, verify=True) as c:
            r = c.post(url, content=body,
                       headers={"Authorization": f"Bearer {s.model_api_key}",
                                "Content-Type": "application/json"})
        result["http_status"] = r.status_code
        if r.status_code == 200:
            d = r.json()
            try:
                txt = d["choices"][0]["message"].get("content") or ""
            except Exception:
                txt = ""
            result["accepted"] = True
            result["reply_head"] = txt[:200]
            result["usage"] = d.get("usage")
        else:
            result["accepted"] = False
            result["error_head"] = r.text[:300]
    except Exception as exc:
        result["accepted"] = False
        result["exception"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    print(json.dumps(result, indent=2, ensure_ascii=False))
    (RUN / "native_probe.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
