import asyncio, os, signal, stat, sys, time
from pathlib import Path

from support import ManagerCase


STUB = "import time\ntime.sleep(3600)\n"


class Constructor(ManagerCase):
    def add_bot(self, bot_id=5001, owner=111, username='mybot', name='Shop'):
        os.makedirs(os.path.join(self.tmp, 'inst', f'b{bot_id}'), exist_ok=True)
        con = self.mod.db()
        con.execute("INSERT INTO bots(bot_id,owner_id,username,first_name,container_name,instance_dir,status) VALUES(?,?,?,?,?,?,'stopped')",
                    (bot_id, owner, username, name, f'c{bot_id}', os.path.join(self.tmp, 'inst', f'b{bot_id}')))
        con.commit(); con.close()

    async def test_menus(self):
        await self.start()
        owner = self.keyboard_texts(await self.say(self.msg('/start', 111)))
        self.assertIn('📊 Статистика', owner); self.assertIn('💳 Оплата лицензий', owner)
        for gone in ('👥 Пользователи', 'ℹ️ Инструкция', '📊 Сводка'): self.assertNotIn(gone, owner)
        user = self.keyboard_texts(await self.say(self.msg('/start', 222)))
        self.assertIn('ℹ️ Инструкция', user)                                      # regular users keep the instruction button
        self.assertIn('Нет доступа', ' '.join(self.texts(await self.say(self.msg('📊 Статистика', 222)))))

    async def test_my_bots_are_buttons_that_open_a_card_without_status(self):
        await self.start()
        self.assertIn('нет подключённых ботов', ' '.join(self.texts(await self.say(self.msg('🤖 Мои боты')))))
        self.add_bot(5001); self.add_bot(5002, username='', name='Без юзернейма')
        r = await self.say(self.msg('🤖 Мои боты'))
        self.assertEqual({b['callback_data'] for b in self.buttons(r)}, {'b:open:5001', 'b:open:5002'})
        card = await self.say(self.cb('b:open:5001'))
        self.assertEqual(self.texts(card)[0], '🤖 Shop (@mybot)\nID: 5001')
        labels = [b['text'] for b in self.buttons(card)]
        self.assertFalse(any('Статус' in x for x in labels)); self.assertIn('⬅️ К списку ботов', labels)
        self.assertEqual(self.texts(await self.say(self.cb('b:open:5002')))[0], '🤖 Без юзернейма\nID: 5002')   # no "@нет username"
        back = await self.say(self.cb('b:list:0')); self.assertEqual(len(self.buttons(back)), 2)
        self.assertTrue(self.alerts(await self.say(self.cb('b:open:5001', 222))))                             # somebody else's bot
        self.assertIn('Отменено', ' '.join(self.alerts(await self.say(self.cb('b:cancel:0')))))

    async def test_statistics_and_legacy_buttons(self):
        await self.start()
        r = await self.say(self.msg('📊 Статистика'))
        labels = [b['text'] for b in self.buttons(r)]
        self.assertIn('📊 Статистика конструктора', self.texts(r)[0]); self.assertNotIn('↻ Обновить сводку', labels)
        self.assertIn('👥 Пользователи и боты', labels)
        self.assertIn('adm:dashboard', [b['callback_data'] for b in self.buttons(await self.say(self.cb('adm:users:0')))])
        for old in ('👥 Пользователи', '📊 Сводка'):
            r = await self.say(self.msg(old)); self.assertIn('Меню обновлено', self.texts(r)[0]); self.assertIn('Статистика конструктора', self.texts(r)[1])
        self.assertEqual(await self.say(self.msg('📊 Сводка', 222)), [])        # only the owner gets the legacy answer

    async def test_double_sent_token_creates_exactly_one_bot(self):
        M = await self.start()
        stub = Path(self.tmp) / 'subscription_bot.py'; stub.write_text(STUB); M.TEMPLATE_SCRIPT = stub
        token = '777777:' + 'A' * 30
        await self.say(self.msg('➕ Создать бота'))
        self.tg.recent.clear()
        await asyncio.gather(self.tg.dp.feed_update(self.tg.bot, self.msg(token)), self.tg.dp.feed_update(self.tg.bot, self.msg(token)))
        con = M.db(); rows = con.execute('SELECT bot_id,owner_id FROM bots').fetchall(); con.close()
        self.assertEqual([tuple(r) for r in rows], [(777777, 111)])
        inst = M.INSTANCES / M.safe_name(111, 777777)
        self.assertTrue((inst / '.env').exists()); self.assertEqual(M.instance_status(inst), 'active')
        mode = stat.S_IMODE(os.stat(inst / '.env').st_mode)
        self.assertEqual(mode, 0o600)                                              # the token file is private
        self.assertIn('Этот бот уже подключён', ' '.join(self.texts(self.tg.recent)))
        M.stop_instance(inst)

    async def test_license_expiry_warning_reaches_the_owner(self):
        M = self.mod
        inst = M.INSTANCES / 'b1'; (inst / 'data').mkdir(parents=True)
        import sqlite3, datetime
        tdb = sqlite3.connect(inst / 'data' / 'subscriptions.sqlite3'); tdb.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)).isoformat()
        tdb.executemany('INSERT INTO settings VALUES(?,?)', [('license_active', '1'), ('license_expires_at', expiry)]); tdb.commit(); tdb.close()
        con = M.db(); con.execute("INSERT INTO bots(bot_id,owner_id,username,first_name,container_name,instance_dir,status) VALUES(5001,222,'',?,'c1',?,'stopped')", ('Shop', str(inst))); con.commit(); con.close()
        await self.start()
        await self.wait_for(lambda: [1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'До окончания лицензии' in (d.get('text') or '')])
        text = [d['text'] for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111][0]
        self.assertIn('Статистика → Пользователи и боты', text); self.assertIn('Shop', text)


class Processes(ManagerCase):
    """Real subprocesses: crashes must be noticed and a recycled PID must never be killed."""

    async def prepare(self):
        M = self.mod
        stub = Path(self.tmp) / 'subscription_bot.py'; stub.write_text(STUB); M.TEMPLATE_SCRIPT = stub
        inst = Path(self.tmp) / 'inst'; inst.mkdir(); (inst / '.env').write_text('BOT_TOKEN=x\n')
        self.addCleanup(lambda: M.stop_instance(inst))
        return M, inst

    async def test_a_crashed_bot_is_reported_as_stopped_and_can_be_restarted(self):
        M, inst = await self.prepare()
        M.start_instance('n', inst / '.env', inst / 'data'); pid = M.read_pid(inst)
        self.assertEqual(M.instance_status(inst), 'active')
        os.kill(pid, signal.SIGKILL); time.sleep(0.4)
        self.assertEqual(M.instance_status(inst), 'stopped')                      # not a zombie that still looks alive
        M.start_instance('n', inst / '.env', inst / 'data')
        self.assertEqual(M.instance_status(inst), 'active')
        M.restart_instance('n', inst, inst / '.env', inst / 'data'); self.assertNotEqual(M.read_pid(inst), pid)
        self.assertEqual(M.instance_status(inst), 'active')
        self.assertTrue(M.stop_instance(inst)); self.assertEqual(M.instance_status(inst), 'stopped')

    async def test_a_recycled_pid_is_never_signalled(self):
        M, inst = await self.prepare()
        (inst / 'bot.pid').write_text(str(os.getpid()))                          # the PID now belongs to this unrelated process
        self.assertEqual(M.instance_status(inst), 'stopped')
        self.assertTrue(M.stop_instance(inst))                                    # and we are still alive to assert it
