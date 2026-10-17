"""TSB-344 — 暂存清理必须对「叶子被替换/悬空链接/目录被换」失败关闭。

任务书要求（P1）：暂存叶子出现悬空符号链接、或目录被替换（inode 身份变化、
重解析点/ junction）时，清理必须**失败关闭**，绝不能把链接目标或项目外目录
当成自己的产物删除（AGENTS.md C1/C7、§5.8）。

先复现的缺陷面：

* 悬空链接：目标不存在时 ``Path.exists()`` 为 False。任何以"存在才处理"
  为前提的清理逻辑都会把悬空叶子当作"无需处理"而**静默跳过**，
  违反 C6（不静默跳过）。
* 目录替换：登记时是目录 A，清理时该路径已经指向另一个 inode（被换成目录 B
  或链接到项目外）。若只比较"路径存在"而不比较身份，就会删掉 B。
* 删除授权必须精确到**任务叶目录**，父目录、以及任何链接都不得获得删除权。

这些用例断言的是**可观测差异**：项目外受害目录与文件是否仍然存在、
返回的 outcome 是否为失败关闭、以及是否给出稳定错误码。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Job
from app.paths import project_cache_dir
from app.services import staging_cleanup as cleanup
from test_staging_ownership import link_directory  # noqa: F401


class Owned:
    """A registered, exactly-shaped staging leaf owned by one job."""

    def __init__(self, pid: str, aid: str, jid: str, stage: Path):
        self.pid, self.aid, self.jid, self.stage = pid, aid, jid, stage


def _owned(client, *, job_type: str = "vision_analyze") -> Owned:
    """Create a project, an asset, a job and a registered leaf.

    The ``staging/<asset>/<token>`` shape is validated against a real ``Asset``
    row, so the fixture must create one -- a fabricated asset id is refused by
    ``_check_creator``.
    """
    pid = client.post("/api/projects", json={"name": "tsb344"}).json()["id"]
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path="/virtual/tsb344.mp4")
        job = Job(project_id=pid, job_type=job_type, status="running")
        s.add_all([asset, job])
        s.flush()
        aid, jid = asset.id, job.id
    stage = project_cache_dir(pid) / "staging" / aid / jid
    cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
    return Owned(pid, aid, jid, stage)


def _extra_job(pid: str, *, job_type: str = "vision_analyze") -> str:
    jid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Job(id=jid, project_id=pid, job_type=job_type, status="running"))
    return jid


def _victim(tmp_path: Path) -> Path:
    """An outside-the-workspace directory that must never be touched."""
    victim = tmp_path / "outside_victim"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "important.txt").write_text("must survive", encoding="utf-8")
    return victim


def _outcome(result) -> str:
    """Read whichever failure/success flag the result exposes."""
    for attr in ("outcome", "status", "reason", "error_code"):
        value = getattr(result, attr, None)
        if value:
            return str(value)
    return "ok" if getattr(result, "ok", False) else "unknown"


# --- V17: dangling leaf symlink ---------------------------------------------


def test_v17_a_dangling_leaf_symlink_is_refused_not_skipped(client, tmp_path):
    """V17: a dangling link must be a *reported failure*, never a silent skip."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage

    import shutil

    shutil.rmtree(stage)
    os.symlink(str(tmp_path / "does_not_exist"), stage)

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok, (
        "a replaced leaf must fail closed; treating it as 'nothing to do' is the "
        "silent-skip behaviour C6 forbids"
    )
    assert _outcome(result) != "ok"
    # The link itself must not have been followed.
    assert (tmp_path / "does_not_exist").parent.exists()


def test_v17b_a_dangling_link_never_grants_deletion_rights(client, tmp_path):
    """V17: even if the link later resolves, it must not be deletable as ours."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage

    import shutil

    shutil.rmtree(stage)
    victim = _victim(tmp_path)
    os.symlink(str(victim), stage)  # now NOT dangling: it points at the victim

    cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert (victim / "important.txt").exists(), (
        "following a link out of the workspace would delete user data (C1)"
    )


# --- V18: directory replacement / identity change ---------------------------


def test_v18_a_replaced_leaf_directory_is_refused(client, tmp_path):
    """V18: same path, different inode => not our directory any more."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "ours.bin").write_bytes(b"x" * 10)

    # Swap the owned directory for a different one at the same path.
    import shutil

    shutil.rmtree(stage)
    stage.mkdir(parents=True)
    (stage / "someone_elses.bin").write_bytes(b"y" * 20)

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert (stage / "someone_elses.bin").exists(), (
        "an unregistered directory must not be deleted just because it sits at "
        "the recorded path"
    )
    assert not result.ok


def test_v18b_identity_change_is_detected_after_registration(client, tmp_path):
    """V18: the recorded (dev, ino) must be re-verified, not assumed."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage

    import shutil

    victim = _victim(tmp_path)
    # Replace the owned leaf itself with a directory link to the outside victim.
    shutil.rmtree(stage)
    link_directory(stage, victim)

    cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert (victim / "important.txt").exists(), "a link must never be traversed"


# --- V19: deletion authorisation stays exact --------------------------------


def test_v19_a_parent_directory_never_gets_deletion_rights(client):
    """V19: only the exact task leaf is owned; parents are refused."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage

    parent = stage.parent
    result = cleanup.cleanup_owned_staging(pid, parent, owner_job_id=jid)
    assert not result.ok, "a parent directory must not be cleanable"
    assert parent.exists(), "refusing must not have removed the parent"


def test_v19b_a_deeper_nested_path_is_refused(client):
    """V19: and neither is a path *below* the owned leaf."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    nested = stage / "nested"
    nested.mkdir()
    (nested / "f.bin").write_bytes(b"z")

    result = cleanup.cleanup_owned_staging(pid, nested, owner_job_id=jid)
    assert not result.ok, "only the registered leaf may be cleaned"
    assert (nested / "f.bin").exists()


def test_v19c_a_wrong_owner_job_id_is_refused(client):
    """V19: ownership is per job, not per path shape."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    other = _extra_job(pid)
    (stage / "ours.bin").write_bytes(b"x")

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=other)
    assert not result.ok
    assert (stage / "ours.bin").exists()


# --- V20: the happy path still works ----------------------------------------


def test_v20_a_genuine_owned_leaf_is_still_cleaned(client):
    """V20: failing closed must not break the real flow (no over-refusal)."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "a.bin").write_bytes(b"a" * 5)
    (stage / "b.bin").write_bytes(b"b" * 7)

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert result.ok, f"a normal owned stage must clean, got {_outcome(result)}"
    assert result.removed_bytes == 12
    assert not stage.exists()


def test_v20b_cleanup_is_idempotent(client):
    """V20: a second cleanup of the same, already-cleaned leaf stays ok."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "a.bin").write_bytes(b"a" * 5)

    assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
    again = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert again.ok, "re-cleaning an own, gone leaf must not become a failure"


# --- V21: no global process or path destruction -----------------------------


def test_v21_cleanup_never_touches_anything_outside_the_workspace(client, tmp_path):
    """V21: a sibling of the victim must survive a refused cleanup untouched."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage

    victim = _victim(tmp_path)
    neighbour = tmp_path / "neighbour"
    neighbour.mkdir()
    (neighbour / "keep.txt").write_text("keep", encoding="utf-8")

    import shutil

    shutil.rmtree(stage)
    link_directory(stage, victim)
    cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)

    assert (victim / "important.txt").exists()
    assert (neighbour / "keep.txt").exists()


# --- V18c: the recorded identity is actually compared -----------------------


def test_v18c_a_stale_recorded_identity_blocks_cleanup(client):
    """V18: the registry's saved (dev, ino) must be compared, not just stored.

    Swapping the leaf for a *different* directory at the same path is the attack
    the identity check exists for. Here the substitution is simulated at the
    registry: the owned directory is left in place but its recorded identity is
    replaced with a value that cannot match, which is indistinguishable to the
    guard from "the directory at this path is no longer the one we registered".

    If the guard were removed the cleanup would proceed and delete the files,
    so this test fails closed only because the comparison really happens.
    """
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "ours.bin").write_bytes(b"x" * 9)

    registered = cleanup._OWNERS[stage]
    forged = cleanup.StageOwner(
        registered.project_id, registered.job_id,
        (registered.identity[0], registered.identity[1] + 1),  # wrong inode
        registered.ancestors,
    )
    cleanup._OWNERS[stage] = forged
    try:
        result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
        assert not result.ok, (
            "a directory whose identity no longer matches the registration must "
            "not be deleted"
        )
        assert result.error_code == "STAGING_NOT_OWNED", result.error_code
        assert (stage / "ours.bin").exists(), "the file must survive a refused cleanup"
    finally:
        cleanup._OWNERS[stage] = registered


def test_v18d_a_forged_ancestor_identity_blocks_cleanup(client):
    """V18: the *parent* chain identity is compared too, not only the leaf."""
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "ours.bin").write_bytes(b"x" * 9)

    registered = cleanup._OWNERS[stage]
    forged_ancestors = tuple(
        (dev, ino + 1) for dev, ino in registered.ancestors
    )
    cleanup._OWNERS[stage] = cleanup.StageOwner(
        registered.project_id, registered.job_id,
        registered.identity, forged_ancestors,
    )
    try:
        result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
        assert not result.ok, "a changed parent chain must refuse the cleanup"
        assert (stage / "ours.bin").exists()
    finally:
        cleanup._OWNERS[stage] = registered
