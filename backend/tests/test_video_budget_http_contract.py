"""M5.3-R3.1 / TSB-351、TSB-352 —— 预算必须与**真实 HTTP 尝试**一一对应。

任务书要求（P0/P1）：B01、B02、B03、B07、B08、B11 必须通过**真实 Mock HTTP
服务实际收到的请求次数与 `content`** 来断言，不能只调用
`VideoBudget.note_attempt()` 自证。本文件即为此而设：每一例都起一个真实的
回环 HTTP 服务，记录它**确实收到**的 POST 数量与 body 字节数，再与预算台账比对。

R3 引入的缺陷（本轮 RED）：

* ``_analyze_one`` 先调用 ``charge_attempt(0)``（有副作用地预扣一次），
  随后 Provider 在每次 ``httpx.post`` 前又调用 ``attempt_guard`` →
  **一次真实 HTTP 尝试被计费两次**。``max_calls=1`` 时第一条请求在真正发出前
  就被自己的预扣用光额度，于是 **POST=0** 而账本 calls=1。
* ``_request_body_bytes()`` 是**估算**（Base64 长度 + prompt 长度 + 固定 512），
  而 Provider 实际发送 ``json.dumps(payload).encode("utf-8")``；
  两者不保证相等，预算台账与真实 Body 长度脱节。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.vision.providers.native_video import NativeVideoVisionProvider
from app.services.vision.video_job import VideoBudget
from test_native_video_provider import _init_provider, _request


class _CountingModel:
    """A real loopback model server that records every POST it receives."""

    def __init__(self, *, responses=None):
        #: list of (status, payload) consumed in order; the last one repeats.
        self.responses = list(responses or [(200, self._ok_body())])
        self.seen: list[dict] = []
        self._outer = self

    @staticmethod
    def _ok_body() -> dict:
        content = json.dumps(
            {
                "clip_id": "clip-1",
                "segment_id": "seg-1",
                "summary": "ok",
                "observed_temporal_quality": "clear",
                "audio_analyzed": False,
                "limitations": [],
                "events": [],
            }
        )
        return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 7}}

    def handle(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                outer.seen.append(
                    {"path": self.path, "content": raw, "status": None}
                )
                idx = min(len(outer.seen) - 1, len(outer.responses) - 1)
                status, payload = outer.responses[idx]
                outer.seen[-1]["status"] = status
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output clean
                pass

        return Handler

    def start(self):
        outer = self
        self.server = HTTPServer(("127.0.0.1", 0), self.handle())
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def posts(self) -> int:
        return len(self.seen)

    @property
    def body_sizes(self) -> list[int]:
        return [len(s["content"]) for s in self.seen]


@pytest.fixture(autouse=True)
def _mock_model_contract(monkeypatch):
    """Enable the upload switch **for this module only**, against loopback.

    TSB-343 discipline: without this the authorization gate refuses before the
    branch under test, and CI (no ``.env``) would fail differently from a
    developer machine. No production default is changed.
    """
    from conftest import apply_mock_model_contract

    return apply_mock_model_contract(monkeypatch)


@pytest.fixture()
def model_server():
    server = _CountingModel().start()
    try:
        yield server
    finally:
        server.stop()


def _provider_for(server: _CountingModel, *, max_retries: int, timeout: float = 5.0):
    provider = NativeVideoVisionProvider()
    provider.base_url = f"http://127.0.0.1:{server.port}"
    provider.api_key = "test-key"
    provider.allow_insecure = True
    provider.max_retries = max_retries
    provider.timeout_sec = timeout
    return provider


def _budget(**kw) -> VideoBudget:
    base = dict(
        max_segments=10, max_total_sec=300.0,
        max_total_bytes=50_000_000, max_calls=10,
    )
    base.update(kw)
    return VideoBudget(**base)


class _Guard:
    """The caller-side charge point, mirroring ``_analyze_one``'s contract."""

    def __init__(self, budget: VideoBudget):
        self.budget = budget
        self.sizes: list[int] = []

    def __call__(self, attempt: int, body_bytes: int) -> None:
        self.sizes.append(body_bytes)
        self.budget.note_attempt(calls=1, bytes_sent=body_bytes)


# ---------------------------------------------------------------------------
# B01 — max_calls=1, model answers 200 immediately
# ---------------------------------------------------------------------------


def test_b01_one_call_budget_sends_exactly_one_post(model_server, monkeypatch):
    """B01: 配额 1 次、模型立即 200 ⇒ **1 次真实 POST**，账本 calls=1。

    修复前：Job 层的 ``charge_attempt(0)`` 已把唯一的额度用掉，
    Provider 的第一次 ``attempt_guard`` 抛预算错误 ⇒ **POST=0**、calls=1。
    """
    _init_provider(monkeypatch)
    budget = _budget(max_calls=1)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    response = provider.complete_video(
        _request(), attempt_guard=lambda a, n: guard(a, n)
    )

    assert model_server.posts == 1, (
        f"配额 1 次必须恰好发出 1 个 POST，实际 {model_server.posts} "
        f"（修复前为 0：预扣把额度用光，请求根本没出去）"
    )
    assert budget.ledger()["calls"] == 1, (
        f"一次真实尝试必须只计 1 次，实际 {budget.ledger()['calls']}（修复前为 2）"
    )
    assert response is not None


def test_b01b_the_single_attempt_is_charged_once_not_twice(model_server, monkeypatch):
    """B01 补充：一次 HTTP 尝试的计费必须是 1，而不是"预扣 + 真实扣"=2。"""
    _init_provider(monkeypatch)
    budget = _budget(max_calls=10)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    provider.complete_video(_request(), attempt_guard=lambda a, n: guard(a, n))

    assert model_server.posts == 1
    assert len(guard.sizes) == 1, (
        f"回调必须被调用且仅被调用 1 次，实际 {len(guard.sizes)} 次"
    )
    assert budget.ledger()["calls"] == 1, "一次 POST 只能扣一次额度"


# ---------------------------------------------------------------------------
# B02 / B03 — 503 then retry
# ---------------------------------------------------------------------------


def test_b02_a_503_cannot_retry_past_a_one_call_budget(model_server, monkeypatch):
    """B02: 配额 1 次、首个 POST 返回 503 ⇒ 只有 1 次 POST，重试被预算拒绝。"""
    _init_provider(monkeypatch)
    server = _CountingModel(responses=[(503, {"error": "no healthy upstream"})]).start()
    try:
        budget = _budget(max_calls=1)
        guard = _Guard(budget)
        provider = _provider_for(server, max_retries=3)

        from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError

        with pytest.raises(ModelError) as exc:
            provider.complete_video(_request(), attempt_guard=lambda a, n: guard(a, n))
        assert exc.value.code == MODEL_BUDGET_EXCEEDED, (
            f"重试必须被预算拒绝，实际错误码 {exc.value.code}"
        )
        assert server.posts == 1, f"只能发出 1 个 POST，实际 {server.posts}"
        assert budget.ledger()["calls"] == 1
    finally:
        server.stop()


def test_b03_two_call_budget_allows_exactly_one_retry(model_server, monkeypatch):
    """B03: 配额 2 次、503 → 200 ⇒ **2 次真实 POST**，账本 calls=2。"""
    _init_provider(monkeypatch)
    server = _CountingModel(
        responses=[(503, {"error": "no healthy upstream"}), (200, _CountingModel._ok_body())]
    ).start()
    try:
        budget = _budget(max_calls=2)
        guard = _Guard(budget)
        provider = _provider_for(server, max_retries=3)

        provider.complete_video(_request(), attempt_guard=lambda a, n: guard(a, n))

        assert server.posts == 2, (
            f"503→200 必须恰好发出 2 个 POST，实际 {server.posts}"
        )
        assert budget.ledger()["calls"] == 2, (
            f"两次真实尝试必须计 2 次，实际 {budget.ledger()['calls']}"
        )
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# B04 / B05 — 拒绝路径必须 0 POST、0 计费
# ---------------------------------------------------------------------------


def test_b04_a_denied_authorization_sends_nothing_and_charges_nothing(
    model_server, monkeypatch
):
    """B04: 授权拒绝 ⇒ 0 POST、账本 calls=0。"""
    _init_provider(monkeypatch)
    budget = _budget(max_calls=1)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    from app.services.vision.errors import ModelError

    with pytest.raises(ModelError):
        provider.complete_video(
            _request(authorization=None), attempt_guard=lambda a, n: guard(a, n)
        )
    assert model_server.posts == 0, "授权被拒不得发出任何请求"
    assert budget.ledger()["calls"] == 0, "未发出的尝试不得计费"


def test_b05_a_non_video_payload_sends_nothing_and_charges_nothing(
    model_server, monkeypatch
):
    """B05: 非法视频部件 ⇒ 0 POST、账本 calls=0。"""
    _init_provider(monkeypatch)
    budget = _budget(max_calls=1)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    from app.services.vision.errors import ModelError
    from test_native_video_provider import mp4_bytes
    from app.services.vision.providers.base import VideoPart

    bad = _request(
        video=VideoPart(
            clip_id="c", data=b"<html>not a video</html>", mime_type="video/mp4",
            duration_sec=1.0,
        )
    )
    with pytest.raises(ModelError):
        provider.complete_video(bad, attempt_guard=lambda a, n: guard(a, n))
    assert model_server.posts == 0
    assert budget.ledger()["calls"] == 0


# ---------------------------------------------------------------------------
# B07 / B08 / B09 / B11 — 精确 Body 字节数
# ---------------------------------------------------------------------------


def test_b07_the_reserved_size_equals_the_real_post_body(model_server, monkeypatch):
    """B07/B11: 预留的字节数必须**等于 Mock 实际收到的 body 长度**。"""
    _init_provider(monkeypatch)
    budget = _budget(max_calls=5)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    provider.complete_video(_request(), attempt_guard=lambda a, n: guard(a, n))

    assert model_server.posts == 1
    actual = model_server.body_sizes[0]
    assert guard.sizes == [actual], (
        f"预留字节必须等于真实 Body 长度：预留 {guard.sizes}，实际 {actual}"
    )
    assert budget.ledger()["bytes"] == actual, (
        f"账本 bytes 必须等于真实 Body 长度："
        f"账本 {budget.ledger()['bytes']}，实际 {actual}"
    )


def test_b08_a_budget_one_byte_short_refuses_before_posting(model_server, monkeypatch):
    """B08: 精确 Body 为 B，配额 B-1 ⇒ 0 POST 且报稳定预算错误。"""
    _init_provider(monkeypatch)
    provider = _provider_for(model_server, max_retries=0)

    # Measure the real body length first with an ample budget.
    measure = _budget(max_calls=5)
    provider.complete_video(
        _request(), attempt_guard=_Guard(measure)
    )
    real = model_server.body_sizes[0]
    assert measure.ledger()["bytes"] == real

    server2 = _CountingModel().start()
    try:
        provider2 = _provider_for(server2, max_retries=0)
        tight = _budget(max_calls=5, max_total_bytes=real - 1)
        from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError

        with pytest.raises(ModelError) as exc:
            provider2.complete_video(_request(), attempt_guard=_Guard(tight))
        assert exc.value.code == MODEL_BUDGET_EXCEEDED
        assert server2.posts == 0, (
            f"配额差 1 字节必须在 POST 之前拒绝，实际发出 {server2.posts} 个请求"
        )
        assert tight.ledger()["bytes"] == 0, "被拒绝的尝试不得计入字节"
    finally:
        server2.stop()


def test_b08b_a_budget_exactly_equal_to_the_body_is_allowed(model_server, monkeypatch):
    """B08 边界：配额恰好等于 Body B ⇒ 允许 1 次 POST（不得过度拒绝）。"""
    _init_provider(monkeypatch)

    measure_server = _CountingModel().start()
    try:
        provider = _provider_for(measure_server, max_retries=0)
        provider.complete_video(_request(), attempt_guard=_Guard(_budget(max_calls=5)))
        real = measure_server.body_sizes[0]
    finally:
        measure_server.stop()

    exact = _budget(max_calls=5, max_total_bytes=real)
    guard = _Guard(exact)
    provider2 = _provider_for(model_server, max_retries=0)
    provider2.complete_video(_request(), attempt_guard=lambda a, n: guard(a, n))

    assert model_server.posts == 1, "恰好等于配额的请求必须被允许"
    assert exact.ledger()["bytes"] == real


def test_b09_a_chinese_prompt_length_is_measured_exactly(model_server, monkeypatch):
    """B09: 含中文 system prompt/label ⇒ 精确长度一致，而非固定 +512 估算。"""
    _init_provider(monkeypatch)
    budget = _budget(max_calls=5)
    guard = _Guard(budget)
    provider = _provider_for(model_server, max_retries=0)

    provider.complete_video(
        _request(prompt="请描述这段旅行视频中的画面变化与情绪。"),
        attempt_guard=lambda a, n: guard(a, n),
    )

    actual = model_server.body_sizes[0]
    assert guard.sizes == [actual], (
        f"多字节内容必须按 UTF-8 精确计量：预留 {guard.sizes}，实际 {actual}"
    )
    # Sanity: the CJK prompt makes the body materially larger than ASCII.
    assert actual > 0


def test_b09b_the_old_estimate_was_not_authoritative(monkeypatch):
    """B09 补充：不得存在两套独立算法 —— 估算函数不应再被计费路径使用。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj._analyze_one)
    assert "_request_body_bytes(" not in source, (
        "计费必须使用 Provider 真实序列化后的 Body 长度；"
        "保留独立估算函数会产生两套算法，二者不保证相等"
    )


# ---------------------------------------------------------------------------
# B13 / B14 / B15 — 任务级重试与超时必须真正生效，且任务之间不串扰
#
# 修复前：`NativeVideoVisionProvider.__init__` 从 `Settings` 读
# `model_max_retries` / `model_timeout_sec`，而 Job 快照里的值只进了
# `VideoBudget`，从未传给 Provider。于是快照记录 0 次重试、实际仍按 settings
# 重试；快照记录 7.5 秒超时、实际仍用 settings 的 120 秒。
# ---------------------------------------------------------------------------


def _options(max_retries, timeout_sec):
    from app.services.vision.providers.native_video import CallOptions

    return CallOptions.resolve(max_retries=max_retries, timeout_sec=timeout_sec)


def test_b13_zero_retries_sends_exactly_one_post(model_server, monkeypatch):
    """B13: 快照 max_retries=0 ⇒ 最多 1 次 POST，即使模型持续 503。"""
    _init_provider(monkeypatch)
    server = _CountingModel(responses=[(503, {"error": "unavailable"})]).start()
    try:
        budget = _budget(max_calls=50)
        guard = _Guard(budget)
        provider = _provider_for(server, max_retries=99)  # provider's own value
        from app.services.vision.errors import ModelError

        with pytest.raises(ModelError):
            provider.complete_video(
                _request(), attempt_guard=lambda a, n: guard(a, n),
                options=_options(0, 5.0),  # the JOB's value must win
            )
        assert server.posts == 1, (
            f"max_retries=0 必须只发 1 个 POST，实际 {server.posts}"
            f"（修复前会按 settings 的 2 次重试共发 3 个）"
        )
        assert budget.ledger()["calls"] == 1
    finally:
        server.stop()


def test_b14_two_retries_send_three_posts(model_server, monkeypatch):
    """B14: 快照 max_retries=2 ⇒ 最多 3 次 POST（配额足够时）。"""
    _init_provider(monkeypatch)
    server = _CountingModel(responses=[(503, {"error": "unavailable"})]).start()
    try:
        budget = _budget(max_calls=50)
        guard = _Guard(budget)
        provider = _provider_for(server, max_retries=0)  # provider's own value
        from app.services.vision.errors import ModelError

        with pytest.raises(ModelError):
            provider.complete_video(
                _request(), attempt_guard=lambda a, n: guard(a, n),
                options=_options(2, 5.0),  # the JOB's value must win
            )
        assert server.posts == 3, (
            f"max_retries=2 必须发 3 个 POST，实际 {server.posts}"
            f"（修复前会按 settings 的 0 次重试只发 1 个）"
        )
        assert budget.ledger()["calls"] == 3
    finally:
        server.stop()


def test_b15_two_jobs_with_different_timeouts_do_not_interfere(
    model_server, monkeypatch
):
    """B15: A/B 两个任务超时值不同 ⇒ 每次 httpx.post 的 timeout 确实不同。"""
    _init_provider(monkeypatch)
    seen_timeouts: list[float] = []

    import httpx

    real_post = httpx.post

    def spy(url, **kw):
        seen_timeouts.append(kw.get("timeout"))
        return real_post(url, **kw)

    monkeypatch.setattr(httpx, "post", spy)

    server = _CountingModel().start()
    try:
        provider = _provider_for(server, max_retries=0)
        # Job A: 3s
        provider.complete_video(
            _request(), attempt_guard=_Guard(_budget(max_calls=5)),
            options=_options(0, 3.0),
        )
        # Job B: 11s
        provider.complete_video(
            _request(), attempt_guard=_Guard(_budget(max_calls=5)),
            options=_options(0, 11.0),
        )
    finally:
        server.stop()

    assert seen_timeouts == [3.0, 11.0], (
        f"每个任务必须用自己的超时：实际 {seen_timeouts}"
    )


def test_b15b_reversed_order_gives_the_same_result(model_server, monkeypatch):
    """B15: 运行次序互换结果一致 —— 证明没有共享可变状态被污染。"""
    _init_provider(monkeypatch)
    seen_timeouts: list[float] = []

    import httpx

    real_post = httpx.post
    monkeypatch.setattr(
        httpx, "post",
        lambda url, **kw: (seen_timeouts.append(kw.get("timeout")), real_post(url, **kw))[1],
    )

    server = _CountingModel().start()
    try:
        provider = _provider_for(server, max_retries=0)
        provider.complete_video(  # B first this time
            _request(), attempt_guard=_Guard(_budget(max_calls=5)),
            options=_options(0, 11.0),
        )
        provider.complete_video(  # then A
            _request(), attempt_guard=_Guard(_budget(max_calls=5)),
            options=_options(0, 3.0),
        )
    finally:
        server.stop()

    assert seen_timeouts == [11.0, 3.0], (
        f"次序互换后仍应是各自的值：实际 {seen_timeouts}"
    )


def test_b15c_a_job_cannot_raise_the_server_ceiling(monkeypatch):
    """B15c: 任务参数不得越过服务端上限（不能靠请求放大费用）。"""
    from app.config import get_settings
    from app.services.vision.providers.native_video import CallOptions

    settings = get_settings()
    monkeypatch.setattr(settings, "model_max_retries", 1, raising=False)
    monkeypatch.setattr(settings, "model_timeout_sec", 30.0, raising=False)

    got = CallOptions.resolve(max_retries=99, timeout_sec=9999)
    assert got.max_retries == 1, "重试次数必须被服务端上限钳制"
    assert got.timeout_sec == 30.0, "超时必须被服务端上限钳制"


def test_b15d_the_shared_provider_is_never_mutated(model_server, monkeypatch):
    """B15d: 不得把任务参数写进共享 Provider 的实例属性（否则并发互相污染）。"""
    import inspect

    import app.services.vision.providers.native_video as nv

    source = inspect.getsource(nv.NativeVideoVisionProvider.complete_video)
    assert "self.max_retries =" not in source, (
        "不得在调用中改写共享实例的 max_retries"
    )
    assert "self.timeout_sec =" not in source, (
        "不得在调用中改写共享实例的 timeout_sec"
    )


def test_b15e_the_job_call_site_passes_its_snapshot_options():
    """B15e (接线): Job 调用点必须把快照里的重试/超时传给本次调用。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj._analyze_one)
    assert "options=CallOptions.resolve(" in source, (
        "调用点必须传入 CallOptions，否则 Provider 仍按 settings 执行"
    )
    assert "plan.budget.max_retries" in source and "plan.budget.timeout_sec" in source
