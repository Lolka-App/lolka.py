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
Opus sources (``FFmpegOpusAudio``) need the system libopus (``apt install libopus0``; bundled on Windows);
PCM sources (``FFmpegPCMAudio``) don't.

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

The track passed to ``on_receive_track`` only delivers frames from the moment it was handed over and
keeps just the latest one, so read it (``await track.recv()`` in a loop until ``MediaStreamError``)
or call ``track.stop()`` if you do not need it. Only audio producers are consumed (video is ignored).

``after`` callbacks of ``vc.play()`` run in a helper thread, after ``source.cleanup()``, so they may
block or call ``vc.play()`` again; use ``asyncio.run_coroutine_threadsafe(coro, vc.loop)`` to run a
coroutine from them. ``after`` may arrive up to 2 seconds late if the source's ``read()`` hangs.

If the voice connection is lost (the voice server restarted, the network dropped), the client disconnects
and leaves the channel; ``reconnect=True`` is accepted for compatibility but reconnecting is up to the bot.

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
