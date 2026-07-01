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

# Внутренний модуль: WebRTC voice-слой для VoiceClient.
# lolka voice использует WebRTC (как клиенты lolka), а не Discord-voice/libsodium.
# Импортируется ЛЕНИВО из voice_client.py, чтобы `import lolka` не требовал extras.
# Зависит от extras "voice" (см. pyproject.toml).

from __future__ import annotations

import asyncio
import logging
import time
from fractions import Fraction
from typing import Any, Callable, Dict, Optional

import aiohttp
import av

from aiortc import MediaStreamTrack

from pymediasoup import Device, AiortcHandler
from pymediasoup.rtp_parameters import RtpCapabilities, RtpParameters
from pymediasoup.models.transport import IceParameters, IceCandidate, DtlsParameters

from .opus import Encoder as _OpusEncoder
from .player import AudioSource

_log = logging.getLogger('lolka.voice')

# 20 мс аудио: 48 кГц, stereo, s16le.
SAMPLE_RATE = _OpusEncoder.SAMPLING_RATE
CHANNELS = _OpusEncoder.CHANNELS
SAMPLES_PER_FRAME = _OpusEncoder.SAMPLES_PER_FRAME
FRAME_BYTES = _OpusEncoder.FRAME_SIZE
SILENCE = b'\x00' * FRAME_BYTES


class SourceAudioTrack(MediaStreamTrack):
    """Постоянный исходящий аудио-трек для aiortc.

    Отдаёт 20-мс кадры из активного :class:`AudioSource`; при отсутствии
    источника — тишину. :meth:`set_source` переключает источник без
    пересоздания трека/producer, поэтому play()/stop()/смена source дешёвые.
    Поддерживает как PCM-источники, так и Opus (декодирует в PCM).
    """

    kind = 'audio'

    def __init__(self) -> None:
        super().__init__()
        self._source: Optional[AudioSource] = None
        self._after: Optional[Callable[[Optional[Exception]], Any]] = None
        self._decoder = None
        self._paused = False
        self._samples = 0
        self._start: Optional[float] = None

    def set_source(
        self,
        source: Optional[AudioSource],
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
    ) -> None:
        old = self._source
        self._source = source
        self._after = after
        self._decoder = None
        self._paused = False
        if source is not None and source.is_opus():
            from . import opus

            self._decoder = opus.Decoder()
        if old is not None and old is not source:
            try:
                old.cleanup()
            except Exception:
                pass

    @property
    def source(self) -> Optional[AudioSource]:
        return self._source

    @property
    def is_playing(self) -> bool:
        return self._source is not None and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._source is not None and self._paused

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def _finish(self, error: Optional[Exception]) -> None:
        after = self._after
        self._source = None
        self._after = None
        self._decoder = None
        if after is not None:
            try:
                after(error)
            except Exception:
                _log.exception('ошибка в after-callback')

    async def recv(self) -> 'av.AudioFrame':
        # Держим реальный темп 20 мс/кадр (иначе aiortc захлебнётся кадрами).
        if self._start is None:
            self._start = time.monotonic()
        target = self._start + (self._samples / SAMPLE_RATE)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        data = SILENCE
        src = self._source
        if src is not None and not self._paused:
            try:
                chunk = src.read()
                if chunk and self._decoder is not None:
                    chunk = self._decoder.decode(chunk)
            except Exception as exc:
                self._finish(exc)
                chunk = b''
            else:
                if not chunk:
                    self._finish(None)
                else:
                    if len(chunk) < FRAME_BYTES:
                        chunk = chunk + b'\x00' * (FRAME_BYTES - len(chunk))
                    data = chunk[:FRAME_BYTES]

        frame = av.AudioFrame(format='s16', layout='stereo', samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._samples
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(data)
        self._samples += SAMPLES_PER_FRAME
        return frame


class Signaling:
    """JSON-RPC поверх WebSocket к голосовому серверу.

    Форматы:
      request:      {id, method, data}
      response:     {id, response: true, data} | {id, response: true, error}
      notification: {notification: true, method, data}
    """

    def __init__(self, url: str, on_notification: Callable[[str, dict], Any]) -> None:
        self._url = url
        self._on_notification = on_notification
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, heartbeat=15)
        self._reader = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                obj = msg.json()
                if obj.get('response'):
                    fut = self._pending.pop(obj.get('id'), None)
                    if fut is not None and not fut.done():
                        err = obj.get('error')
                        if err:
                            fut.set_exception(RuntimeError(str(err)))
                        else:
                            fut.set_result(obj.get('data') or {})
                elif obj.get('notification'):
                    try:
                        await self._on_notification(obj.get('method'), obj.get('data') or {})
                    except Exception:
                        _log.exception('ошибка обработки нотификации %s', obj.get('method'))
        except Exception:
            if not self._closed:
                _log.exception('сигналинг: обрыв чтения')
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError('signaling closed'))
            self._pending.clear()

    async def request(self, method: str, data: Optional[dict] = None, timeout: float = 15.0) -> dict:
        assert self._ws is not None
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_json({'id': rid, 'method': method, 'data': data or {}})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()


class VoiceConnection:
    """Один сеанс WebRTC-войса: сигналинг, транспорты, producer, consumers.

    Инкапсулирует всю низкоуровневую механику; :class:`~lolka.VoiceClient` —
    тонкий фасад поверх этого класса.
    """

    def __init__(self, endpoint: str, token: str, on_receive_track: Optional[Callable] = None) -> None:
        self._endpoint = endpoint
        self._token = token
        self._on_receive_track = on_receive_track

        self.signaling: Optional[Signaling] = None
        self.device: Optional[Device] = None
        self.send_transport = None
        self.recv_transport = None
        self.producer = None
        self.out_track: Optional[SourceAudioTrack] = None
        self._send_connected = asyncio.Event()
        self._recv_connected = asyncio.Event()
        self.consumers: Dict[str, Any] = {}
        self._closed = False

    def _signaling_url(self) -> str:
        ep = self._endpoint or ''
        if '://' not in ep:
            ep = 'ws://' + ep
        sep = '&' if '?' in ep else '?'
        return f'{ep}{sep}token={self._token}'

    async def start(self) -> None:
        self.signaling = Signaling(self._signaling_url(), self._on_notification)
        await self.signaling.connect()

        caps = await self.signaling.request('getRouterRtpCapabilities', {})
        self.device = Device(handlerFactory=AiortcHandler.createFactory())
        await self.device.load(routerRtpCapabilities=RtpCapabilities(**caps))

        await self._create_send_transport()
        await self._create_recv_transport()

        self.out_track = SourceAudioTrack()
        self.producer = await self.send_transport.produce(
            track=self.out_track,
            stopTracks=False,
            appData={'source': 'mic'},
        )
        _log.debug('voice: producer создан id=%s', self.producer.id)

        res = await self.signaling.request('getProducers', {})
        for p in res.get('producers', []):
            await self._consume(p['producerId'], p.get('userId'), p.get('kind'))

    async def _create_send_transport(self) -> None:
        params = await self.signaling.request('createWebRtcTransport', {'direction': 'send'})
        self.send_transport = self.device.createSendTransport(
            id=params['id'],
            iceParameters=IceParameters(**params['iceParameters']),
            iceCandidates=[IceCandidate(**c) for c in params['iceCandidates']],
            dtlsParameters=DtlsParameters(**params['dtlsParameters']),
            sctpParameters=None,
        )

        @self.send_transport.on('connect')
        async def _on_connect(dtlsParameters):
            await self.signaling.request(
                'connectTransport',
                {'transportId': self.send_transport.id, 'dtlsParameters': dtlsParameters.model_dump(exclude_none=True)},
            )
            self._send_connected.set()

        @self.send_transport.on('produce')
        async def _on_produce(kind, rtpParameters, appData):
            # ждём подтверждения connectTransport, иначе produce может уйти раньше
            await self._send_connected.wait()
            res = await self.signaling.request(
                'produce',
                {
                    'transportId': self.send_transport.id,
                    'kind': kind,
                    'rtpParameters': rtpParameters.model_dump(exclude_none=True),
                    'appData': appData or {},
                },
            )
            return res['id']

    async def _create_recv_transport(self) -> None:
        params = await self.signaling.request('createWebRtcTransport', {'direction': 'recv'})
        self.recv_transport = self.device.createRecvTransport(
            id=params['id'],
            iceParameters=IceParameters(**params['iceParameters']),
            iceCandidates=[IceCandidate(**c) for c in params['iceCandidates']],
            dtlsParameters=DtlsParameters(**params['dtlsParameters']),
            sctpParameters=None,
        )

        @self.recv_transport.on('connect')
        async def _on_connect(dtlsParameters):
            await self.signaling.request(
                'connectTransport',
                {'transportId': self.recv_transport.id, 'dtlsParameters': dtlsParameters.model_dump(exclude_none=True)},
            )
            self._recv_connected.set()

    async def _consume(self, producer_id: str, user_id: Any, kind: Any) -> None:
        if self.recv_transport is None or self.device is None or producer_id in self.consumers:
            return
        try:
            res = await self.signaling.request(
                'consume',
                {
                    'transportId': self.recv_transport.id,
                    'producerId': producer_id,
                    'rtpCapabilities': self.device.rtpCapabilities.model_dump(exclude_none=True),
                },
            )
            consumer = await self.recv_transport.consume(
                id=res['id'],
                producerId=res['producerId'],
                kind=res['kind'],
                rtpParameters=RtpParameters(**res['rtpParameters']),
            )
            self.consumers[producer_id] = consumer
            await self.signaling.request('resumeConsumer', {'consumerId': consumer.id})
            _log.debug('voice: consume producer=%s user=%s kind=%s', producer_id, user_id, kind)
            if self._on_receive_track is not None:
                try:
                    self._on_receive_track(consumer.track, user_id, producer_id)
                except Exception:
                    _log.exception('ошибка on_receive_track')
        except Exception:
            _log.exception('voice: ошибка consume producer=%s', producer_id)

    async def _on_notification(self, method: str, data: dict) -> None:
        _log.debug('voice: нотификация %s %s', method, data)
        if method == 'newProducer':
            await self._consume(data.get('producerId'), data.get('userId'), data.get('kind'))
        elif method in ('consumerClosed', 'producerClosed'):
            pid = data.get('producerId')
            if pid:
                self.consumers.pop(pid, None)
        elif method == 'kicked':
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.consumers.clear()
        if self.out_track is not None:
            self.out_track.set_source(None)
        for tr in (self.send_transport, self.recv_transport):
            if tr is not None:
                try:
                    await tr.close()
                except Exception:
                    pass
        self.send_transport = None
        self.recv_transport = None
        if self.signaling is not None:
            try:
                await self.signaling.close()
            except Exception:
                pass
            self.signaling = None
