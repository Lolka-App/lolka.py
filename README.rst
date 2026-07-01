lolka.py
========

**Fork of** `discord.py <https://github.com/Rapptz/discord.py>`_ **(MIT License, © 2015-present Rapptz).**

``lolka.py`` (imported as ``import lolka``) is a Python wrapper for the **lolka** bot API. It targets
lolka by default — no manual REST/Gateway overrides needed — decodes lolka snowflakes, and resolves
lolka CDN asset URLs. See ``LICENSE`` for licensing (the original MIT copyright is retained).

Installing
----------

**Python 3.8 or higher is required**

.. code:: sh

    # Linux/macOS
    python3 -m pip install -U lolka.py

    # Windows
    py -3 -m pip install -U lolka.py

Quick Example
-------------

.. code:: py

    import lolka

    class MyClient(lolka.Client):
        async def on_ready(self):
            print('Logged on as', self.user)

        async def on_message(self, message):
            if message.author == self.user:
                return

            if message.content == 'ping':
                await message.channel.send('pong')

    intents = lolka.Intents.default()
    intents.message_content = True
    client = MyClient(intents=intents)
    client.run('token')

Bot Example
~~~~~~~~~~~

.. code:: py

    import lolka
    from lolka.ext import commands

    intents = lolka.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='>', intents=intents)

    @bot.command()
    async def ping(ctx):
        await ctx.send('pong')

    bot.run('token')

Voice
-----

Voice works over WebRTC (lolka's voice stack), not Discord's UDP transport.
The required dependencies ship with the base install — nothing extra to add.

The API stays discord.py-compatible — ``await channel.connect()`` returns a
``VoiceClient`` that speaks lolka's WebRTC voice under the hood:

.. code:: py

    vc = await channel.connect()
    vc.play(lolka.FFmpegPCMAudio('song.mp3'))

    # receive/record other participants' audio (optional):
    def on_track(track, user_id, producer_id):
        ...  # track is an aiortc MediaStreamTrack
    vc.on_receive_track = on_track

    ...
    await vc.disconnect()

Migrating from discord.py
--------------------------

Already have a discord.py bot? You don't have to rewrite it — just alias the import:

.. code:: py

    import lolka as discord
    from lolka.ext import commands

    # everything below is unmodified discord.py bot code
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix='>', intents=intents)

    @bot.command()
    async def ping(ctx):
        await ctx.send('pong')

    bot.run('token')

``import lolka as discord`` covers every ``discord.Whatever`` attribute access in your existing code
(``discord.Client``, ``discord.Intents``, ``discord.Embed``, checks, errors, and so on). The only
lines that actually need editing are explicit submodule imports, since Python resolves those by
real module path rather than by the alias:

- ``import discord`` → ``import lolka as discord``
- ``from discord import X`` → ``from lolka import X``
- ``from discord.ext import commands`` → ``from lolka.ext import commands``
- ``from discord import app_commands`` → ``from lolka import app_commands``

That's it — the rest of your bot's code stays exactly as it is.
