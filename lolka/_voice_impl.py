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
# Импортируется ЛЕНИВО из voice_client.py, чтобы `import lolka` не тянул aiortc/pymediasoup
# до первого VoiceClient. Зависимости (aiortc, pymediasoup) входят в основной requirements.txt;
# extra `voice` пустой и оставлен для совместимости.
# libopus нужна только Opus-источникам (is_opus(): FFmpegOpusAudio) — они декодируются в PCM;
# PCM-источникам не нужна (кодирование в Opus делает aiortc). На Linux/macOS libopus
# не входит в поставку, без неё play() Opus-источника бросает OpusNotLoaded.

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from fractions import Fraction
from typing import Any, Callable, Dict, Optional

import aiohttp
import av

from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError

from pymediasoup import Device, AiortcHandler
from pymediasoup.rtp_parameters import RtpCapabilities, RtpParameters
from pymediasoup.models.transport import IceParameters, IceCandidate, DtlsParameters

from . import opus as _opus
from .opus import Encoder as _OpusEncoder
from .player import AudioSource

_log = logging.getLogger('lolka.voice')

# 20 мс аудио: 48 кГц, stereo, s16le.
SAMPLE_RATE = _OpusEncoder.SAMPLING_RATE
CHANNELS = _OpusEncoder.CHANNELS
SAMPLES_PER_FRAME = _OpusEncoder.SAMPLES_PER_FRAME
FRAME_BYTES = _OpusEncoder.FRAME_SIZE
SILENCE = b'\x00' * FRAME_BYTES

# Сколько 20-мс кадров поток-читатель держит наготове (~160 мс).
READ_AHEAD_FRAMES = 8
# Сколько ждать завершения потока-читателя перед вызовом after.
AFTER_JOIN_TIMEOUT = 2.0


class _End:
    """Сентинел в очереди кадров: источник закончился (или упал с ошибкой)."""

    __slots__ = ('error', 'owner')

    def __init__(self, error: Optional[Exception], owner: '_Playback') -> None:
        self.error = error
        # очередь может переходить к следующему _Playback (replace_source): конец
        # чужого источника не должен останавливать новый
        self.owner = owner


class _Playback:
    """Один проигрываемый источник и поток, который его читает.

    ``AudioSource.read()``/``cleanup()`` блокирующие (ffmpeg-пайп, сеть), поэтому
    читаются в отдельном потоке и складываются в ограниченную очередь; ``recv()``
    трека забирает готовые кадры, не блокируя event loop.
    """

    def __init__(
        self,
        source: AudioSource,
        after: Optional[Callable[[Optional[Exception]], Any]],
        wait_for: Optional['_Playback'] = None,
        frames: Optional['queue.Queue[Any]'] = None,
    ) -> None:
        # Декодер создаём синхронно: если libopus недоступна, OpusNotLoaded
        # уходит вызывающему play()/set_source, а не умирает в потоке-читателе.
        self._decoder = _opus.Decoder() if source.is_opus() else None
        self.source = source
        self.after = after
        self.queue: 'queue.Queue[Any]' = (
            frames if frames is not None else queue.Queue(maxsize=READ_AHEAD_FRAMES)
        )
        self.exited = threading.Event()
        self._stop = threading.Event()
        self._cleanup = True
        self._cleaned = False
        self._wait_for = wait_for
        self._thread = threading.Thread(
            target=self._run, name='lolka-voice-reader', daemon=True
        )
        self._thread.start()

    def stop(self, cleanup: bool = True) -> None:
        """Попросить поток-читатель выйти; ``cleanup`` — вызвать ``source.cleanup()``."""
        self._cleanup = cleanup
        self._stop.set()

    def _put(self, item: Any) -> bool:
        while not self._stop.is_set():
            try:
                self.queue.put(item, timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def _do_cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.source.cleanup()
        except Exception:
            _log.exception('error while cleaning up the audio source')

    def _wait_prev(self) -> bool:
        """Дождаться выхода предыдущего читателя; ``False`` — нас остановили."""
        prev, self._wait_for = self._wait_for, None
        if prev is None:
            return True
        # Предыдущий читатель мог читать тот же канал (источник-обёртка):
        # два потока на один пайп недопустимы, ждём его выхода.
        while not prev.exited.wait(0.05):
            if self._stop.is_set():
                # cleanup не должен идти параллельно с read() старого потока
                # Замещённый читатель (cleanup=False) ждёт без таймаута: иначе в
                # цепочке A->B->C при зависшем read() у A читатель C начнёт
                # читать параллельно с A.
                prev.exited.wait(AFTER_JOIN_TIMEOUT if self._cleanup else None)
                return False
        return not self._stop.is_set()

    def _put_pcm(self, pcm: bytes) -> bool:
        """Нарезать PCM на 20-мс кадры (последний дополнить тишиной)."""
        for i in range(0, len(pcm), FRAME_BYTES):
            piece = bytes(pcm[i : i + FRAME_BYTES])
            if len(piece) < FRAME_BYTES:
                piece += b'\x00' * (FRAME_BYTES - len(piece))
            if not self._put(piece):
                return False
        return True

    def _run(self) -> None:
        try:
            if not self._wait_prev():
                return
            while not self._stop.is_set():
                try:
                    pcm = self.source.read()
                    if pcm and self._decoder is not None:
                        pcm = self._decoder.decode(pcm)
                        if not pcm:
                            continue
                except Exception as exc:
                    end = _End(exc, self)
                else:
                    if pcm:
                        if not self._put_pcm(pcm):
                            return
                        continue
                    end = _End(None, self)
                if self._stop.is_set():
                    return
                # cleanup строго до after: обе стороны (after и cleanup) видят
                # источник уже освобождённым.
                self._do_cleanup()
                self._put(end)
                return
        except Exception as exc:
            _log.exception('voice: reader thread failed')
            self._do_cleanup()
            self._put(_End(exc, self))
        finally:
            if self._cleanup and not self._cleaned:
                self._do_cleanup()
            self.exited.set()


class SourceAudioTrack(MediaStreamTrack):
    """Постоянный исходящий аудио-трек для aiortc.

    Отдаёт 20-мс кадры из активного :class:`AudioSource`; при отсутствии
    источника (или пока читатель не успел подготовить кадр) — тишину.
    :meth:`set_source` переключает источник без пересоздания трека/producer, поэтому
    play()/stop()/смена source дешёвые. Поддерживает как PCM-источники, так и Opus
    (декодирует в PCM).

    Чтение источника идёт в отдельном потоке, ``after`` вызывается тоже вне
    event loop (как у upstream, где это поток плеера). Все методы управления
    потокобезопасны и могут вызываться из любого потока, в том числе из ``after``.
    """

    kind = 'audio'

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._playback: Optional[_Playback] = None
        self._paused = False
        self._samples = 0
        self._start: Optional[float] = None
        self._played = 0

    def set_source(
        self,
        source: AudioSource,
        after: Optional[Callable[[Optional[Exception]], Any]] = None,
    ) -> None:
        """Начать проигрывать ``source``; текущий источник (если есть) останавливается."""
        with self._lock:
            old = self._playback
            self._playback = _Playback(source, after, wait_for=old)
            self._paused = False
            self._played = 0
            if old is not None:
                old.stop(cleanup=True)
        if old is not None:
            self._dispatch_after(old, None)

    def replace_source(self, source: AudioSource) -> None:
        """Подменить источник «на лету».

        Без ``cleanup()`` прежнего источника (его может оборачивать новый, как в
        ``vc.source = PCMVolumeTransformer(vc.source)``), с сохранением ``after``,
        паузы и счётчика кадров. Прочитанные вперёд кадры прежнего источника доигрываются
        (теряется максимум кадр, который читался в момент подмены). Новый читатель стартует, только когда прежний вышел из
        своего последнего ``read()``.
        """
        with self._lock:
            old = self._playback
            if old is None:
                raise ValueError('the client is not playing anything')
            # прочитанные вперёд кадры прежнего читателя переходят новому
            self._playback = _Playback(
                source, old.after, wait_for=old, frames=old.queue
            )
            old.stop(cleanup=False)

    @property
    def source(self) -> Optional[AudioSource]:
        pb = self._playback
        return pb.source if pb is not None else None

    @property
    def is_playing(self) -> bool:
        return self._playback is not None and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._playback is not None and self._paused

    @property
    def played_frames(self) -> int:
        """Number of 20 ms frames of the current source that have been sent."""
        return self._played

    def stop_source(self) -> None:
        """Stop the current source and call its ``after`` callback."""
        with self._lock:
            pb = self._playback
        if pb is not None:
            self._end(pb, None)

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        """Ничего не делает: трек живёт дольше одной WebRTC-сессии.

        aiortc при закрытии транспорта сам вызывает ``track.stop()`` у sender'а, а на
        завершённом треке новая сессия уже не сможет сделать ``produce`` ("track ended").
        Трек переносится в новую сессию при смене голосового сервера, поэтому завершает
        его только владелец — через :meth:`end`.
        """

    def end(self) -> None:
        """Окончательно завершить трек (сессия закрывается без переноса трека)."""
        super().stop()

    def reset_clock(self) -> None:
        """Сбросить отсчёт темпа кадров.

        Нужен при переносе трека в новую сессию (смена голосового сервера): пока
        трек никто не читал, расписание ушло в прошлое, и без сброса recv() выдал
        бы накопившиеся кадры пачкой без пауз.
        """
        self._samples = 0
        self._start = None

    def _end(self, pb: _Playback, error: Optional[Exception]) -> None:
        with self._lock:
            if self._playback is not pb:
                return
            self._playback = None
            self._paused = False
            pb.stop(cleanup=True)
        self._dispatch_after(pb, error)

    @staticmethod
    def _dispatch_after(pb: _Playback, error: Optional[Exception]) -> None:
        after = pb.after
        if after is None:
            return

        def run() -> None:
            # after видит уже освобождённый источник (cleanup идёт в потоке-читателе)
            pb.exited.wait(AFTER_JOIN_TIMEOUT)
            try:
                after(error)
            except Exception:
                _log.exception('ошибка в after-callback')

        threading.Thread(target=run, name='lolka-voice-after', daemon=True).start()

    async def recv(self) -> 'av.AudioFrame':
        # Держим реальный темп 20 мс/кадр (иначе aiortc захлебнётся кадрами).
        if self._start is None:
            self._start = time.monotonic()
        target = self._start + (self._samples / SAMPLE_RATE)
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        data = SILENCE
        pb = self._playback
        if pb is not None and not self._paused:
            try:
                item = pb.queue.get_nowait()
            except queue.Empty:
                item = None
            if isinstance(item, _End):
                if item.owner is pb:
                    self._end(pb, item.error)
            elif item is not None:
                data = item
                self._played += 1

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

    Нотификации обрабатываются отдельными задачами: обработчик вправе слать свои
    запросы, ответы на которые читает тот же цикл чтения.
    """

    def __init__(
        self,
        url: str,
        on_notification: Callable[[str, dict], Any],
        on_disconnect: Optional[Callable[[], Any]] = None,
        request_timeout: float = 15.0,
    ) -> None:
        self._url = url
        self._on_notification = on_notification
        self._on_disconnect = on_disconnect
        self._request_timeout = request_timeout
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._reader: Optional[asyncio.Task] = None
        self._tasks: set = set()
        self._closed = False

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._url, heartbeat=15)
        except BaseException:
            await self._session.close()
            raise
        self._reader = asyncio.ensure_future(self._read_loop())

    async def _handle_notification(self, method: Any, data: dict) -> None:
        try:
            await self._on_notification(method, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception('ошибка обработки нотификации %s', method)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        cancelled = False
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
                    task = asyncio.ensure_future(
                        self._handle_notification(
                            obj.get('method'), obj.get('data') or {}
                        )
                    )
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            if not self._closed:
                _log.exception('сигналинг: обрыв чтения')
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError('signaling closed'))
            self._pending.clear()
            if not cancelled and not self._closed and self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:
                    _log.exception('ошибка в on_disconnect сигналинга')

    async def request(
        self, method: str, data: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        assert self._ws is not None
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send_json({'id': rid, 'method': method, 'data': data or {}})
            return await asyncio.wait_for(
                fut, timeout=self._request_timeout if timeout is None else timeout
            )
        finally:
            self._pending.pop(rid, None)

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        if self._reader is not None and self._reader is not current:
            self._reader.cancel()
        try:
            if self._ws is not None:
                await self._ws.close()
        finally:
            if self._session is not None:
                await self._session.close()


class VoiceConnection:
    """Один сеанс WebRTC-войса: сигналинг, транспорты, producer, consumers.

    Инкапсулирует всю низкоуровневую механику; :class:`~lolka.VoiceClient` —
    тонкий фасад поверх этого класса.

    ``on_closed(reason)`` вызывается, когда сессия закончилась не по инициативе
    владельца: ``"kicked"`` (сервер выкинул пира из комнаты — например, при
    переезде в другой канал) или ``"disconnected"`` (оборвался сигналинг). Трек
    исходящего звука при этом остаётся живым — что с ним делать, решает владелец.
    ``on_track_ended(producer_id)`` — producer участника закрылся.
    """

    def __init__(
        self,
        endpoint: str,
        token: str,
        on_receive_track: Optional[Callable] = None,
        out_track: Optional[SourceAudioTrack] = None,
        on_closed: Optional[Callable[[str], Any]] = None,
        on_track_ended: Optional[Callable[[str], Any]] = None,
        request_timeout: float = 15.0,
    ) -> None:
        self._endpoint = endpoint
        self._token = token
        self._on_receive_track = on_receive_track
        self._on_closed = on_closed
        self._on_track_ended = on_track_ended
        self._request_timeout = request_timeout

        self.signaling: Optional[Signaling] = None
        self.device: Optional[Device] = None
        self.send_transport = None
        self.recv_transport = None
        self.producer = None
        # out_track можно передать из прежней сессии (смена голосового сервера) —
        # тогда текущее воспроизведение продолжится в новой.
        self.out_track: Optional[SourceAudioTrack] = out_track
        self._send_connected = asyncio.Event()
        self._recv_connected = asyncio.Event()
        self.consumers: Dict[str, Any] = {}  # producerId -> consumer
        self._consumer_producers: Dict[str, str] = {}  # consumerId -> producerId
        self._consuming: set = set()
        self._closed_producers: set = set()
        self._closed_consumers: set = set()
        self._pending_close: Optional[str] = None
        self._started = False
        self._close_notified = False
        self._closed = False

    def _signaling_url(self) -> str:
        ep = self._endpoint or ''
        if '://' not in ep:
            ep = 'ws://' + ep
        sep = '&' if '?' in ep else '?'
        return f'{ep}{sep}token={self._token}'

    async def _request(self, method: str, data: Optional[dict] = None) -> dict:
        sig = self.signaling
        if sig is None:
            raise ConnectionError('signaling closed')
        return await sig.request(method, data, timeout=self._request_timeout)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError('voice connection was closed while starting')

    async def start(self) -> None:
        try:
            await self._start()
        except BaseException as exc:
            # Любой сбой (в том числе закрытие во время старта): добиваем всё, что
            # успело подняться — сигналинг, транспорты, «призрачный» пир в SFU.
            closed_meanwhile = self._closed
            self._closed = True
            await self._cleanup()
            if closed_meanwhile and isinstance(exc, Exception):
                if not isinstance(exc, RuntimeError):
                    raise RuntimeError(
                        'voice connection was closed while starting'
                    ) from exc
            raise
        self._started = True
        pending, self._pending_close = self._pending_close, None
        if pending is not None:
            self._notify_closed(pending)

    async def _start(self) -> None:
        self.signaling = Signaling(
            self._signaling_url(),
            self._on_notification,
            on_disconnect=lambda: self._notify_closed('disconnected'),
            request_timeout=self._request_timeout,
        )
        await self.signaling.connect()
        self._check_open()

        caps = await self._request('getRouterRtpCapabilities', {})
        self._check_open()
        self.device = Device(handlerFactory=AiortcHandler.createFactory())
        await self.device.load(routerRtpCapabilities=RtpCapabilities(**caps))
        self._check_open()

        await self._create_send_transport()
        self._check_open()
        await self._create_recv_transport()
        self._check_open()

        if self.out_track is None:
            self.out_track = SourceAudioTrack()
        else:
            self.out_track.reset_clock()
        self.producer = await self.send_transport.produce(
            track=self.out_track,
            stopTracks=False,
            appData={'source': 'mic'},
        )
        self._check_open()
        _log.debug('voice: producer создан id=%s', self.producer.id)

        res = await self._request('getProducers', {})
        self._check_open()
        for p in res.get('producers', []):
            await self._consume(p['producerId'], p.get('userId'), p.get('kind'))
            self._check_open()

    async def _create_send_transport(self) -> None:
        params = await self._request('createWebRtcTransport', {'direction': 'send'})
        self._check_open()
        self.send_transport = self.device.createSendTransport(
            id=params['id'],
            iceParameters=IceParameters(**params['iceParameters']),
            iceCandidates=[IceCandidate(**c) for c in params['iceCandidates']],
            dtlsParameters=DtlsParameters(**params['dtlsParameters']),
            sctpParameters=None,
        )

        @self.send_transport.on('connect')
        async def _on_connect(dtlsParameters):
            await self._request(
                'connectTransport',
                {
                    'transportId': self.send_transport.id,
                    'dtlsParameters': dtlsParameters.model_dump(exclude_none=True),
                },
            )
            self._send_connected.set()

        @self.send_transport.on('produce')
        async def _on_produce(kind, rtpParameters, appData):
            # ждём подтверждения connectTransport, иначе produce может уйти раньше
            await self._send_connected.wait()
            res = await self._request(
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
        params = await self._request('createWebRtcTransport', {'direction': 'recv'})
        self._check_open()
        self.recv_transport = self.device.createRecvTransport(
            id=params['id'],
            iceParameters=IceParameters(**params['iceParameters']),
            iceCandidates=[IceCandidate(**c) for c in params['iceCandidates']],
            dtlsParameters=DtlsParameters(**params['dtlsParameters']),
            sctpParameters=None,
        )

        @self.recv_transport.on('connect')
        async def _on_connect(dtlsParameters):
            await self._request(
                'connectTransport',
                {
                    'transportId': self.recv_transport.id,
                    'dtlsParameters': dtlsParameters.model_dump(exclude_none=True),
                },
            )
            self._recv_connected.set()

    async def _consume(self, producer_id: str, user_id: Any, kind: Any) -> None:
        # Бот слушает только голос: видео-продюсеры (камера и т.п.) не потребляем,
        # иначе aiortc зря декодирует видеопоток.
        if kind != 'audio' or not producer_id:
            return
        if (
            self._closed
            or self.recv_transport is None
            or self.device is None
            or producer_id in self.consumers
            or producer_id in self._consuming
        ):
            return
        # регистрируем синхронно, до первого await: producerClosed, пришедший
        # посреди consume, увидит, что producer «в работе»
        self._consuming.add(producer_id)
        try:
            res = await self._request(
                'consume',
                {
                    'transportId': self.recv_transport.id,
                    'producerId': producer_id,
                    'rtpCapabilities': self.device.rtpCapabilities.model_dump(
                        exclude_none=True
                    ),
                },
            )
            if res.get('kind') != 'audio':
                return
            consumer = await self.recv_transport.consume(
                id=res['id'],
                producerId=res['producerId'],
                kind=res['kind'],
                rtpParameters=RtpParameters(**res['rtpParameters']),
            )
            if (
                self._closed
                or producer_id in self._closed_producers
                or consumer.id in self._closed_consumers
            ):
                await consumer.close()
                return
            self.consumers[producer_id] = consumer
            self._consumer_producers[consumer.id] = producer_id
            await self._request('resumeConsumer', {'consumerId': consumer.id})
            if self.consumers.get(producer_id) is not consumer:
                return  # закрыт, пока шёл resume
            _log.debug(
                'voice: consume producer=%s user=%s kind=%s', producer_id, user_id, kind
            )
            if self._on_receive_track is not None:
                try:
                    self._on_receive_track(consumer.track, user_id, producer_id)
                except Exception:
                    _log.exception('ошибка on_receive_track')
        except Exception:
            _log.exception('voice: ошибка consume producer=%s', producer_id)
        finally:
            self._consuming.discard(producer_id)
            self._closed_producers.discard(producer_id)
            if not self._consuming:
                self._closed_consumers.clear()

    async def _drop_consumer(self, producer_id: Optional[str]) -> None:
        """Producer участника закрылся: закрываем consumer, сообщаем владельцу."""
        if not producer_id:
            return
        if producer_id in self._consuming:
            self._closed_producers.add(producer_id)
        consumer = self.consumers.pop(producer_id, None)
        if consumer is None:
            return
        self._consumer_producers.pop(consumer.id, None)
        try:
            await consumer.close()
        except Exception:
            _log.exception('voice: ошибка закрытия consumer producer=%s', producer_id)
        if self._on_track_ended is not None:
            try:
                self._on_track_ended(producer_id)
            except Exception:
                _log.exception('ошибка on_track_ended')

    def _notify_closed(self, reason: str) -> None:
        if self._closed or self._close_notified:
            return
        if not self._started:
            # закрытие во время start(): сообщим владельцу, когда сессия поднимется
            if self._pending_close is None:
                self._pending_close = reason
            return
        self._close_notified = True
        if self._on_closed is not None:
            try:
                self._on_closed(reason)
            except Exception:
                _log.exception('ошибка on_closed')

    async def _on_notification(self, method: str, data: dict) -> None:
        _log.debug('voice: нотификация %s %s', method, data)
        if method == 'newProducer':
            await self._consume(
                data.get('producerId'), data.get('userId'), data.get('kind')
            )
        elif method == 'producerClosed':
            await self._drop_consumer(data.get('producerId'))
        elif method == 'consumerClosed':
            consumer_id = data.get('consumerId')
            producer_id = self._consumer_producers.get(consumer_id)
            if producer_id is None and self._consuming and consumer_id:
                # consumer ещё создаётся (recv_transport.consume): закроем после
                self._closed_consumers.add(consumer_id)
            await self._drop_consumer(producer_id)
        elif method == 'kicked':
            self._notify_closed('kicked')

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Освобождает всё, что успело подняться; безопасно вызывать повторно."""
        consumers = list(self.consumers.values())
        self.consumers.clear()
        self._consumer_producers.clear()
        if self.out_track is not None:
            self.out_track.stop_source()
        for consumer in consumers:
            try:
                await consumer.close()
            except Exception:
                pass
        for tr in (self.send_transport, self.recv_transport):
            if tr is not None:
                try:
                    await tr.close()
                except Exception:
                    pass
        self.send_transport = None
        self.recv_transport = None
        # Трек всё ещё наш (его не забрали в новую сессию) — завершаем окончательно.
        if self.out_track is not None:
            self.out_track.end()
        signaling, self.signaling = self.signaling, None
        if signaling is not None:
            try:
                await signaling.close()
            except Exception:
                pass


async def drain_track(track: MediaStreamTrack) -> None:
    """Read and discard frames until the track ends.

    aiortc keeps every decoded frame of a received track in an unbounded queue
    until it is read; draining keeps memory flat when nobody consumes the track.
    """
    while True:
        try:
            await track.recv()
        except MediaStreamError:
            return
