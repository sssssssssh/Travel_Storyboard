"""Probe: does a cancel actually reclaim the whole descendant tree?

Runs the same parent -> grandchild chain the test suite uses and reports, for
each pid, whether it is still alive after ``run()`` raised FFmpegCancelled.
Kept as a standalone script so the behaviour can be checked without pytest.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ffmpeg import FFmpegCancelled, run  # noqa: E402

SCRIPT = textwrap.dedent(
    """
    import os, sys, subprocess, time
    pid_file, child_marker = sys.argv[1], sys.argv[2]
    grandchild = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(600)",
         child_marker]
    )
    open(pid_file, "w").write(f"{os.getpid()} {grandchild.pid}")
    time.sleep(600)
    """
)


def alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="probe-cancel-tree-"))
    pid_file, marker = d / "pids.txt", d / "grandchild.txt"
    cmd = [sys.executable, "-c", SCRIPT, str(pid_file), str(marker)]

    try:
        run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.05)
    except FFmpegCancelled:
        pass
    else:
        print("expected FFmpegCancelled")
        return 1

    for _ in range(100):
        if pid_file.exists():
            break
        time.sleep(0.05)
    parent_pid, grandchild_pid = (int(x) for x in pid_file.read_text().split())

    time.sleep(1.0)
    p_alive, g_alive = alive(parent_pid), alive(grandchild_pid)
    print(f"parent     {parent_pid}: {'ALIVE' if p_alive else 'gone'}")
    print(f"grandchild {grandchild_pid}: {'ALIVE' if g_alive else 'gone'}")

    # Clean up anything that survived, so the probe never leaks processes.
    if p_alive or g_alive:
        for pid in (parent_pid, grandchild_pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True)
        print("VERDICT: FAIL - descendants survived the cancel")
        return 1
    print("VERDICT: PASS - the whole tree was reclaimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
