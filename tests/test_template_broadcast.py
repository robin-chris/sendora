import asyncio

from support import TemplateCase
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
iso = lambda delta: (now + delta).isoformat()


class Broadcasts(TemplateCase):
    async def users(self, ids):
        for u in ids:
            await self.mod.DB.run("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)", (u, f'u{u}', f'U{u}'))

    async def seed_groups(self):
        S = self.mod
        await self.seed_basic(methods=False)
        await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title) VALUES('Второй','d','100 ₽',30,-100,'Канал')")
        await self.users([11, 12, 13, 14, 15, 111])
        ins = "INSERT INTO subscriptions(user_id,tariff_id,channel_id,expires_at,status) VALUES(?,?,-100,?,?)"
        await S.DB.run(ins, (11, 1, iso(timedelta(days=5)), 'active')); await S.DB.run(ins, (12, 1, iso(timedelta(days=5)), 'active'))
        await S.DB.run(ins, (13, 2, iso(timedelta(days=5)), 'active')); await S.DB.run(ins, (14, 1, iso(-timedelta(days=5)), 'expired'))
        await S.DB.run(ins, (111, 1, iso(timedelta(days=5)), 'active'))           # an admin with a subscription is excluded from groups

    async def launch(self, segment, text='Привет'):
        await self.say(self.msg('📨 Рассылка')); await self.say(self.cb(segment)); await self.say(self.msg(text))
        return await self.say(self.cb('broadcast:confirm'))

    async def copies(self):
        # the admin's own copy is the preview shown before sending: it is not a recipient
        return sorted(d['chat_id'] for n, d in self.tg.calls if n == 'CopyMessage' and d['chat_id'] != 111)

    async def test_groups_show_recipient_counts(self):
        await self.seed_groups(); await self.start()
        labels = [b['text'] for b in self.buttons(await self.say(self.msg('📨 Рассылка')))]
        for expected in ('👥 Все пользователи (6)', '🟢 Активные подписчики (3)', '⌛ Истёкшие подписки (1)', '🆕 Без подписки (1)'):
            self.assertIn(expected, labels)
        tariffs = [b['text'] for b in self.buttons(await self.say(self.cb('bc:tariffs')))]
        self.assertIn('Месяц (2)', tariffs); self.assertIn('Второй (1)', tariffs)

    async def test_each_group_reaches_only_its_members(self):
        await self.seed_groups(); await self.start()
        await self.launch('bc:seg:active')
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM broadcast_jobs WHERE status='done'", one=True))
        self.assertEqual(await self.copies(), [11, 12, 13])
        self.tg.calls.clear(); await self.launch('bc:seg:expired'); await self.wait_for(lambda: self.copies())
        self.assertEqual(await self.copies(), [14])
        self.tg.calls.clear(); await self.launch('bc:t:2'); await self.wait_for(lambda: self.copies())
        self.assertEqual(await self.copies(), [13])

    async def test_empty_group_cannot_be_chosen_and_cancel_works(self):
        await self.seed_groups(); await self.mod.DB.run("DELETE FROM users WHERE user_id=15"); await self.start()
        await self.mod.DB.run("DELETE FROM subscriptions WHERE status='expired'")
        await self.say(self.msg('📨 Рассылка'))
        self.assertIn('нет получателей', ' '.join(self.alerts(await self.say(self.cb('bc:seg:expired')))))
        await self.say(self.cb('bc:seg:all')); self.assertIn('отменена', ' '.join(self.texts(await self.say(self.msg('/cancel')))))
        self.assertIn('отменена', ' '.join(self.texts(await self.say(self.cb('bc:exit')))))
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('bc:seg:all', 222)))))

    async def test_blocked_users_and_flood_limits(self):
        await self.seed_basic(methods=False); await self.users(range(1000, 1030)); await self.start()
        self.tg.blocked.update({1005, 1006}); self.tg.flood_once.add(1010)
        r = await self.launch('bc:seg:all')
        progress = [d for n, d in r if n == 'SendMessage'][0]
        self.assertIn('bcj:stop:1', [b['callback_data'] for row in progress['reply_markup']['inline_keyboard'] for b in row])
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM broadcast_jobs WHERE status='done'", one=True), timeout=15)
        counts = {c['status']: c['n'] for c in await self.mod.DB.run('SELECT status,COUNT(*) AS n FROM broadcast_recipients GROUP BY status', all=True)}
        self.assertEqual(counts, {'sent': 28, 'failed': 2})
        self.assertIn(1010, await self.copies())                       # the flood-limited user was retried, not dropped
        self.assertTrue([1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'завершена' in (d.get('text') or '')])

    async def test_admin_can_stop_a_running_broadcast(self):
        await self.seed_basic(methods=False); await self.users(range(2000, 2200)); await self.start()
        await self.launch('bc:seg:all')
        await self.wait_for(lambda: len([1 for n, d in self.tg.calls if n == 'CopyMessage']) >= 5)
        r = await self.say(self.cb('bcj:stop:1'))
        self.assertIn('остановлена', ' '.join(self.alerts(r)))
        sent_at_stop = len([1 for n, d in self.tg.calls if n == 'CopyMessage']); await asyncio.sleep(1.5)
        self.assertLessEqual(len([1 for n, d in self.tg.calls if n == 'CopyMessage']) - sent_at_stop, 1)
        self.assertEqual((await self.mod.DB.run('SELECT status FROM broadcast_jobs', one=True))['status'], 'stopped')
        self.assertIn('уже завершена', ' '.join(self.alerts(await self.say(self.cb('bcj:stop:1')))))
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('bcj:stop:1', 222)))))

    async def test_unfinished_broadcast_resumes_after_restart(self):
        S = self.mod; await S.DB.init(); await self.users([1000, 1001, 1002])
        await S.DB.run("INSERT INTO broadcast_jobs(created_by,source_chat_id,source_message_id,label,total) VALUES(111,111,5,'Восстановленная',3)")
        for u in (1000, 1001, 1002): await S.DB.run("INSERT INTO broadcast_recipients(job_id,user_id) VALUES(1,?)", (u,))
        await self.start()
        await self.wait_for(lambda: S.DB.run("SELECT 1 AS n FROM broadcast_jobs WHERE status='done'", one=True))
        self.assertEqual(await self.copies(), [1000, 1001, 1002])

    async def test_deleted_source_message_stops_the_job_and_warns(self):
        S = self.mod; await S.DB.init(); await self.users([2001, 2002])
        await self.start(); self.tg.source_gone = True
        await S.DB.run("INSERT INTO broadcast_jobs(created_by,source_chat_id,source_message_id,label,total) VALUES(111,111,9,'Удалённое',2)")
        for u in (2001, 2002): await S.DB.run("INSERT INTO broadcast_recipients(job_id,user_id) VALUES(1,?)", (u,))
        await self.wait_for(lambda: S.DB.run("SELECT 1 AS n FROM broadcast_jobs WHERE status='stopped'", one=True), timeout=10)
        await self.wait_for(lambda: [1 for n, d in self.tg.calls if n == 'SendMessage' and 'исходное сообщение удалено' in (d.get('text') or '')])

    async def test_history_lists_recent_jobs(self):
        await self.seed_groups(); await self.start()
        await self.launch('bc:seg:active'); await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM broadcast_jobs WHERE status='done'", one=True))
        await self.say(self.msg('📨 Рассылка')); text = ' '.join(self.texts(await self.say(self.cb('bc:history'))))
        self.assertIn('№1', text); self.assertIn('завершена', text); self.assertIn('отправлено 3 из 3', text)
