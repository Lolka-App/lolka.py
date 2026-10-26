import datetime
import inspect
import pathlib
import re
from unittest import mock

import pytest

import lolka
from lolka import utils
from lolka.ext import commands
from lolka import asset as asset_mod
from lolka.asset import Asset
from lolka.channel import DMChannel, GroupChannel, PartialMessageable
import lolka.ext.commands
from lolka.ext.commands import converter as conv
from lolka.invite import Invite
from lolka.member import Member
from lolka.message import MessageInteraction, MessageInteractionMetadata, MessageReference, PartialMessage
from lolka.role import Role
from lolka.soundboard import BaseSoundboardSound
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


# --- ассеты, ссылки, инвайты, роли, мемберы -----------------------------------------------

CDN = asset_mod.LOLKA_CDN
WEB = utils.LOLKA_WEB


def _asset(factory, *args):
    return getattr(Asset, factory)(mock.MagicMock(), *args)


@pytest.mark.parametrize(
    'key, url, animated',
    [
        ('avatars/1/abc.webp', f'{CDN}/avatars/1/abc.webp', False),
        ('/avatars/1/abc_animated.webp', f'{CDN}/avatars/1/abc_animated.webp', True),
        (f'{CDN}/avatars/1/abc.webp', f'{CDN}/avatars/1/abc.webp', False),
        (f'{CDN}/avatars/1/abc_animated.webp?x=1', f'{CDN}/avatars/1/abc_animated.webp?x=1', True),
        ('https://example.com/a.gif', 'https://example.com/a.gif', True),
        ('https://example.com/a.apng', 'https://example.com/a.apng', True),
    ],
)
def test_asset_resolver(key, url, animated):
    a = _asset('_from_avatar', 1, key)
    assert a.url == url
    assert a.is_animated() is animated


@pytest.mark.parametrize(
    'factory, args',
    [
        ('_from_avatar_decoration', ('decorations/a.webp',)),
        ('_from_app_icon', (1, 'app-icons/x.webp', 'icon')),
        ('_from_cover_image', (1, 'app-assets/x.webp')),
        ('_from_scheduled_event_cover_image', (1, 'guild-events/x.webp')),
        ('_from_sticker_banner', ('stickers/x.webp',)),
        ('_from_primary_guild', (1, 'guild-tags/x.webp')),
        ('_from_user_collectible', ('collectibles/x.webp',)),
        ('_from_guild_avatar', (1, 2, 'guild-avatars/x.webp')),
        ('_from_guild_banner', (1, 2, 'guild-banners/x.webp')),
        ('_from_icon', (1, 'guild-icons/x.webp', 'guild')),
        ('_from_guild_image', (1, 'guild-images/x.webp', 'splashes')),
        ('_from_guild_icon', (1, 'guild-icons/x.webp')),
        ('_from_user_banner', (1, 'banners/x.webp')),
    ],
)
def test_asset_factories_accept_keys_and_urls(factory, args):
    key = args[[i for i, v in enumerate(args) if isinstance(v, str) and '/' in v][0]]
    assert _asset(factory, *args).url == f'{CDN}/{key}'
    full = f'{CDN}/{key}'
    full_args = tuple(full if a == key else a for a in args)
    assert _asset(factory, *full_args).url == full


def test_avatar_decoration_backend_format():
    url = f'{CDN}/cosmetics/123/static'
    a = _asset('_from_avatar_decoration', url)
    assert a.url == url
    assert not a.is_animated()


def test_avatar_decoration_full_url():
    a = _asset('_from_avatar_decoration', f'{CDN}/decorations/x_animated.webp')
    assert a.url == f'{CDN}/decorations/x_animated.webp'
    assert a.is_animated()
    assert not _asset('_from_avatar_decoration', f'{CDN}/decorations/x.webp').is_animated()


def test_asset_base_is_lolka_cdn():
    assert Asset.BASE == CDN


def test_asset_modifiers_keep_url():
    animated = _asset('_from_avatar', 1, 'avatars/1/abc_animated.webp')
    static = _asset('_from_avatar', 1, 'avatars/1/abc.webp')
    for a in (animated, static):
        url = a.url
        assert a.with_size(256).url == url
        assert a.with_format('webp').url == url
        assert a.with_static_format('png').url == url
        assert a.replace(size=64, format='webp', static_format='png').url == url
    assert animated.with_format('gif').url == animated.url
    with pytest.raises(ValueError):
        static.with_format('gif')
    with pytest.raises(ValueError):
        static.replace(format='gif')
    with pytest.raises(ValueError):
        static.with_static_format('gif')  # type: ignore
    for bad in (15, 100, 8192):
        with pytest.raises(ValueError):
            static.with_size(bad)
        with pytest.raises(ValueError):
            static.replace(size=bad)


def test_emoji_and_sound_urls():
    for animated in (False, True):
        emoji = lolka.PartialEmoji(name='x', id=123, animated=animated)
        assert emoji.url == f'{CDN}/emojis/123.webp'
    sound = object.__new__(BaseSoundboardSound)
    sound.id = 5
    assert sound.url == f'{CDN}/soundboard/5.ogg'


@pytest.mark.parametrize('payload', [{}, {'colors': None}, {'color': 255}, {'colors': {'primary_color': 7}}])
def test_role_update_without_colors(payload):
    role = object.__new__(Role)
    role._update({'name': 'r', 'id': '1', **payload})  # type: ignore
    expected = payload.get('colors', {}) and payload['colors'].get('primary_color') or payload.get('color', 0)
    assert role._colour == expected


def test_channel_jump_urls():
    guild = mock.MagicMock()
    guild.id = 10
    ch = object.__new__(lolka.TextChannel)
    ch.id = 20
    ch.guild = guild
    assert ch.jump_url == f'{WEB}/servers/10/channels/20'
    for cls in (DMChannel, GroupChannel):
        dm = object.__new__(cls)
        dm.id = 30
        assert dm.jump_url == f'{WEB}/me/channels/30'
    pm = object.__new__(PartialMessageable)
    pm.id = 40
    pm.guild_id = None
    assert pm.jump_url == f'{WEB}/me/channels/40'
    pm.guild_id = 10
    assert pm.jump_url == f'{WEB}/servers/10/channels/40'


def test_message_jump_urls():
    ref = MessageReference(message_id=3, channel_id=2, guild_id=1)
    assert ref.jump_url == f'{WEB}/servers/1/channels/2/3'
    ref = MessageReference(message_id=3, channel_id=2)
    assert ref.jump_url == f'{WEB}/me/channels/2/3'
    msg = object.__new__(PartialMessage)
    msg.id = 3
    msg.channel = mock.MagicMock(id=2, guild_id=None)
    msg.guild = mock.MagicMock(id=1)
    assert msg.jump_url == f'{WEB}/servers/1/channels/2/3'
    msg.guild = None
    assert msg.jump_url == f'{WEB}/me/channels/2/3'


def test_invite_url_and_resolve():
    inv = object.__new__(Invite)
    inv.code = 'abc'
    inv.scheduled_event_id = None
    assert inv.url == f'{utils.LOLKA_INVITE}/abc'
    assert utils.resolve_invite(inv) == ('abc', None)
    assert utils.resolve_invite(inv.url) == ('abc', None)
    inv.scheduled_event_id = 5
    assert inv.url == f'{utils.LOLKA_INVITE}/abc?event=5'
    assert utils.resolve_invite(inv.url) == ('abc', 5)


@pytest.mark.parametrize(
    'invite_base, web_base',
    [('https://dev.lolka.gg', 'http://localhost:5173'), ('http://localhost:5173/i', 'https://web.example.org')],
)
def test_invite_roundtrip_custom_hosts(monkeypatch, invite_base, web_base):
    monkeypatch.setattr(utils, 'LOLKA_INVITE', invite_base)
    monkeypatch.setattr(utils, 'LOLKA_WEB', web_base)
    inv = object.__new__(Invite)
    inv.code = 'abc'
    inv.scheduled_event_id = 3
    assert inv.url.startswith(invite_base)
    assert utils.resolve_invite(inv.url) == ('abc', 3)
    assert utils.resolve_invite(f'{web_base}/i/xyz') == ('xyz', None)


@pytest.mark.parametrize(
    'value, code, event',
    [
        ('https://lolka.gg/abc', 'abc', None),
        ('http://lolka.gg/abc?event=7', 'abc', 7),
        ('lolka.gg/abc', 'abc', None),
        ('https://lolka.app/i/abc', 'abc', None),
        ('lolka.app/i/abc?event=9', 'abc', 9),
        ('abc-DEF_1', 'abc-DEF_1', None),
        ('https://lolka.gg/abc/', 'abc', None),
        ('HTTPS://LOLKA.GG/abc', 'abc', None),
    ],
)
def test_resolve_invite(value, code, event):
    assert utils.resolve_invite(value) == (code, event)


def test_resolve_invite_rejects_garbage():
    with pytest.raises(ValueError):
        utils.resolve_invite('https://example.com/abc')


def test_oauth_url():
    url = utils.oauth_url(123, scopes=['bot'])
    assert url == f'{WEB}/bot-authorize?client_id=123&scope=bot'
    assert utils.oauth_url(123, disable_guild_select=True).endswith('&disable_guild_select=true')


def _member_data(**extra):
    return {
        'roles': [],
        'user': {'id': '1', 'username': 'u', 'discriminator': '0', 'avatar': None},
        **extra,
    }


def test_member_without_flags():
    member = Member(data=_member_data(), guild=mock.MagicMock(), state=mock.MagicMock())  # type: ignore
    assert member._flags == 0


def test_member_update_from_message_and_decoration():
    deco_user = {'asset': f'{CDN}/decorations/user.webp', 'sku_id': '5'}
    deco_member = {'asset': f'{CDN}/decorations/member_animated.webp', 'sku_id': '6'}
    data = _member_data()
    data['user']['avatar_decoration_data'] = deco_user
    state = mock.MagicMock()
    state.store_user.side_effect = lambda payload, **kw: lolka.User(state=state, data=payload)
    member = Member(data=data, guild=mock.MagicMock(), state=state)  # type: ignore
    assert member.avatar_decoration.url == deco_user['asset']
    assert member.avatar_decoration_sku_id == 5

    member._update_from_message(_member_data(avatar='avatars/1/a.webp', avatar_decoration_data=deco_member))  # type: ignore
    assert member._avatar == 'avatars/1/a.webp'
    assert member.avatar_decoration.url == deco_member['asset']
    assert member.avatar_decoration.is_animated()
    assert member.avatar_decoration_sku_id == 6

    member._update_from_message(_member_data())  # type: ignore
    assert member._avatar == 'avatars/1/a.webp'
    assert member._avatar_decoration_data == deco_member

    member._update_from_message(_member_data(avatar=None, avatar_decoration_data=None))  # type: ignore
    assert member._avatar is None
    assert member.avatar_decoration.url == deco_user['asset']


# (файл относительно lolka/, подстрока строки): осознанные упоминания Discord
DISCORD_ALLOWED = (
    ('template.py', 'discord.new'),
    ('widget.py', 'discord.com/api/guilds'),
    ('http.py', 'а не в discord.com'),
    ('client.py', 'discord.new'),
    ('ext/commands/converter.py', 'discord(?:app)?\\.com/channels/'),
)


def test_no_discord_urls_in_package():
    root = pathlib.Path(lolka.__file__).parent
    pattern = re.compile(r'discord(?:\(\?:app\)\?)?\\?\.(?:com|gg|net|media|new)|discordapp')
    offenders = []
    for path in root.rglob('*.py'):
        rel = path.relative_to(root).as_posix()
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if pattern.search(line) and not any(rel == f and sub in line for f, sub in DISCORD_ALLOWED):
                offenders.append(f'{rel}:{lineno}: {line.strip()}')
    assert not offenders, '\n'.join(offenders)


def test_converters_parse_lolka_links():
    ctx = mock.MagicMock()
    ctx.channel.id = 1
    parse = conv.PartialMessageConverter._get_id_matches
    guild_ch = object.__new__(lolka.TextChannel)
    guild_ch.id = 222222222222222
    guild_ch.guild = mock.MagicMock(id=111111111111111)
    msg = object.__new__(PartialMessage)
    msg.id = 333333333333333
    msg.channel = guild_ch
    msg.guild = guild_ch.guild
    assert parse(ctx, msg.jump_url) == (111111111111111, 333333333333333, 222222222222222)
    dm = object.__new__(DMChannel)
    dm.id = 444444444444444
    msg.channel = dm
    msg.guild = None
    ctx.guild = mock.MagicMock(id=5)
    assert parse(ctx, msg.jump_url) == (None, 333333333333333, 444444444444444)
    assert parse(ctx, msg.jump_url + '/') == (None, 333333333333333, 444444444444444)
    discord_link = 'https://discord.com/channels/111111111111111/222222222222222/333333333333333'
    assert parse(ctx, discord_link) == (111111111111111, 333333333333333, 222222222222222)
    with pytest.raises(conv.MessageNotFound):
        parse(ctx, f'{WEB}/servers/111111111111111/channels/222222222222222')


def test_guild_channel_converter_parses_lolka_links(monkeypatch):
    ch = object.__new__(lolka.TextChannel)
    ch.id = 222222222222222
    ch.guild = mock.MagicMock(id=111111111111111)
    match = conv.GuildChannelConverter._parse_from_url(ch.jump_url)
    assert match is not None and int(match.group(1)) == ch.id
    dm = object.__new__(DMChannel)
    dm.id = 444444444444444
    match = conv.GuildChannelConverter._parse_from_url(dm.jump_url + '/333333333333333')
    assert match is not None and int(match.group(1)) == dm.id
    monkeypatch.setattr(utils, 'LOLKA_WEB', 'http://localhost:5173')
    match = conv.GuildChannelConverter._parse_from_url(ch.jump_url)
    assert match is not None and int(match.group(1)) == ch.id


def test_partial_message_jump_url_uncached_guild():
    state = mock.MagicMock()
    state._get_guild.return_value = None
    pm = PartialMessageable(state=state, id=222222222222222, guild_id=111111111111111)
    msg = PartialMessage(channel=pm, id=333333333333333)
    assert msg.jump_url == f'{WEB}/servers/111111111111111/channels/222222222222222/333333333333333'


def test_converter_exports_are_intact():
    assert 'datetime' not in vars(lolka.ext.commands)
    assert 'PartialMessageConverter' in conv.__all__
    assert '_lolka_link_regex' not in conv.__all__


def _reply_message(channel, ref):
    data = {
        'id': '333333333333333',
        'channel_id': str(channel.id),
        'type': 19,
        'content': 'x',
        'author': {'id': '777777777777777', 'username': 'u', 'discriminator': '0', 'avatar': None},
        'attachments': [],
        'embeds': [],
        'mentions': [],
        'mention_roles': [],
        'pinned': False,
        'mention_everyone': False,
        'tts': False,
        'timestamp': '2026-01-01T00:00:00+00:00',
        'edited_timestamp': None,
        'message_reference': ref,
    }
    return lolka.Message(state=channel._state, channel=channel, data=data)


@pytest.mark.parametrize('cached', [True, False])
@pytest.mark.parametrize(
    'ref, in_guild, expected_guild',
    [
        ({'type': 0, 'message_id': '1' * 15, 'channel_id': '222222222222222'}, True, 111111111111111),
        ({'type': 1, 'message_id': '1' * 15, 'channel_id': '999999999999999'}, True, None),
        (
            {'type': 1, 'message_id': '1' * 15, 'channel_id': '999999999999999', 'guild_id': '888888888888888'},
            True,
            888888888888888,
        ),
        ({'type': 0, 'message_id': '1' * 15, 'channel_id': '555555555555555'}, False, None),
    ],
)
def test_message_reference_guild_fallback(ref, in_guild, expected_guild, cached):
    state = mock.MagicMock()
    guild = mock.MagicMock(spec=lolka.Guild)
    guild.id = 111111111111111
    state._get_guild.return_value = guild if cached and in_guild else None
    if in_guild:
        channel = PartialMessageable(state=state, id=222222222222222, guild_id=111111111111111)
    else:
        channel = PartialMessageable(state=state, id=555555555555555)
    message = _reply_message(channel, ref)
    assert message.reference.guild_id == expected_guild
