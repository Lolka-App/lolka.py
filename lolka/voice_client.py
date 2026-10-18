"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

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
import inspect
import logging
from typing import Any, Callable, List, Optional, Tuple, TYPE_CHECKING, Union

from .errors import ClientException
from .player import AudioSource

if TYPE_CHECKING:
    from .client import Client
    from .guild import Guild
    from .state import ConnectionState
    from .user import ClientUser
    from .opus import APPLICATION_CTL, BAND_CTL, SIGNAL_CTL
    from .channel import StageChannel, VoiceChannel
    from . import abc

    from .types.voice import (
        GuildVoiceState as GuildVoiceStatePayload,
        VoiceServerUpdate as VoiceServerUpdatePayload,
    )

    VocalGuildChannel = Union[VoiceChannel, StageChannel]

__all__ = (
    'VoiceProtocol',
    'VoiceClient',
)


_log = logging.getLogger(__name__)


def _to_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class VoiceProtocol:
    """A class that represents the Discord voice protocol.

    This is an abstract class. The library provides a concrete implementation
    under :class:`VoiceClient`.

    This class allows you to implement a protocol to allow for an external
    method of sending voice, such as Lavalink_ or a native library implementation.

    These classes are passed to :meth:`abc.Connectable.connect <VoiceChannel.connect>`.

    .. _Lavalink: https://github.com/freyacodes/Lavalink

    Parameters
    ------------
    client: :class:`Client`
        The client (or its subclasses) that started the connection request.
    channel: :class:`abc.Connectable`
        The voice channel that is being connected to.
    """

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        self.client: Client = client
        self.channel: abc.Connectable = channel

    async def on_voice_state_update(self, data: GuildVoiceStatePayload, /) -> None:
        """|coro|

        An abstract method that is called when the client's voice state
        has changed. This corresponds to ``VOICE_STATE_UPDATE``.

        .. warning::

            This method is not the same as the event. See: :func:`on_voice_state_update`

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice state payload <resources/voice#voice-state-object>`.
        """
        raise NotImplementedError

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload, /) -> None:
        """|coro|

        An abstract method that is called when initially connecting to voice.
        This corresponds to ``VOICE_SERVER_UPDATE``.

        Parameters
        ------------
        data: :class:`dict`
            The raw :ddocs:`voice server update payload <topics/gateway-events#voice-server-update>`.
        """
        raise NotImplementedError

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = False, self_mute: bool = False) -> None:
        """|coro|

        An abstract method called when the client initiates the connection request.

        When a connection is requested initially, the library calls the constructor
        under ``__init__`` and then calls :meth:`connect`. If :meth:`connect` fails at
        some point then :meth:`disconnect` is called.

        Within this method, to start the voice connection flow it is recommended to
        use :meth:`Guild.change_voice_state` to start the flow. After which,
        :meth:`on_voice_server_update` and :meth:`on_voice_state_update` will be called.
        The order that these two are called is unspecified.

        Parameters
        ------------
        timeout: :class:`float`
            The timeout for the connection.
        reconnect: :class:`bool`
            Whether reconnection is expected.
        self_mute: :class:`bool`
            Indicates if the client should be self-muted.

            .. versionadded:: 2.0
        self_deaf: :class:`bool`
            Indicates if the client should be self-deafened.

            .. versionadded:: 2.0
        """
        raise NotImplementedError

    async def disconnect(self, *, force: bool) -> None:
        """|coro|

        An abstract method called when the client terminates the connection.

        See :meth:`cleanup`.

        Parameters
        ------------
        force: :class:`bool`
            Whether the disconnection was forced.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """This method *must* be called to ensure proper clean-up during a disconnect.

        It is advisable to call this from within :meth:`disconnect` when you are
        completely done with the voice protocol instance.

        This method removes it from the internal state cache that keeps track of
        currently alive voice clients. Failure to clean-up will cause subsequent
        connections to report that it's still connected.
        """
        key_id, _ = self.channel._get_voice_client_key()
        self.client._connection._remove_voice_client(key_id)


class VoiceClient(VoiceProtocol):
    """Represents a lolka voice connection over WebRTC.

    You do not create these, you typically get them from
    e.g. :meth:`VoiceChannel.connect`.

    Voice in lolka works over WebRTC (not Discord UDP/libsodium), so it requires
    the WebRTC voice dependencies (aiortc, pymediasoup), which ship with the base
    install of ``lolka.py``. The public interface stays compatible with
    discord.py's :class:`VoiceClient`, so existing bots keep working after
    ``import lolka as discord``.

    Warning
    --------
    In order to play Opus based AudioSources (``is_opus()`` returns ``True``, e.g.
    :class:`FFmpegOpusAudio`) you must have the libopus library available (it is
    loaded automatically when it can be found; bundled on Windows, on Linux install
    e.g. ``apt install libopus0``): they are decoded to PCM first. PCM based
    AudioSources (:class:`FFmpegPCMAudio`) do not need libopus, audio is encoded to
    Opus by aiortc before it is sent.

    Attributes
    -----------
    session_id: :class:`str`
        The voice connection session ID.
    token: :class:`str`
        The voice connection token.
    endpoint: :class:`str`
        The voice signaling endpoint we are connecting to.
    channel: Union[:class:`VoiceChannel`, :class:`StageChannel`]
        The voice channel connected to.
    on_receive_track: Optional[Callable]
        Optional callback ``(track, user_id, producer_id)`` invoked for every
        incoming audio track that is consumed from other participants (video is
        never consumed). ``track`` is an :class:`aiortc.MediaStreamTrack`; the
        callback may be a coroutine function. The track only delivers frames from
        the moment it was handed over and keeps just the latest frame, so read it
        (``await track.recv()`` in a loop until ``MediaStreamError``) or call
        ``track.stop()`` when you are not interested in it.
    """

    channel: VocalGuildChannel

    # WebRTC voice does not use PyNaCl/davey; kept for client.py compatibility.
    warn_nacl: bool = False
    warn_dave: bool = False

    #: Discord UDP encryption modes; there are none for the WebRTC transport.
    supported_modes: Tuple[str, ...] = ()

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        try:
            from . import _voice_impl as _ms
        except ImportError as exc:
            raise RuntimeError(
                'voice requires aiortc and pymediasoup: reinstall "lolka.py"'
            ) from exc

        super().__init__(client, channel)
        self._ms = _ms
        state = client._connection
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state: ConnectionState = state

        self._session_id: Optional[str] = None
        self._token: Optional[str] = None
        self._endpoint: Optional[str] = None
        self._state_ready: asyncio.Event = asyncio.Event()
        self._server_ready: asyncio.Event = asyncio.Event()

        self._conn: Optional[Any] = None  # _voice_impl.VoiceConnection
        self._connected: bool = False
        self._timeout: float = 30.0
        # migrations to a new voice server run one at a time
        self._migrate_lock: asyncio.Lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._request_timeout: float = 15.0
        # The backend moves the bot between rooms by kicking it from the old SFU
        # room and issuing a new token (VOICE_STATE_UPDATE, then
        # VOICE_SERVER_UPDATE with the same endpoint on the same instance).
        self._room_changed: bool = False
        self._channel_id: Any = getattr(channel, 'id', None)
        self._moved: asyncio.Event = asyncio.Event()
        self._kick_grace: float = 5.0
        self._disconnecting: bool = False
        self._left: asyncio.Event = asyncio.Event()
        self._leave_wait: float = 1.5

        #: user callback for incoming tracks (track, user_id, producer_id)
        self.on_receive_track: Optional[Callable[[Any, Any, Any], Any]] = None

        # producer_id -> (track, user_id, relay, drain task)
        self._remote_tracks: dict[str, tuple[Any, Any, Any, asyncio.Task]] = {}

    # tasks

    def _spawn(self, coro: Any, name: Optional[str] = None) -> asyncio.Future:
        task = asyncio.ensure_future(coro, loop=self.loop)
        if name is not None and isinstance(task, asyncio.Task):
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error('unhandled error in voice task %s', task.get_name() if isinstance(task, asyncio.Task) else task, exc_info=exc)

    @property
    def guild(self) -> Guild:
        """:class:`Guild`: The guild we're connected to."""
        return self.channel.guild

    @property
    def user(self) -> ClientUser:
        """:class:`ClientUser`: The user connected to voice (i.e. ourselves)."""
        return self._state.user  # type: ignore

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def token(self) -> Optional[str]:
        return self._token

    @property
    def endpoint(self) -> Optional[str]:
        return self._endpoint

    @property
    def ssrc(self) -> int:
        """:class:`int`: Always ``0``, there is no RTP SSRC handshake on the WebRTC transport."""
        return 0

    @property
    def mode(self) -> str:
        """:class:`str`: The transport mode, always ``'webrtc'``."""
        return 'webrtc'

    @property
    def secret_key(self) -> List[int]:
        """List[:class:`int`]: Always empty, encryption is handled by WebRTC (DTLS-SRTP)."""
        return []

    @property
    def ws(self) -> None:
        """Always ``None``, there is no Discord voice websocket on the WebRTC transport."""
        return None

    @property
    def voice_privacy_code(self) -> Optional[str]:
        """Always ``None``, there is no DAVE session on the WebRTC transport."""
        return None

    @property
    def timeout(self) -> float:
        return self._timeout

    def checked_add(self, attr: str, value: int, limit: int) -> None:
        """Adds ``value`` to ``attr``, wrapping it to ``0`` once it passes ``limit``."""
        val = getattr(self, attr)
        if val + value > limit:
            setattr(self, attr, 0)
        else:
            setattr(self, attr, val + value)

    @property
    def latency(self) -> float:
        """:class:`float`: Voice latency in seconds.

        Not tracked for the WebRTC transport; returns ``0.0``.
        Kept for discord.py API compatibility.
        """
        return 0.0

    @property
    def average_latency(self) -> float:
        """:class:`float`: Average voice latency in seconds (see :attr:`latency`)."""
        return 0.0

    # connection related

    async def on_voice_state_update(self, data: GuildVoiceStatePayload) -> None:
        session_id = data.get('session_id')
        raw_channel_id = data.get('channel_id')
        if raw_channel_id is None:
            if self._disconnecting:
                self._left.set()
                return
            if not self._state_ready.is_set():
                # a late "left the channel" of the previous session of this guild
                return
            self._session_id = session_id
            # we were disconnected from outside (kick/move/delete)
            self._spawn(self._teardown(), name='voice-teardown')
            return
        self._session_id = session_id
        channel_id = int(raw_channel_id)
        if channel_id != self._channel_id:
            # also while the first session is still coming up (connect())
            if self._connected or self._conn is not None:
                self._room_changed = True
            self._channel_id = channel_id
            new_channel = self.guild.get_channel(channel_id)
            if new_channel is not None:
                self.channel = new_channel  # type: ignore
        self._state_ready.set()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        token = data.get('token')
        endpoint = data.get('endpoint')
        if not token or not endpoint:
            _log.warning('Awaiting voice server endpoint...')
            return
        # A repeated VOICE_SERVER_UPDATE for an already connected client means
        # that the voice server changed (failover or the channel moved to another
        # instance) or that the bot was moved to another room (new token for the
        # new room, possibly on the same instance): rebuild the session. A token
        # change alone keeps the session (the backend reissues the token on every
        # VOICE_STATE_UPDATE, e.g. on self_mute).
        server_changed = self._connected and (endpoint != self._endpoint or self._room_changed)
        self._token = token
        self._endpoint = endpoint
        self._server_ready.set()
        if server_changed:
            self._room_changed = False
            self._moved.set()
            self._spawn(self._migrate(), name='voice-migrate')

    def _make_conn(self, endpoint: str, token: str, out_track: Any = None) -> Any:
        conn = self._ms.VoiceConnection(
            endpoint,
            token,
            on_receive_track=self._handle_track,
            out_track=out_track,
            on_closed=lambda reason: self._spawn(self._on_conn_closed(conn, reason), name='voice-conn-closed'),
            on_track_ended=self._forget_producer,
            request_timeout=self._request_timeout,
        )
        return conn

    async def _on_conn_closed(self, conn: Any, reason: str) -> None:
        """The voice server dropped our session (kicked / connection lost).

        When the bot is being moved to another room, the new VOICE_SERVER_UPDATE
        follows and rebuilds the session (``_migrate``); otherwise the client is
        disconnected (the voice state is cleared); a lost connection is not
        re-established by the library.
        """
        if conn is not self._conn:
            return
        _log.info('voice session ended (%s)', reason)
        try:
            await asyncio.wait_for(self._moved.wait(), self._kick_grace)
        except asyncio.TimeoutError:
            pass
        else:
            return
        if self._conn is conn:
            # leave the room properly (channel=None), or the backend keeps
            # showing the bot in the channel for a while
            await self.disconnect(force=True)

    async def _migrate(self) -> None:
        """Rebuilds the WebRTC session on the new voice server / room.

        The outgoing track is carried over to the new session, so the current
        playback (source, ``after`` callback, pause) is not interrupted.
        """
        try:
            async with self._migrate_lock:
                old = self._conn
                endpoint, token = self._endpoint, self._token
                if old is None or not self._connected or not endpoint or not token:
                    return
                if old._endpoint == endpoint and old._token == token:
                    return  # already moved (events arrived in a batch)

                _log.info('voice server changed, migrating to endpoint=%s', endpoint)
                # detach the track from the old session, or its close() would reset the source
                conn = self._make_conn(endpoint, token, old.out_track)
                # Set the new session right away: play()/stop()/is_playing() keep
                # working with the same track while it comes up. The track is
                # detached from the old session only after that, so there is no
                # window in which self._conn has no track.
                self._conn = conn
                old.out_track = None
                self._drop_remote_tracks()
                try:
                    await old.close()
                except Exception:
                    pass

                try:
                    await conn.start()
                except Exception:
                    if self._conn is not conn:
                        return  # disconnected while migrating
                    _log.exception('failed to migrate to the new voice server')
                    await self.disconnect(force=True)
        finally:
            self._moved.clear()

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        """|coro|

        Connects to the voice channel.

        ``reconnect`` is accepted for discord.py compatibility but has no effect:
        if the voice connection is lost (the voice server restarted, the network
        dropped), the client disconnects and leaves the channel; reconnecting is
        up to the bot.
        """
        self._timeout = timeout
        guild = self.channel.guild
        await guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)
        await asyncio.wait_for(
            asyncio.gather(self._state_ready.wait(), self._server_ready.wait()),
            timeout=timeout,
        )
        if not self._endpoint or not self._token:
            raise ClientException('voice server did not provide an endpoint/token')

        _log.info('connecting to voice endpoint=%s', self._endpoint)
        conn = self._make_conn(self._endpoint, self._token)
        self._conn = conn
        try:
            await conn.start()
        except BaseException:
            await self.disconnect(force=True)
            raise
        self._connected = True
        if conn._endpoint != self._endpoint or conn._token != self._token:
            # the bot was moved while the session was coming up
            self._room_changed = False
            self._moved.set()
            self._spawn(self._migrate(), name='voice-migrate')

    def _handle_track(self, track: Any, user_id: Any, producer_id: Any) -> None:
        relay = self._ms.MediaRelay()
        uid = _to_int(user_id)
        old = self._remote_tracks.pop(producer_id, None)
        if old is not None:
            old[3].cancel()
        # Always read the track: aiortc queues every decoded frame of a received
        # track without limit, and the relay (unbuffered) keeps only the latest
        # frame for the other subscribers.
        drain = self._spawn(
            self._drain(relay.subscribe(track, buffered=False)),
            name=f'voice-drain: {producer_id}',
        )
        self._remote_tracks[producer_id] = (track, uid, relay, drain)
        track.on('ended', lambda: self._forget_track(producer_id, track))

        cb = self.on_receive_track
        if cb is not None:
            result = cb(relay.subscribe(track, buffered=False), user_id, producer_id)
            if inspect.isawaitable(result):
                self._spawn(result, name=f'voice-on-receive-track: {producer_id}')

    async def _drain(self, proxy: Any) -> None:
        try:
            await self._ms.drain_track(proxy)
        finally:
            proxy.stop()

    def _forget_track(self, producer_id: str, track: Any) -> None:
        entry = self._remote_tracks.get(producer_id)
        if entry is not None and entry[0] is track:
            self._forget_producer(producer_id)

    def _forget_producer(self, producer_id: str) -> None:
        """The participant's track is gone: stop everything attached to it."""
        entry = self._remote_tracks.pop(producer_id, None)
        if entry is not None:
            entry[3].cancel()

    def _drop_remote_tracks(self) -> None:
        for entry in self._remote_tracks.values():
            entry[3].cancel()
        self._remote_tracks.clear()

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._connected

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connected

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self._disconnecting = True
        self._connected = False  # no new play() from after-callbacks of stop()
        self.stop()
        conn, self._conn = self._conn, None
        try:
            if conn is not None:
                await conn.close()
        finally:
            self._drop_remote_tracks()
            left = False
            try:
                await self.channel.guild.change_voice_state(channel=None)
                left = self._state_ready.is_set()
            except Exception:
                pass
            if left:
                # The backend answers with VOICE_STATE_UPDATE(channel=null); let it
                # arrive here, otherwise it lands in the next VoiceClient of this
                # guild and tears it down.
                try:
                    await asyncio.wait_for(self._left.wait(), self._leave_wait)
                except asyncio.TimeoutError:
                    pass
            self.cleanup()

    async def move_to(self, channel: Optional[abc.Snowflake], *, timeout: Optional[float] = 30.0) -> None:
        """|coro|

        Moves you to a different voice channel.

        .. note::

            For the WebRTC transport this changes the gateway voice state to the
            target channel. Re-establishing the media session on a new room
            happens through the resulting ``VOICE_SERVER_UPDATE``.

        Parameters
        -----------
        channel: Optional[:class:`abc.Snowflake`]
            The channel to move to. If this is ``None``, it is an equivalent of calling :meth:`.disconnect`.
        timeout: Optional[:class:`float`]
            Not used for the WebRTC transport; kept for compatibility.
        """
        if channel is None:
            await self.disconnect()
            return
        if self.channel and channel.id == self.channel.id:
            return
        await self.channel.guild.change_voice_state(channel=channel)

    async def _teardown(self) -> None:
        self._connected = False
        self.stop()
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
        self._drop_remote_tracks()
        try:
            self.cleanup()
        except Exception:
            pass

    # audio related

    def play(
        self,
        source: AudioSource,
        *,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
        application: APPLICATION_CTL = 'audio',
        bitrate: int = 128,
        fec: bool = True,
        expected_packet_loss: float = 0.15,
        bandwidth: BAND_CTL = 'full',
        signal_type: SIGNAL_CTL = 'auto',
    ) -> None:
        """Plays an :class:`AudioSource`.

        The finalizer, ``after`` is called after the source has been exhausted,
        stopped or an error occurred. It runs in a separate thread, *after*
        ``source.cleanup()``, and may be delayed by up to 2 seconds if
        ``source.read()`` hangs. It may call :meth:`play` again.

        The audio is fed into the WebRTC send track and (re)encoded to Opus by
        aiortc, then streamed. The Opus encoder parameters
        (``application``, ``bitrate``, ``fec``, ``expected_packet_loss``,
        ``bandwidth``, ``signal_type``) are accepted for discord.py API
        compatibility but are managed by aiortc for this transport.

        Parameters
        -----------
        source: :class:`AudioSource`
            The audio source we're reading from.
        after: Callable[[Optional[:class:`Exception`]], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        OpusNotLoaded
            The source is Opus based (``is_opus()``) and libopus is not available.
        TypeError
            Source is not a :class:`AudioSource` or after is not a callable.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')
        if self.is_playing():
            raise ClientException('Already playing audio.')
        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')
        if after is not None and not callable(after):
            raise TypeError(f'after must be a callable not {after.__class__.__name__}')

        # ``_after`` runs in a helper thread, not in the event loop (like in
        # discord.py, where it runs in the player thread): a blocking ``after``
        # does not freeze the bot and may call :meth:`play` again.
        def _after(exc: Optional[Exception]) -> None:
            if after is not None:
                after(exc)
            elif exc is not None:
                _log.error('Error while playing audio', exc_info=exc)

        conn = self._conn
        track = conn.out_track if conn else None
        if track is None:
            raise ClientException('Not connected to voice.')
        track.set_source(source, _after)

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        conn = self._conn
        track = conn.out_track if conn else None
        return track is not None and track.is_playing

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        conn = self._conn
        track = conn.out_track if conn else None
        return track is not None and track.is_paused

    def stop(self) -> None:
        """Stops playing audio.

        The ``after`` callback of the stopped source is called.
        """
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.stop_source()

    def pause(self) -> None:
        """Pauses the audio playing."""
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        conn = self._conn
        track = conn.out_track if conn else None
        if track is not None:
            track.resume()

    @property
    def source(self) -> Optional[AudioSource]:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        The previous source is *not* cleaned up (the new one usually wraps it, e.g.
        ``vc.source = PCMVolumeTransformer(vc.source)``); the ``after`` callback and
        the paused state are kept.
        """
        conn = self._conn
        track = conn.out_track if conn else None
        return track.source if track is not None else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')

        conn = self._conn
        track = conn.out_track if conn else None
        if track is None or track.source is None:
            raise ValueError('Not playing anything.')
        track.replace_source(value)

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        """Not supported: raw packets cannot be sent over the WebRTC transport.

        Raises
        -------
        ClientException
            Always, use :meth:`play` instead.
        """
        raise ClientException('send_audio_packet is not supported by the WebRTC transport, use play()')
