"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz; lolka fork

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiortc import MediaStreamTrack

from lolka import _voice_impl
from lolka._voice_impl import Signaling, SourceAudioTrack, VoiceConnection

pytestmark = pytest.mark.asyncio


class FakeServer:
    """In-process stand-in for the voice server signaling WebSocket."""

    def __init__(self):
        self.requests = []
        self.responses = {}
        self.delays = {}
        self.ws = None
        self.connected = asyncio.Event()

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._handler)
        self.runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/"

    async def stop(self):
        await self.runner.cleanup()

    async def _handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        self.connected.set()
        async for msg in ws:
            obj = json.loads(msg.data)
            self.requests.append(obj)
            asyncio.ensure_future(self._answer(ws, obj))
        return ws

    async def _answer(self, ws, obj):
        method = obj["method"]
        await asyncio.sleep(self.delays.get(method, 0))
        if method == "hang":
            return
        data = self.responses.get(method, {})
        if callable(data):
            data = data(obj)
        if not ws.closed:
            await ws.send_json({"id": obj["id"], "response": True, "data": data})

    async def notify(self, method, data):
        await self.ws.send_json({"notification": True, "method": method, "data": data})

    def count(self, method):
        return sum(1 for r in self.requests if r["method"] == method)


class _Track(MediaStreamTrack):
    kind = "audio"

    async def recv(self):
        await asyncio.sleep(3600)


class FakeConsumer:
    def __init__(self, id, producer_id):
        self.id = id
        self.producerId = producer_id
        self.track = _Track()
        self.closed = False

    async def close(self):
        self.closed = True


class FakeTransport:
    id = "recv-transport"

    def __init__(self):
        self.consumers = []

    async def consume(self, id, producerId, kind, rtpParameters):
        consumer = FakeConsumer(id, producerId)
        self.consumers.append(consumer)
        return consumer

    async def close(self):
        pass


@pytest_asyncio.fixture
async def server():
    srv = FakeServer()
    await srv.start()
    srv.responses["consume"] = lambda obj: {
        "id": "c-" + obj["data"]["producerId"],
        "producerId": obj["data"]["producerId"],
        "kind": "audio",
        "rtpParameters": {},
    }
    yield srv
    await srv.stop()


@pytest.fixture(autouse=True)
def _plain_rtp_parameters(monkeypatch):
    monkeypatch.setattr(_voice_impl, "RtpParameters", lambda **kw: kw)


async def make_conn(server, **kwargs):
    events = SimpleNamespace(received=[], closed=[], ended=[])
    conn = VoiceConnection(
        "x",
        "t",
        on_receive_track=lambda track, uid, pid: events.received.append(pid),
        on_closed=events.closed.append,
        on_track_ended=events.ended.append,
        request_timeout=2.0,
        **kwargs,
    )
    conn.signaling = Signaling(
        server.url,
        conn._on_notification,
        on_disconnect=lambda: conn._notify_closed("disconnected"),
        request_timeout=2.0,
    )
    await conn.signaling.connect()
    await server.connected.wait()
    conn.device = SimpleNamespace(
        rtpCapabilities=SimpleNamespace(model_dump=lambda **kw: {})
    )
    conn.recv_transport = FakeTransport()
    conn._started = True
    return conn, events


async def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_notification_handler_may_issue_requests(server):
    # newProducer -> consume -> its response is read by the same connection
    conn, events = await make_conn(server)
    t0 = time.monotonic()
    await server.notify("newProducer", {"producerId": "p1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: events.received == ["p1"])
    assert time.monotonic() - t0 < 1.0
    assert server.count("consume") == 1 and server.count("resumeConsumer") == 1
    await conn.close()


async def test_video_producers_are_not_consumed(server):
    conn, events = await make_conn(server)
    await server.notify("newProducer", {"producerId": "v1", "userId": 7, "kind": "video"})
    await server.notify("newProducer", {"producerId": "a1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: events.received == ["a1"])
    assert server.count("consume") == 1
    await conn.close()


async def test_kicked_notifies_owner_and_keeps_out_track_alive(server):
    out_track = SourceAudioTrack()
    conn, events = await make_conn(server, out_track=out_track)
    await server.notify("kicked", {})
    await wait_for(lambda: events.closed == ["kicked"])
    assert out_track.readyState == "live"
    await conn.close()
    assert out_track.readyState == "ended"


async def test_signaling_loss_notifies_owner(server):
    conn, events = await make_conn(server)
    await server.ws.close()
    await wait_for(lambda: events.closed == ["disconnected"])
    await conn.close()


async def test_consumer_closed_closes_consumer_and_reports_track_end(server):
    conn, events = await make_conn(server)
    await server.notify("newProducer", {"producerId": "p1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: events.received == ["p1"])
    consumer = conn.recv_transport.consumers[0]
    await server.notify("consumerClosed", {"consumerId": consumer.id})
    await wait_for(lambda: events.ended == ["p1"])
    assert consumer.closed
    assert conn.consumers == {}
    await conn.close()


async def test_producer_closed_closes_consumer(server):
    conn, events = await make_conn(server)
    await server.notify("newProducer", {"producerId": "p1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: events.received == ["p1"])
    await server.notify("producerClosed", {"producerId": "p1"})
    await wait_for(lambda: events.ended == ["p1"])
    assert conn.recv_transport.consumers[0].closed
    await conn.close()


async def test_producer_closed_while_consume_in_flight(server):
    server.delays["consume"] = 0.2
    conn, events = await make_conn(server)
    await server.notify("newProducer", {"producerId": "p1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: server.count("consume") == 1)
    await server.notify("producerClosed", {"producerId": "p1"})
    await wait_for(lambda: conn.recv_transport.consumers and conn.recv_transport.consumers[0].closed)
    assert events.received == []
    assert conn.consumers == {}
    assert conn._closed_producers == set() and conn._consuming == set()
    await conn.close()


async def test_request_timeout_forgets_pending_request(server):
    sig = Signaling(server.url, lambda m, d: None, request_timeout=0.1)
    await sig.connect()
    with pytest.raises(asyncio.TimeoutError):
        await sig.request("hang")
    assert sig._pending == {}
    await sig.close()


async def test_close_during_start_cleans_everything_up(server):
    server.delays["getRouterRtpCapabilities"] = 0.3
    conn = VoiceConnection("x", "t", request_timeout=2.0)
    conn._signaling_url = lambda: server.url
    starting = asyncio.ensure_future(conn.start())
    await server.connected.wait()
    await conn.close()
    with pytest.raises(RuntimeError):
        await starting
    assert conn.signaling is None
    await wait_for(lambda: server.ws.closed)


async def test_failed_start_cleans_up(server):
    server.responses["getRouterRtpCapabilities"] = lambda obj: {"bad": 1}
    conn = VoiceConnection("x", "t", request_timeout=2.0)
    conn._signaling_url = lambda: server.url
    with pytest.raises(Exception):
        await conn.start()
    assert conn.signaling is None
    await wait_for(lambda: server.ws.closed)


async def test_video_producers_from_get_producers_are_not_consumed(server):
    conn, events = await make_conn(server)
    await conn._consume("v1", 7, "video")
    assert server.count("consume") == 0
    assert events.received == []
    await conn.close()


async def test_consumer_closed_while_consume_in_flight(server):
    server.delays["resumeConsumer"] = 0.0
    conn, events = await make_conn(server)
    original = conn.recv_transport.consume
    gate = asyncio.Event()

    async def slow_consume(**kw):
        consumer = await original(**kw)
        await gate.wait()
        return consumer

    conn.recv_transport.consume = slow_consume
    await server.notify("newProducer", {"producerId": "p1", "userId": 7, "kind": "audio"})
    await wait_for(lambda: conn.recv_transport.consumers)
    await server.notify("consumerClosed", {"consumerId": "c-p1"})
    await wait_for(lambda: "c-p1" in conn._closed_consumers)
    gate.set()
    await wait_for(lambda: conn.recv_transport.consumers[0].closed)
    assert events.received == []
    assert conn.consumers == {}
    assert conn._closed_consumers == set()
    await conn.close()


async def test_kicked_during_start_is_reported_after_start(server):
    conn, events = await make_conn(server)
    conn._started = False
    conn._notify_closed("kicked")
    assert events.closed == []
    conn._started = True
    conn._notify_closed(conn._pending_close)
    assert events.closed == ["kicked"]
    await conn.close()


async def test_reader_cancel_does_not_report_disconnect(server):
    conn, events = await make_conn(server)
    conn.signaling._reader.cancel()
    await asyncio.sleep(0.1)
    assert events.closed == []
    await conn.close()
