import asyncio
from datetime import datetime, timedelta, timezone

from support import TemplateCase

now = datetime.now(timezone.utc)
iso = lambda delta: (now + delta).isoformat()


class Expiry(TemplateCase):
    async def seed(self):
        S = self.mod
        await self.seed_basic(methods=False)
        for u in (77, 88): await S.DB.run("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)", (u, 'u', 'U'))
        ins = "INSERT INTO subscriptions(user_id,tariff_id,channel_id,expires_at,status) VALUES(?,1,-100,?,'active')"
        await S.DB.run(ins, (77, iso(-timedelta(hours=1))))                      # only an expired subscription
        await S.DB.run(ins, (88, iso(-timedelta(hours=1)))); await S.DB.run(ins, (88, iso(timedelta(days=5))))   # expired + still valid

    async def test_access_is_closed_only_when_nothing_else_is_valid(self):
        await self.seed(); await self.start(CHECK_INTERVAL=1)
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM subscriptions WHERE user_id=88 AND status='expired'", one=True))
        bans = [d['user_id'] for n, d in self.tg.calls if n == 'BanChatMember']
        self.assertEqual(bans, [77])                                              # user 88 keeps the channel
        self.assertTrue([1 for n, d in self.tg.calls if n == 'UnbanChatMember' and d['user_id'] == 77])
        texts = {d['chat_id']: d['text'] for n, d in self.tg.calls if n == 'SendMessage' and '⏰' in (d.get('text') or '')}
        self.assertIn('других активных подписок', texts[77]); self.assertIn('Доступ к каналу сохраняется', texts[88])

    async def test_admin_is_told_once_when_access_cannot_be_closed(self):
        await self.seed(); self.tg.ban_fails = True
        await self.start(CHECK_INTERVAL=1)
        await self.wait_for(lambda: [1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'Не удалось закрыть доступ' in (d.get('text') or '')])
        await asyncio.sleep(2.5)       # the worker keeps retrying, but the admin is not spammed
        alerts = [1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'Не удалось закрыть доступ' in (d.get('text') or '')]
        self.assertEqual(len(alerts), 1)


class Reminders(TemplateCase):
    async def seed(self):
        S = self.mod
        await self.seed_basic(methods=False)
        await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title,active) VALUES('Короткий','',?,2,-100,'Канал',1)", ('50 ₽',))
        await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title,active) VALUES('Старый','',?,30,-100,'Канал',0)", ('50 ₽',))
        for u in (11, 12, 13, 14, 15, 16, 17): await S.DB.run("INSERT OR IGNORE INTO users(user_id,username,first_name) VALUES(?,?,?)", (u, 'u', 'U'))
        ins = "INSERT INTO subscriptions(user_id,tariff_id,channel_id,expires_at,status) VALUES(?,?,-100,?,'active')"
        await S.DB.run(ins, (11, 1, iso(timedelta(days=2, hours=12))))          # 3-day reminder
        await S.DB.run(ins, (12, 1, iso(timedelta(hours=20))))                  # 1-day reminder
        await S.DB.run(ins, (13, 2, iso(timedelta(hours=40))))                  # short tariff: no 3-day reminder
        await S.DB.run(ins, (14, 1, iso(timedelta(days=2))))                    # blocked the bot
        await S.DB.run(ins, (15, 1, iso(timedelta(days=2))))                    # temporary send failure
        await S.DB.run(ins, (16, 1, iso(timedelta(days=20))))                   # far from expiry
        await S.DB.run(ins, (17, 3, iso(timedelta(days=2))))                    # tariff is no longer sold
        self.tg.blocked.add(14); self.tg.fail_send.add(15)

    def reminded(self):
        """Users who actually received a reminder (Telegram accepted the message)."""
        return sorted(d['chat_id'] for n, d in self.tg.calls if n == 'SendMessage' and 'заканчивается' in (d.get('text') or ''))

    def attempts(self, user):
        """How often the bot tried to remind this user, including failed attempts."""
        return len([1 for n, d in self.tg.attempted if n == 'SendMessage' and d['chat_id'] == user and 'заканчивается' in (d.get('text') or '')])

    async def test_reminders_follow_the_rules(self):
        await self.seed(); await self.start(REMINDER_INTERVAL=1)
        await self.wait_for(lambda: self.reminded() == [11, 12, 17])
        self.assertEqual(self.reminded(), [11, 12, 17])                         # not 13 (short tariff) and not 16 (too early)
        texts = {d['chat_id']: d for n, d in self.tg.calls if n == 'SendMessage' and 'заканчивается' in (d.get('text') or '')}
        self.assertIn('меньше суток', texts[12]['text'])
        def target(user): return texts[user]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.assertEqual(target(11), 'order:new:1'); self.assertEqual(target(17), 'order:back')   # unsold tariff -> tariff list
        # a blocked user is tried once and never again; a temporary failure is retried
        await asyncio.sleep(2.5); self.assertEqual(self.attempts(14), 1); self.assertGreaterEqual(self.attempts(15), 2)
        self.tg.fail_send.clear()
        await self.wait_for(lambda: 15 in self.reminded())
        before = len(self.reminded()); await asyncio.sleep(2.5)
        self.assertEqual(len(self.reminded()), before)                          # each reminder is sent once

    async def test_next_milestone_and_renewal_get_a_new_reminder(self):
        await self.seed(); await self.start(REMINDER_INTERVAL=1)
        await self.wait_for(lambda: 11 in self.reminded())
        first = self.attempts(11)
        await self.mod.DB.run("UPDATE subscriptions SET expires_at=? WHERE user_id=11", (iso(timedelta(hours=10)),))      # now inside the last day
        await self.wait_for(lambda: self.attempts(11) == first + 1)
        await self.mod.DB.run("UPDATE subscriptions SET expires_at=? WHERE user_id=11", (iso(timedelta(days=2, hours=2)),))   # renewed and expiring again
        await self.wait_for(lambda: self.attempts(11) == first + 2)

    async def test_admin_can_switch_reminders_off(self):
        await self.seed(); await self.start(REMINDER_INTERVAL=1)
        await self.wait_for(lambda: 12 in self.reminded())
        labels = [b['text'] for b in self.buttons(await self.say(self.msg('📊 Статистика')))]
        self.assertIn('🔔 Напоминания о продлении: вкл', labels)
        r = await self.say(self.cb('stats:reminders')); self.assertIn('выключены', ' '.join(self.alerts(r)))
        self.assertEqual(await self.mod.DB.setting('reminders_enabled'), '0')
        await self.mod.DB.run("UPDATE subscriptions SET expires_at=? WHERE user_id=12", (iso(timedelta(hours=5)),))
        before = self.attempts(12); await asyncio.sleep(2.5)
        self.assertEqual(self.attempts(12), before)
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('stats:reminders', 222)))))
