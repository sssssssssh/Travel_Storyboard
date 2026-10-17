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

import json
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


# ---------------------------------------------------------------------------
# TSB-345 — 代际身份（generation identity）
#
# Ubuntu V18a 的真实失败：`rmtree` + `mkdir` 之后 ext4/overlayfs **立即复用**
# 刚释放的 inode，于是 `_identity()` 返回与登记时完全相同的 (st_dev, st_ino)。
# 只比较 dev/ino 的守卫因此认为"还是我们那个目录"，把替换目录里的
# `someone_elses.bin` 删掉了。macOS 的 APFS 不复用 inode，所以这条缺陷在开发机
# 上**永远测不出来** —— 这正是"仅本机通过"不能作为验收的原因（C8）。
#
# 下面的用例不依赖 inode 复用是否恰好发生，而是**直接把登记身份改写成与替换
# 目录一致**，从而在任意平台上确定性地复现同一条件。断言的是可观测结果：
# 替换目录里的文件是否存活、是否返回稳定错误码。
# ---------------------------------------------------------------------------


def test_v21_reused_inode_identity_cannot_authorise_deletion(client):
    """V21 (TSB-345): 身份相等但代际不同 => 必须拒绝，且不得删除任何文件。

    Ubuntu V18a 的确切条件：路径、dev、ino 全部相同，但目录是**另一个创建事件**
    的产物。只靠 (dev, ino) 无法区分这两者，必须依赖创建时写入的代际标记。
    """
    o = _owned(client)
    pid, jid, stage = o.pid, o.jid, o.stage
    (stage / "ours.bin").write_bytes(b"x" * 10)

    import shutil

    shutil.rmtree(stage)
    stage.mkdir(parents=True)
    (stage / "someone_elses.bin").write_bytes(b"y" * 20)

    # Force the Linux inode-reuse condition on any platform.
    owner = cleanup._OWNERS[stage]
    live = cleanup._identity(cleanup._lstat(stage))
    cleanup._OWNERS[stage] = cleanup.StageOwner(
        owner.project_id, owner.job_id, live, owner.ancestors,
        owner.nonce, owner.marker,
    )

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert (stage / "someone_elses.bin").exists(), (
        "相同 (dev, ino) 但不同创建事件的目录，绝不能因此获得删除权 "
        "（Ubuntu CI 上真实删除了这个文件）"
    )
    assert not result.ok
    assert result.error_code == cleanup.ERR_NOT_OWNED, (
        f"必须给出稳定错误码，实际 {result.error_code!r}"
    )


def test_v21b_the_generation_marker_is_unforgeable_by_recreation(client):
    """V21b: 重新创建目录无法复现代际标记（高熵 nonce）。"""
    o = _owned(client)
    stage, jid, pid = o.stage, o.jid, o.pid
    marker = cleanup._OWNERS[stage].marker
    nonce = cleanup._OWNERS[stage].nonce
    assert marker and len(nonce) >= 32, "代际标记必须存在且高熵"

    import shutil

    shutil.rmtree(stage)
    stage.mkdir(parents=True)
    # Even an attempt to plant a marker file cannot guess the nonce.
    (stage / marker).write_text("not-the-real-nonce")

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok, "伪造的代际标记不得被接受"
    assert result.error_code == cleanup.ERR_NOT_OWNED
    assert (stage / marker).exists(), "拒绝时不得删除任何内容"


def test_v21c_a_missing_marker_fails_closed(client):
    """V21c: 标记丢失/不可读 => 失败关闭，不退回路径或 inode 匹配。"""
    o = _owned(client)
    stage, jid, pid = o.stage, o.jid, o.pid
    (stage / "ours.bin").write_bytes(b"x" * 10)
    marker = cleanup._OWNERS[stage].marker
    os.unlink(stage / marker)

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok, (
        "代际标记缺失时必须失败关闭；退回 (dev, ino) 匹配会重新引入 V18a"
    )
    assert result.error_code == cleanup.ERR_NOT_OWNED
    assert (stage / "ours.bin").exists(), "拒绝时不得删除内容"


def test_v21d_a_linked_marker_is_not_proof(client):
    """V21d: 标记文件是链接 => 不作为所有权证据（不跟随链接）。"""
    o = _owned(client)
    stage, jid, pid = o.stage, o.jid, o.pid
    (stage / "ours.bin").write_bytes(b"x" * 10)
    marker = cleanup._OWNERS[stage].marker
    outside = stage.parent / "outside-marker"
    outside.write_text("y" * 32)
    os.unlink(stage / marker)
    try:
        os.symlink(outside, stage / marker)
    except (OSError, NotImplementedError):  # pragma: no cover - platform fact
        pytest.skip("link creation is a platform fact")

    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok, "指向外部文件的链接标记不得作为所有权证据"
    assert result.error_code == cleanup.ERR_NOT_OWNED
    assert outside.exists(), "项目外目标不得被删除"


def test_v21e_the_generation_token_never_reaches_public_output(client):
    """V21e: 代际 nonce 不得泄露到时刻/结果等可公开输出。"""
    o = _owned(client)
    stage, jid, pid = o.stage, o.jid, o.pid
    nonce = cleanup._OWNERS[stage].nonce
    (stage / "ours.bin").write_bytes(b"x" * 10)
    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert result.ok, "正常清理必须成功（不因新增代际校验而退化）"

    blob = repr(result) + str(result.message) + str(result.path)
    assert nonce not in blob, "代际 nonce 泄露到了可公开的清理结果中"
    # The absence receipt kept for idempotence must not carry the token either.
    # (``_OWNERS`` is the private registry and legitimately holds it; what must
    # not leak is any *public* surface: results, messages, stats or reports.)
    assert nonce not in repr(cleanup._CLEANED.get(stage, ""))
    public = json.dumps(
        {
            "result": str(result),
            "message": result.message,
            "path": result.path,
            "error_code": result.error_code,
        },
        ensure_ascii=False,
    )
    assert nonce not in public


def test_v21f_normal_cleanup_still_works_and_is_idempotent(client):
    """V21f (D): 合法创建/清理必须继续通过，且保持幂等（不过度拒绝）。"""
    o = _owned(client)
    stage, jid, pid = o.stage, o.jid, o.pid
    (stage / "ours.bin").write_bytes(b"x" * 10)

    first = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert first.ok, f"正常清理不得退化：{first.error_code} {first.message}"
    assert first.removed_files == 1
    assert first.removed_bytes == 10
    assert not stage.exists(), "目录必须真的被删除"

    second = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert second.ok, "重复清理必须幂等"
    assert second.removed_files == 0


# ---------------------------------------------------------------------------
# TSB-345 C — `FileExistsError` 不得裸 pass（不得接管他人目录）
# ---------------------------------------------------------------------------


def test_v22_an_unregistered_directory_at_the_leaf_path_is_never_adopted(client):
    """V22: 路径上已有的、非本任务登记的目录 => 不得接管，更不得获得删除权。"""
    o = _owned(client)
    pid, aid, jid = o.pid, o.aid, o.jid
    # A *different* segment token under the same asset: never registered here.
    # This is the case the old bare ``pass`` would have adopted, because the
    # existence of a directory at the deterministic path was taken as proof.
    stage = cleanup.project_cache_dir(pid) / "staging" / aid / str(uuid.uuid4())
    stage.mkdir(parents=True)
    (stage / "not_ours.bin").write_bytes(b"z" * 8)

    assert not cleanup.is_registered_leaf(pid, stage, owner_job_id=jid), (
        "未登记的目录不得被报告为本任务拥有的叶子"
    )
    result = cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok, "未登记的现有目录不得获得删除权"
    assert result.error_code == cleanup.ERR_NOT_OWNED
    assert (stage / "not_ours.bin").exists(), "他人目录的内容不得被删除"


def test_v22b_a_registered_leaf_is_reported_as_owned(client):
    """V22b (D): 真正由本任务登记的叶子仍被正确认可（不过度拒绝）。"""
    o = _owned(client)
    assert cleanup.is_registered_leaf(o.pid, o.stage, owner_job_id=o.jid), (
        "本任务创建的叶子必须被认可，否则正常重用路径会退化为报错"
    )


def test_v22c_another_jobs_leaf_is_not_reusable(client):
    """V22c: 同一路径若登记在别的 job 名下，本 job 不得复用。"""
    o = _owned(client)
    assert not cleanup.is_registered_leaf(o.pid, o.stage, owner_job_id="some-other-job"), (
        "其他 job 的叶子不得被当前 job 复用或取得删除权"
    )


def test_v22d_a_replaced_leaf_is_not_reported_as_owned(client):
    """V22d: 登记后代际身份被破坏 => 不再报告为拥有（与 V21 同一防线）。"""
    o = _owned(client)
    stage, pid, jid = o.stage, o.pid, o.jid
    owner = cleanup._OWNERS[stage]
    cleanup._OWNERS[stage] = cleanup.StageOwner(
        owner.project_id, owner.job_id, owner.identity, owner.ancestors,
        "deadbeef" * 8, owner.marker,
    )
    assert not cleanup.is_registered_leaf(pid, stage, owner_job_id=jid), (
        "代际身份不符时不得报告为拥有，否则 FileExistsError 分支会接管被替换的目录"
    )


def test_v22e_a_retired_leaf_cannot_regain_deletion_rights(client):
    """V22e: 已清理的叶子不得再次被报告为拥有（防止二次删除授权）。"""
    o = _owned(client)
    stage, pid, jid = o.stage, o.pid, o.jid
    (stage / "ours.bin").write_bytes(b"x" * 4)
    assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
    assert not cleanup.is_registered_leaf(pid, stage, owner_job_id=jid), (
        "清理后出现的新目录不能继承删除权"
    )
