"""Shared test support: a fake Telegram transport so the real handlers run without network.

Every test case gets its own temporary directory and a freshly imported copy of the module
under test, so tests never share a database.
"""
from __future__ import annotations
import asyncio, datetime, importlib, os, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
import aiogram.types as T
from aiogram.types import Update

TOKEN = '123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
_CURRENT: list = []          # the FakeTelegram of the running test


class FakeTelegram(BaseSession):
    """Records every API call and answers like Telegram would."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []      # everything Telegram accepted
        self.attempted: list[tuple[str, dict]] = []  # every attempt, including failures
        self.recent: list[tuple[str, dict]] = []     # reset by `say`
        self.blocked: set[int] = set()               # users that blocked the bot (Forbidden)
        self.fail_send: set[int] = set()             # users whose messages fail with a generic error
        self.flood_once: set[int] = set()            # users answered with RetryAfter once
        self.no_channel_rights = False               # bot is not an admin of the channel
        self.ban_fails = False
        self.source_gone = False

    async def close(self): pass
    async def stream_content(self, *a, **k): yield b''

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        data = method.model_dump(exclude_none=True)
        self.attempted.append((name, data))               # every attempt, including failed ones
        result = await self._respond(bot, method, name, data)   # raises like Telegram would
        self.calls.append((name, data)); self.recent.append((name, data))   # only what Telegram accepted
        return result

    async def _respond(self, bot, method, name, data):
        chat = data.get('chat_id')
        if name == 'GetMe':
            return T.User(id=bot.id, is_bot=True, first_name='Bot', username='testbot')
        if name == 'BanChatMember' and self.ban_fails:
            raise RuntimeError('not enough rights')
        if name == 'SendMessage' and chat in self.fail_send:
            raise RuntimeError('temporary failure')
        if name in ('SendMessage', 'CopyMessage') and chat in self.blocked:
            raise TelegramForbiddenError(method=method, message='Forbidden: bot was blocked by the user')
        if name == 'CopyMessage':
            if self.source_gone:
                raise TelegramBadRequest(method=method, message='Bad Request: message to copy not found')
            if chat in self.flood_once:
                self.flood_once.discard(chat)
                raise TelegramRetryAfter(method=method, message='Flood control', retry_after=1)
            return T.MessageId(message_id=1)
        if name in ('SendMessage', 'EditMessageText'):
            return T.Message(message_id=len(self.calls), date=datetime.datetime.now(), chat=T.Chat(id=chat or 1, type='private'), text='x')
        if name == 'GetChatMember':
            if self.no_channel_rights:
                return T.ChatMemberMember.model_construct(status='member', user=T.User(id=bot.id, is_bot=True, first_name='b'))
            return T.ChatMemberAdministrator.model_construct(status='administrator', user=T.User(id=bot.id, is_bot=True, first_name='b'), can_invite_users=True, can_restrict_members=True)
        if name == 'CreateChatInviteLink':
            return T.ChatInviteLink(invite_link='https://t.me/+abc', creator=T.User(id=bot.id, is_bot=True, first_name='b'), creates_join_request=False, is_primary=False, is_revoked=False)
        if name in ('SendPhoto', 'SendDocument'):
            return T.Message(message_id=len(self.calls), date=datetime.datetime.now(), chat=T.Chat(id=chat or 1, type='private'))
        return True


_installed = False
def _install_patches():
    global _installed
    if _installed: return
    _installed = True
    original = Bot.__init__
    def init(self, token, *a, **k):
        k.pop('session', None)
        original(self, token, session=_CURRENT[-1], **k)
    Bot.__init__ = init
    async def fake_poll(self, bot, *a, **k):
        _CURRENT[-1].dp, _CURRENT[-1].bot = self, bot
    Dispatcher.start_polling = fake_poll


class BotCase(unittest.IsolatedAsyncioTestCase):
    """Base class: loads `module_name` fresh with a private data directory."""
    module_name = ''
    env: dict = {}

    def load(self):
        for name in ('subscription_bot', 'manager'):
            sys.modules.pop(name, None)
        sys.path[:] = [p for p in sys.path if not p.endswith(('template', 'bot_constructor_v1'))]
        path = ROOT / 'template' if self.module_name == 'subscription_bot' else ROOT
        sys.path.insert(0, str(path))
        os.environ.update(self.env_for(self.tmp))
        return importlib.import_module(self.module_name)

    def env_for(self, tmp: str) -> dict:
        raise NotImplementedError

    async def asyncSetUp(self):
        _install_patches()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.tg = FakeTelegram()
        _CURRENT.append(self.tg)
        self.mod = self.load()
        self._uid = 0

    async def asyncTearDown(self):
        # Stop the bot's background workers before the database directory disappears.
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _CURRENT.pop()
        self._tmp.cleanup()

    # ---- update builders -------------------------------------------------
    def _next(self) -> int:
        self._uid += 1
        return self._uid

    def msg(self, text, user=111):
        n = self._next()
        return Update(update_id=n, message=T.Message(message_id=n, date=datetime.datetime.now(), chat=T.Chat(id=user, type='private'),
                      from_user=T.User(id=user, is_bot=False, first_name=f'U{user}', username=f'u{user}'), text=text))

    def photo(self, user, unique='U1', document=False):
        n = self._next()
        kw = ({'document': T.Document(file_id='F' + unique, file_unique_id=unique)} if document
              else {'photo': [T.PhotoSize(file_id='F' + unique, file_unique_id=unique, width=1, height=1)]})
        return Update(update_id=n, message=T.Message(message_id=n, date=datetime.datetime.now(), chat=T.Chat(id=user, type='private'),
                      from_user=T.User(id=user, is_bot=False, first_name=f'U{user}', username=f'u{user}'), **kw))

    def cb(self, data, user=111):
        n = self._next()
        holder = T.Message(message_id=500 + n, date=datetime.datetime.now(), chat=T.Chat(id=user, type='private'),
                           from_user=T.User(id=1, is_bot=True, first_name='bot'), text='old')
        return Update(update_id=n, callback_query=T.CallbackQuery(id=str(n), from_user=T.User(id=user, is_bot=False, first_name=f'U{user}', username=f'u{user}'),
                      chat_instance='x', message=holder, data=data))

    # ---- driving the dispatcher -------------------------------------------
    async def say(self, update):
        self.tg.recent.clear()
        await self.tg.dp.feed_update(self.tg.bot, update)
        return list(self.tg.recent)

    @staticmethod
    def texts(result):
        return [(d.get('text') or d.get('caption') or '') for n, d in result if n in ('SendMessage', 'EditMessageText', 'SendPhoto', 'SendDocument')]

    @staticmethod
    def alerts(result):
        return [d.get('text') for n, d in result if n == 'AnswerCallbackQuery' and d.get('text')]

    @staticmethod
    def buttons(result):
        out = []
        for n, d in result:
            if n in ('SendMessage', 'EditMessageText', 'SendPhoto', 'SendDocument'):
                markup = d.get('reply_markup') or {}
                for row in markup.get('inline_keyboard', []):
                    out.extend(row)
        return out

    @staticmethod
    def keyboard_texts(result):
        out = []
        for n, d in result:
            for row in (d.get('reply_markup') or {}).get('keyboard', []):
                out.extend(b['text'] for b in row)
        return out

    async def wait_for(self, condition, timeout=8.0, step=0.1):
        """Poll an async or sync predicate until it is truthy (for background workers)."""
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            value = condition()
            if asyncio.iscoroutine(value): value = await value
            if value: return value
            await asyncio.sleep(step)
        self.fail('condition not reached in time')


class TemplateCase(BotCase):
    """Personal subscription bot (template/subscription_bot.py)."""
    module_name = 'subscription_bot'
    admins = '111'
    platform_owners = '111'

    def env_for(self, tmp):
        return {'BOT_TOKEN': TOKEN, 'ADMIN_IDS': self.admins, 'OWNER_IDS': self.admins.split(',')[0],
                'CONSTRUCTOR_OWNER_IDS': self.platform_owners, 'DB_PATH': os.path.join(tmp, 'bot.sqlite3')}

    async def start(self, **overrides):
        """Initialise the DB (so tests can seed it), apply overrides, then run the bot's main()."""
        S = self.mod
        await S.DB.init()
        self.seeded = True
        for key, value in overrides.items(): setattr(S, key, value)
        await S.main()
        return S

    async def seed_basic(self, price='500 ₽', old_price='', description='Описание', methods=True):
        S = self.mod
        await S.DB.init()
        await S.DB.run("INSERT INTO channels(channel_id,title,active) VALUES(-100,'Канал',1)")
        await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title,old_price) VALUES('Месяц',?,?,30,-100,'Канал',?)", (description, price, old_price))
        if methods:
            await S.DB.run("INSERT INTO payment_methods(name,details,active) VALUES('Карта','1234',1)")
            await S.DB.run("INSERT INTO tariff_payment_methods(tariff_id,payment_method_id) VALUES(1,1)")

    async def buy(self, user, tariff=1, method=1, unique='R1'):
        """Walk a buyer through choosing a tariff and uploading a receipt; returns the last result."""
        await self.say(self.msg('/start', user))
        await self.say(self.cb(f'order:new:{tariff}', user))
        await self.say(self.cb(f'order:pay:{tariff}:{method}', user))
        return await self.say(self.photo(user, unique))


class ManagerCase(BotCase):
    """Constructor bot (manager.py)."""
    module_name = 'manager'

    def env_for(self, tmp):
        return {'CONSTRUCTOR_BOT_TOKEN': TOKEN, 'OWNER_IDS': '111', 'CONSTRUCTOR_DB': os.path.join(tmp, 'c.sqlite3'),
                'INSTANCES_DIR': os.path.join(tmp, 'inst')}

    async def start(self):
        M = self.mod
        async def slow_poll(dp, bot, *a, **k):
            self.tg.dp, self.tg.bot = dp, bot
            await asyncio.sleep(3600)
        from aiogram import Dispatcher as D
        self._saved_poll = D.start_polling
        D.start_polling = slow_poll
        self.addCleanup(lambda: setattr(D, 'start_polling', self._saved_poll))
        self.main_task = asyncio.create_task(M.main())
        await self.wait_for(lambda: getattr(self.tg, 'dp', None) is not None, 5)
        self.addCleanup(self.main_task.cancel)
        return M
