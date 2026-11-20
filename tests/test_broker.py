"""Broker behaviour: allow, block, timeout, cancel, concurrency."""

import asyncio
import queue

from mcp_guard.core.models import ApprovalRequest
from mcp_guard.daemon.broker import Broker


def _req(tool: str = "t") -> ApprovalRequest:
    return ApprovalRequest(server_name="s", method="tools/call", tool_name=tool)


async def test_allow_and_block() -> None:
    broker = Broker(queue.Queue(), timeout_s=5)
    a, b = _req("a"), _req("b")
    ta = asyncio.create_task(broker.submit(a))
    tb = asyncio.create_task(broker.submit(b))
    await asyncio.sleep(0)
    broker.resolve(b.request_id, "block", "user")
    broker.resolve(a.request_id, "allow", "user")
    assert (await ta).decision == "allow"
    assert (await tb).decision == "block"
    assert broker.pending_ids == []


async def test_timeout_blocks() -> None:
    broker = Broker(queue.Queue(), timeout_s=0.05)
    decision = await broker.submit(_req())
    assert (decision.decision, decision.reason) == ("block", "timeout")


async def test_cancel_and_shutdown() -> None:
    broker = Broker(queue.Queue(), timeout_s=5)
    r1, r2 = _req(), _req()
    t1 = asyncio.create_task(broker.submit(r1))
    t2 = asyncio.create_task(broker.submit(r2))
    await asyncio.sleep(0)
    broker.cancel(r1.request_id)
    broker.shutdown()
    assert (await t1).reason == "cancelled"
    assert (await t2).reason == "daemon_shutdown"


async def test_cap_blocks_extra_requests_without_popup() -> None:
    ui: queue.Queue = queue.Queue()
    broker = Broker(ui, timeout_s=5, max_pending=2)
    held = [asyncio.create_task(broker.submit(_req())) for _ in range(2)]
    await asyncio.sleep(0)
    extra = await broker.submit(_req())
    assert (extra.decision, extra.reason) == ("block", "overloaded")
    assert ui.qsize() == 2  # only the two held requests opened popups
    broker.shutdown()
    await asyncio.gather(*held)


async def test_duplicate_id_is_blocked_and_original_kept() -> None:
    broker = Broker(queue.Queue(), timeout_s=5)
    r = _req()
    original = asyncio.create_task(broker.submit(r))
    await asyncio.sleep(0)
    dup = await broker.submit(r)
    assert (dup.decision, dup.reason) == ("block", "error")
    broker.resolve(r.request_id, "allow", "user")
    assert (await original).decision == "allow"


async def test_many_concurrent_are_independent() -> None:
    broker = Broker(queue.Queue(), timeout_s=5, max_pending=20)
    reqs = [_req(str(i)) for i in range(20)]
    tasks = [asyncio.create_task(broker.submit(r)) for r in reqs]
    await asyncio.sleep(0)
    for i, r in enumerate(reqs):
        broker.resolve(r.request_id, "allow" if i % 2 == 0 else "block", "user")
    results = await asyncio.gather(*tasks)
    assert [d.decision for d in results] == ["allow" if i % 2 == 0 else "block" for i in range(20)]
