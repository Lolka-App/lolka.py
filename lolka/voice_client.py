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
import logging
from typing import Any, Callable, Optional, TYPE_CHECKING, Union

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
    the ``voice`` extra: ``pip install "lolka.py[voice]"`` (which installs the
    WebRTC voice dependencies). The public interface stays compatible with
    discord.py's :class:`VoiceClient`, so existing bots keep working after
    ``import lolka as discord``.

    Warning
    --------
    In order to use PCM based AudioSources you must have the opus library
    available (bundled with ``av``/aiortc). Audio is (re)encoded to Opus by
    aiortc before it is sent.

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
        incoming audio track that is consumed from other participants. ``track``
        is an :class:`aiortc.MediaStreamTrack`. Assign this to receive/record
        other participants' audio.
    """

    channel: VocalGuildChannel

    # WebRTC voice does not use PyNaCl/davey; kept for client.py compatibility.
    warn_nacl: bool = False
    warn_dave: bool = False

    def __init__(self, client: Client, channel: abc.Connectable) -> None:
        try:
            from . import _voice_impl as _ms
        except ImportError as exc:
            raise RuntimeError(
                'voice requires extra dependencies: install "lolka.py[voice]"'
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

        #: пользовательский колбэк приёма входящих треков (track, user_id, producer_id)
        self.on_receive_track: Optional[Callable[[Any, Any, Any], Any]] = None

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
    def timeout(self) -> float:
        return self._timeout

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
        self._session_id = data.get('session_id')
        if data.get('channel_id') is None:
            # нас отключили извне (kick/move/delete)
            self.loop.create_task(self._teardown())
            return
        self._state_ready.set()

    async def on_voice_server_update(self, data: VoiceServerUpdatePayload) -> None:
        self._token = data.get('token')
        self._endpoint = data.get('endpoint')
        self._server_ready.set()

    async def connect(self, *, reconnect: bool, timeout: float, self_deaf: bool = False, self_mute: bool = False) -> None:
        self._timeout = timeout
        guild = self.channel.guild
        await guild.change_voice_state(channel=self.channel, self_mute=self_mute, self_deaf=self_deaf)
        await asyncio.wait_for(
            asyncio.gather(self._state_ready.wait(), self._server_ready.wait()),
            timeout=timeout,
        )
        if not self._endpoint or not self._token:
            raise ClientException('voice server did not provide an endpoint/token')

        _log.info('voice: connecting to voice endpoint=%s', self._endpoint)
        self._conn = self._ms.VoiceConnection(
            self._endpoint, self._token, on_receive_track=self._handle_track
        )
        await self._conn.start()
        self._connected = True

    def _handle_track(self, track: Any, user_id: Any, producer_id: Any) -> None:
        cb = self.on_receive_track
        if cb is not None:
            cb(track, user_id, producer_id)

    def wait_until_connected(self, timeout: Optional[float] = 30.0) -> bool:
        return self._connected

    def is_connected(self) -> bool:
        """Indicates if the voice client is connected to voice."""
        return self._connected

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        self.stop()
        try:
            if self._conn is not None:
                await self._conn.close()
        finally:
            self._conn = None
            self._connected = False
            try:
                await self.channel.guild.change_voice_state(channel=None)
            except Exception:
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
            The channel to move to. Must be a voice channel.
        timeout: Optional[:class:`float`]
            How long to wait for the move to complete.
        """
        await self.channel.guild.change_voice_state(channel=channel)

    async def _teardown(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._connected = False
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

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

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

        Raises
        -------
        ClientException
            Already playing audio or not connected.
        TypeError
            Source is not a :class:`AudioSource`.
        """
        if not self.is_connected():
            raise ClientException('Not connected to voice.')
        if self.is_playing():
            raise ClientException('Already playing audio.')
        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')

        self._conn.out_track.set_source(source, after)

    def is_playing(self) -> bool:
        """Indicates if we're currently playing audio."""
        track = self._conn.out_track if self._conn else None
        return track is not None and track.is_playing

    def is_paused(self) -> bool:
        """Indicates if we're playing audio, but if we're paused."""
        track = self._conn.out_track if self._conn else None
        return track is not None and track.is_paused

    def stop(self) -> None:
        """Stops playing audio."""
        if self._conn is not None and self._conn.out_track is not None:
            self._conn.out_track.set_source(None)

    def pause(self) -> None:
        """Pauses the audio playing."""
        if self._conn is not None and self._conn.out_track is not None:
            self._conn.out_track.pause()

    def resume(self) -> None:
        """Resumes the audio playing."""
        if self._conn is not None and self._conn.out_track is not None:
            self._conn.out_track.resume()

    @property
    def source(self) -> Optional[AudioSource]:
        """Optional[:class:`AudioSource`]: The audio source being played, if playing.

        This property can also be used to change the audio source currently being played.
        """
        track = self._conn.out_track if self._conn else None
        return track.source if track is not None else None

    @source.setter
    def source(self, value: AudioSource) -> None:
        if not isinstance(value, AudioSource):
            raise TypeError(f'expected AudioSource not {value.__class__.__name__}.')
        track = self._conn.out_track if self._conn else None
        if track is None or track.source is None:
            raise ValueError('Not playing anything.')
        track.set_source(value, track._after)
