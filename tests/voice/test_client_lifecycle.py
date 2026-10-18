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
import io
import threading
import time
from types import SimpleNamespace

import pytest

import lolka.voice_client as voice_client_module
import lolka
from lolka.player import PCMAudio
from lolka import _voice_impl

pytestmark = pytest.mark.asyncio

FRAME_BYTES = _voice_impl.FRAME_BYTES


class FakeConn:
    instances = []

    def __init__(self, endpoint, token, **kwargs):
        self._endpoint = endpoint
        self._token = token
        self.kwargs = kwargs
        self.out_track = kwargs.get("out_track") or _voice_impl.SourceAudioTrack()
        self.started = False
        self.closed = False
        FakeConn.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True
        if self.out_track is not None:
            self.out_track.stop_source()
            self.out_track.end()

    def kick(self, reason="kicked"):
        self.kwargs["on_closed"](reason)


class FakeGuild:
    def __init__(self):
        self.states = []
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def change_voice_state(self, *, channel=None, **kwargs):
        self.states.append(channel)


def make_vc():
    FakeConn.instances.clear()
    loop = asyncio.get_running_loop()
    client = SimpleNamespace(
        _connection=SimpleNamespace(loop=loop, user=None), loop=loop
    )
    guild = FakeGuild()
    channel = SimpleNamespace(id=1, guild=guild)
    guild.channels[1] = channel
    guild.channels[2] = SimpleNamespace(id=2, guild=guild)
    vc = voice_client_module.VoiceClient(client, channel=channel)
    vc._ms = SimpleNamespace(
        VoiceConnection=FakeConn,
        MediaRelay=_voice_impl.MediaRelay,
        drain_track=_voice_impl.drain_track,
    )
    conn = vc._make_conn("ep1", "t1")
    conn.started = True
    vc._conn = conn
    vc._endpoint, vc._token = "ep1", "t1"
    vc._connected = True
    vc._state_ready.set()
    vc.cleanup = lambda: None
    return vc, guild


def vsu(channel_id):
    # discord.py passes the raw gateway payload: channel_id is a snowflake string
    return {"session_id": "s", "channel_id": None if channel_id is None else str(channel_id)}


def vserveru(token, endpoint):
    return {"token": token, "endpoint": endpoint, "guild_id": "1"}


async def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_move_to_channel_on_the_same_server_rebuilds_the_session():
    vc, _ = make_vc()
    old = vc._conn
    old_track = old.out_track
    await vc.on_voice_state_update(vsu(2))
    await vc.on_voice_server_update(vserveru("t-room2", "ep1"))
    await wait_for(lambda: vc._conn is not old and vc._conn.started)
    assert old.closed
    assert vc._conn.out_track is old_track
    assert vc.is_connected()
    assert vc.channel.id == 2
    assert not vc._room_changed
    vc.play(PCMAudio(io.BytesIO(b"\x01\x00" * FRAME_BYTES)))
    assert vc.is_playing()
    vc.stop()


async def test_token_reissue_in_the_same_room_keeps_the_session():
    vc, _ = make_vc()
    old = vc._conn
    await vc.on_voice_state_update(vsu(1))
    await vc.on_voice_server_update(vserveru("t-new", "ep1"))
    await asyncio.sleep(0.05)
    assert vc._conn is old and not old.closed


async def test_kicked_before_the_move_events_still_migrates():
    vc, _ = make_vc()
    vc._kick_grace = 1.0
    old = vc._conn
    old.kick()
    await asyncio.sleep(0.05)
    assert vc.is_connected()  # waiting for the new room
    await vc.on_voice_state_update(vsu(2))
    await vc.on_voice_server_update(vserveru("t-room2", "ep1"))
    await wait_for(lambda: vc._conn is not old and vc._conn.started)
    await asyncio.sleep(0.1)
    assert vc.is_connected()
    assert vc._conn.out_track.readyState == "live"


async def test_kicked_without_a_move_tears_the_client_down():
    vc, guild = make_vc()
    vc._kick_grace = 0.05
    vc._leave_wait = 0.05
    done = threading.Event()
    vc.play(PCMAudio(io.BytesIO(b"\x00" * FRAME_BYTES * 100)), after=lambda e: done.set())
    vc._conn.kick()
    await wait_for(lambda: not vc.is_connected())
    assert not vc.is_playing()
    assert done.wait(2)
    assert guild.states == [None]


async def test_signaling_loss_tears_the_client_down_and_leaves_the_channel():
    vc, guild = make_vc()
    vc._kick_grace = 0.05
    vc._leave_wait = 0.05
    vc._conn.kick("disconnected")
    await wait_for(lambda: not vc.is_connected())
    await wait_for(lambda: guild.states == [None])


async def test_move_during_the_first_connect_migrates_after_start(monkeypatch):
    vc, guild = make_vc()
    vc._conn = None
    vc._connected = False
    vc._state_ready.clear()
    vc._channel_id = 1

    async def start_with_move(self):
        self.started = True
        await vc.on_voice_state_update(vsu(2))
        await vc.on_voice_server_update(vserveru("t-room2", "ep1"))

    monkeypatch.setattr(FakeConn, "start", start_with_move)
    await vc.on_voice_state_update(vsu(1))
    await vc.on_voice_server_update(vserveru("t1", "ep1"))
    await vc.connect(reconnect=True, timeout=1.0)
    first = vc._conn
    await wait_for(lambda: vc._conn is not first and vc._conn.started)
    assert first.closed
    assert vc.is_connected()
    assert vc.channel.id == 2


async def test_uncached_channel_does_not_trigger_a_rebuild_on_every_state_update():
    vc, _ = make_vc()
    await vc.on_voice_state_update(vsu(7))
    assert vc._room_changed
    vc._room_changed = False
    await vc.on_voice_state_update(vsu(7))
    assert not vc._room_changed


async def test_late_null_voice_state_of_the_previous_session_is_ignored():
    vc, _ = make_vc()
    vc._state_ready.clear()  # a new client that has not seen its first VSU
    await vc.on_voice_state_update(vsu(None))
    await asyncio.sleep(0.05)
    assert vc.is_connected()


async def test_disconnect_waits_for_the_null_voice_state():
    vc, guild = make_vc()
    vc._leave_wait = 2.0
    task = asyncio.ensure_future(vc.disconnect())
    await wait_for(lambda: guild.states == [None])
    await asyncio.sleep(0.1)
    assert not task.done()
    await vc.on_voice_state_update(vsu(None))
    await asyncio.wait_for(task, 1.0)
    assert not vc.is_connected()


async def test_disconnect_does_not_wait_forever_for_the_null_voice_state():
    vc, _ = make_vc()
    vc._leave_wait = 0.1
    await asyncio.wait_for(vc.disconnect(), 1.0)
    assert not vc.is_connected()


async def test_ended_remote_track_is_forgotten_with_its_tasks():
    from .test_webrtc import _QueueTrack

    vc, _ = make_vc()
    remote = _QueueTrack()
    vc._handle_track(remote, 9, "p")
    assert "p" in vc._remote_tracks
    drain = vc._remote_tracks["p"][3]
    vc._forget_producer("p")
    await asyncio.sleep(0.05)
    assert "p" not in vc._remote_tracks
    assert drain.cancelled() or drain.done()


async def test_dict_payload_with_the_current_channel_does_not_migrate():
    vc, _ = make_vc()
    old = vc._conn
    for _ in range(3):
        await vc.on_voice_state_update({"session_id": "s2", "channel_id": "1"})
        await vc.on_voice_server_update(vserveru("t-reissued", "ep1"))
    await asyncio.sleep(0.05)
    assert not vc._room_changed
    assert vc._conn is old and not old.closed
    assert vc.session_id == "s2"
    assert vc.channel.id == 1


async def test_kicked_without_vsu_disconnects_through_the_public_api():
    vc, guild = make_vc()
    vc._kick_grace = 0.05
    vc._leave_wait = 0.05
    conn = vc._conn
    conn.kick()
    await wait_for(lambda: not vc.is_connected())
    assert conn.closed
    assert vc._conn is None
    assert guild.states == [None]


async def test_voice_client_keeps_the_discord_py_public_api():
    for name in (
        "connect", "disconnect", "move_to", "play", "stop", "pause", "resume",
        "is_playing", "is_paused", "is_connected", "wait_until_connected",
        "latency", "average_latency", "guild", "user", "source",
    ):
        assert hasattr(voice_client_module.VoiceClient, name), name
    assert lolka.VoiceClient is voice_client_module.VoiceClient
