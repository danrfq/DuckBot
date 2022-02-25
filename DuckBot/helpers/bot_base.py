import datetime
import io
import logging
import os
import re
import traceback
import typing
from collections import defaultdict, deque, namedtuple
from typing import (
    List,
    Optional,
    Any,
    TYPE_CHECKING
)

import aiohttp
import aiohttp.web
import asyncpg
import asyncpraw
import discord
import topgg
from asyncpg import Record
from dotenv import load_dotenv
from asyncdagpi import Client as DagpiClient
from discord.ext import commands, ipc

from DuckBot import errors
from DuckBot.cogs.economy.helper_classes import Wallet
from DuckBot.helpers import constants
from DuckBot.helpers.context import CustomContext
from DuckBot.helpers.helper import LoggingEventsFlags

SimpleMessage = None
if TYPE_CHECKING:
    from ..cogs.moderation.snipe import SimpleMessage

initial_extensions = (
    'jishaku',
)

extensions = ('DuckBot.cogs.beta', 'DuckBot.cogs.logs', 'DuckBot.cogs.economy',
              'DuckBot.cogs.events', 'DuckBot.cogs.fun', 'DuckBot.cogs.guild_config',
              'DuckBot.cogs.hideout', 'DuckBot.cogs.image_manipulation', 'DuckBot.cogs.info',
              'DuckBot.cogs.management', 'DuckBot.cogs.modmail', 'DuckBot.cogs.ipc',
              'DuckBot.cogs.test', 'DuckBot.cogs.utility', 'DuckBot.cogs.moderation')
load_dotenv()


class LoggingConfig:
    __slots__ = ('default', 'message', 'member', 'join_leave', 'voice', 'server')

    def __init__(self, default, message, member, join_leave, voice, server):
        self.default = default
        self.message = message
        self.member = member
        self.join_leave = join_leave
        self.voice = voice
        self.server = server

    def _replace(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class BaseDuck(commands.Bot):
    PRE: tuple = ('db.',)

    def __init__(self) -> None:
        intents = discord.Intents.all()
        # noinspection PyDunderSlots,PyUnresolvedReferences
        intents.typing = False

        super().__init__(
            intents=intents,
            command_prefix=self.get_pre,
            case_insensitive=True,
            activity=discord.Streaming(name="db.help", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            strip_after_prefix=True,
            chunk_guilds_at_startup=False
        )
        self.ipc: ipc.Server = ipc.Server(self, secret_key="testing")
        self.ipc.start()
        self.allowed_mentions = discord.AllowedMentions.none()

        self.reddit = asyncpraw.Reddit(client_id=os.getenv('ASYNC_PRAW_CID'),
                                       client_secret=os.getenv('ASYNC_PRAW_CS'),
                                       user_agent=os.getenv('ASYNC_PRAW_UA'),
                                       username=os.getenv('ASYNC_PRAW_UN'),
                                       password=os.getenv('ASYNC_PRAW_PA'))

        self.owner_id = 349373972103561218

        # noinspection PyProtectedMember
        self._BotBase__cogs = commands.core._CaseInsensitiveDict()

        # Bot based stuff
        self.invite_url = "https://discord.com/api/oauth2/authorize?client_id=788278464474120202&permissions=8&scope" \
                          "=bot%20applications.commands "
        self.vote_top_gg = "https://top.gg/bot/788278464474120202"
        self.vote_bots_gg = "https://discord.bots.gg/bots/788278464474120202"
        self.repo = "https://github.com/LeoCx1000/discord-bots"
        self.maintenance = None
        self.noprefix = False
        self.persistent_views_added = False
        self.uptime = self.last_rall = datetime.datetime.utcnow()
        self.session: aiohttp.ClientSession = None  # type: ignore
        self.top_gg = topgg.DBLClient(self, os.getenv('TOPGG_TOKEN'))
        self.dev_mode = True if os.getenv('DEV_MODE') == 'yes' else False
        self.dagpi_cooldown = commands.CooldownMapping.from_cooldown(60, 60, commands.BucketType.default)
        self.dagpi_client = DagpiClient(os.getenv('DAGPI_TOKEN'))
        self.constants = constants

        # Cache stuff
        self.invites = None
        self.prefixes = {}
        self.blacklist = {}
        self.afk_users = {}
        self.auto_un_afk = {}
        self.welcome_channels = {}
        self.suggestion_channels = {}
        self.dm_webhooks = defaultdict(str)
        self.wallets: typing.Dict[Wallet] = {}
        self.counting_channels = {}
        self.counting_rewards = {}
        self.saved_messages = {}
        self.common_discrims = []
        self.log_channels: typing.Dict[int, LoggingConfig] = {}
        self.log_cache = defaultdict(lambda: defaultdict(list))
        self.guild_loggings: typing.Dict[int, LoggingEventsFlags] = {}
        self.snipes: typing.Dict[int, typing.Deque[SimpleMessage]] = defaultdict(lambda: deque(maxlen=50))

        self.global_mapping = commands.CooldownMapping.from_cooldown(10, 12, commands.BucketType.user)
        self.db: asyncpg.Pool = self.loop.run_until_complete(self.create_db_pool())
        self.loop.run_until_complete(self.load_cogs())
        self.loop.run_until_complete(self.populate_cache())

    def _load_extension(self, name: str) -> None:
        try:
            logging.info(f'Attempting to load {name}')
            self.load_extension(name)
        except Exception as e:
            logging.error(f'Failed to load extension {name}', exc_info=e)

    async def load_cogs(self) -> None:
        for ext in initial_extensions:
            self._load_extension(ext)
        for ext in extensions:
            self._load_extension(ext)

    async def get_pre(self, bot, message: discord.Message, raw_prefix: Optional[bool] = False) -> List[str]:
        if not message:
            return commands.when_mentioned_or(*self.PRE)(bot, message) if not raw_prefix else self.PRE
        if not message.guild:
            return commands.when_mentioned_or(*self.PRE)(bot, message) if not raw_prefix else self.PRE
        try:
            prefix = self.prefixes[message.guild.id]
        except KeyError:
            prefix = [x['prefix'] for x in
                      await bot.db.fetch('SELECT prefix FROM pre WHERE guild_id = $1', message.guild.id)] or self.PRE
            self.prefixes[message.guild.id] = prefix

        should_noprefix = False
        if not message.content.startswith(('jishaku', 'eval', 'jsk', 'ev', 'rall', 'dev', 'rmsg')):
            pass
        elif not message.guild:
            should_noprefix = True
        elif not message.guild.get_member(788278464474120202):
            should_noprefix = True

        if await bot.is_owner(message.author) and (bot.noprefix is True or should_noprefix):
            return commands.when_mentioned_or(*prefix, "")(bot, message) if not raw_prefix else prefix
        return commands.when_mentioned_or(*prefix)(bot, message) if not raw_prefix else prefix

    async def fetch_prefixes(self, message):
        prefixes = [x['prefix'] for x in
                    await self.db.fetch('SELECT prefix FROM pre WHERE guild_id = $1', message.guild.id)]
        if not prefixes:
            await self.db.execute('INSERT INTO pre (guild_id, prefix) VALUES ($1, $2)', message.guild.id, self.PRE[0])
            return tuple(await self.fetch_prefixes(message))
        return tuple(prefixes)

    async def get_context(self, message, *, cls=CustomContext):
        return await super().get_context(message, cls=cls)

    async def on_ready(self) -> None:
        logging.info("======[ BOT ONLINE! ]=======")
        logging.info("\033[42mLogged in as " + self.user.name + "\033[0m")

    async def on_message(self, message: discord.Message) -> None:
        await self.wait_until_ready()
        if self.user:
            if re.fullmatch(rf"<@!?{self.user.id}>", message.content):
                prefix = await self.get_pre(self, message, raw_prefix=True)
                if isinstance(prefix, str):
                    await message.reply(f"For a list of commands do `{prefix}help` 💞")
                elif isinstance(prefix, (tuple, list)):
                    await message.reply(
                        f"My prefixes here are `{'`, `'.join(prefix[0:10])}`\n"
                        f"For a list of commands do`{prefix[0]}help` 💞"[0:2000])
        await self.process_commands(message)

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        traceback_string = traceback.format_exc()
        for line in traceback_string.split('\n'):
            logging.info(line)
        await self.wait_until_ready()
        error_channel = self.get_channel(880181130408636456)
        to_send = f"```yaml\nAn error occurred in an {event_method} event``````py" \
                  f"\n{traceback_string}\n```"
        if len(to_send) < 2000:
            try:
                await error_channel.send(to_send)

            except (discord.Forbidden, discord.HTTPException):

                await error_channel.send(f"```yaml\nAn error occurred in an {event_method} event``````py",
                                         file=discord.File(io.StringIO(traceback_string),  # type: ignore
                                                           filename='traceback.py'))
        else:
            await error_channel.send(f"```yaml\nAn error occurred in an {event_method} event``````py",
                                     file=discord.File(io.StringIO(traceback_string),  # type: ignore
                                                       filename='traceback.py'))

    async def populate_cache(self):
        _temp_prefixes = defaultdict(list)
        for x in await self.db.fetch('SELECT * FROM pre'):
            _temp_prefixes[x['guild_id']].append(x['prefix'] or self.PRE)
        self.prefixes = dict(_temp_prefixes)

        async def _populate_guild_cache():
            await self.wait_until_ready()
            for guild in self.guilds:
                try:
                    self.prefixes[guild.id]
                except KeyError:
                    self.prefixes[guild.id] = self.PRE

        self.loop.create_task(_populate_guild_cache())

        values = await self.db.fetch("SELECT user_id, is_blacklisted FROM blacklist")
        for value in values:
            self.blacklist[value['user_id']] = (value['is_blacklisted'] or False)

        values = await self.db.fetch("SELECT guild_id, welcome_channel FROM prefixes")
        for value in values:
            self.welcome_channels[value['guild_id']] = (value['welcome_channel'] or None)

        self.afk_users = dict(
            [(r['user_id'], True) for r in (await self.db.fetch('SELECT user_id, start_time FROM afk')) if
             r['start_time']])
        self.auto_un_afk = dict(
            [(r['user_id'], r['auto_un_afk']) for r in (await self.db.fetch('SELECT user_id, auto_un_afk FROM afk')) if
             r['auto_un_afk'] is not None])
        self.suggestion_channels = dict([(r['channel_id'], r['image_only']) for r in
                                         (await self.db.fetch('SELECT channel_id, image_only FROM suggestions'))])
        self.counting_channels = dict((x['guild_id'], {'channel': x['channel_id'],
                                                       'number': x['current_number'],
                                                       'last_counter': x['last_counter'],
                                                       'delete_messages': x['delete_messages'],
                                                       'reset': x['reset_on_fail'],
                                                       'last_message_id': None,
                                                       'messages': deque(maxlen=100)})
                                      for x in await self.db.fetch('SELECT * FROM count_settings'))

        for x in await self.db.fetch('SELECT * FROM counting'):
            try:
                self.counting_rewards[x['guild_id']].add(x['reward_number'])
            except KeyError:
                self.counting_rewards[x['guild_id']] = {x['reward_number']}

        for entry in await self.db.fetch('SELECT * FROM log_channels'):
            guild_id = entry['guild_id']
            await self.db.execute('INSERT INTO logging_events(guild_id) VALUES ($1) ON CONFLICT (guild_id) DO NOTHING',
                                  entry['guild_id'])

            self.log_channels[guild_id] = LoggingConfig(default=entry['default_channel'],
                                                        message=entry['message_channel'],
                                                        join_leave=entry['join_leave_channel'],
                                                        member=entry['member_channel'],
                                                        voice=entry['voice_channel'],
                                                        server=entry['server_channel'])

            flags = dict(await self.db.fetchrow(
                'SELECT message_delete, message_purge, message_edit, member_join, member_leave, member_update, user_ban, user_unban, '
                'user_update, invite_create, invite_delete, voice_join, voice_leave, voice_move, voice_mod, emoji_create, emoji_delete, '
                'emoji_update, sticker_create, sticker_delete, sticker_update, server_update, stage_open, stage_close, channel_create, '
                'channel_delete, channel_edit, role_create, role_delete, role_edit FROM logging_events WHERE guild_id = $1',
                guild_id))
            self.guild_loggings[guild_id] = LoggingEventsFlags(**flags)

        logging.info('All cache populated successfully')
        self.dispatch('cache_ready')

    async def start(self, *args, **kwargs):
        self.session = aiohttp.ClientSession()
        await super().start(*args, **kwargs)

    async def close(self):
        await self.db.close()
        await self.session.close()
        await super().close()

    async def create_db_pool(self) -> asyncpg.Pool:
        credentials = {
            "user": f"{os.getenv('PSQL_USER')}",
            "password": f"{os.getenv('PSQL_PASSWORD')}",
            "database": f"{os.getenv('PSQL_DB')}",
            "host": f"{os.getenv('PSQL_HOST')}",
            "port": f"{os.getenv('PSQL_PORT')}"
        }
        try:
            return await asyncpg.create_pool(**credentials)
        except Exception as e:
            logging.error("Could not create database pool", exc_info=e)
        finally:
            self.dispatch('pool_create')
            logging.info('Database successful.')