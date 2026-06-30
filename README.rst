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
