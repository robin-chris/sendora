import datetime
from datetime import timedelta, timezone

from support import TemplateCase


class Parsing(TemplateCase):
    async def test_duration_and_percent_parsing(self):
        S = self.mod
        for text, minutes in (('36', 2160), ('12ч', 720), ('2 д', 2880), ('1,5 часа', 90), ('24 hours', 1440), ('7 days', 10080)):
            self.assertEqual(S.parse_duration(text), minutes, text)
        for bad in ('', 'abc', '0', '30 мин', '400 д', '12 недель', '-5'):
            self.assertIsNone(S.parse_duration(bad), bad)
        self.assertEqual(S.parse_percent('-50'), -50); self.assertEqual(S.parse_percent('+15%'), 15); self.assertEqual(S.parse_percent('−7,5'), -7.5)
        for bad in ('0', '-95', '501', 'много'):
            self.assertIsNone(S.parse_percent(bad), bad)
        self.assertEqual(S.format_minutes(1440), '1 дн.'); self.assertEqual(S.format_minutes(90), '1 ч 30 мин'); self.assertEqual(S.format_minutes(720), '12 ч')


class Promotions(TemplateCase):
    async def tariff(self, tid=1):
        return await self.mod.DB.run('SELECT * FROM tariffs WHERE id=?', (tid,), one=True)

    async def launch(self, pct=-50, minutes=1440, tid=1):
        await self.say(self.cb(f'promo:start:{tid}')); await self.say(self.cb(f'promo:pct:{pct}'))
        preview = self.texts(await self.say(self.cb(f'promo:dur:{minutes}')))[0]
        await self.say(self.cb('promo:go'))
        return preview

    async def test_discount_is_applied_shown_and_reverted_by_the_timer(self):
        await self.seed_basic(); await self.start(PROMO_INTERVAL=1)
        screen = await self.say(self.cb('promo:start:1'))
        self.assertIn('promo:pct:-50', [b['callback_data'] for b in self.buttons(screen)])
        duration = await self.say(self.cb('promo:pct:-50'))
        self.assertIn('promo:dur:1440', [b['callback_data'] for b in self.buttons(duration)])
        preview = self.texts(await self.say(self.cb('promo:dur:1440')))[0]
        self.assertIn('500 ₽ → 250 ₽', preview); self.assertIn('вернётся автоматически', preview)
        started = await self.say(self.cb('promo:go'))
        self.assertIn('Акция запущена', ' '.join(self.texts(started)))
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('250 ₽', '500 ₽'))
        card = self.texts(await self.say(self.cb('tariff:edit:1')))[0]
        self.assertIn('⏳ Акция до', card); self.assertIn('500 ₽', card)
        buttons = [b['callback_data'] for b in self.buttons(await self.say(self.cb('tariff:edit:1')))]
        self.assertIn('promo:end:1', buttons); self.assertNotIn('promo:start:1', buttons)
        self.assertIn('⏳ Акция до', self.texts(await self.say(self.cb('order:new:1', 900)))[0])
        self.assertIn('⏳ Акция до', self.texts(await self.say(self.msg('💳 Тарифы', 900)))[0])
        # the timer runs out
        past = (datetime.datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await self.mod.DB.run("UPDATE price_promos SET ends_at=? WHERE tariff_id=1", (past,))
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM price_promos WHERE status='done'", one=True))
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('500 ₽', ''))
        alert = [d['text'] for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'Акция закончилась' in (d.get('text') or '')]
        self.assertTrue(alert); self.assertIn('250 ₽ → 500 ₽', alert[0])

    async def test_original_old_price_is_restored_and_new_promo_does_not_compound(self):
        await self.seed_basic(old_price='700 ₽'); await self.start(PROMO_INTERVAL=1)
        await self.launch(-50)
        self.assertEqual((await self.tariff())['price'], '250 ₽')
        preview = await self.launch(-20)             # replaces the running promotion, starting from the original price
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('400 ₽', '500 ₽'))
        rows = await self.mod.DB.run('SELECT status FROM price_promos ORDER BY id', all=True)
        self.assertEqual([r['status'] for r in rows], ['replaced', 'active'])
        await self.say(self.cb('promo:end:1'))
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('500 ₽', '700 ₽'))   # both restored

    async def test_old_price_toggle_off_keeps_the_original_old_price(self):
        await self.seed_basic(old_price='700 ₽'); await self.start()
        await self.say(self.cb('promo:start:1')); await self.say(self.cb('promo:pct:-30'))
        preview = await self.say(self.cb('promo:dur:360'))
        self.assertIn('ВКЛ', self.buttons(preview)[0]['text'])            # a discount shows the old price by default
        r = await self.say(self.cb('promo:old')); self.assertIn('ВЫКЛ', self.buttons(r)[0]['text'])
        await self.say(self.cb('promo:go'))
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('350 ₽', '700 ₽'))

    async def test_manual_price_change_cancels_the_timer(self):
        await self.seed_basic(); await self.start(PROMO_INTERVAL=1)
        await self.launch(-50)
        await self.say(self.cb('edit:price:1')); r = await self.say(self.msg('300 ₽'))
        self.assertIn('Акция по таймеру отменена', ' '.join(self.texts(r)))
        self.assertEqual((await self.mod.DB.run('SELECT status FROM price_promos', one=True))['status'], 'cancelled')
        past = (datetime.datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await self.mod.DB.run("UPDATE price_promos SET ends_at=?", (past,))
        import asyncio; await asyncio.sleep(2.5)
        self.assertEqual((await self.tariff())['price'], '300 ₽')       # not overwritten

    async def test_price_edited_outside_the_bot_is_not_overwritten(self):
        await self.seed_basic(); await self.start(PROMO_INTERVAL=1)
        await self.launch(-50)
        await self.mod.DB.run("UPDATE tariffs SET price='111 ₽' WHERE id=1")
        past = (datetime.datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        await self.mod.DB.run("UPDATE price_promos SET ends_at=?", (past,))
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM price_promos WHERE status='superseded'", one=True))
        self.assertEqual((await self.tariff())['price'], '111 ₽')

    async def test_promotion_survives_a_restart(self):
        await self.seed_basic(); S = self.mod
        past = (datetime.datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        await S.DB.run("UPDATE tariffs SET price='250 ₽', old_price='500 ₽' WHERE id=1")
        await S.DB.run("INSERT INTO price_promos(tariff_id,original_price,original_old_price,promo_price,percent,ends_at) VALUES(1,'500 ₽','','250 ₽',-50,?)", (past,))
        await self.start(PROMO_INTERVAL=1)          # the bot was "down" while the promotion ended
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM price_promos WHERE status='done'", one=True))
        t = await self.tariff(); self.assertEqual((t['price'], t['old_price']), ('500 ₽', ''))

    async def test_custom_percent_and_duration_are_validated(self):
        await self.seed_basic(); await self.start()
        await self.say(self.cb('promo:start:1')); await self.say(self.cb('promo:pctc'))
        for bad in ('abc', '0', '-95'): self.assertIn('Введи число', ' '.join(self.texts(await self.say(self.msg(bad)))))
        r = await self.say(self.msg('-25'))
        self.assertIn('promo:dur:360', [b['callback_data'] for b in self.buttons(r) if b.get('callback_data')])
        await self.say(self.cb('promo:durc'))
        for bad in ('abc', '0', '30 мин', '999 д'): self.assertIn('Не понял срок', ' '.join(self.texts(await self.say(self.msg(bad)))))
        preview = self.texts(await self.say(self.msg('36')))[0]
        self.assertIn('500 ₽ → 375 ₽', preview); self.assertIn('1 дн.', self.mod.format_minutes(1440)); self.assertIn('36 ч', preview)
        await self.say(self.cb('promo:go')); self.assertEqual((await self.tariff())['price'], '375 ₽')

    async def test_cancel_returns_to_the_card_and_changes_nothing(self):
        await self.seed_basic(); await self.start()
        await self.say(self.cb('promo:start:1')); await self.say(self.cb('promo:pct:-50'))
        r = await self.say(self.cb('promo:cancel'))
        self.assertIn('Карточка тарифа', ' '.join(self.texts(r)))
        self.assertEqual((await self.tariff())['price'], '500 ₽')
        self.assertEqual(await self.mod.DB.run('SELECT * FROM price_promos', all=True), [])

    async def test_bulk_promotion_skips_tariffs_without_a_number(self):
        S = self.mod; await S.DB.init()
        await S.DB.run("INSERT INTO channels(channel_id,title,active) VALUES(-100,'К',1)")
        for name, price in (('A', '500 ₽'), ('B', '1 000 ₽'), ('C', 'Бесплатно')):
            await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title) VALUES(?,?,?,30,-100,'К')", (name, 'd', price))
        await self.start()
        await self.say(self.cb('bulk:start'))
        for i in (1, 2, 3): await self.say(self.cb(f'bulk:t:{i}'))
        await self.say(self.cb('bulk:next')); await self.say(self.cb('bulk:a:promo')); await self.say(self.cb('promo:pct:-20'))
        preview = self.texts(await self.say(self.cb('promo:dur:4320')))[0]
        self.assertIn('Тарифов в акции: 2 из 3', preview); self.assertIn('Пропущено', preview)
        done = self.texts(await self.say(self.cb('promo:go')))
        self.assertIn('Акция запущена на тарифах: 2', ' '.join(done))
        prices = [t['price'] for t in await S.DB.run('SELECT price FROM tariffs ORDER BY id', all=True)]
        self.assertEqual(prices, ['400 ₽', '800 ₽', 'Бесплатно'])
        self.assertEqual(len(await S.DB.run("SELECT * FROM price_promos WHERE status='active'", all=True)), 2)

    async def test_bulk_price_edit_cancels_running_timers(self):
        await self.seed_basic(); await self.start(); await self.launch(-50)
        await self.say(self.cb('bulk:start')); await self.say(self.cb('bulk:t:1')); await self.say(self.cb('bulk:next'))
        await self.say(self.cb('bulk:a:price')); await self.say(self.msg('300 ₽')); await self.say(self.cb('bulk:apply'))
        self.assertEqual((await self.mod.DB.run('SELECT status FROM price_promos', one=True))['status'], 'cancelled')

    async def test_guards(self):
        await self.seed_basic(); await self.start()
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('promo:start:1', 222)))))
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('promo:end:1', 222)))))
        self.assertIn('Сессия устарела', ' '.join(self.alerts(await self.say(self.cb('promo:go')))))
        self.assertIn('Акции по этому тарифу уже нет', ' '.join(self.alerts(await self.say(self.cb('promo:end:1')))))
