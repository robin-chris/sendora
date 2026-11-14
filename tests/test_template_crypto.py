import asyncio, datetime
from datetime import timedelta, timezone

from aiohttp import web

from support import TemplateCase


class FakeCryptoPay:
    """A tiny stand-in for https://pay.crypt.bot/api/ used by the tests."""

    def __init__(self):
        self.invoices, self.calls = {}, []
        self.down = False
        self.fail_create = None

    async def handle(self, request):
        if self.down:
            return web.Response(status=502, text='bad gateway')
        net, method = request.match_info['net'], request.match_info['method']
        token = request.headers.get('Crypto-Pay-API-Token', '')
        body = await request.json() if request.can_read_body else {}
        self.calls.append((net, method, body))
        if not ((token.startswith('111:') and net == 'main') or (token.startswith('222:') and net == 'test')):
            return web.json_response({'ok': False, 'error': {'code': 401, 'name': 'UNAUTHORIZED'}})
        if method == 'getMe':
            return web.json_response({'ok': True, 'result': {'app_id': 1, 'name': 'MyShop'}})
        if method == 'createInvoice':
            if self.fail_create:
                return web.json_response({'ok': False, 'error': {'code': 400, 'name': self.fail_create}})
            i = len(self.invoices) + 1
            inv = {'invoice_id': i, 'status': 'active', 'amount': body['amount'], 'fiat': body['fiat'], 'payload': body['payload'],
                   'bot_invoice_url': f'https://t.me/CryptoBot?start=IV{i}'}
            self.invoices[i] = inv
            return web.json_response({'ok': True, 'result': inv})
        if method == 'getInvoices':
            ids = [int(v) for v in str(body.get('invoice_ids', '')).split(',') if v]
            return web.json_response({'ok': True, 'result': {'items': [self.invoices[i] for i in ids if i in self.invoices]}})
        return web.json_response({'ok': False, 'error': {'code': 404, 'name': 'METHOD_NOT_FOUND'}})

    def pay(self, invoice_id, **changes):
        self.invoices[invoice_id].update({'status': 'paid', 'paid_asset': 'USDT', 'paid_amount': '5.5', **changes})


GOOD_TOKEN = '111:' + 'A' * 30


class CryptoBot(TemplateCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.api = FakeCryptoPay()
        app = web.Application(); app.router.add_post('/{net}/{method}', self.api.handle)
        self.runner = web.AppRunner(app); await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = {'main': f'http://127.0.0.1:{port}/main/', 'test': f'http://127.0.0.1:{port}/test/'}

    async def asyncTearDown(self):
        await self.runner.cleanup()
        await super().asyncTearDown()

    async def boot(self, connect=True):
        await self.seed_basic(methods=False)
        await self.start(CRYPTO_API=self.base, CRYPTO_POLL_SECONDS=1)
        if connect:
            await self.say(self.cb('crypto:connect')); await self.say(self.msg(GOOD_TOKEN))
            await self.say(self.cb('crypto:setfiat:RUB')); await self.say(self.cb('crypto:linkall'))

    async def pay(self, user=333):
        """Buyer opens the tariff and chooses Crypto Bot (creates the invoice)."""
        await self.say(self.msg('/start', user)); await self.say(self.cb('order:new:1', user))
        return await self.say(self.cb('order:pay:1:1', user))

    async def order(self, tid=1):
        return await self.mod.DB.run('SELECT * FROM orders ORDER BY id DESC', one=True)

    async def test_admin_connects_with_token_validation_and_hidden_secret(self):
        await self.boot(connect=False)
        self.assertIn('crypto:menu', [b.get('callback_data') for b in self.buttons(await self.say(self.msg('💰 Способы оплаты')))])
        self.assertIn('не подключён', ' '.join(self.texts(await self.say(self.cb('crypto:menu')))))
        await self.say(self.cb('crypto:connect'))
        r = await self.say(self.msg('hello')); self.assertIn('не похоже на токен', ' '.join(self.texts(r))); self.assertTrue(any(n == 'DeleteMessage' for n, d in r))
        self.assertIn('не подошёл', ' '.join(self.texts(await self.say(self.msg('333:' + 'A' * 30)))))
        self.api.down = True; self.assertIn('недоступен', ' '.join(self.texts(await self.say(self.msg(GOOD_TOKEN))))); self.api.down = False
        r = await self.say(self.msg(GOOD_TOKEN)); self.assertIn('подключён', ' '.join(self.texts(r)))
        self.assertIn('crypto:setfiat:USD', [b['callback_data'] for b in self.buttons(r)])
        await self.say(self.cb('crypto:setfiat:USD'))
        S = self.mod
        self.assertEqual((await S.DB.setting('cryptobot_fiat'), await S.DB.setting('cryptobot_network'), await S.DB.setting('cryptobot_app')), ('USD', 'main', 'MyShop'))
        method = await S.DB.run("SELECT * FROM payment_methods WHERE kind='cryptobot'", one=True)
        self.assertTrue(method['active']); self.assertNotIn('111:', method['details'])
        # the token is only used by the bot, never shown to buyers or written to the log texts
        self.assertFalse(any(GOOD_TOKEN in (d.get('text') or '') for n, d in self.tg.calls))

    async def test_testnet_token_is_detected(self):
        await self.boot(connect=False)
        await self.say(self.cb('crypto:connect')); await self.say(self.msg('222:' + 'B' * 30))
        self.assertEqual(await self.mod.DB.setting('cryptobot_network'), 'test')

    async def test_checkout_creates_one_invoice_and_reuses_it(self):
        await self.boot()
        await self.say(self.msg('/start', 333)); r = await self.say(self.cb('order:new:1', 333)); self.assertIn('🪙 Crypto Bot', [b['text'] for b in self.buttons(r)])
        r = await self.pay()
        buttons = self.buttons(r)
        self.assertEqual(buttons[0]['url'], 'https://t.me/CryptoBot?start=IV1'); self.assertEqual(buttons[1]['callback_data'], 'crypto:check:1')
        create = [b for net, m, b in self.api.calls if m == 'createInvoice'][0]
        self.assertEqual((create['currency_type'], create['fiat'], create['amount'], create['payload']), ('fiat', 'RUB', '500.00', 'order:1'))
        await self.say(self.cb('order:pay:1:1', 333))
        self.assertEqual(len([1 for net, m, b in self.api.calls if m == 'createInvoice']), 1)
        self.assertIn('пока не найдена', ' '.join(self.alerts(await self.say(self.cb('crypto:check:1', 333)))))
        self.assertIn('не найдена', ' '.join(self.alerts(await self.say(self.cb('crypto:check:1', 444)))))

    async def test_paid_invoice_grants_access_automatically_once(self):
        await self.boot(); S = self.mod
        await self.pay(); self.api.pay(1)
        await self.wait_for(lambda: S.DB.run("SELECT 1 AS n FROM orders WHERE status='approved'", one=True))
        order = await self.order()
        self.assertIn('Crypto Bot invoice 1', order['review_note']); self.assertIn('USDT', order['review_note'])
        subs = await S.DB.run('SELECT user_id,status FROM subscriptions', all=True); self.assertEqual([(x['user_id'], x['status']) for x in subs], [(333, 'active')])
        links = [d for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 333 and 'Персональные ссылки' in d['text']]
        admin = [d for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'подтверждён автоматически' in (d.get('text') or '')]
        self.assertEqual((len(links), len(admin)), (1, 1))
        self.assertIn('уже подтверждена', ' '.join(self.alerts(await self.say(self.cb('crypto:check:1', 333)))))
        await asyncio.sleep(2.5); self.assertEqual(len(await S.DB.run('SELECT * FROM subscriptions', all=True)), 1)

    async def test_check_button_and_worker_never_grant_twice(self):
        await self.boot(); S = self.mod
        await self.pay(); self.api.pay(1)
        await asyncio.gather(self.tg.dp.feed_update(self.tg.bot, self.cb('crypto:check:1', 333)), asyncio.sleep(1.05))
        await asyncio.sleep(2)
        links = [d for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 333 and 'Персональные ссылки' in d['text']]
        self.assertEqual(len(links), 1); self.assertEqual(len(await S.DB.run('SELECT * FROM subscriptions', all=True)), 1)

    async def test_missing_channel_rights_are_retried_and_admin_warned_once(self):
        await self.boot(); S = self.mod
        await self.pay(); self.tg.no_channel_rights = True; self.api.pay(1)
        await asyncio.sleep(3.5)
        self.assertEqual((await self.order())['status'], 'awaiting_crypto')
        warnings = [d for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'доступ выдать не удалось' in (d.get('text') or '')]
        self.assertEqual(len(warnings), 1)
        self.tg.no_channel_rights = False
        await self.wait_for(lambda: S.DB.run("SELECT 1 AS n FROM orders WHERE status='approved'", one=True))

    async def test_wrong_amount_is_not_trusted(self):
        await self.boot()
        await self.pay(); self.api.pay(1, amount='1.00')
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM orders WHERE status='crypto_mismatch'", one=True))
        self.assertEqual(await self.mod.DB.run('SELECT * FROM subscriptions', all=True), [])
        self.assertTrue([1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'не совпадает' in (d.get('text') or '')])

    async def test_expired_invoice_cancels_the_order_and_tells_the_buyer(self):
        await self.boot()
        await self.pay(); self.api.invoices[1]['status'] = 'expired'
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM orders WHERE status='cancelled'", one=True))
        self.assertTrue([1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 333 and 'истёк' in (d.get('text') or '')])

    async def test_paid_order_for_a_deleted_tariff_needs_a_human(self):
        await self.boot()
        await self.pay(); await self.mod.DB.run('DELETE FROM tariffs WHERE id=1'); self.api.pay(1)
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM orders WHERE status='paid_unprocessed'", one=True))
        self.assertTrue([1 for n, d in self.tg.calls if n == 'SendMessage' and d['chat_id'] == 111 and 'тариф удалён' in (d.get('text') or '')])

    async def test_api_errors_are_explained_to_the_buyer(self):
        await self.boot()
        await self.say(self.msg('/start', 333)); await self.say(self.cb('order:new:1', 333))
        self.api.fail_create = 'AMOUNT_TOO_SMALL'
        r = await self.say(self.cb('order:pay:1:1', 333)); self.assertIn('меньше минимальной', ' '.join(self.alerts(r)))
        self.assertEqual((await self.order())['status'], 'cancelled')
        self.api.fail_create = None; self.api.down = True
        self.assertIn('недоступен', ' '.join(self.alerts(await self.say(self.cb('order:pay:1:1', 333)))))
        self.api.down = False
        await self.mod.DB.run("UPDATE tariffs SET price='Бесплатно'")
        self.assertIn('определить сумму', ' '.join(self.alerts(await self.say(self.cb('order:pay:1:1', 333)))))

    async def test_worker_survives_an_outage(self):
        await self.boot()
        await self.pay(); self.api.down = True; await asyncio.sleep(2.5); self.api.down = False; self.api.pay(1)
        await self.wait_for(lambda: self.mod.DB.run("SELECT 1 AS n FROM orders WHERE status='approved'", one=True))

    async def test_menu_controls_and_disconnect(self):
        await self.boot(); S = self.mod
        await self.say(self.cb('crypto:toggle')); self.assertEqual((await S.DB.run("SELECT active FROM payment_methods WHERE kind='cryptobot'", one=True))['active'], 0)
        self.assertNotIn('🪙 Crypto Bot', [b['text'] for b in self.buttons(await self.say(self.cb('order:new:1', 333)))])
        await self.say(self.cb('crypto:toggle'))
        self.assertIn('Токен не работает', 'Токен не работает ' + ' '.join(self.alerts(await self.say(self.cb('crypto:net')))))   # main token is invalid on testnet
        self.assertEqual(await S.DB.setting('cryptobot_network'), 'main')
        await self.say(self.cb('crypto:disc')); await self.say(self.cb('crypto:discyes'))
        self.assertEqual((await S.DB.setting('cryptobot_token'), await S.DB.run('SELECT * FROM payment_methods', all=True)), ('', []))
        self.assertIn('Нет доступа', ' '.join(self.alerts(await self.say(self.cb('crypto:menu', 333)))))

    async def test_interrupted_payment_is_not_lost_on_restart(self):
        await self.boot(); S = self.mod
        await S.DB.run("INSERT INTO orders(user_id,tariff_id,payment_method_id,status,amount,crypto_invoice_id) VALUES(777,1,1,'processing',500,'99')")
        await S.DB.run("INSERT INTO orders(user_id,tariff_id,payment_method_id,status,amount,proof_file_id,proof_type) VALUES(778,1,1,'processing',500,'F','photo')")
        await S.DB.init()
        states = {u: (await S.DB.run('SELECT status FROM orders WHERE user_id=?', (u,), one=True))['status'] for u in (777, 778)}
        self.assertEqual(states, {777: 'awaiting_crypto', 778: 'pending'})
