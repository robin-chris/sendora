import asyncio

from support import TemplateCase


class MenusAndStats(TemplateCase):
    async def test_admin_menu_has_no_settings_or_orders_buttons(self):
        await self.seed_basic(); await self.start()
        keys = self.keyboard_texts(await self.say(self.msg('/start')))
        self.assertIn('📊 Статистика', keys); self.assertIn('💳 Оплата', keys)
        for gone in ('⚙️ Настройки', '🧾 Заявки на оплату', '📊 Сводка'):
            self.assertNotIn(gone, keys)

    async def test_statistics_has_no_refresh_and_ignores_unfinished_purchases(self):
        await self.seed_basic(); await self.start()
        await self.mod.DB.run("INSERT INTO orders(user_id,tariff_id,status,amount) VALUES(900,1,'awaiting_proof',500)")  # draft
        result = await self.say(self.msg('📊 Статистика'))
        self.assertIn('Покупок на проверке: 0', self.texts(result)[0])
        labels = [b['text'] for b in self.buttons(result)]
        self.assertNotIn('↻ Обновить', labels)
        self.assertIn('👥 Подписчики', labels)

    async def test_old_keyboard_buttons_still_work_after_update(self):
        await self.seed_basic(); await self.start()
        r = await self.say(self.msg('📊 Сводка'))
        self.assertIn('Меню обновлено.', self.texts(r)[0]); self.assertIn('СТАТИСТИКА', self.texts(r)[1])
        r = await self.say(self.msg('⚙️ Настройки')); self.assertIn('Оплата', self.texts(r)[1])
        r = await self.say(self.msg('🧾 Заявки на оплату')); self.assertIn('Покупок на проверке нет', ' '.join(self.texts(r)))
        self.assertEqual(await self.say(self.msg('📊 Сводка', user=333)), [])   # buyers are ignored


class LicenseEntry(TemplateCase):
    admins = '111,222'

    async def test_only_platform_owner_sees_license_management(self):
        await self.seed_basic(); await self.start()
        owner = [b['text'] for b in self.buttons(await self.say(self.msg('💳 Оплата', 111)))]
        other = [b['text'] for b in self.buttons(await self.say(self.msg('💳 Оплата', 222)))]
        self.assertIn('⚙️ Управление лицензией', owner); self.assertNotIn('⚙️ Управление лицензией', other)
        self.assertIn('Только владелец', ' '.join(self.alerts(await self.say(self.cb('owner:settings', 222)))))
        self.assertIn('📈 Оборот и лицензия', ' '.join(self.texts(await self.say(self.cb('owner:settings', 111)))))


class Purchases(TemplateCase):
    async def test_receipt_is_a_new_purchase_and_can_be_approved_once(self):
        await self.seed_basic(); await self.start()
        r = await self.buy(333)
        caption = [d['caption'] for n, d in r if n == 'SendPhoto'][0]
        self.assertTrue(caption.startswith('🛍 Новая покупка'))
        self.assertNotIn('Заявка', caption); self.assertNotIn('№', caption)
        self.assertEqual([d['chat_id'] for n, d in r if n == 'SendPhoto'], [111])
        r = await self.say(self.cb('order:approve:1'))
        self.assertIn('Покупка подтверждена', ' '.join(self.texts(r)))
        subs = await self.mod.DB.run('SELECT user_id,status FROM subscriptions', all=True)
        self.assertEqual([(s['user_id'], s['status']) for s in subs], [(333, 'active')])
        self.assertTrue(any(d['chat_id'] == 333 and 'Персональные ссылки' in d['text'] for n, d in self.tg.calls if n == 'SendMessage'))
        again = await self.say(self.cb('order:approve:1'))
        self.assertIn('Покупка уже обработана', ' '.join(self.alerts(again)))
        self.assertEqual(len(await self.mod.DB.run('SELECT * FROM subscriptions', all=True)), 1)

    async def test_two_admins_pressing_approve_at_once_grant_access_once(self):
        await self.seed_basic(); await self.start(); await self.buy(333)
        await asyncio.gather(self.tg.dp.feed_update(self.tg.bot, self.cb('order:approve:1', 111)),
                             self.tg.dp.feed_update(self.tg.bot, self.cb('order:approve:1', 111)))
        self.assertEqual(len(await self.mod.DB.run('SELECT * FROM subscriptions', all=True)), 1)
        self.assertEqual((await self.mod.DB.run('SELECT status FROM orders', one=True))['status'], 'approved')
        links = [1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 333 and 'Персональные ссылки' in d['text']]
        self.assertEqual(len(links), 1)

    async def test_reject_tells_the_buyer_without_order_numbers(self):
        await self.seed_basic(); await self.start(); await self.buy(333)
        r = await self.say(self.cb('order:reject:1'))
        buyer = [d['text'] for n, d in r if n == 'SendMessage' and d['chat_id'] == 333][0]
        self.assertIn('не подтверждена', buyer); self.assertNotIn('№', buyer)
        self.assertEqual((await self.mod.DB.run('SELECT status FROM orders', one=True))['status'], 'rejected')

    async def test_buyer_cannot_use_admin_buttons(self):
        await self.seed_basic(); await self.start(); await self.buy(333)
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('order:approve:1', 333)))))

    async def test_missing_channel_rights_keep_the_purchase_pending(self):
        await self.seed_basic(); await self.start(); await self.buy(333)
        self.tg.no_channel_rights = True
        r = await self.say(self.cb('order:approve:1'))
        self.assertTrue(self.alerts(r))
        self.assertEqual((await self.mod.DB.run('SELECT status FROM orders', one=True))['status'], 'pending')
        self.assertEqual(await self.mod.DB.run('SELECT * FROM subscriptions', all=True), [])

    async def test_same_receipt_file_on_two_purchases_is_flagged(self):
        await self.seed_basic(); await self.start()
        first = await self.buy(333, unique='SAME'); second = await self.buy(444, unique='SAME'); third = await self.buy(555, unique='OTHER')
        cap = lambda r: [d['caption'] for n, d in r if n == 'SendPhoto'][0]
        self.assertNotIn('⚠️', cap(first)); self.assertIn('уже прикреплён к другой покупке', cap(second)); self.assertNotIn('⚠️', cap(third))

    async def test_pending_queue_resends_receipts_and_manual_cases(self):
        await self.seed_basic(); await self.start()
        await self.buy(333, unique='A'); await self.buy(444, unique='B')
        await self.mod.DB.run("INSERT INTO orders(user_id,tariff_id,status,amount,crypto_invoice_id) VALUES(555,1,'crypto_mismatch',500,'7')")
        labels = [b['text'] for b in self.buttons(await self.say(self.msg('📊 Статистика')))]
        self.assertIn('🛍 Покупки на проверке (3)', labels)
        r = await self.say(self.cb('orders:pending'))
        self.assertEqual(len([1 for n, d in r if n == 'SendPhoto']), 2)
        self.assertEqual(len([b for b in self.buttons(r) if b['callback_data'].startswith('order:approve:')]), 2)
        self.assertIn('Требуют ручного решения', ' '.join(self.texts(r)))
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('orders:pending', 222)))))
