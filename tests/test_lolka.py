import datetime
import inspect
from unittest import mock

import pytest

import lolka
from lolka import utils
from lolka.ext import commands
from lolka.ext.commands import converter as conv
from lolka.message import MessageInteraction, MessageInteractionMetadata
from lolka.template import Template

DISCORD_ID = 175928847299117063
DISCORD_DT = datetime.datetime(2016, 4, 30, 11, 18, 25, 796000, tzinfo=datetime.timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'model, expected',
    [(lolka.Member, commands.MemberConverter), (lolka.User, commands.UserConverter)],
)
async def test_model_annotation_uses_converter(model, expected):
    assert conv.CONVERTER_MAPPING[model] is expected
    param = inspect.Parameter('member', inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=model)
    convert = mock.AsyncMock(return_value='ok')
    with mock.patch.object(expected, 'convert', convert):
        result = await conv._actual_conversion(mock.MagicMock(), model, '123', param)
    assert result == 'ok'
    convert.assert_awaited_once()


def test_escape_mentions_15_digit():
    assert utils.escape_mentions('<@123456789012345>') == '<@​123456789012345>'
    assert utils.escape_mentions('@everyone') == '@​everyone'


def test_message_interaction_created_at():
    for cls in (MessageInteraction, MessageInteractionMetadata):
        obj = object.__new__(cls)
        obj.id = DISCORD_ID
        assert obj.created_at == DISCORD_DT


def test_template_url():
    tpl = object.__new__(Template)
    tpl.code = 'abc'
    assert tpl.url == 'https://discord.new/abc'


def test_snowflake_roundtrip():
    assert utils.LOLKA_SNOWFLAKE_SHIFT == 14
    assert utils.DISCORD_EPOCH == 1735689600000
    dt = datetime.datetime(2025, 6, 1, 12, 0, 0, 123000, tzinfo=datetime.timezone.utc)
    sf = utils.time_snowflake(dt)
    assert sf == int(dt.timestamp() * 1000 - 1735689600000) << 14
    assert utils.snowflake_time(sf) == dt
    assert utils.time_snowflake(dt, high=True) - sf == 2**14 - 1


@pytest.mark.parametrize(
    'id, expected',
    [(0, 3), (1, 4), (123, 5), (7500000000000001, 0), (1234567890123456, 5)],
)
def test_default_avatar_index(id, expected):
    assert utils._lolka_default_avatar_index(id) == expected


WEBHOOK_TOKEN = 'a' * 64


def _webhook_classes_with_sessions():
    requests = pytest.importorskip('requests')
    return ((lolka.Webhook, mock.MagicMock()), (lolka.SyncWebhook, requests.Session()))


@pytest.mark.parametrize(
    'url, webhook_id',
    [
        (f'https://lolka.app/api/webhooks/123456789012345/{WEBHOOK_TOKEN}', 123456789012345),
        (f'http://localhost/api/webhooks/123456789012345/{WEBHOOK_TOKEN}', 123456789012345),
        (f'https://discord.com/api/webhooks/175928847299117063/{WEBHOOK_TOKEN}', 175928847299117063),
        (f'https://discordapp.com/api/webhooks/175928847299117063/{WEBHOOK_TOKEN}', 175928847299117063),
    ],
)
def test_webhook_from_url_accepts_any_host(url, webhook_id):
    for cls, session in _webhook_classes_with_sessions():
        hook = cls.from_url(url, session=session)
        assert hook.id == webhook_id
        assert hook.token == WEBHOOK_TOKEN


def test_webhook_from_url_rejects_garbage():
    for cls, session in _webhook_classes_with_sessions():
        with pytest.raises(ValueError):
            cls.from_url('https://lolka.app/api/bot/v1/webhooks/123', session=session)


@pytest.mark.parametrize(
    'base, origin',
    [
        ('https://lolka.app/api/bot', 'https://lolka.app'),
        ('http://localhost:8080/api/bot', 'http://localhost:8080'),
    ],
)
def test_webhook_url_uses_lolka_origin(monkeypatch, base, origin):
    from lolka import http

    monkeypatch.setattr(http, 'LOLKA_API_BASE', base)
    for cls, session in _webhook_classes_with_sessions():
        hook = cls.partial(123456789012345, WEBHOOK_TOKEN, session=session)
        assert hook.url == f'{origin}/api/webhooks/123456789012345/{WEBHOOK_TOKEN}'
