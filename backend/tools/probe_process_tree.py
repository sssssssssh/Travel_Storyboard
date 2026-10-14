"""Probe: is the parent pid seen by Popen the same one the child reports?

If the venv's python re-execs (or a launcher interposes), `proc.pid` is not the
process that spawns the grandchild, and any test asserting pid equality would be
wrong. Prints the facts; asserts nothing.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

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


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="probe-tree-"))
    pid_file, marker = d / "p.txt", d / "g.txt"
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT, str(pid_file), str(marker)])
    print("Popen.pid        :", proc.pid)
    print("sys.executable   :", sys.executable)

    for _ in range(200):
        if pid_file.exists():
            break
        time.sleep(0.05)

    text = pid_file.read_text().strip()
    print("pid file         :", text)
    parent_pid, grandchild_pid = (int(x) for x in text.split())
    print("parent == Popen  :", parent_pid == proc.pid)
    print("grandchild == par:", grandchild_pid == parent_pid)

    subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"], capture_output=True)
    try:
        proc.wait(timeout=10)
        print("parent wait      : reaped")
    except subprocess.TimeoutExpired:
        print("parent wait      : TIMED OUT (this is the hang)")
    # Clean up whatever is left.
    for pid in (parent_pid, grandchild_pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
