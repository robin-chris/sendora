from support import TemplateCase


class Prices(TemplateCase):
    async def test_reprice_keeps_currency_and_number_style(self):
        S = self.mod
        cases = [('500 ₽', -10, '450 ₽'), ('1 500 ₽', 10, '1 650 ₽'), ('9,99 USD', -20, '7,99 USD'), ('99', -10, '89'),
                 ('2 990₽', -30, '2 093₽'), ('$19.90', -50, '$9.95'), ('Бесплатно', -10, None), ('1 ₽', -90, None)]
        for price, pct, expected in cases:
            self.assertEqual(S.reprice(price, pct), expected, price)

    async def test_old_price_rules(self):
        S = self.mod
        self.assertTrue(S.old_price_valid('500', '700')); self.assertTrue(S.old_price_valid('500', ''))
        self.assertFalse(S.old_price_valid('500', '400')); self.assertFalse(S.old_price_valid('500', 'abc'))
        self.assertIn('→', S.price_line('690 ₽', '990 ₽')); self.assertIn('−30%', S.price_line('690 ₽', '990 ₽'))
        self.assertEqual(S.price_line('690 ₽', ''), '690 ₽')


class SingleTariff(TemplateCase):
    async def test_duration_buttons_and_custom_value(self):
        await self.seed_basic(); await self.start()
        r = await self.say(self.cb('edit:days:1'))
        self.assertIn('tdays:set:1:90', [b['callback_data'] for b in self.buttons(r) if b.get('callback_data')])
        await self.say(self.cb('tdays:set:1:90'))
        self.assertEqual((await self.mod.DB.run('SELECT days FROM tariffs', one=True))['days'], 90)
        await self.say(self.cb('tdays:custom:1'))
        for bad in ('abc', '0', '99999'):
            self.assertTrue(self.texts(await self.say(self.msg(bad))))
            self.assertEqual((await self.mod.DB.run('SELECT days FROM tariffs', one=True))['days'], 90)
        await self.say(self.msg('45'))
        self.assertEqual((await self.mod.DB.run('SELECT days FROM tariffs', one=True))['days'], 45)
        self.assertTrue(self.alerts(await self.say(self.cb('tdays:set:1:30', 222))))

    async def test_wizard_accepts_a_duration_button(self):
        await self.seed_basic(methods=False); await self.start()
        await self.say(self.cb('tariff:add')); await self.say(self.msg('Новый')); await self.say(self.msg('Описание')); r = await self.say(self.msg('500 ₽'))
        self.assertIn('twdays:30', [b['callback_data'] for b in self.buttons(r) if b.get('callback_data')])
        r = await self.say(self.cb('twdays:90'))
        self.assertIn('Срок сохранён: 90', ' '.join(self.texts(r)))
        self.assertIn('Этот шаг уже пройден', ' '.join(self.alerts(await self.say(self.cb('twdays:7')))))

    async def test_old_price_is_validated_shown_and_cleared(self):
        await self.seed_basic(); await self.start()
        await self.say(self.cb('edit:old_price:1'))
        self.assertIn('выше текущей', ' '.join(self.texts(await self.say(self.msg('400 ₽')))))
        self.assertIn('число', ' '.join(self.texts(await self.say(self.msg('дорого')))))
        await self.say(self.msg('900 ₽'))
        row = await self.mod.DB.run('SELECT price,old_price FROM tariffs', one=True)
        self.assertEqual(row['old_price'], '900 ₽')
        card = self.texts(await self.say(self.cb('tariff:edit:1')))[0]
        self.assertIn('→', card); self.assertIn('−44%', card)
        buyer = self.texts(await self.say(self.cb('order:new:1', 900)))[0]
        self.assertIn('→', buyer)
        # raising the price above the old price removes the old price automatically
        await self.say(self.cb('edit:price:1')); r = await self.say(self.msg('950 ₽'))
        self.assertIn('Старая цена убрана', ' '.join(self.texts(r)))
        self.assertEqual((await self.mod.DB.run('SELECT old_price FROM tariffs', one=True))['old_price'], '')
        await self.say(self.cb('edit:old_price:1')); await self.say(self.msg('1200 ₽'))
        await self.say(self.cb('oldprice:clear:1'))
        self.assertEqual((await self.mod.DB.run('SELECT old_price FROM tariffs', one=True))['old_price'], '')

    async def test_buyers_see_the_description(self):
        await self.seed_basic(description='Все материалы курса', old_price='700 ₽'); await self.start()
        listing = self.texts(await self.say(self.msg('💳 Тарифы', 900)))[0]
        self.assertIn('Все материалы курса', listing); self.assertIn('→', listing)
        card = self.texts(await self.say(self.cb('order:new:1', 900)))[0]
        self.assertIn('Описание: Все материалы курса', card)


class BulkEditing(TemplateCase):
    async def prepare(self):
        S = self.mod; await S.DB.init()
        await S.DB.run("INSERT INTO channels(channel_id,title,active) VALUES(-100,'Канал',1)")
        for name, price in (('A', '500 ₽'), ('B', '1 000 ₽'), ('C', 'Бесплатно')):
            await S.DB.run("INSERT INTO tariffs(name,description,price,days,channel_id,channel_title) VALUES(?,?,?,30,-100,'Канал')", (name, 'd', price))
        for name, active in (('Карта', 1), ('Сбер', 1)):
            await S.DB.run("INSERT INTO payment_methods(name,details,active) VALUES(?,?,?)", (name, 'x', active))
        await S.DB.run("INSERT INTO tariff_payment_methods VALUES(1,1)")
        await self.start()

    async def pick(self, ids=(1, 2, 3)):
        await self.say(self.cb('bulk:start'))
        for i in ids: await self.say(self.cb(f'bulk:t:{i}'))
        return await self.say(self.cb('bulk:next'))

    async def rows(self):
        return [(t['name'], t['price'], t['old_price'], t['days'], t['active']) for t in await self.mod.DB.run('SELECT * FROM tariffs ORDER BY id', all=True)]

    async def test_list_offers_bulk_editing_and_requires_a_selection(self):
        await self.prepare()
        self.assertIn('bulk:start', [b.get('callback_data') for b in self.buttons(await self.say(self.msg('💳 Тарифы')))])
        await self.say(self.cb('bulk:start'))
        self.assertIn('хотя бы один', ' '.join(self.alerts(await self.say(self.cb('bulk:next')))))

    async def test_percent_discount_remembers_old_price_and_skips_unpriced_tariffs(self):
        await self.prepare(); await self.pick(); r = await self.say(self.cb('bulk:a:pct'))
        preview = self.texts(await self.say(self.cb('bulk:pct:-10')))[0]
        self.assertIn('Будет изменено тарифов: 2 из 3', preview); self.assertIn('Пропущено', preview)
        await self.say(self.cb('bulk:apply'))
        rows = await self.rows()
        self.assertEqual(rows[0][1:3], ('450 ₽', '500 ₽')); self.assertEqual(rows[1][1:3], ('900 ₽', '1 000 ₽')); self.assertEqual(rows[2][1:3], ('Бесплатно', ''))
        # selection is reset after applying
        self.assertTrue(self.alerts(await self.say(self.cb('bulk:menu'))))

    async def test_custom_percent_validation_and_remember_toggle(self):
        await self.prepare(); await self.pick(); await self.say(self.cb('bulk:a:pct')); await self.say(self.cb('bulk:pctc'))
        for bad in ('abc', '0', '-95'): self.assertIn('Введи число', ' '.join(self.texts(await self.say(self.msg(bad)))))
        await self.say(self.msg('+10')); await self.say(self.cb('bulk:apply'))
        rows = await self.rows()
        self.assertEqual(rows[0][1:3], ('550 ₽', '')); self.assertEqual(rows[1][1:3], ('1 100 ₽', ''))

    async def test_set_price_old_price_days_and_activity(self):
        await self.prepare()
        await self.pick((1, 2)); await self.say(self.cb('bulk:a:price')); await self.say(self.msg('300 ₽')); await self.say(self.cb('bulk:rem')); await self.say(self.cb('bulk:apply'))
        rows = await self.rows(); self.assertEqual((rows[0][1], rows[0][2]), ('300 ₽', '500 ₽')); self.assertEqual((rows[1][1], rows[1][2]), ('300 ₽', '1 000 ₽'))
        await self.pick((1, 2, 3)); await self.say(self.cb('bulk:a:old')); await self.say(self.cb('bulk:oldset')); r = await self.say(self.msg('400 ₽'))
        self.assertIn('Будет изменено тарифов: 3 из 3', self.texts(r)[0])      # nothing to compare with on a free tariff: allowed
        await self.say(self.cb('bulk:apply'))
        self.assertEqual([r[2] for r in await self.rows()], ['400 ₽', '400 ₽', '400 ₽'])
        await self.pick((1, 2)); await self.say(self.cb('bulk:a:old')); await self.say(self.cb('bulk:oldclear')); await self.say(self.cb('bulk:apply'))
        self.assertEqual([r[2] for r in await self.rows()], ['', '', '400 ₽'])
        await self.pick((1, 2, 3)); await self.say(self.cb('bulk:a:days')); await self.say(self.cb('bulk:days:90')); await self.say(self.cb('bulk:apply'))
        self.assertEqual([r[3] for r in await self.rows()], [90, 90, 90])
        await self.pick((1, 2, 3)); await self.say(self.cb('bulk:a:days')); await self.say(self.cb('bulk:daysc')); self.assertIn('от 1 до 36500', ' '.join(self.texts(await self.say(self.msg('0')))))
        await self.say(self.msg('45')); await self.say(self.cb('bulk:apply')); self.assertEqual([r[3] for r in await self.rows()], [45, 45, 45])
        await self.pick((1, 2, 3)); await self.say(self.cb('bulk:a:off')); await self.say(self.cb('bulk:apply')); self.assertEqual([r[4] for r in await self.rows()], [0, 0, 0])
        await self.pick((1, 2, 3)); await self.say(self.cb('bulk:a:on')); await self.say(self.cb('bulk:apply')); self.assertEqual([r[4] for r in await self.rows()], [1, 1, 1])
        await self.pick((1, 2, 3)); r = await self.say(self.cb('bulk:a:on'))
        self.assertIn('Будет изменено тарифов: 0', self.texts(r)[0]); self.assertNotIn('bulk:apply', [b['callback_data'] for b in self.buttons(r)])

    async def test_payment_methods_add_and_replace(self):
        await self.prepare()
        pairs = lambda: self.mod.DB.run('SELECT tariff_id,payment_method_id FROM tariff_payment_methods ORDER BY 1,2', all=True)
        await self.pick(); await self.say(self.cb('bulk:a:pm'))
        self.assertIn('Отметь хотя бы один', ' '.join(self.alerts(await self.say(self.cb('bulk:pmgo:add')))))
        await self.say(self.cb('bulk:pm:2')); await self.say(self.cb('bulk:pmgo:add')); await self.say(self.cb('bulk:apply'))
        self.assertEqual([(p['tariff_id'], p['payment_method_id']) for p in await pairs()], [(1, 1), (1, 2), (2, 2), (3, 2)])
        await self.pick(); await self.say(self.cb('bulk:a:pm')); await self.say(self.cb('bulk:pm:2')); await self.say(self.cb('bulk:pmgo:replace')); await self.say(self.cb('bulk:apply'))
        self.assertEqual([(p['tariff_id'], p['payment_method_id']) for p in await pairs()], [(1, 2), (2, 2), (3, 2)])

    async def test_non_admins_and_stale_sessions_are_rejected(self):
        await self.prepare(); await self.pick()
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('bulk:apply', 222)))))
        await self.say(self.cb('bulk:exit'))
        self.assertIn('Сессия устарела', ' '.join(self.alerts(await self.say(self.cb('bulk:apply')))))
