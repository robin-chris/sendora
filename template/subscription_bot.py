from __future__ import annotations
import asyncio, json, logging, math, os, re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timedelta, timezone
from pathlib import Path
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN','').strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
OWNER_IDS = {int(x.strip()) for x in os.getenv('OWNER_IDS','').split(',') if x.strip().isdigit()}
PLATFORM_OWNER_IDS = {int(x.strip()) for x in os.getenv('CONSTRUCTOR_OWNER_IDS','').split(',') if x.strip().isdigit()}
DB_PATH = os.getenv('DB_PATH','./data/subscriptions.sqlite3')
CHECK_INTERVAL = max(15, int(os.getenv('SUBSCRIPTION_CHECK_INTERVAL','60')))
REMINDER_INTERVAL = max(15, int(os.getenv('REMINDER_CHECK_INTERVAL','600')))
PROMO_INTERVAL = max(5, int(os.getenv('PROMO_CHECK_INTERVAL','30')))
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
log = logging.getLogger('subscription_bot')

MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='💳 Тарифы'), KeyboardButton(text='👤 Моя подписка')]], resize_keyboard=True)
ADMIN_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📊 Статистика'), KeyboardButton(text='💳 Тарифы')], [KeyboardButton(text='📣 Каналы'), KeyboardButton(text='💰 Способы оплаты')], [KeyboardButton(text='📨 Рассылка'), KeyboardButton(text='💳 Оплата')]], resize_keyboard=True)
OWNER_ADMIN_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='📊 Статистика'), KeyboardButton(text='💳 Тарифы')], [KeyboardButton(text='📣 Каналы'), KeyboardButton(text='💰 Способы оплаты')], [KeyboardButton(text='📨 Рассылка'), KeyboardButton(text='💳 Оплата')]], resize_keyboard=True)
def admin_menu_for(uid: int): return OWNER_ADMIN_MENU if uid in OWNER_IDS or uid in PLATFORM_OWNER_IDS else ADMIN_MENU

class TariffWizard(StatesGroup):
    name=State(); description=State(); price=State(); days=State(); channel=State()
class PaymentInfoState(StatesGroup):
    waiting=State()
class PaymentMethodWizard(StatesGroup):
    name=State(); details=State()
class EditPaymentMethod(StatesGroup):
    value=State()
class EditTariff(StatesGroup):
    field=State(); value=State()
class ChannelWizard(StatesGroup):
    channel_id=State()
class EditChannel(StatesGroup):
    value=State()
class BroadcastState(StatesGroup):
    content=State(); confirm=State()
class OwnerSettingsState(StatesGroup):
    value=State()
class OwnerLicensePlanState(StatesGroup):
    value=State()

class BulkEdit(StatesGroup): percent=State(); price=State(); old_price=State(); days=State()
class CryptoSetup(StatesGroup): token=State()
class PromoEdit(StatesGroup): percent=State(); duration=State()

class Store:
    def __init__(self,path): self.path=path
    async def init(self):
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        async with aiosqlite.connect(self.path, timeout=20) as d:
            await d.execute('PRAGMA journal_mode=WAL')  # persistent: concurrent readers do not block writers
            await d.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS tariffs(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',price TEXT NOT NULL,days INTEGER NOT NULL,channel_id INTEGER NOT NULL,channel_title TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER NOT NULL UNIQUE, title TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,details TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS license_payment_methods(id INTEGER PRIMARY KEY,name TEXT NOT NULL,details TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS tariff_payment_methods(tariff_id INTEGER NOT NULL, payment_method_id INTEGER NOT NULL, PRIMARY KEY(tariff_id,payment_method_id));
            CREATE TABLE IF NOT EXISTS tariff_extra_channels(tariff_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, channel_title TEXT NOT NULL DEFAULT '', PRIMARY KEY(tariff_id,channel_id));
            CREATE TABLE IF NOT EXISTS license_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,amount REAL NOT NULL,payment_method_id INTEGER,status TEXT NOT NULL DEFAULT 'awaiting_proof',proof_file_id TEXT,proof_type TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,reviewed_at TEXT,license_tariff_id INTEGER); 
            CREATE TABLE IF NOT EXISTS license_tariffs(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,turnover_limit REAL NOT NULL,price REAL NOT NULL,duration_days INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,admin_free INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,tariff_id INTEGER NOT NULL,payment_method_id INTEGER,status TEXT NOT NULL DEFAULT 'pending',proof_type TEXT,proof_file_id TEXT,proof_text TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,reviewed_at TEXT,reviewed_by INTEGER,review_note TEXT,amount REAL NOT NULL DEFAULT 0,FOREIGN KEY(user_id) REFERENCES users(user_id));
            CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,tariff_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP,removed_at TEXT,FOREIGN KEY(user_id) REFERENCES users(user_id));
            CREATE TABLE IF NOT EXISTS subscription_reminders(subscription_id INTEGER NOT NULL, expires_at TEXT NOT NULL, milestone TEXT NOT NULL, sent_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(subscription_id,expires_at,milestone));
            CREATE TABLE IF NOT EXISTS broadcast_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,created_by INTEGER NOT NULL,source_chat_id INTEGER NOT NULL,source_message_id INTEGER NOT NULL,label TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'running',total INTEGER NOT NULL DEFAULT 0,progress_chat_id INTEGER,progress_message_id INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,finished_at TEXT);
            CREATE TABLE IF NOT EXISTS broadcast_recipients(job_id INTEGER NOT NULL,user_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',PRIMARY KEY(job_id,user_id));
            CREATE TABLE IF NOT EXISTS price_promos(id INTEGER PRIMARY KEY AUTOINCREMENT,tariff_id INTEGER NOT NULL,original_price TEXT NOT NULL,original_old_price TEXT NOT NULL DEFAULT '',promo_price TEXT NOT NULL,percent REAL NOT NULL,ends_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP,finished_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_price_promos_active ON price_promos(status,ends_at);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry ON subscriptions(status,expires_at);
            ''');
            # Migrate existing databases without losing orders.
            lp_columns = [r[1] for r in await (await d.execute('PRAGMA table_info(license_payments)')).fetchall()]
            if 'license_tariff_id' not in lp_columns:
                await d.execute('ALTER TABLE license_payments ADD COLUMN license_tariff_id INTEGER')
            # Snapshot the purchased terms at checkout. Editing a tariff later must
            # not silently change the limit or duration for an already-paid receipt.
            for column, declaration in (
                ('license_tariff_name', 'TEXT'),
                ('license_turnover_limit', 'REAL'),
                ('license_duration_days', 'INTEGER'),
            ):
                if column not in lp_columns:
                    await d.execute(f'ALTER TABLE license_payments ADD COLUMN {column} {declaration}')
            license_tariff_columns = [r[1] for r in await (await d.execute('PRAGMA table_info(license_tariffs)')).fetchall()]
            if 'admin_free' not in license_tariff_columns:
                await d.execute('ALTER TABLE license_tariffs ADD COLUMN admin_free INTEGER NOT NULL DEFAULT 0')
            columns = [r[1] for r in await (await d.execute('PRAGMA table_info(orders)')).fetchall()]
            if 'payment_method_id' not in columns:
                await d.execute('ALTER TABLE orders ADD COLUMN payment_method_id INTEGER')
            if 'amount' not in columns:
                await d.execute('ALTER TABLE orders ADD COLUMN amount REAL NOT NULL DEFAULT 0')
            if 'invite_links_json' not in columns:
                await d.execute("ALTER TABLE orders ADD COLUMN invite_links_json TEXT NOT NULL DEFAULT '[]'")
            for column in ('crypto_invoice_id','crypto_invoice_url','crypto_expires_at','proof_unique_id'):
                if column not in columns:
                    await d.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            tariff_columns = [r[1] for r in await (await d.execute('PRAGMA table_info(tariffs)')).fetchall()]
            if 'old_price' not in tariff_columns:
                await d.execute("ALTER TABLE tariffs ADD COLUMN old_price TEXT NOT NULL DEFAULT ''")
            method_columns = [r[1] for r in await (await d.execute('PRAGMA table_info(payment_methods)')).fetchall()]
            if 'kind' not in method_columns:
                await d.execute("ALTER TABLE payment_methods ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'")
            # Recover approvals interrupted by a process/server crash. The approval
            # handler commits subscriptions and the final 'approved' order status in
            # one SQLite transaction, so a surviving 'processing' status means the
            # final transaction did not commit and it is safe to retry review.
            # Preserve a valid receipt and return it to the admin queue.
            await d.execute("""UPDATE orders
                SET status=CASE
                    WHEN crypto_invoice_id<>'' AND proof_file_id IS NULL THEN 'awaiting_crypto'
                    WHEN proof_file_id IS NOT NULL AND proof_type IN ('photo','document') THEN 'pending'
                    ELSE 'awaiting_proof'
                END
                WHERE status='processing'""")
            # Bring channels from existing tariffs into the management list without changing IDs.
            await d.execute('INSERT OR IGNORE INTO channels(channel_id,title) SELECT channel_id, COALESCE(NULLIF(channel_title, ""), CAST(channel_id AS TEXT)) FROM tariffs')
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('turnover_limit','5000')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('reminders_enabled','1')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('license_price','990')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('license_duration_days','30')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('license_expires_at','')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('license_active','0')")
            await d.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('license_turnover_capped','0')")
            plans = await (await d.execute('SELECT COUNT(*) FROM license_tariffs')).fetchone()
            if not plans or plans[0] == 0:
                limit = await (await d.execute("SELECT value FROM settings WHERE key='turnover_limit'")).fetchone()
                price = await (await d.execute("SELECT value FROM settings WHERE key='license_price'")).fetchone()
                duration = await (await d.execute("SELECT value FROM settings WHERE key='license_duration_days'")).fetchone()
                await d.execute('INSERT INTO license_tariffs(name,turnover_limit,price,duration_days) VALUES(?,?,?,?)', ('Базовый', float(limit[0] if limit else 5000), float(price[0] if price else 990), int(duration[0] if duration else 30)))
            # Admins receive the free 5,000 turnover allowance automatically.
            # It is not represented as a purchasable/editable license tariff.
            # Existing legacy free-plan rows are removed after migrating their active state.
            old_free = await (await d.execute("SELECT id FROM license_tariffs WHERE admin_free=1 LIMIT 1")).fetchone()
            if old_free:
                current_active = await (await d.execute("SELECT value FROM settings WHERE key='license_active'")).fetchone()
                current_capped = await (await d.execute("SELECT value FROM settings WHERE key='license_turnover_capped'")).fetchone()
                if current_active and current_active[0] == '1' and current_capped and current_capped[0] == '1':
                    await d.execute("UPDATE settings SET value='' WHERE key='license_expires_at'")
                    await d.execute("UPDATE settings SET value='5000' WHERE key='turnover_limit'")
                await d.execute('DELETE FROM license_tariffs WHERE admin_free=1')
            active_row = await (await d.execute("SELECT value FROM settings WHERE key='license_active'")).fetchone()
            expiry_row = await (await d.execute("SELECT value FROM settings WHERE key='license_expires_at'")).fetchone()
            # Grant the free allowance on a fresh bot. Do not override an existing paid license.
            if active_row and active_row[0] == '0' and expiry_row and not (expiry_row[0] or '').strip():
                await d.execute("UPDATE settings SET value='1' WHERE key='license_active'")
                await d.execute("UPDATE settings SET value='1' WHERE key='license_turnover_capped'")
                await d.execute("UPDATE settings SET value='5000' WHERE key='turnover_limit'")
            await d.commit()
    async def run(self,sql,args=(),one=False,all=False):
        async with aiosqlite.connect(self.path, timeout=20) as d:
            d.row_factory=aiosqlite.Row; c=await d.execute(sql,args); rows=await c.fetchall() if all else None; row=await c.fetchone() if one else None; await d.commit()
            if all:
                return [dict(x) for x in rows]
            if one:
                # SELECT ... one=True must return None when no row exists;
                # returning cursor.rowcount (-1 for SELECT) makes callers treat
                # an absent setting as a row and then subscript an integer.
                return dict(row) if row is not None else None
            return c.lastrowid if sql.lstrip().upper().startswith('INSERT') else c.rowcount
    async def tariffs(self,active=None):
        if active is None: return await self.run('SELECT * FROM tariffs ORDER BY id DESC',all=True)
        return await self.run('SELECT t.* FROM tariffs t LEFT JOIN channels c ON c.channel_id=t.channel_id WHERE t.active=? AND COALESCE(c.active,0)=1 ORDER BY t.id DESC',(active,),all=True)
    async def setting(self,key,default=''):
        r=await self.run('SELECT value FROM settings WHERE key=?',(key,),one=True); return r['value'] if r else default
    async def set_setting(self,key,value): await self.run('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
DB=Store(DB_PATH)
BOT_INSTANCE: Bot | None = None
def admin(uid): return uid in ADMIN_IDS or uid in OWNER_IDS or uid in PLATFORM_OWNER_IDS
def ikb(rows): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=c) for t,c in row] for row in rows])
def fmt_date(value):
    try: return datetime.fromisoformat(value).astimezone().strftime('%d.%m.%Y %H:%M')
    except Exception: return value

def calculate_license_expiry(current_expiry: str, duration_days: int, now=None) -> str:
    """Extend a license from its current future expiry, otherwise from now."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    base = current
    if current_expiry:
        try:
            parsed = datetime.fromisoformat(current_expiry.strip().replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
            if parsed > base:
                base = parsed
        except (TypeError, ValueError):
            # An invalid legacy value must not block a legitimate renewal.
            pass
    return (base + timedelta(days=max(1, int(duration_days)))).isoformat()

def parse_amount(price) -> float:
    """First number in a free-text price ('1 500 ₽', '9,99 USD'); 0.0 if there is none."""
    digits = re.sub(r'(?<=\d)\s+(?=\d)', '', str(price or ''))
    match = re.search(r'\d+(?:[.,]\d+)?', digits)
    return float(match.group(0).replace(',', '.')) if match else 0.0

def strike(text) -> str:
    """Strikethrough for plain-text Telegram messages (combining long stroke overlay)."""
    return ''.join(ch if ch.isspace() else ch + '\u0336' for ch in str(text))

def price_line(price, old_price='') -> str:
    old = (old_price or '').strip()
    if not old:
        return str(price)
    line = f'{strike(old)}  →  {price}'
    new_value, old_value = parse_amount(price), parse_amount(old)
    if new_value > 0 and old_value > new_value:
        line += f'  (−{round((1 - new_value / old_value) * 100)}%)'
    return line

def old_price_valid(price, old_price) -> bool:
    """An old price must contain a number and, when both are numeric, be higher than the current price."""
    old = (old_price or '').strip()
    if not old:
        return True
    old_value = parse_amount(old)
    if old_value <= 0:
        return False
    new_value = parse_amount(price)
    return not (new_value > 0 and old_value <= new_value)

_PRICE_NUMBER = re.compile(r'\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?')

def reprice(price, percent) -> str | None:
    """Change the first number in a price text by `percent`, keeping currency and number style."""
    text = str(price or '')
    match = _PRICE_NUMBER.search(text)
    if not match:
        return None
    raw = match.group(0)
    try:
        value = Decimal(raw.replace(' ', '').replace('\u00a0', '').replace(',', '.'))
    except InvalidOperation:
        return None
    new_value = value * (Decimal(100) + Decimal(str(percent))) / Decimal(100)
    has_fraction = bool(re.search(r'[.,]\d', raw))
    new_value = new_value.quantize(Decimal('0.01') if has_fraction else Decimal('1'), rounding=ROUND_HALF_UP)
    if new_value <= 0:
        return None
    if has_fraction:
        formatted = f'{new_value:.2f}'
        if ',' in raw:
            formatted = formatted.replace('.', ',')
    else:
        formatted = f'{int(new_value)}'
        if re.search(r'\d[ \u00a0]\d{3}', raw):
            formatted = f'{int(new_value):,}'.replace(',', ' ')
    return text[:match.start()] + formatted + text[match.end():]

async def drop_stale_old_price(tariff_id: int) -> bool:
    """After a price change, remove an old price that is no longer higher than the current one."""
    row = await DB.run('SELECT price,old_price FROM tariffs WHERE id=?', (tariff_id,), one=True)
    if row and (row['old_price'] or '').strip() and not old_price_valid(row['price'], row['old_price']):
        await DB.run("UPDATE tariffs SET old_price='' WHERE id=?", (tariff_id,))
        return True
    return False

async def duplicate_receipt_note(order_id: int, unique_id: str) -> str:
    """Warning line when the very same receipt file is already attached to another purchase."""
    if not unique_id:
        return ''
    other = await DB.run("SELECT id,user_id,status FROM orders WHERE proof_unique_id=? AND id<>? AND status IN ('pending','processing','approved') ORDER BY id LIMIT 1", (unique_id, order_id), one=True)
    if not other:
        return ''
    states = {'pending': 'ждёт проверки', 'processing': 'обрабатывается', 'approved': 'уже подтверждена'}
    return f"\n⚠️ Этот же файл чека уже прикреплён к другой покупке (покупатель ID {other['user_id']}, {states.get(other['status'], other['status'])}). Проверьте, не повторная ли это оплата одним чеком."

def parse_percent(text):
    """'-50', '+15', '10%', '−7,5' -> number in [-90, 500] without zero; None if invalid."""
    raw = str(text or '').strip().replace('−', '-').replace('–', '-').replace('%', '').replace(',', '.').replace(' ', '')
    try:
        value = float(raw)
    except ValueError:
        return None
    if not -90 <= value <= 500 or value == 0:
        return None
    return int(value) if value == int(value) else value

def parse_duration(text):
    """'36' / '12ч' / '2 д' -> minutes (1 hour .. 365 days); None if not understood."""
    match = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Zа-яА-ЯёЁ]*)\s*', str(text or ''))
    if not match:
        return None
    value = float(match.group(1).replace(',', '.')); unit = match.group(2).lower()
    if unit in ('', 'ч', 'час', 'часа', 'часов', 'h', 'hr', 'hrs', 'hour', 'hours'):
        minutes = value * 60
    elif unit in ('д', 'дн', 'дня', 'дней', 'день', 'd', 'day', 'days'):
        minutes = value * 1440
    else:
        return None
    minutes = int(round(minutes))
    return minutes if 60 <= minutes <= 365 * 1440 else None

def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f'{minutes // 1440} дн.'
    hours, rest = divmod(minutes, 60)
    return f'{hours} ч' + (f' {rest} мин' if rest else '')

async def active_promo(tariff_id: int):
    return await DB.run("SELECT * FROM price_promos WHERE tariff_id=? AND status='active' ORDER BY id DESC LIMIT 1", (tariff_id,), one=True)

async def active_promos() -> dict:
    rows = await DB.run("SELECT * FROM price_promos WHERE status='active'", all=True)
    return {r['tariff_id']: r for r in rows}

async def cancel_promos(tariff_ids) -> int:
    """A manual price change wins: forget the timer so it never overwrites the admin's edit."""
    total = 0
    for tid in tariff_ids:
        total += int(await DB.run("UPDATE price_promos SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE tariff_id=? AND status='active'", (tid,)) or 0)
    return total

async def start_promo(tariff_id: int, percent, minutes: int, show_old: bool):
    """Change the price now and remember how to restore it. Returns (info, '') or (None, reason)."""
    ends = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    async with aiosqlite.connect(DB.path, timeout=20) as d:
        d.row_factory = aiosqlite.Row
        await d.execute('BEGIN IMMEDIATE')
        t = await (await d.execute('SELECT * FROM tariffs WHERE id=?', (tariff_id,))).fetchone()
        if not t:
            await d.rollback(); return None, 'тариф не найден'
        current = await (await d.execute("SELECT * FROM price_promos WHERE tariff_id=? AND status='active' ORDER BY id DESC LIMIT 1", (tariff_id,))).fetchone()
        # A new promotion starts from the ORIGINAL price, so repeated promotions never compound.
        base_price = current['original_price'] if current else t['price']
        base_old = current['original_old_price'] if current else (t['old_price'] or '')
        new_price = reprice(base_price, percent)
        if new_price is None or new_price == base_price:
            await d.rollback(); return None, f"{t['name']}: в цене нет числа или цена не изменится"
        if show_old and 0 < parse_amount(new_price) < parse_amount(base_price):
            new_old = base_price
        else:
            new_old = base_old if old_price_valid(new_price, base_old) else ''
        if current:
            await d.execute("UPDATE price_promos SET status='replaced',finished_at=CURRENT_TIMESTAMP WHERE id=?", (current['id'],))
        await d.execute('UPDATE tariffs SET price=?,old_price=? WHERE id=?', (new_price, new_old, tariff_id))
        await d.execute('INSERT INTO price_promos(tariff_id,original_price,original_old_price,promo_price,percent,ends_at) VALUES(?,?,?,?,?,?)',
                        (tariff_id, base_price, base_old, new_price, float(percent), ends))
        await d.commit()
    return {'name': t['name'], 'from': base_price, 'to': new_price, 'ends': ends}, ''

async def end_promo(promo_id: int, status: str = 'done'):
    """Restore the original price. If the price was edited by hand meanwhile, leave it alone."""
    async with aiosqlite.connect(DB.path, timeout=20) as d:
        d.row_factory = aiosqlite.Row
        await d.execute('BEGIN IMMEDIATE')
        p = await (await d.execute("SELECT * FROM price_promos WHERE id=? AND status='active'", (promo_id,))).fetchone()
        if not p:
            await d.rollback(); return None
        t = await (await d.execute('SELECT * FROM tariffs WHERE id=?', (p['tariff_id'],))).fetchone()
        if t and t['price'] == p['promo_price']:
            await d.execute('UPDATE tariffs SET price=?,old_price=? WHERE id=?', (p['original_price'], p['original_old_price'], t['id']))
            result = {'name': t['name'], 'from': p['promo_price'], 'to': p['original_price'], 'restored': True}
            final = status
        else:
            result = {'name': t['name'] if t else f"№{p['tariff_id']}", 'from': p['promo_price'], 'to': t['price'] if t else '', 'restored': False}
            final = 'superseded'
        await d.execute('UPDATE price_promos SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?', (final, promo_id))
        await d.commit()
    return result

async def tariff_channels(tariff):
    rows = await DB.run('SELECT tec.channel_id,COALESCE(NULLIF(tec.channel_title,""),c.title) AS channel_title FROM tariff_extra_channels tec JOIN channels c ON c.channel_id=tec.channel_id AND c.active=1 WHERE tec.tariff_id=? ORDER BY tec.channel_id', (tariff['id'],), all=True)
    result=[{'channel_id':tariff['channel_id'],'channel_title':tariff.get('channel_title') or str(tariff['channel_id'])}]
    seen={tariff['channel_id']}
    for row in rows:
        if row['channel_id'] not in seen:
            result.append(row); seen.add(row['channel_id'])
    return result

async def revoke_created_invites(bot, invites):
    """Best-effort cleanup for invite links created before a failed DB commit."""
    for channel, invite_link, _expiry in invites:
        try:
            await bot.revoke_chat_invite_link(
                chat_id=int(channel['channel_id']),
                invite_link=invite_link,
            )
        except Exception:
            log.warning(
                'Could not revoke orphan invite link for channel=%s',
                channel.get('channel_id'), exc_info=True,
            )

async def _approved_order_amount(row):
    # Keep historical approved payments countable even if a tariff was edited/deleted.
    amount=float(row.get('amount') or 0)
    if amount<=0:
        t=await DB.run('SELECT price FROM tariffs WHERE id=?',(row['tariff_id'],),one=True)
        match=re.search(r'\d+(?:[.,]\d+)?', str(t['price'] if t else ''))
        if match: amount=float(match.group(0).replace(',','.'))
    return amount

async def turnover_total():
    """All-time turnover, used by paid licenses and owner reporting."""
    rows=await DB.run("SELECT amount,tariff_id FROM orders WHERE status='approved'",all=True)
    total=0.0
    for row in rows:
        total += await _approved_order_amount(row)
    return total

async def free_license_month_turnover(now=None):
    """Approved turnover for the current UTC calendar month, for free admin license only.

    Uses reviewed_at (when payment was approved), falling back to created_at for
    legacy rows. The monthly counter resets automatically; history is preserved.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    month_key = (now.year, now.month)
    rows=await DB.run("SELECT amount,tariff_id,reviewed_at,created_at FROM orders WHERE status='approved'",all=True)
    total=0.0
    for row in rows:
        raw_date = row.get('reviewed_at') or row.get('created_at')
        if not raw_date:
            continue
        try:
            approved_at=datetime.fromisoformat(str(raw_date).replace('Z','+00:00'))
            if approved_at.tzinfo is None:
                approved_at=approved_at.replace(tzinfo=timezone.utc)
            approved_at=approved_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if (approved_at.year, approved_at.month) == month_key:
            total += await _approved_order_amount(row)
    return total

async def license_is_active():
    if await DB.setting('license_active','0')!='1': return False
    # The admin free allowance has no expiry; its monthly turnover counter resets by calendar month.
    if await DB.setting('license_turnover_capped','0') == '1': return True
    expiry=await DB.setting('license_expires_at','')
    if not expiry: return False
    try:
        exp=datetime.fromisoformat(expiry)
        if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
    except ValueError:
        return False

async def turnover_blocked():
    """Fail closed when the license is inactive/expired or its turnover allowance is exhausted.

    The special free allowance for bot admins resets by UTC calendar month. Paid
    licenses use the established all-time approved-turnover calculation. A valid
    paid license does not bypass its configured turnover limit.
    """
    if not await license_is_active():
        return True
    capped_free = await DB.setting('license_turnover_capped','0') == '1'
    try:
        limit = float(await DB.setting('turnover_limit','5000'))
    except (TypeError, ValueError):
        limit = 5000.0
    if limit <= 0:
        return True
    current_turnover = await free_license_month_turnover() if capped_free else await turnover_total()
    return current_turnover >= limit

async def show_tariff_card(message: Message, tariff_id: int, edit: bool = False):
    t = await DB.run('SELECT * FROM tariffs WHERE id=?', (tariff_id,), one=True)
    if not t:
        await message.answer('Тариф не найден или уже удалён.')
        return
    linked = await DB.run('SELECT pm.name FROM tariff_payment_methods tp JOIN payment_methods pm ON pm.id=tp.payment_method_id WHERE tp.tariff_id=? ORDER BY pm.id', (tariff_id,), all=True)
    extra_channels = await DB.run('SELECT channel_title FROM tariff_extra_channels WHERE tariff_id=? ORDER BY channel_id', (tariff_id,), all=True)
    promo = await active_promo(tariff_id)
    promo_card_line = f"⏳ Акция до {fmt_date(promo['ends_at'])}, затем цена вернётся: {promo['original_price']}\n" if promo else ''
    channel = (t['channel_title'] or str(t['channel_id'])).replace('\n', ' ')
    if extra_channels:
        channel += ' + ' + ', '.join(x['channel_title'] for x in extra_channels)
    text = (f"💳 Карточка тарифа: {t['name']}\n\n"
            f"Статус: {'🟢 активен' if t['active'] else '⏸ отключён'}\n"
            f"Цена: {price_line(t['price'], t.get('old_price'))}\n{promo_card_line}Срок: {t['days']} дней\n"
            f"Описание: {t['description'] or 'не задано'}\n"
            f"Канал(ы): {channel}\n"
            f"Способы оплаты: {', '.join(x['name'] for x in linked) if linked else 'не привязаны'}")
    buttons = [
        [('✏️ Название', f"edit:name:{tariff_id}"), ('✏️ Описание', f"edit:description:{tariff_id}")],
        [('💰 Цена', f"edit:price:{tariff_id}"), ('📅 Срок', f"edit:days:{tariff_id}")],
        [('🏷 Старая цена' + (' ✅' if (t.get('old_price') or '').strip() else ''), f"edit:old_price:{tariff_id}")],
        [('⏹ Завершить акцию сейчас', f"promo:end:{tariff_id}")] if promo else [('⏳ Акция на время', f"promo:start:{tariff_id}")],
        [('📣 Канал подписки', f"edit:channel_id:{tariff_id}")],
        [('💳 Способы оплаты', f"tariff:payments:{tariff_id}")],
        [('➕ Дополнительные каналы', f"tariff:channels:{tariff_id}")],
        [((('⏸ Отключить' if t['active'] else '🟢 Включить')), f"tariff:toggle:{tariff_id}"), ('🗑 Удалить', f"tariff:delete:{tariff_id}")],
        [('↩️ К списку тарифов', 'tariff:list')],
    ]
    if edit:
        try:
            await message.edit_text(text, reply_markup=ikb(buttons))
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=ikb(buttons))

async def show_tariffs(message: Message, is_admin=False):
    tariffs = await DB.tariffs(None if is_admin else 1)
    if not tariffs:
        if is_admin:
            await message.answer('Тарифов пока нет. Добавь первый тариф.', reply_markup=ikb([[('➕ Добавить тариф','tariff:add')],[('↩️ Меню','menu:admin')]]))
        else:
            await message.answer('Пока нет доступных тарифов.', reply_markup=MENU)
        return
    if not is_admin:
        rows = [[(f"{'🔥 ' if (t.get('old_price') or '').strip() else ''}{t['name']} — {t['price']} · {t['days']} дн."[:60], f"order:new:{t['id']}")] for t in tariffs]
        lines = ['💳 Доступные тарифы', '']
        promos = await active_promos()
        for t in tariffs:
            lines.append(f"• {t['name']} — {price_line(t['price'], t.get('old_price'))} · {t['days']} дн.")
            if t['id'] in promos:
                lines.append(f"   ⏳ Акция до {fmt_date(promos[t['id']]['ends_at'])}")
            desc = (t.get('description') or '').strip().replace('\n', ' ')
            if desc:
                lines.append('   ' + desc[:150] + ('…' if len(desc) > 150 else ''))
        text = '\n'.join(lines)
        if len(text) > 3700:  # Telegram message limit: keep the plain heading for very long lists
            text = '💳 Доступные тарифы'
        await message.answer(text + '\n\nВыбери подходящий тариф:', reply_markup=ikb(rows))
        return
    promos = await active_promos()
    rows = [[(f"{('🟢' if t['active'] else '⏸')} {t['name']} · {t['price']}{' 🏷' if (t.get('old_price') or '').strip() else ''}{' ⏳' if t['id'] in promos else ''} · {t['days']} дн."[:60], f"tariff:edit:{t['id']}")] for t in tariffs]
    if len(tariffs) >= 2:
        rows += [[('🧰 Массовое редактирование','bulk:start')]]
    rows += [[('➕ Добавить тариф','tariff:add')], [('↩️ Меню','menu:admin')]]
    await message.answer('💳 ТАРИФЫ ДЛЯ ПОЛЬЗОВАТЕЛЕЙ\n\nВыбери тариф, чтобы открыть его карточку и изменить настройки:', reply_markup=ikb(rows))

async def start(message: Message, state:FSMContext):
    await state.clear(); u=message.from_user
    if not u:return
    existed=await DB.run('SELECT user_id FROM users WHERE user_id=?',(u.id,),one=True)
    await DB.run('INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name',(u.id,u.username,u.first_name or ''))
    if not existed and not admin(u.id):
        joined=datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
        note=f'👤 Новый пользователь запустил бота\nИмя: {u.full_name}\nUsername: @{u.username or "—"}\nID: {u.id}\nВремя: {joined}'
        for owner_id in OWNER_IDS:
            try:
                if BOT_INSTANCE is not None: await BOT_INSTANCE.send_message(owner_id,note)
            except Exception: log.warning('Could not notify owner %s about new user %s',owner_id,u.id)
    await message.answer('Панель администратора.' if admin(u.id) else 'Добро пожаловать! Выберите тариф, отправьте подтверждение оплаты и дождитесь проверки администратором.',reply_markup=admin_menu_for(u.id) if admin(u.id) else MENU)

# ----------------------------------------------------------------------
# Crypto Bot (Crypto Pay API, https://help.send.tg/en/articles/10279948-crypto-pay-api)
# ----------------------------------------------------------------------
CRYPTO_API = {'main': 'https://pay.crypt.bot/api/', 'test': 'https://testnet-pay.crypt.bot/api/'}
CRYPTO_FIATS = ['RUB', 'USD', 'EUR', 'UAH', 'KZT', 'BYN', 'GEL', 'TRY', 'UZS', 'AMD', 'PLN', 'GBP']
CRYPTO_INVOICE_TTL = 3600          # seconds an invoice stays payable
CRYPTO_POLL_SECONDS = 10
CRYPTO_TOKEN_RE = re.compile(r'^\d{1,12}:[A-Za-z0-9_\-]{16,}$')

class CryptoError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

async def crypto_request(token: str, network: str, method: str, params: dict | None = None):
    url = CRYPTO_API.get(network, CRYPTO_API['main']) + method
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=params or {}, headers={'Crypto-Pay-API-Token': token}) as resp:
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise CryptoError('NETWORK') from e
    if not isinstance(data, dict) or not data.get('ok'):
        err = data.get('error') if isinstance(data, dict) else None
        name = err.get('name') if isinstance(err, dict) else None
        raise CryptoError(str(name or 'UNKNOWN'))
    return data.get('result')

async def crypto_config():
    token = (await DB.setting('cryptobot_token', '')).strip()
    if not token:
        return None
    return {'token': token,
            'network': (await DB.setting('cryptobot_network', 'main')) or 'main',
            'fiat': (await DB.setting('cryptobot_fiat', 'RUB')) or 'RUB'}

async def crypto_call(method: str, params: dict | None = None, cfg: dict | None = None):
    cfg = cfg or await crypto_config()
    if not cfg:
        raise CryptoError('NOT_CONNECTED')
    return await crypto_request(cfg['token'], cfg['network'], method, params)

def crypto_error_text(e: CryptoError) -> str:
    texts = {
        'NETWORK': 'Crypto Bot сейчас недоступен. Попробуйте через минуту.',
        'NOT_CONNECTED': 'Оплата через Crypto Bot временно недоступна.',
        'UNAUTHORIZED': 'Оплата через Crypto Bot временно недоступна: администратор должен обновить токен.',
        'AMOUNT_TOO_SMALL': 'Сумма тарифа меньше минимальной для оплаты через Crypto Bot.',
        'AMOUNT_TOO_LARGE': 'Сумма тарифа больше максимальной для оплаты через Crypto Bot.',
    }
    return texts.get(e.code, 'Не удалось создать счёт в Crypto Bot. Попробуйте позже или выберите другой способ оплаты.')

async def crypto_method_row():
    return await DB.run("SELECT * FROM payment_methods WHERE kind='cryptobot' ORDER BY id LIMIT 1", one=True)

async def crypto_ensure_method() -> dict:
    row = await crypto_method_row()
    if row:
        return row
    await DB.run("INSERT INTO payment_methods(name,details,active,kind) VALUES(?,?,1,'cryptobot')",
                 ('🪙 Crypto Bot', 'Оплата криптовалютой через Crypto Bot (USDT, TON, BTC и другие). Доступ выдаётся автоматически после оплаты.'))
    return await crypto_method_row()

async def main():
    if not TOKEN: raise RuntimeError('Не задан BOT_TOKEN в .env')
    if not ADMIN_IDS: raise RuntimeError('Не задан ADMIN_IDS в .env')
    global BOT_INSTANCE
    await DB.init(); bot=Bot(TOKEN); BOT_INSTANCE=bot; bot_info=await bot.get_me(); bot_id=bot_info.id; dp=Dispatcher()
    dp.message.register(start,CommandStart())
    async def tariffs_menu(m: Message):
        await show_tariffs(m, admin(m.from_user.id))
    dp.message.register(tariffs_menu,F.text=='💳 Тарифы')
    @dp.message(F.text=='👤 Моя подписка')
    async def my_sub(m:Message):
        rows=await DB.run("SELECT s.*,t.name AS tariff_name,t.channel_title FROM subscriptions s LEFT JOIN tariffs t ON t.id=s.tariff_id WHERE s.user_id=? AND s.status='active' ORDER BY s.expires_at",(m.from_user.id,),all=True)
        if not rows: await m.answer('Активной подписки нет.',reply_markup=MENU); return
        for s in rows: await m.answer(f"Тариф: {s['tariff_name'] or 'Удалённый тариф'}\nКанал: {s['channel_title'] or s['channel_id']}\nДоступ до: {fmt_date(s['expires_at'])}",reply_markup=MENU)
    async def build_admin_dashboard(uid: int):
        users_row = await DB.run("SELECT COUNT(*) AS n FROM users", one=True)
        active_subs_row = await DB.run(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE status='active' AND expires_at>?",
            (datetime.now(timezone.utc).isoformat(),), one=True)
        expiring_row = await DB.run(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE status='active' AND expires_at>? AND expires_at<=?",
            (datetime.now(timezone.utc).isoformat(),
             (datetime.now(timezone.utc)+timedelta(days=7)).isoformat()), one=True)
        pending_orders_row = await DB.run(
            "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending','processing')",
            one=True)
        active_tariffs_row = await DB.run("SELECT COUNT(*) AS n FROM tariffs WHERE active=1", one=True)
        active_channels_row = await DB.run("SELECT COUNT(*) AS n FROM channels WHERE active=1", one=True)
        approved_row = await DB.run(
            "SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS n FROM orders WHERE status='approved'",
            one=True)
        # Fallback for older records where amount was stored as zero.
        approved_total = float((approved_row or {}).get('total') or 0)
        approved_count = int((approved_row or {}).get('n') or 0)
        lines = [
            "📊 СТАТИСТИКА",
            "",
            f"👥 Пользователей: {int((users_row or {}).get('n') or 0)}",
            f"🔓 Активных подписок: {int((active_subs_row or {}).get('n') or 0)}",
            f"⏳ Подписок истекает за 7 дней: {int((expiring_row or {}).get('n') or 0)}",
            f"🛍 Покупок на проверке: {int((pending_orders_row or {}).get('n') or 0)}",
            f"💳 Активных тарифов: {int((active_tariffs_row or {}).get('n') or 0)}",
            f"📣 Активных каналов: {int((active_channels_row or {}).get('n') or 0)}",
            f"✅ Подтверждённых оплат: {approved_count}",
            f"💰 Сумма подтверждённых оплат: {approved_total:.2f}",
        ]
        # License/turnover details remain restricted to the platform owner.
        if uid in PLATFORM_OWNER_IDS:
            active = await license_is_active()
            capped = await DB.setting('license_turnover_capped', '0') == '1'
            expiry = await DB.setting('license_expires_at', '')
            limit = await DB.setting('turnover_limit', '5000')
            turnover = await free_license_month_turnover() if capped else await turnover_total()
            status = 'активна' if active else 'не активна / истекла'
            expiry_text = 'ежемесячный лимит' if capped else (fmt_date(expiry) if expiry else 'не задано')
            lines += [
                "",
                "🔐 ЛИЦЕНЗИЯ КОНСТРУКТОРА",
                f"Статус: {status}",
                f"Действует до: {expiry_text}",
                f"Оборот: {turnover:.2f} / {limit}",
            ]
        return "\n".join(lines)

    async def stats_markup():
        pending = (await DB.run("SELECT COUNT(*) AS n FROM orders WHERE status='pending' AND proof_file_id IS NOT NULL",one=True))['n']
        attention = (await DB.run("SELECT COUNT(*) AS n FROM orders WHERE status IN ('crypto_mismatch','paid_unprocessed')",one=True))['n']
        total = pending + attention
        reminders_on = (await DB.setting('reminders_enabled','1')) == '1'
        return ikb([[(f'🛍 Покупки на проверке ({total})' if total else '🛍 Покупки на проверке', 'orders:pending')],
                    [('👥 Подписчики', 'settings:subscribers')],
                    [(f"🔔 Напоминания о продлении: {'вкл' if reminders_on else 'выкл'}", 'stats:reminders')],
                    [('↩️ Меню', 'menu:admin')]])

    @dp.callback_query(F.data == 'stats:reminders')
    async def toggle_reminders(c: CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа', show_alert=True); return
        on = (await DB.setting('reminders_enabled','1')) == '1'
        await DB.set_setting('reminders_enabled', '0' if on else '1')
        await c.answer('Напоминания выключены' if on else 'Напоминания включены: за 3 дня и за 1 день до конца подписки')
        await admin_dashboard_refresh(c)

    async def send_pending_orders(chat_id: int, limit: int = 10) -> int:
        rows = await DB.run("SELECT o.*,t.name AS tariff_name,t.price AS tariff_price,u.first_name,u.username FROM orders o LEFT JOIN tariffs t ON t.id=o.tariff_id LEFT JOIN users u ON u.user_id=o.user_id WHERE o.status='pending' AND o.proof_file_id IS NOT NULL AND o.proof_type IN ('photo','document') ORDER BY o.id", all=True)
        stuck = await DB.run("SELECT o.id,o.user_id,o.status,o.amount,t.name AS tariff_name FROM orders o LEFT JOIN tariffs t ON t.id=o.tariff_id WHERE o.status IN ('crypto_mismatch','paid_unprocessed') ORDER BY o.id", all=True)
        if not rows and not stuck:
            await bot.send_message(chat_id, '✅ Покупок на проверке нет.', reply_markup=ikb([[('↩️ К статистике', 'stats:open')]])); return 0
        if rows:
            await bot.send_message(chat_id, f'🛍 Покупок с чеком ждёт решения: {len(rows)}' + (f'. Показаны первые {limit}, обработай их и нажми кнопку снова.' if len(rows) > limit else '.'))
        for o in rows[:limit]:
            caption = (f"🛍 Новая покупка\nПользователь: {o.get('first_name') or '—'} (@{o.get('username') or '—'}, ID {o['user_id']})\n"
                       f"Тариф: {o.get('tariff_name') or o['tariff_id']}\nСумма: {o.get('tariff_price') or '—'}\nЧек приложен." + await duplicate_receipt_note(o['id'], o.get('proof_unique_id') or ''))[:1020]
            controls = ikb([[('✅ Подтвердить оплату', f"order:approve:{o['id']}"), ('❌ Отклонить', f"order:reject:{o['id']}")]])
            try:
                if o['proof_type'] == 'photo': await bot.send_photo(chat_id, o['proof_file_id'], caption=caption, reply_markup=controls)
                else: await bot.send_document(chat_id, o['proof_file_id'], caption=caption, reply_markup=controls)
            except Exception:
                log.exception('Could not resend pending order %s', o['id'])
                await bot.send_message(chat_id, f"⚠️ Не удалось показать чек по заявке №{o['id']}.", reply_markup=controls)
            await asyncio.sleep(0.05)
        if stuck:
            names = {'crypto_mismatch': 'сумма или номер счёта не совпали', 'paid_unprocessed': 'оплачено, но тариф удалён'}
            await bot.send_message(chat_id, '⚠️ Требуют ручного решения (Crypto Bot, доступ не выдан):\n' + '\n'.join(f"• №{x['id']} · ID {x['user_id']} · {x.get('tariff_name') or 'тариф удалён'} · {float(x['amount']):.2f} · {names.get(x['status'], x['status'])}" for x in stuck))
        return len(rows) + len(stuck)

    @dp.callback_query(F.data.in_({'orders:pending', 'settings:orders'}))
    async def pending_orders_cb(c: CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа', show_alert=True); return
        await c.answer(); await send_pending_orders(c.from_user.id)

    @dp.message(F.text == '📊 Статистика')
    async def admin_dashboard(m: Message, state: FSMContext):
        if not admin(m.from_user.id):
            return
        await state.clear()
        text = await build_admin_dashboard(m.from_user.id)
        await m.answer(text, reply_markup=await stats_markup())

    @dp.message(F.text.in_({'📊 Сводка','🧾 Заявки на оплату','⚙️ Настройки'}))
    async def legacy_admin_buttons(m: Message, state: FSMContext):
        # Telegram keeps the old reply keyboard until a new one is sent, so admins
        # may still press buttons that were renamed or removed in this version.
        if not admin(m.from_user.id):
            return
        await state.clear()
        await m.answer('Меню обновлено.', reply_markup=admin_menu_for(m.from_user.id))
        if m.text == '📊 Сводка':
            await admin_dashboard(m, state)
        elif m.text == '⚙️ Настройки':
            await license_buy_menu(m, state)
        else:
            await send_pending_orders(m.from_user.id)

    @dp.callback_query(F.data == 'stats:open')
    async def admin_dashboard_refresh(c: CallbackQuery):
        if not admin(c.from_user.id):
            await c.answer('Нет доступа', show_alert=True)
            return
        text = await build_admin_dashboard(c.from_user.id)
        try:
            await c.message.edit_text(text, reply_markup=await stats_markup())
        except Exception:
            await c.message.answer(text, reply_markup=await stats_markup())
        await c.answer()

    @dp.message(F.text=='👥 Подписки')
    async def list_subs(m:Message):
        if not admin(m.from_user.id): return
        rows=await DB.run("SELECT s.*,u.username,t.name AS tariff_name FROM subscriptions s JOIN users u ON u.user_id=s.user_id LEFT JOIN tariffs t ON t.id=s.tariff_id WHERE s.status='active' ORDER BY s.expires_at LIMIT 50",all=True)
        if not rows: await m.answer('Активных подписок нет.'); return
        for s in rows: await m.answer(f"ID {s['id']} · {s['tariff_name'] or 'тариф удалён'}\nПользователь: {s['user_id']} (@{s['username'] or '—'})\nКанал: {s['channel_id']}\nДо: {fmt_date(s['expires_at'])}",reply_markup=ikb([[('⛔ Отозвать доступ',f"sub:revoke:{s['id']}")]]))
    async def show_payment_methods(m:Message, edit:bool=False):
        methods=await DB.run('SELECT * FROM payment_methods ORDER BY id DESC',all=True)
        # Migrate the old single instruction into a method the first time the admin opens this section.
        migrated=await DB.setting('payment_methods_migrated','0')
        if not methods and migrated!='1':
            old=await DB.setting('payment_instructions','')
            if old.strip():
                await DB.run('INSERT INTO payment_methods(name,details,active) VALUES(?,?,1)',('Основной способ оплаты',old))
                methods=await DB.run('SELECT * FROM payment_methods ORDER BY id DESC',all=True)
            await DB.set_setting('payment_methods_migrated','1')
        if not methods:
            text='Способы оплаты пока не добавлены. Нажми «➕ Добавить способ оплаты».'
            markup=ikb([[('➕ Добавить способ оплаты','pay:add'),('🪙 Crypto Bot','crypto:menu')],[('↩️ Меню','menu:admin')]])
            if edit:
                try: await m.edit_text(text,reply_markup=markup)
                except Exception: await m.answer(text,reply_markup=markup)
            else: await m.answer(text,reply_markup=markup)
            return
        lines=['💰 СПОСОБЫ ОПЛАТЫ','']
        buttons=[]
        for i,method in enumerate(methods,1):
            status='🟢 Включён' if method['active'] else '⏸ Отключён'
            details=(method['details'] or '').replace('\n',' ')
            lines.append(f"{i}. {method['name']} · {status}\n   {details[:180]}")
            buttons.append([('✏️ Редактировать',f"pay:edit:{method['id']}"),(('⏸ Отключить' if method['active'] else '🟢 Включить'),f"pay:toggle:{method['id']}")])
            buttons.append([('🗑 Удалить',f"pay:delete:{method['id']}")])
        buttons.extend([[('➕ Добавить способ оплаты','pay:add'),('🪙 Crypto Bot','crypto:menu')],[('↩️ Меню','menu:admin')]])
        text='\n'.join(lines)
        if len(text)>3900:
            kept=[]
            for line in lines:
                if len('\n'.join(kept+[line]))>3700: break
                kept.append(line)
            shown=max(0,len([x for x in kept if x and x[0].isdigit()]))
            remaining=len(methods)-shown
            text='\n'.join(kept)+f"\n\n⚠️ Сообщение ограничено Telegram: не показано способов оплаты — {remaining}. Сократи инструкции или количество способов."
            buttons=buttons[:shown*2]+[[('➕ Добавить способ оплаты','pay:add'),('🪙 Crypto Bot','crypto:menu')],[('↩️ Меню','menu:admin')]]
        markup=ikb(buttons)
        if edit:
            try: await m.edit_text(text,reply_markup=markup)
            except Exception: await m.answer(text,reply_markup=markup)
        else: await m.answer(text,reply_markup=markup)

    @dp.message(F.text=='💰 Способы оплаты')
    async def paysettings(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        await state.clear()
        await show_payment_methods(m)

    async def license_menu_payload(uid:int):
        is_owner = uid in PLATFORM_OWNER_IDS
        if is_owner:
            active=await license_is_active()
            capped=await DB.setting('license_turnover_capped','0')=='1'
            exp=await DB.setting('license_expires_at','')
            limit=await DB.setting('turnover_limit','5000')
            total=await free_license_month_turnover() if capped else await turnover_total()
            status='активна' if active else 'не активна'
            expiry_text='без срока, лимит обновляется ежемесячно' if capped else (fmt_date(exp) if exp else 'не задано')
            turnover_text='оборот за текущий месяц' if capped else 'оборот за всё время'
            intro=f'💳 Оплата и лицензия\n\nСтатус лицензии: {status}\nДействует до: {expiry_text}\n{turnover_text.capitalize()}: {total:.2f}\nЛимит оборота: {limit}\n\nВы можете купить лицензию или продлить действующую.'
        else:
            intro='💳 Оплата лицензии\n\nПокупка и продление доступны ниже. Данные об обороте и статусе лицензии доступны только владельцу основного конструктора.'
        plans=await DB.run('SELECT * FROM license_tariffs WHERE active=1 ORDER BY price,id',all=True)
        rows=[[(f"{x['name'][:30]} · {x['price']:g} · {x['duration_days']} дн.",f"license:plan:{x['id']}")] for x in plans]
        if is_owner: rows.append([('⚙️ Управление лицензией','owner:settings')])
        rows.append([('↩️ Меню','menu:admin')])
        if plans: text=intro+'\n\nВыбери тариф для покупки или продления. После оплаты отправь фото или файл чека.'
        else: text=intro+'\n\nНет доступных тарифов лицензии. Обратитесь к владельцу бота.'
        return text, ikb(rows)

    @dp.message(F.text=='💳 Оплата')
    async def license_buy_menu(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        await state.clear()
        text,markup=await license_menu_payload(m.from_user.id)
        await m.answer(text,reply_markup=markup)

    @dp.callback_query(F.data.startswith('license:plan:'))
    async def license_choose_plan(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Только администратор',show_alert=True); return
        try: plan_id=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Неверный тариф',show_alert=True); return
        plan=await DB.run('SELECT * FROM license_tariffs WHERE id=? AND active=1',(plan_id,),one=True)
        if not plan: await c.answer('Тариф недоступен',show_alert=True); return
        if int(plan.get('admin_free') or 0) == 1:
            await c.answer('Бесплатный лимит выдаётся автоматически и не продаётся.',show_alert=True); return
        methods=await DB.run('SELECT * FROM license_payment_methods WHERE active=1 ORDER BY id',all=True)
        if not methods: await c.answer('Нет активных способов оплаты лицензии',show_alert=True); return
        rows=[[(x['name'][:45],f"license:pay:{plan_id}:{x['id']}")] for x in methods]
        rows.append([('↩️ К тарифам лицензии','license:backplans')])
        await c.message.answer(f"Тариф: {plan['name']}\nЛимит оборота: {plan['turnover_limit']:g}\nЦена: {plan['price']:g}\nСрок лицензии: {plan['duration_days']} дней\n\nВыбери способ оплаты:",reply_markup=ikb(rows)); await c.answer()

    @dp.callback_query(F.data=='license:backplans')
    async def license_back_plans(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Только администратор',show_alert=True); return
        plans=await DB.run('SELECT * FROM license_tariffs WHERE active=1 ORDER BY price,id',all=True)
        rows=[[(f"{x['name'][:30]} · {x['price']:g} · {x['duration_days']} дн.",f"license:plan:{x['id']}")] for x in plans]
        rows.append([('↩️ Меню','menu:admin')])
        await c.message.answer('Выбери тариф лицензии:',reply_markup=ikb(rows)); await c.answer()

    @dp.callback_query(F.data.startswith('license:pay:'))
    async def license_choose_payment(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Только администратор',show_alert=True); return
        parts=c.data.split(':')
        try: plan_id,mid=int(parts[2]),int(parts[3])
        except (ValueError,IndexError): await c.answer('Неверный тариф или способ оплаты',show_alert=True); return
        # Serialize checkout creation. A separate SELECT followed by INSERT can
        # race when the user taps several payment buttons quickly or Telegram
        # delivers duplicate callbacks concurrently.
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            d.row_factory = aiosqlite.Row
            await d.execute('BEGIN IMMEDIATE')
            plan_row = await (await d.execute('SELECT * FROM license_tariffs WHERE id=? AND active=1', (plan_id,))).fetchone()
            method_row = await (await d.execute('SELECT * FROM license_payment_methods WHERE id=? AND active=1', (mid,))).fetchone()
            if not plan_row or not method_row:
                await d.rollback()
                await c.answer('Тариф или способ оплаты недоступен', show_alert=True)
                return
            plan, method = dict(plan_row), dict(method_row)
            pending = await (await d.execute("SELECT id FROM license_payments WHERE user_id=? AND status='pending' LIMIT 1", (c.from_user.id,))).fetchone()
            if pending:
                await d.rollback()
                await c.answer(f"У вас уже есть заявка L{pending['id']} на проверке владельца. Дождитесь решения.", show_alert=True)
                return
            # A draft without a receipt may be replaced, but never while a receipt
            # is waiting for the constructor owner to review it.
            await d.execute("UPDATE license_payments SET status='cancelled' WHERE user_id=? AND status='awaiting_proof'", (c.from_user.id,))
            amount = float(plan['price'])
            cur = await d.execute("INSERT INTO license_payments(user_id,amount,payment_method_id,status,license_tariff_id,license_tariff_name,license_turnover_limit,license_duration_days) VALUES(?,?,?,'awaiting_proof',?,?,?,?)", (c.from_user.id, amount, mid, plan_id, plan['name'], float(plan['turnover_limit']), int(plan['duration_days'])))
            pid = cur.lastrowid
            await d.commit()
        await c.message.answer(f"Лицензия: {plan['name']}\nЛимит оборота: {plan['turnover_limit']:g}\nСумма: {amount:g}\nСрок: {plan['duration_days']} дней\nСпособ оплаты: {method['name']}\n{method['details']}\n\nПосле оплаты загрузите фото или файл чека сюда. Номер заявки: L{pid}.",reply_markup=admin_menu_for(c.from_user.id))
        await c.answer('Загрузите чек после оплаты')

    @dp.callback_query(F.data=='settings:subscribers')
    async def settings_subscribers(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await c.message.answer('Управление подписчиками:',reply_markup=ikb([[('📋 Активные подписки','settings:subs:active')],[('🔎 Найти подписчика по ID','settings:subs:find')],[('↩️ К статистике','stats:open')]])); await c.answer()

    @dp.callback_query(F.data=='settings:subs:active')
    async def settings_active_subs(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        rows=await DB.run("SELECT s.*,u.username,t.name AS tariff_name FROM subscriptions s LEFT JOIN users u ON u.user_id=s.user_id LEFT JOIN tariffs t ON t.id=s.tariff_id WHERE s.status='active' ORDER BY s.expires_at LIMIT 50",all=True)
        if not rows: await c.message.answer('Активных подписок нет.')
        for sub in rows:
            await c.message.answer(f"Подписка №{sub['id']} · {sub['tariff_name'] or 'тариф удалён'}\nПользователь: {sub['user_id']} (@{sub['username'] or '—'})\nКанал: {sub['channel_id']}\nДо: {fmt_date(sub['expires_at'])}",reply_markup=ikb([[('⛔ Отозвать доступ',f"sub:revoke:{sub['id']}")]]))
        await c.answer()

    @dp.callback_query(F.data=='settings:subs:find')
    async def settings_find_subscriber(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.set_state(EditChannel.value); await state.update_data(mode='find_subscriber'); await c.message.answer('Отправь числовой Telegram ID пользователя:'); await c.answer()

    @dp.callback_query(F.data=='settings:payments')
    async def settings_payments(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await show_payment_methods(c.message, edit=True); await c.answer()

    @dp.callback_query(F.data=='settings:back')
    async def settings_back(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        text,markup=await license_menu_payload(c.from_user.id)
        try: await c.message.edit_text(text,reply_markup=markup)
        except Exception: await c.message.answer(text,reply_markup=markup)
        await c.answer()

    @dp.callback_query(F.data=='pay:add')
    async def add_payment_method(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.set_state(PaymentMethodWizard.name)
        await c.message.answer('Название способа оплаты (например, Перевод по СБП):')
        await c.answer()

    @dp.message(PaymentMethodWizard.name)
    async def payment_method_name(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        name=(m.text or '').strip()
        if not name: await m.answer('Название не должно быть пустым.'); return
        await state.update_data(payment_name=name[:100])
        await state.set_state(PaymentMethodWizard.details)
        await m.answer('Инструкция по оплате: реквизиты, порядок оплаты, ссылки и дополнительные пояснения. Можно отправить одним сообщением.')

    @dp.message(PaymentMethodWizard.details)
    async def payment_method_details(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        details=(m.text or '').strip()
        if not details: await m.answer('Инструкция не должна быть пустой.'); return
        d=await state.get_data()
        await DB.run('INSERT INTO payment_methods(name,details,active) VALUES(?,?,1)',(d['payment_name'],details[:3500]))
        await state.clear()
        await m.answer('Способ оплаты добавлен и включён.',reply_markup=admin_menu_for(m.from_user.id))
        await show_payment_methods(m)

    @dp.callback_query(F.data.startswith('pay:edit:'))
    async def edit_payment_method(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        mid=int(c.data.rsplit(':',1)[1])
        method=await DB.run('SELECT * FROM payment_methods WHERE id=?',(mid,),one=True)
        if not method: await c.answer('Способ оплаты не найден',show_alert=True); return
        await c.message.edit_text(f"Редактирование: {method['name']}",reply_markup=ikb([[('Название','payfield:name:'+str(mid)),('Инструкция','payfield:details:'+str(mid))],[('↩️ Настройки оплаты','pay:back')]]))
        await c.answer()

    @dp.callback_query(F.data.startswith('payfield:'))
    async def edit_payment_field(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        _,field,mid=c.data.split(':')
        if field not in ('name','details'): await c.answer('Неизвестное поле',show_alert=True); return
        await state.update_data(payment_method_id=int(mid),payment_field=field)
        await state.set_state(EditPaymentMethod.value)
        await c.message.answer('Отправь новое значение названия:' if field=='name' else 'Отправь новую инструкцию оплаты:')
        await c.answer()

    @dp.message(EditPaymentMethod.value)
    async def save_payment_field(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        data=await state.get_data(); value=(m.text or '').strip()
        if not value: await m.answer('Значение не должно быть пустым.'); return
        field=data['payment_field']; mid=data['payment_method_id']
        await DB.run(f'UPDATE payment_methods SET {field}=? WHERE id=?',(value[:100] if field=='name' else value[:3500],mid))
        await state.clear()
        await m.answer('Способ оплаты обновлён.',reply_markup=admin_menu_for(m.from_user.id))
        await show_payment_methods(m)

    @dp.callback_query(F.data.startswith('pay:toggle:'))
    async def toggle_payment_method(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        mid=int(c.data.rsplit(':',1)[1])
        await DB.run('UPDATE payment_methods SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(mid,))
        await c.answer('Статус изменён')
        await show_payment_methods(c.message, edit=True)

    @dp.callback_query(F.data.startswith('pay:delete:'))
    async def delete_payment_method(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        mid=int(c.data.rsplit(':',1)[1])
        method=await DB.run('SELECT name FROM payment_methods WHERE id=?',(mid,),one=True)
        if not method: await c.answer('Способ оплаты не найден',show_alert=True); return
        await c.message.edit_text(f"Удалить способ оплаты «{method['name']}»?",reply_markup=ikb([[('Да, удалить','pay:deleteyes:'+str(mid)),('Отмена','pay:back')]]))
        await c.answer()

    @dp.callback_query(F.data.startswith('pay:deleteyes:'))
    async def delete_payment_method_yes(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        mid=int(c.data.rsplit(':',1)[1])
        await DB.run('DELETE FROM payment_methods WHERE id=?',(mid,))
        await c.answer('Способ оплаты удалён')
        await show_payment_methods(c.message, edit=True)

    @dp.callback_query(F.data=='pay:back')
    async def payment_methods_back(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await show_payment_methods(c.message, edit=True)
        await c.answer()

    BC_SEGMENTS={'all':'👥 Все пользователи','active':'🟢 Активные подписчики','expired':'⌛ Истёкшие подписки','never':'🆕 Без подписки'}
    async def bc_recipients(seg:str, tariff_id:int|None=None)->list:
        now=datetime.now(timezone.utc).isoformat()
        active_sub="EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=u.user_id AND s.status='active' AND s.expires_at>?%s)"
        if seg=='all': rows=await DB.run('SELECT user_id FROM users ORDER BY user_id',all=True); return [r['user_id'] for r in rows]
        if seg=='active': rows=await DB.run(f"SELECT u.user_id FROM users u WHERE {active_sub%''} ORDER BY u.user_id",(now,),all=True)
        elif seg=='tariff': rows=await DB.run(f"SELECT u.user_id FROM users u WHERE {active_sub%' AND s.tariff_id=?'} ORDER BY u.user_id",(now,int(tariff_id or 0)),all=True)
        elif seg=='expired': rows=await DB.run(f"SELECT u.user_id FROM users u WHERE EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=u.user_id) AND NOT {active_sub%''} ORDER BY u.user_id",(now,),all=True)
        elif seg=='never': rows=await DB.run("SELECT u.user_id FROM users u WHERE NOT EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=u.user_id) ORDER BY u.user_id",all=True)
        else: return []
        return [r['user_id'] for r in rows if not admin(r['user_id'])]
    async def bc_label(seg:str, tariff_id:int|None=None)->str:
        if seg=='tariff':
            t=await DB.run('SELECT name FROM tariffs WHERE id=?',(int(tariff_id or 0),),one=True)
            return f"🎯 Подписчики тарифа «{t['name'] if t else tariff_id}»"
        return BC_SEGMENTS.get(seg,seg)
    @dp.message(F.text=='📨 Рассылка')
    async def broadcast_start(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        await state.clear()
        rows=[[(f"{label} ({len(await bc_recipients(key))})",f'bc:seg:{key}')] for key,label in BC_SEGMENTS.items()]
        rows.append([('🎯 Подписчики определённого тарифа','bc:tariffs')]); rows.append([('📋 Последние рассылки','bc:history')]); rows.append([('❌ Отмена','bc:exit')])
        await m.answer('📨 Рассылка\n\nКому отправить сообщение? В скобках указано число получателей. Администраторы в группы подписчиков не входят.',reply_markup=ikb(rows))
    @dp.callback_query(F.data=='bc:exit')
    async def broadcast_exit(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.clear(); await safe_edit(c.message,'Рассылка отменена.',None); await c.answer()
    @dp.callback_query(F.data=='bc:tariffs')
    async def broadcast_pick_tariff(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        tariffs=await DB.tariffs(None)
        if not tariffs: await c.answer('Тарифов пока нет',show_alert=True); return
        rows=[[(f"{t['name']} ({len(await bc_recipients('tariff',t['id']))})"[:60],f"bc:t:{t['id']}")] for t in tariffs[:60]]
        rows.append([('⬅️ Назад','bc:back')])
        await safe_edit(c.message,'🎯 Выбери тариф. В скобках указано число активных подписчиков.',ikb(rows)); await c.answer()
    @dp.callback_query(F.data=='bc:back')
    async def broadcast_back(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        rows=[[(f"{label} ({len(await bc_recipients(key))})",f'bc:seg:{key}')] for key,label in BC_SEGMENTS.items()]
        rows.append([('🎯 Подписчики определённого тарифа','bc:tariffs')]); rows.append([('📋 Последние рассылки','bc:history')]); rows.append([('❌ Отмена','bc:exit')])
        await safe_edit(c.message,'📨 Рассылка\n\nКому отправить сообщение? В скобках указано число получателей. Администраторы в группы подписчиков не входят.',ikb(rows)); await c.answer()
    @dp.callback_query(F.data.startswith('bc:seg:') | F.data.startswith('bc:t:'))
    async def broadcast_choose(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        parts=c.data.split(':')
        if parts[1]=='seg':
            seg=parts[2]; tariff_id=None
            if seg not in BC_SEGMENTS: await c.answer('Неизвестная группа',show_alert=True); return
        else:
            seg='tariff'
            try: tariff_id=int(parts[2])
            except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        n=len(await bc_recipients(seg,tariff_id))
        if n==0: await c.answer('В этой группе пока нет получателей',show_alert=True); return
        label=await bc_label(seg,tariff_id)
        await state.set_state(BroadcastState.content); await state.update_data(bc_seg=seg,bc_tariff=tariff_id)
        await safe_edit(c.message,f'📨 Рассылка · {label}\nПолучателей сейчас: {n}',None)
        await c.message.answer('Отправь сообщение, которое нужно разослать: текст, фото, видео или документ с подписью. Форматирование и подпись сохранятся.\n\nПеред отправкой бот покажет предпросмотр и попросит подтверждение. Для отмены отправь /cancel.')
        await c.answer()

    @dp.message(BroadcastState.content)
    async def broadcast_prepare(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        if (m.text or '').strip().lower()=='/cancel':
            await state.clear(); await m.answer('Рассылка отменена.',reply_markup=admin_menu_for(m.from_user.id)); return
        if m.content_type in {'new_chat_members','left_chat_member','pinned_message','group_chat_created','supergroup_chat_created','channel_chat_created'}:
            await m.answer('Этот тип сообщения нельзя использовать для рассылки. Отправь текст, фото, видео или документ либо /cancel.'); return
        data=await state.get_data(); seg=data.get('bc_seg') or 'all'; tariff_id=data.get('bc_tariff')
        n=len(await bc_recipients(seg,tariff_id)); label=await bc_label(seg,tariff_id)
        await state.update_data(broadcast_chat_id=m.chat.id,broadcast_message_id=m.message_id)
        await state.set_state(BroadcastState.confirm)
        try:
            await bot.copy_message(chat_id=m.chat.id,from_chat_id=m.chat.id,message_id=m.message_id,reply_markup=ikb([[(f'✅ Отправить ({n})','broadcast:confirm')],[('❌ Отменить','broadcast:cancel')]]))
            await m.answer(f'Это предпросмотр рассылки.\nКому: {label}\nПолучателей: {n}\n\nПроверь сообщение и нажми «Отправить» или «Отменить».')
        except Exception as e:
            log.exception('Не удалось подготовить предпросмотр рассылки: %s',e)
            await state.clear(); await m.answer('Не удалось подготовить предпросмотр. Попробуй отправить другое сообщение.',reply_markup=admin_menu_for(m.from_user.id))

    @dp.callback_query(F.data=='broadcast:cancel')
    async def broadcast_cancel(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.clear()
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await c.message.answer('Рассылка отменена.',reply_markup=admin_menu_for(c.from_user.id))
        await c.answer('Отменено')

    def bc_progress_text(job:dict, sent:int, failed:int, final:str='') -> str:
        total=int(job['total']); done=sent+failed; pct=int(done*100/total) if total else 100
        bar='█'*(pct//10)+'░'*(10-pct//10)
        head=f"📨 Рассылка №{job['id']} · {job['label']}"
        body=f"{bar} {pct}%\nОтправлено: {sent} из {total}\nНе доставлено: {failed}"
        return head+'\n'+body+(('\n\n'+final) if final else '')
    async def bc_counts(job_id:int):
        row=await DB.run("SELECT COALESCE(SUM(status='sent'),0) AS sent,COALESCE(SUM(status='failed'),0) AS failed FROM broadcast_recipients WHERE job_id=?",(job_id,),one=True)
        return int(row['sent']),int(row['failed'])
    async def bc_edit_progress(job:dict, text:str, with_stop:bool):
        if not job.get('progress_chat_id') or not job.get('progress_message_id'): return
        markup=ikb([[('⏹ Остановить рассылку',f"bcj:stop:{job['id']}")]]) if with_stop else None
        try: await bot.edit_message_text(text,chat_id=job['progress_chat_id'],message_id=job['progress_message_id'],reply_markup=markup)
        except Exception: pass
    async def bc_run_job(job:dict):
        jid=job['id']; last_edit=0.0; loop=asyncio.get_running_loop()
        while True:
            batch=await DB.run("SELECT user_id FROM broadcast_recipients WHERE job_id=? AND status='pending' ORDER BY user_id LIMIT 25",(jid,),all=True)
            if not batch: break
            for item in batch:
                cur=await DB.run('SELECT status FROM broadcast_jobs WHERE id=?',(jid,),one=True)
                if not cur or cur['status']!='running': return   # stopped by an admin: send nothing more
                uid_=item['user_id']; attempts=0; outcome='failed'
                while True:
                    try:
                        await bot.copy_message(chat_id=uid_,from_chat_id=job['source_chat_id'],message_id=job['source_message_id']); outcome='sent'; break
                    except TelegramRetryAfter as e:
                        await asyncio.sleep(float(e.retry_after)+1)       # flood limit: wait and retry the same user
                    except TelegramForbiddenError: break                  # user blocked the bot
                    except TelegramBadRequest as e:
                        if 'to copy not found' in str(e).lower():
                            await DB.run("UPDATE broadcast_jobs SET status='stopped',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",(jid,))
                            sent,failed=await bc_counts(jid)
                            await bc_edit_progress(job,bc_progress_text(job,sent,failed,'⛔ Остановлено: исходное сообщение удалено из чата. Не удаляй сообщение, пока идёт рассылка.'),False)
                            await notify_admins(f'⛔ Рассылка №{jid} остановлена: исходное сообщение удалено. Отправлено {sent} из {job["total"]}.')
                            return
                        break
                    except (TelegramNetworkError,asyncio.TimeoutError):
                        attempts+=1
                        if attempts>=3: break
                        await asyncio.sleep(2)
                    except Exception: break
                await DB.run('UPDATE broadcast_recipients SET status=? WHERE job_id=? AND user_id=?',(outcome,jid,uid_))
                await asyncio.sleep(0.05)
            if loop.time()-last_edit>3:
                sent,failed=await bc_counts(jid); await bc_edit_progress(job,bc_progress_text(job,sent,failed),True); last_edit=loop.time()
        changed=await DB.run("UPDATE broadcast_jobs SET status='done',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",(jid,))
        if changed==1:
            sent,failed=await bc_counts(jid)
            await bc_edit_progress(job,bc_progress_text(job,sent,failed,'✅ Готово'),False)
            try: await bot.send_message(job['created_by'],f"✅ Рассылка №{jid} завершена.\nКому: {job['label']}\nПолучателей: {job['total']}\nУспешно отправлено: {sent}\nНе доставлено: {failed} (заблокировали бота или удалили чат).",reply_markup=admin_menu_for(job['created_by']))
            except Exception: pass
    bc_wakeup=asyncio.Event()
    async def broadcast_worker():
        # Jobs live in the database, so a restart of the bot simply continues unfinished broadcasts.
        while True:
            try:
                job=await DB.run("SELECT * FROM broadcast_jobs WHERE status='running' ORDER BY id LIMIT 1",one=True)
                if job: await bc_run_job(job)
                else:
                    try: await asyncio.wait_for(bc_wakeup.wait(),timeout=5)
                    except asyncio.TimeoutError: pass
                    bc_wakeup.clear()
            except Exception:
                log.exception('Broadcast worker failed'); await asyncio.sleep(5)
    @dp.callback_query(F.data=='broadcast:confirm')
    async def broadcast_confirm(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        if await state.get_state()!=BroadcastState.confirm.state:
            await c.answer('Нет активной рассылки. Начни заново.',show_alert=True); return
        data=await state.get_data(); source_chat=data.get('broadcast_chat_id'); source_message=data.get('broadcast_message_id')
        if source_chat is None or source_message is None:
            await state.clear(); await c.answer('Не удалось найти сообщение для рассылки.',show_alert=True); return
        seg=data.get('bc_seg') or 'all'; tariff_id=data.get('bc_tariff')
        recipients=await bc_recipients(seg,tariff_id)
        if not recipients: await state.clear(); await c.answer('В выбранной группе нет получателей.',show_alert=True); return
        label=await bc_label(seg,tariff_id)
        await state.clear()
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        job_id=await DB.run("INSERT INTO broadcast_jobs(created_by,source_chat_id,source_message_id,label,total,progress_chat_id) VALUES(?,?,?,?,?,?)",(c.from_user.id,source_chat,source_message,label,len(recipients),c.message.chat.id))
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.executemany('INSERT OR IGNORE INTO broadcast_recipients(job_id,user_id) VALUES(?,?)',[(job_id,uid_) for uid_ in recipients]); await d.commit()
        job=await DB.run('SELECT * FROM broadcast_jobs WHERE id=?',(job_id,),one=True)
        progress=await c.message.answer(bc_progress_text(job,0,0),reply_markup=ikb([[('⏹ Остановить рассылку',f'bcj:stop:{job_id}')]]))
        await DB.run('UPDATE broadcast_jobs SET progress_message_id=? WHERE id=?',(progress.message_id,job_id))
        bc_wakeup.set()
        await c.answer('Рассылка запущена')
    @dp.callback_query(F.data.startswith('bcj:stop:'))
    async def broadcast_stop(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: jid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректная рассылка',show_alert=True); return
        changed=await DB.run("UPDATE broadcast_jobs SET status='stopped',finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",(jid,))
        if changed!=1: await c.answer('Рассылка уже завершена или остановлена',show_alert=True); return
        job=await DB.run('SELECT * FROM broadcast_jobs WHERE id=?',(jid,),one=True); sent,failed=await bc_counts(jid)
        await safe_edit(c.message,bc_progress_text(job,sent,failed,'⏹ Остановлено администратором. Уже отправленные сообщения остаются у получателей.'),None)
        await c.answer('Рассылка остановлена')
    @dp.callback_query(F.data=='bc:history')
    async def broadcast_history(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        jobs=await DB.run('SELECT * FROM broadcast_jobs ORDER BY id DESC LIMIT 5',all=True)
        if not jobs: await c.answer('Рассылок ещё не было',show_alert=True); return
        names={'running':'⏳ идёт','done':'✅ завершена','stopped':'⏹ остановлена'}; lines=['📋 ПОСЛЕДНИЕ РАССЫЛКИ','']
        for j in jobs:
            sent,failed=await bc_counts(j['id'])
            lines.append(f"№{j['id']} · {j['label']}\n   {names.get(j['status'],j['status'])} · отправлено {sent} из {j['total']}, не доставлено {failed}\n   {fmt_date(j['created_at'].replace(' ','T')+'+00:00') if j['created_at'] else ''}")
        await safe_edit(c.message,'\n'.join(lines),ikb([[('⬅️ Назад','bc:back')]])); await c.answer()

    async def show_owner_license_plans(m:Message, edit:bool=False):
        plans=await DB.run('SELECT * FROM license_tariffs ORDER BY id',all=True)
        lines=['📈 ТАРИФЫ ОБОРОТА И ЛИЦЕНЗИИ','']
        buttons=[]
        for x in plans:
            price_text = 'БЕСПЛАТНЫЙ ДЛЯ АДМИНОВ' if int(x.get('admin_free') or 0) else f"цена {x['price']:g}"
            state_text = 'активен' if x['active'] else 'выключен'
            lines.append(f"{x['id']}. {x['name']} · лимит {x['turnover_limit']:g} · {price_text} · {x['duration_days']} дн. · {state_text}")
            buttons.append([('✏️ Настроить',f"owner:plan:edit:{x['id']}"),(('⏸ Выключить' if x['active'] else '🟢 Включить'),f"owner:plan:toggle:{x['id']}")])
            buttons.append([('🗑 Удалить',f"owner:plan:delete:{x['id']}")])
        buttons += [[('➕ Добавить тариф','owner:plan:add')],[('↩️ Оборот и лицензия','owner:settings')]]
        text='\n'.join(lines) if plans else '📈 Тарифов оборота и лицензии пока нет.'
        if edit:
            try: await m.edit_text(text,reply_markup=ikb(buttons))
            except Exception: await m.answer(text,reply_markup=ikb(buttons))
        else: await m.answer(text,reply_markup=ikb(buttons))

    @dp.callback_query(F.data=='owner:tariffs')
    async def owner_manage_tariffs(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        await show_owner_license_plans(c.message, edit=True); await c.answer()

    @dp.callback_query(F.data=='owner:plan:add')
    async def owner_plan_add(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        await state.clear(); await state.set_state(OwnerLicensePlanState.value); await state.update_data(plan_mode='add',plan_field='name',plan_values={})
        await c.message.answer('Название тарифа оборота и лицензии:'); await c.answer()

    @dp.callback_query(F.data.startswith('owner:plan:edit:'))
    async def owner_plan_edit(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        plan_id=int(c.data.rsplit(':',1)[1]); plan=await DB.run('SELECT * FROM license_tariffs WHERE id=?',(plan_id,),one=True)
        if not plan: await c.answer('Тариф не найден',show_alert=True); return
        fields=[[('Название','owner:plan:field:name:'+str(plan_id))],[('Лимит оборота','owner:plan:field:limit:'+str(plan_id))]]
        if not int(plan.get('admin_free') or 0): fields.append([('Цена','owner:plan:field:price:'+str(plan_id))])
        fields += [[('Срок лицензии','owner:plan:field:duration:'+str(plan_id))],[('↩️ К тарифам','owner:tariffs')]]
        await c.message.answer(f"Редактирование тарифа «{plan['name']}»:",reply_markup=ikb(fields)); await c.answer()

    @dp.callback_query(F.data.startswith('owner:plan:field:'))
    async def owner_plan_field(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        parts=c.data.split(':'); field=parts[3]; plan_id=int(parts[4])
        await state.clear(); await state.set_state(OwnerLicensePlanState.value); await state.update_data(plan_mode='edit',plan_id=plan_id,plan_field=field)
        prompts={'name':'Новое название тарифа:','limit':'Новый лимит оборота числом:','price':'Новая цена тарифа числом:','duration':'Новый срок лицензии в целых днях:'}
        await c.message.answer(prompts[field]); await c.answer()

    @dp.message(OwnerLicensePlanState.value)
    async def owner_plan_value(m:Message,state:FSMContext):
        if m.from_user.id not in PLATFORM_OWNER_IDS: return
        data=await state.get_data(); field=data.get('plan_field'); mode=data.get('plan_mode')
        if mode=='add':
            vals=data.get('plan_values',{})
            if field=='name':
                value=(m.text or '').strip()
                if not value or len(value)>80: await m.answer('Название должно быть от 1 до 80 символов.'); return
                vals['name']=value; await state.update_data(plan_values=vals,plan_field='limit'); await m.answer('Лимит оборота для этого тарифа числом:'); return
            if field in ('limit','price','duration'):
                try: value=float((m.text or '').replace(',','.'))
                except ValueError: await m.answer('Введи число.'); return
                if not math.isfinite(value) or value<=0 or value>100000000000: await m.answer('Значение должно быть конечным числом больше нуля и в допустимом диапазоне.'); return
                if field=='duration' and not value.is_integer(): await m.answer('Срок должен быть целым числом дней.'); return
                vals[field]=int(value) if field=='duration' else value
                next_field={'limit':'price','price':'duration','duration':None}[field]
                if next_field:
                    await state.update_data(plan_values=vals,plan_field=next_field)
                    await m.answer({'price':'Цена тарифа числом:','duration':'Срок лицензии в целых днях:'}[next_field]); return
                await DB.run('INSERT INTO license_tariffs(name,turnover_limit,price,duration_days) VALUES(?,?,?,?)',(vals['name'],vals['limit'],vals['price'],vals['duration']))
                await state.clear(); await m.answer('Тариф оборота и лицензии добавлен.'); await show_owner_license_plans(m); return
        else:
            value=(m.text or '').strip()
            existing_plan=await DB.run('SELECT admin_free FROM license_tariffs WHERE id=?',(data['plan_id'],),one=True)
            if existing_plan and int(existing_plan.get('admin_free') or 0) and field=='price':
                await state.clear(); await m.answer('Бесплатный тариф для админов всегда имеет цену 0.'); return
            if field=='name':
                if not value or len(value)>80: await m.answer('Название должно быть от 1 до 80 символов.'); return
                await DB.run('UPDATE license_tariffs SET name=? WHERE id=?',(value,data['plan_id']))
            else:
                try: num=float(value.replace(',','.'))
                except ValueError: await m.answer('Введи число.'); return
                if not math.isfinite(num) or num<=0 or num>100000000000 or (field=='duration' and not num.is_integer()): await m.answer('Недопустимое значение. Укажи конечное положительное число, а срок — целое количество дней.'); return
                col={'limit':'turnover_limit','price':'price','duration':'duration_days'}[field]
                await DB.run(f'UPDATE license_tariffs SET {col}=? WHERE id=?',(int(num) if field=='duration' else num,data['plan_id']))
            if field=='limit' and existing_plan and int(existing_plan.get('admin_free') or 0):
                await DB.set_setting('turnover_limit',str(num))
            await state.clear(); await m.answer('Тариф обновлён.'); await show_owner_license_plans(m)

    @dp.callback_query(F.data.startswith('owner:plan:toggle:'))
    async def owner_plan_toggle(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        plan_id=int(c.data.rsplit(':',1)[1]); plan=await DB.run('SELECT active FROM license_tariffs WHERE id=?',(plan_id,),one=True)
        if not plan: await c.answer('Тариф не найден',show_alert=True); return
        await DB.run('UPDATE license_tariffs SET active=? WHERE id=?',(0 if plan['active'] else 1,plan_id)); await show_owner_license_plans(c.message,edit=True); await c.answer('Статус тарифа изменён')

    @dp.callback_query(F.data.startswith('owner:plan:delete:'))
    async def owner_plan_delete(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        plan_id=int(c.data.rsplit(':',1)[1]); target=await DB.run('SELECT admin_free FROM license_tariffs WHERE id=?',(plan_id,),one=True)
        if target and int(target.get('admin_free') or 0): await c.answer('Специальный бесплатный тариф для админов нельзя удалить. Его можно отключить или изменить лимит.',show_alert=True); return
        count=await DB.run("SELECT COUNT(*) AS n FROM license_payments WHERE license_tariff_id=? AND status IN ('awaiting_proof','pending')",(plan_id,),one=True)
        if count and count['n']:
            await c.answer('Есть незавершённые оплаты по этому тарифу. Сначала отключи его.',show_alert=True); return
        plans=await DB.run('SELECT COUNT(*) AS n FROM license_tariffs WHERE active=1',( ),one=True)
        plan=await DB.run('SELECT active FROM license_tariffs WHERE id=?',(plan_id,),one=True)
        if plan and plan['active'] and plans and plans['n']<=1: await c.answer('Нельзя удалить единственный активный тариф.',show_alert=True); return
        await DB.run('DELETE FROM license_tariffs WHERE id=?',(plan_id,)); await show_owner_license_plans(c.message,edit=True); await c.answer('Тариф удалён')

    @dp.callback_query(F.data=='owner:settings')
    async def owner_settings(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец может менять лимит',show_alert=True); return
        capped_free = await DB.setting('license_turnover_capped','0') == '1'
        total = await free_license_month_turnover() if capped_free else await turnover_total()
        limit=await DB.setting('turnover_limit','5000'); price=await DB.setting('license_price','990'); duration=await DB.setting('license_duration_days','30'); exp=await DB.setting('license_expires_at',''); active=(await license_is_active())
        turnover_label = 'Оборот бесплатного тарифа за текущий месяц' if capped_free else 'Текущий оборот'
        turnover_note = 'Оборот бесплатного тарифа обнуляется автоматически в начале каждого календарного месяца (UTC).' if capped_free else 'Для платной лицензии оборот считается по подтверждённым оплатам за всё время.'
        await c.message.edit_text(f"📈 Оборот и лицензия\n\n{turnover_label}: {total:.2f}\nЛимит оборота: {limit}\nЛицензия: {'активна' if active else 'не активна'}\nДействует до: {fmt_date(exp) if exp else 'не задано'}\nЦена лицензии: {price}\nСрок лицензии: {duration} дней\n\n{turnover_note} При достижении лимита новые покупки блокируются до активации лицензии. Здесь можно изменить параметры и управлять тарифами оборота и лицензии.",reply_markup=ikb([[('✏️ Изменить лимит','owner:setlimit'),('✏️ Цена лицензии','owner:setprice')],[('✏️ Срок лицензии','owner:setduration')],[('📊 Тарифы оборота и лицензии','owner:tariffs')],[('🔓 Активировать лицензию','owner:license:on'),('⛔ Отключить лицензию','owner:license:off')],[('↩️ Назад','settings:back')]])); await c.answer()

    @dp.callback_query(F.data.in_({'owner:setlimit','owner:setprice','owner:setduration'}))
    async def owner_setting_prompt(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        key='turnover_limit' if c.data=='owner:setlimit' else ('license_price' if c.data=='owner:setprice' else 'license_duration_days')
        await state.set_state(OwnerSettingsState.value); await state.update_data(owner_setting_key=key)
        await c.message.answer('Введи новое значение числом: лимит оборота, цену лицензии или срок лицензии в днях.'); await c.answer()

    @dp.message(OwnerSettingsState.value)
    async def owner_setting_save(m:Message,state:FSMContext):
        if m.from_user.id not in PLATFORM_OWNER_IDS: return
        data=await state.get_data(); key=data.get('owner_setting_key')
        try: value=float((m.text or '').replace(',','.'))
        except ValueError: await m.answer('Нужно ввести число.'); return
        if key=='license_duration_days' and (not value.is_integer() or value<1): await m.answer('Срок лицензии должен быть целым числом дней от 1.'); return
        if value<0 or value>100000000000: await m.answer('Число вне допустимого диапазона.'); return
        if key=='turnover_limit' and value<=0: await m.answer('Лимит оборота должен быть больше нуля.'); return
        if key=='license_price' and value<=0: await m.answer('Цена лицензии должна быть больше нуля.'); return
        await DB.set_setting(key,str(value)); await state.clear(); await m.answer('Настройка сохранена.',reply_markup=admin_menu_for(m.from_user.id))

    @dp.callback_query(F.data=='owner:license:on')
    @dp.callback_query(F.data=='owner:license:off')
    async def owner_license_toggle(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец',show_alert=True); return
        await DB.set_setting('license_active','1' if c.data.endswith(':on') else '0')
        await DB.set_setting('license_turnover_capped','0')
        if c.data.endswith(':on'):
            await DB.set_setting('license_expires_at',(datetime.now(timezone.utc)+timedelta(days=36500)).isoformat())
        else: await DB.set_setting('license_expires_at','')
        await c.answer('Статус лицензии обновлён'); total=await turnover_total(); limit=await DB.setting('turnover_limit','5000'); price=await DB.setting('license_price','990'); duration=await DB.setting('license_duration_days','30'); exp=await DB.setting('license_expires_at',''); active=(await license_is_active())
        await c.message.edit_text(f"📈 Оборот и лицензия\n\nТекущий оборот: {total:.2f}\nЛимит оборота: {limit}\nЛицензия: {'активна' if active else 'не активна'}\nЦена лицензии: {price}\nСрок лицензии: {duration} дней",reply_markup=ikb([[('✏️ Изменить лимит','owner:setlimit'),('✏️ Цена лицензии','owner:setprice')],[('✏️ Срок лицензии','owner:setduration')],[('📊 Тарифы оборота и лицензии','owner:tariffs')],[('🔓 Активировать лицензию','owner:license:on'),('⛔ Отключить лицензию','owner:license:off')],[('↩️ Назад','settings:back')]]))

    @dp.message(F.text=='📣 Каналы')
    async def channels_menu(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        await state.clear(); await show_channels(m)

    async def show_channels(m:Message, edit:bool=False):
        channels=await DB.run('SELECT * FROM channels ORDER BY id DESC',all=True)
        if not channels:
            text='Каналы ещё не добавлены. Добавь канал, чтобы использовать его в тарифах.'
            markup=ikb([[('➕ Добавить канал','channel:add')],[('↩️ Меню','menu:admin')]])
            if edit:
                try: await m.edit_text(text,reply_markup=markup)
                except Exception: await m.answer(text,reply_markup=markup)
            else: await m.answer(text,reply_markup=markup)
            return
        lines=['📣 УПРАВЛЕНИЕ КАНАЛАМИ','']
        buttons=[]
        for i,ch in enumerate(channels,1):
            status='🟢 Активен' if ch['active'] else '⏸ Отключён'
            title=(ch['title'] or '').replace('\n',' ')
            lines.append(f"{i}. {title} · {status}\n   ID: {ch['channel_id']}")
            buttons.append([('✏️ Редактировать',f"channel:edit:{ch['id']}"),(('⏸ Отключить' if ch['active'] else '🟢 Включить'),f"channel:toggle:{ch['id']}")])
            buttons.append([('🗑 Удалить',f"channel:delete:{ch['id']}")])
        buttons.extend([[('➕ Добавить канал','channel:add')],[('↩️ Меню','menu:admin')]])
        text='\n'.join(lines)
        if len(text)>3900:
            kept=[]
            for line in lines:
                if len('\n'.join(kept+[line]))>3700: break
                kept.append(line)
            shown=max(0,len([x for x in kept if x and x[0].isdigit()]))
            remaining=len(channels)-shown
            text='\n'.join(kept)+f"\n\n⚠️ Сообщение ограничено Telegram: не показано каналов — {remaining}. Сократи список каналов."
            buttons=buttons[:shown*2]+[[('➕ Добавить канал','channel:add')],[('↩️ Меню','menu:admin')]]
        markup=ikb(buttons)
        if edit:
            try: await m.edit_text(text,reply_markup=markup)
            except Exception: await m.answer(text,reply_markup=markup)
        else: await m.answer(text,reply_markup=markup)

    @dp.callback_query(F.data=='channels:open')
    async def channels_open(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await show_channels(c.message, edit=True); await c.answer()

    async def prompt_tariff_channel(m: Message):
        channels=await DB.run('SELECT * FROM channels WHERE active=1 ORDER BY title',all=True)
        if not channels:
            await m.answer('Для создания тарифа сначала добавь канал. Нажми кнопку ниже и перешли боту любой пост из нужного канала.',reply_markup=ikb([[('➕ Добавить канал','channel:add:tariff')],[('↩️ Отмена','menu:admin')]]))
            return
        rows=[[(ch['title'][:55],f"tariff:choosechannel:{ch['id']}")] for ch in channels]
        rows.append([('➕ Добавить канал','channel:add:tariff')])
        rows.append([('↩️ Отмена','menu:admin')])
        await m.answer('Выбери канал, к которому будет привязан тариф. После выбора отметишь способы оплаты и сможешь проверить всё перед сохранением.',reply_markup=ikb(rows))

    @dp.callback_query(F.data=='channel:add')
    @dp.callback_query(F.data=='channel:add:tariff')
    async def channel_add(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        return_to_tariff = c.data.endswith(':tariff')
        previous_data = await state.get_data() if return_to_tariff else {}
        await state.clear()
        await state.update_data(**previous_data, return_to_tariff=return_to_tariff)
        await state.set_state(ChannelWizard.channel_id)
        await c.message.answer('Чтобы добавить канал, перешли сюда любой пост из этого канала.\n\nВажно: пересылай именно сообщение из канала, а не скриншот. Бот должен быть администратором канала с правами приглашать пользователей и блокировать участников.')
        await c.answer()

    @dp.message(ChannelWizard.channel_id)
    async def channel_forward_save(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        origin=getattr(m,'forward_origin',None)
        source_chat=getattr(origin,'chat',None) if origin else None
        if not source_chat or getattr(source_chat,'type',None)!='channel':
            await m.answer('Не удалось определить канал по сообщению. Перешли боту именно пост из Telegram-канала.'); return
        cid=int(source_chat.id)
        title=(getattr(source_chat,'title',None) or str(cid)).strip()
        try:
            chat=await bot.get_chat(cid)
            me=await bot.get_chat_member(cid,bot_id)
            if me.status not in ('administrator','creator'):
                await m.answer('Бот не является администратором этого канала. Добавь бота в администраторы и перешли пост ещё раз.'); return
            if me.status=='administrator' and (not getattr(me,'can_restrict_members',False) or not getattr(me,'can_invite_users',False)):
                await m.answer('Боту нужны права приглашать пользователей и блокировать участников. Исправь права и перешли пост ещё раз.'); return
            title=chat.title or title
        except Exception as e:
            await m.answer(f'Не удалось проверить канал и права бота: {e}\nУбедись, что бот добавлен в канал администратором.'); return
        try:
            await DB.run('INSERT INTO channels(channel_id,title) VALUES(?,?)',(cid,title))
        except Exception:
            existing=await DB.run('SELECT id FROM channels WHERE channel_id=?',(cid,),one=True)
            if not existing:
                await m.answer('Не удалось добавить канал. Проверь, не добавлен ли он уже.'); return
            await DB.run('UPDATE channels SET title=?,active=1 WHERE channel_id=?',(title,cid))
        await DB.run('UPDATE tariffs SET channel_title=? WHERE channel_id=?',(title,cid))
        data=await state.get_data(); return_to_tariff=data.get('return_to_tariff',False)
        if return_to_tariff:
            await state.set_state(TariffWizard.channel)
            await m.answer(f'Канал «{title}» добавлен. Теперь выбери его для тарифа.')
            await prompt_tariff_channel(m)
        else:
            await state.clear()
            await m.answer(f'Канал «{title}» добавлен.',reply_markup=admin_menu_for(m.from_user.id))
            await show_channels(m)

    @dp.callback_query(F.data.startswith('tariff:choosechannel:'))
    async def tariff_choose_channel(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: channel_record_id=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный канал',show_alert=True); return
        ch=await DB.run('SELECT * FROM channels WHERE id=? AND active=1',(channel_record_id,),one=True)
        if not ch: await c.answer('Канал не найден или отключён',show_alert=True); return
        data=await state.get_data()
        if not all(k in data for k in ('name','description','price','days')):
            await state.clear(); await c.message.answer('Данные тарифа потерялись. Начни создание тарифа заново.',reply_markup=admin_menu_for(c.from_user.id)); await c.answer(); return
        methods=await DB.run('SELECT id,name,active FROM payment_methods ORDER BY id',all=True)
        active_methods=[pm for pm in methods if pm['active']]
        if not active_methods:
            await c.answer('Нет активных способов оплаты',show_alert=True)
            await c.message.answer('Нельзя создать тариф: нет активных способов оплаты. Добавь способ оплаты или включи существующий, затем начни создание тарифа заново.',reply_markup=ikb([[('💰 Способы оплаты','settings:payments')],[('↩️ В меню','menu:admin')]]))
            return
        await state.update_data(selected_channel_record=channel_record_id,selected_channel_id=ch['channel_id'],selected_channel_title=ch['title'],selected_payment_methods=[])
        rows=[]
        for pm in active_methods:
            rows.append([(f"▫️ {pm['name']}",f"tariffpay:toggle:{pm['id']}")])
        rows += [[('💾 Проверить и сохранить тариф','tariffpay:save')],[('✖️ Отменить создание','tariffwizard:cancel')]]
        await c.message.answer('💰 Способы оплаты тарифа\n\nНажми на каждый подходящий способ: ▫️ — не выбран, ✅ — выбран. Выбранные способы будут привязаны только к этому тарифу.\n\nВыбрано: 0',reply_markup=ikb(rows))
        await c.answer()

    @dp.callback_query(F.data.startswith('tariffpay:toggle:'))
    async def tariff_payment_toggle(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: mid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный способ оплаты',show_alert=True); return
        data=await state.get_data(); selected=list(data.get('selected_payment_methods',[]))
        method=await DB.run('SELECT id,name,active FROM payment_methods WHERE id=?',(mid,),one=True)
        if not method or not method['active']: await c.answer('Способ оплаты отключён или не найден',show_alert=True); return
        if mid in selected: selected.remove(mid)
        else: selected.append(mid)
        await state.update_data(selected_payment_methods=selected)
        methods=await DB.run('SELECT id,name,active FROM payment_methods WHERE active=1 ORDER BY id',all=True)
        rows=[[(('✅ ' if x['id'] in selected else '▫️ ')+x['name'],f"tariffpay:toggle:{x['id']}")] for x in methods]
        rows += [[('💾 Проверить и сохранить тариф','tariffpay:save')],[('✖️ Отменить создание','tariffwizard:cancel')]]
        await c.message.edit_text('💰 Способы оплаты тарифа\n\nНажми на каждый подходящий способ: ▫️ — не выбран, ✅ — выбран.\n\nВыбрано: '+str(len(selected)), reply_markup=ikb(rows)); await c.answer('Выбор обновлён')

    @dp.callback_query(F.data=='tariffpay:save')
    async def tariff_payment_save(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        data=await state.get_data(); selected=list(data.get('selected_payment_methods',[]))
        if not selected: await c.answer('Выбери хотя бы один способ оплаты',show_alert=True); return
        required=('name','description','price','days','selected_channel_id','selected_channel_title')
        if not all(k in data for k in required): await state.clear(); await c.answer('Данные тарифа потеряны. Начни заново.',show_alert=True); return
        active_methods=await DB.run('SELECT id,name FROM payment_methods WHERE active=1 AND id IN ('+','.join('?' for _ in selected)+')',tuple(selected),all=True)
        active_ids={x['id'] for x in active_methods}
        selected=[mid for mid in selected if mid in active_ids]
        if not selected:
            await c.answer('Выбранные способы оплаты отключены. Выбери активные способы.',show_alert=True); return
        # Save tariff and payment links in one transaction to avoid a tariff without its selected payment methods.
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.execute('PRAGMA foreign_keys=ON')
            cur=await d.execute('INSERT INTO tariffs(name,description,price,days,channel_id,channel_title) VALUES(?,?,?,?,?,?)',(data['name'],data['description'],data['price'],data['days'],data['selected_channel_id'],data['selected_channel_title']))
            tid=cur.lastrowid
            await d.executemany('INSERT OR IGNORE INTO tariff_payment_methods(tariff_id,payment_method_id) VALUES(?,?)',[(tid,mid) for mid in selected])
            check=await (await d.execute('SELECT COUNT(*) FROM tariff_payment_methods WHERE tariff_id=?',(tid,))).fetchone()
            await d.commit()
        if not check or check[0] != len(selected):
            await c.answer('Тариф создан, но число привязок не совпало. Проверь способы оплаты в карточке тарифа.',show_alert=True); return
        method_names=', '.join(x['name'] for x in active_methods if x['id'] in selected)
        await state.clear(); await c.message.answer(f"✅ Тариф «{data['name']}» создан.\nПривязанные способы оплаты ({len(selected)}): {method_names}.",reply_markup=admin_menu_for(c.from_user.id))
        await show_tariffs(c.message,True); await c.answer('Тариф создан')

    @dp.callback_query(F.data.startswith('tariff:payments:'))
    async def tariff_payment_edit(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        tid=int(c.data.rsplit(':',1)[1]); tariff=await DB.run('SELECT id,name FROM tariffs WHERE id=?',(tid,),one=True)
        if not tariff: await c.answer('Тариф не найден',show_alert=True); return
        methods=await DB.run('SELECT id,name,active FROM payment_methods ORDER BY id',all=True)
        if not methods: await c.message.answer('Способы оплаты не добавлены.',reply_markup=ikb([[('💰 Способы оплаты','settings:payments')]])); await c.answer(); return
        linked=await DB.run('SELECT payment_method_id FROM tariff_payment_methods WHERE tariff_id=?',(tid,),all=True)
        selected=[x['payment_method_id'] for x in linked]
        await state.clear(); await state.update_data(edit_tariff_payment_id=tid,selected_payment_methods=selected)
        rows=[]
        for pm in methods:
            if pm['active']: rows.append([(('✅ ' if pm['id'] in selected else '▫️ ')+pm['name'],f"tariffeditpay:toggle:{pm['id']}")])
        rows += [[('💾 Сохранить способы оплаты','tariffeditpay:save')],[('↩️ К тарифам','menu:admin')]]
        await c.message.answer(f"Способы оплаты тарифа «{tariff['name']}». Выбери один или несколько:",reply_markup=ikb(rows)); await c.answer()

    @dp.callback_query(F.data.startswith('tariffeditpay:toggle:'))
    async def tariff_edit_payment_toggle(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        mid=int(c.data.rsplit(':',1)[1]); data=await state.get_data(); selected=list(data.get('selected_payment_methods',[]))
        pm=await DB.run('SELECT id,name,active FROM payment_methods WHERE id=?',(mid,),one=True)
        if not pm or not pm['active']: await c.answer('Способ оплаты отключён',show_alert=True); return
        if mid in selected: selected.remove(mid)
        else: selected.append(mid)
        await state.update_data(selected_payment_methods=selected)
        tid=data.get('edit_tariff_payment_id')
        methods=await DB.run('SELECT id,name,active FROM payment_methods WHERE active=1 ORDER BY id',all=True)
        rows=[[((('✅ ' if x['id'] in selected else '▫️ ')+x['name']),f"tariffeditpay:toggle:{x['id']}")] for x in methods]
        rows += [[('💾 Сохранить способы оплаты','tariffeditpay:save')],[('↩️ К тарифам','menu:admin')]]
        # Refresh button labels as well as state: edit_reply_markup alone can leave
        # the old selected/unselected labels visible, making the choice look broken.
        tariff=await DB.run('SELECT name FROM tariffs WHERE id=?',(tid,),one=True)
        title=tariff['name'] if tariff else str(tid)
        await c.message.edit_text(f"Способы оплаты тарифа «{title}». Выбери один или несколько.\n\nВыбрано: {len(selected)}",reply_markup=ikb(rows)); await c.answer('Выбор обновлён')

    @dp.callback_query(F.data=='tariffeditpay:save')
    async def tariff_edit_payment_save(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        data=await state.get_data(); tid=data.get('edit_tariff_payment_id'); selected=list(data.get('selected_payment_methods',[]))
        if not tid: await c.answer('Сессия редактирования устарела',show_alert=True); return
        if not selected: await c.answer('Выбери хотя бы один способ оплаты',show_alert=True); return
        active_methods=await DB.run('SELECT id,name FROM payment_methods WHERE active=1 AND id IN ('+','.join('?' for _ in selected)+')',tuple(selected),all=True)
        active_ids={x['id'] for x in active_methods}
        selected=[mid for mid in selected if mid in active_ids]
        if not selected: await c.answer('Выбери хотя бы один активный способ оплаты',show_alert=True); return
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.execute('PRAGMA foreign_keys=ON')
            await d.execute('DELETE FROM tariff_payment_methods WHERE tariff_id=?',(tid,))
            await d.executemany('INSERT OR IGNORE INTO tariff_payment_methods(tariff_id,payment_method_id) VALUES(?,?)',[(tid,mid) for mid in selected])
            cur=await d.execute('SELECT COUNT(*) FROM tariff_payment_methods WHERE tariff_id=?',(tid,))
            count=(await cur.fetchone())[0]
            await d.commit()
        if count != len(selected): await c.answer('Не все способы сохранились. Попробуй ещё раз.',show_alert=True); return
        names=', '.join(x['name'] for x in active_methods if x['id'] in selected)
        await state.clear(); await c.message.answer(f'Способы оплаты тарифа сохранены: {names}.',reply_markup=admin_menu_for(c.from_user.id)); await show_tariffs(c.message,True); await c.answer('Сохранено')

    @dp.callback_query(F.data.startswith('tariff:channels:'))
    async def tariff_channels_manage(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        tid=int(c.data.rsplit(':',1)[1]); t=await DB.run('SELECT * FROM tariffs WHERE id=?',(tid,),one=True)
        if not t: await c.answer('Тариф не найден',show_alert=True); return
        extras=await DB.run('SELECT * FROM tariff_extra_channels WHERE tariff_id=? ORDER BY channel_title',(tid,),all=True)
        available=await DB.run('SELECT * FROM channels WHERE active=1 AND channel_id<>? ORDER BY title',(t['channel_id'],),all=True)
        rows=[]
        for ch in available:
            exists=any(x['channel_id']==ch['channel_id'] for x in extras)
            rows.append([(('➖ Убрать ' if exists else '➕ Добавить ')+ch['title'][:35],f"tariff:channelstoggle:{tid}:{ch['id']}:{1 if exists else 0}")])
        rows += [[('↩️ К тарифу',f"tariff:edit:{tid}")]]
        names=[x['channel_title'] for x in extras]
        text=f"Дополнительные каналы тарифа «{t['name']}»\nОсновной канал: {t['channel_title']}\nДополнительные: {', '.join(names) if names else 'нет'}\n\nВыбери каналы, к которым нужно выдавать доступ вместе с основным."
        try: await c.message.edit_text(text,reply_markup=ikb(rows))
        except Exception: await c.message.answer(text,reply_markup=ikb(rows))
        await c.answer()

    @dp.callback_query(F.data.startswith('tariff:channelstoggle:'))
    async def tariff_channels_toggle(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: _,_,tid_s,chrec_s,was_s=c.data.split(':'); tid=int(tid_s); chrec=int(chrec_s); was=int(was_s)
        except Exception: await c.answer('Некорректный выбор',show_alert=True); return
        ch=await DB.run('SELECT channel_id,title FROM channels WHERE id=? AND active=1',(chrec,),one=True)
        if not ch: await c.answer('Канал недоступен',show_alert=True); return
        if was: await DB.run('DELETE FROM tariff_extra_channels WHERE tariff_id=? AND channel_id=?',(tid,ch['channel_id']))
        else:
            try: await DB.run('INSERT INTO tariff_extra_channels(tariff_id,channel_id,channel_title) VALUES(?,?,?)',(tid,ch['channel_id'],ch['title']))
            except Exception: pass
        # refresh same management screen
        extras=await DB.run('SELECT * FROM tariff_extra_channels WHERE tariff_id=? ORDER BY channel_title',(tid,),all=True)
        t=await DB.run('SELECT * FROM tariffs WHERE id=?',(tid,),one=True)
        available=await DB.run('SELECT * FROM channels WHERE active=1 AND channel_id<>? ORDER BY title',(t['channel_id'],),all=True)
        rows=[]
        for item in available:
            exists=any(x['channel_id']==item['channel_id'] for x in extras)
            rows.append([(('➖ Убрать ' if exists else '➕ Добавить ')+item['title'][:35],f"tariff:channelstoggle:{tid}:{item['id']}:{1 if exists else 0}")])
        rows += [[('↩️ К тарифу',f"tariff:edit:{tid}")]]
        names=[x['channel_title'] for x in extras]
        await c.message.edit_text(f"Дополнительные каналы тарифа «{t['name']}»\nОсновной канал: {t['channel_title']}\nДополнительные: {', '.join(names) if names else 'нет'}\n\nВыбери каналы, к которым нужно выдавать доступ вместе с основным.",reply_markup=ikb(rows)); await c.answer('Список каналов обновлён')

    @dp.callback_query(F.data.startswith('channel:edit:'))
    async def channel_edit(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        cid=int(c.data.rsplit(':',1)[1]); ch=await DB.run('SELECT * FROM channels WHERE id=?',(cid,),one=True)
        if not ch: await c.answer('Канал не найден',show_alert=True); return
        await c.message.edit_text(f"Редактирование канала «{ch['title']}»",reply_markup=ikb([[('Название','chfield:title:'+str(cid)),('ID канала','chfield:channel_id:'+str(cid))],[('↩️ К списку каналов','channels:open')]])); await c.answer()

    @dp.callback_query(F.data.startswith('chfield:'))
    async def channel_edit_field(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        _,field,record_id=c.data.split(':'); await state.set_state(EditChannel.value); await state.update_data(mode='edit_channel',channel_record_id=int(record_id),channel_field=field); await c.message.answer('Отправь новое значение:'); await c.answer()

    @dp.message(EditChannel.value)
    async def channel_edit_save(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        data=await state.get_data()
        if data.get('mode')=='find_subscriber':
            try: uid=int((m.text or '').strip())
            except ValueError: await m.answer('ID должен быть числом. Попробуй ещё раз.'); return
            rows=await DB.run("SELECT s.*,t.name AS tariff_name FROM subscriptions s LEFT JOIN tariffs t ON t.id=s.tariff_id WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 50",(uid,),all=True)
            if not rows: await m.answer(f'Подписок для пользователя {uid} не найдено.',reply_markup=admin_menu_for(m.from_user.id))
            else:
                await m.answer(f'Подписки пользователя {uid}:')
                for sub in rows:
                    await m.answer(f"№{sub['id']} · {sub['tariff_name'] or 'тариф удалён'}\nСтатус: {sub['status']}\nКанал: {sub['channel_id']}\nДо: {fmt_date(sub['expires_at'])}",reply_markup=ikb([[('⛔ Отозвать доступ',f"sub:revoke:{sub['id']}")]] if sub['status']=='active' else []))
            await state.clear(); return
        if data.get('mode')!='edit_channel': return
        value=(m.text or '').strip(); record_id=data['channel_record_id']; field=data['channel_field']
        ch=await DB.run('SELECT * FROM channels WHERE id=?',(record_id,),one=True)
        if not ch: await state.clear(); await m.answer('Канал не найден.'); return
        if field=='title':
            if not value: await m.answer('Название не должно быть пустым.'); return
            await DB.run('UPDATE channels SET title=? WHERE id=?',(value[:150],record_id)); await DB.run('UPDATE tariffs SET channel_title=? WHERE channel_id=?',(value[:150],ch['channel_id']))
        elif field=='channel_id':
            try: new_id=int(value); chat=await bot.get_chat(new_id); me=await bot.get_chat_member(new_id,bot_id)
            except Exception as e: await m.answer(f'Не удалось проверить канал: {e}'); return
            if me.status not in ('administrator','creator'): await m.answer('Бот должен быть администратором нового канала.'); return
            try: await DB.run('UPDATE channels SET channel_id=? WHERE id=?',(new_id,record_id))
            except Exception: await m.answer('Этот ID уже используется другим каналом.'); return
            await DB.run('UPDATE tariffs SET channel_id=?,channel_title=? WHERE channel_id=?',(new_id,chat.title or ch['title'],ch['channel_id']))
        else: await m.answer('Неизвестное поле.'); return
        await state.clear(); await m.answer('Канал обновлён.',reply_markup=admin_menu_for(m.from_user.id)); await show_channels(m)

    @dp.callback_query(F.data.startswith('channel:toggle:'))
    async def channel_toggle(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        cid=int(c.data.rsplit(':',1)[1]); await DB.run('UPDATE channels SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(cid,)); await c.answer('Статус изменён'); await show_channels(c.message, edit=True)

    @dp.callback_query(F.data.startswith('channel:delete:'))
    async def channel_delete(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        cid=int(c.data.rsplit(':',1)[1]); await c.message.edit_text('Удалить канал из списка управления? Тарифы и выданные подписки автоматически не удаляются.',reply_markup=ikb([[('Да, удалить','channel:deleteyes:'+str(cid)),('Отмена','channels:open')]])); await c.answer()

    @dp.callback_query(F.data.startswith('channel:deleteyes:'))
    async def channel_delete_yes(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        cid=int(c.data.rsplit(':',1)[1]); ch=await DB.run('SELECT channel_id,title FROM channels WHERE id=?',(cid,),one=True)
        if not ch: await c.answer('Канал не найден',show_alert=True); return
        await DB.run('UPDATE tariffs SET active=0 WHERE channel_id=?',(ch['channel_id'],))
        await DB.run('DELETE FROM channels WHERE id=?',(cid,)); await c.answer('Канал удалён; связанные тарифы отключены'); await show_channels(c.message, edit=True)

    @dp.callback_query(F.data=='menu:admin')
    async def back(c:CallbackQuery):
        if admin(c.from_user.id): await c.message.answer('Панель администратора.',reply_markup=admin_menu_for(c.from_user.id))
        await c.answer()
    @dp.callback_query(F.data=='tariffwizard:skipdesc')
    async def tariff_skip_description(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        if await state.get_state()!=TariffWizard.description.state:
            await c.answer('Этот шаг уже завершён',show_alert=True); return
        await state.update_data(description=''); await state.set_state(TariffWizard.price)
        await c.message.answer('Описание пропущено.\n\nШаг 3 из 5 · Стоимость\nУкажи цену, например: 500 ₽ или 9.99 €.',reply_markup=ikb([[('✖️ Отменить создание','tariffwizard:cancel')]])); await c.answer()

    @dp.callback_query(F.data=='tariffwizard:cancel')
    async def tariff_cancel(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.clear(); await c.message.answer('Создание тарифа отменено. Введённые данные не сохранены.',reply_markup=admin_menu_for(c.from_user.id)); await show_tariffs(c.message,True); await c.answer('Отменено')

    @dp.callback_query(F.data=='tariff:add')
    async def addtariff(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        await state.clear(); await state.set_state(TariffWizard.name); await c.message.answer('➕ Создание тарифа · шаг 1 из 5\n\nНапиши название тарифа.\nНапример: «На 30 дней» или «Премиум».', reply_markup=ikb([[('✖️ Отменить создание','tariffwizard:cancel')]])); await c.answer()
    @dp.message(TariffWizard.name)
    async def tname(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        name=(m.text or '').strip()
        if not name: await m.answer('Название не должно быть пустым.'); return
        await state.update_data(name=name[:100]); await state.set_state(TariffWizard.description); await m.answer('✅ Название сохранено.\n\nШаг 2 из 5 · Описание\nНапиши описание тарифа или нажми «Пропустить».', reply_markup=ikb([[('⏭ Пропустить описание','tariffwizard:skipdesc')],[('✖️ Отменить создание','tariffwizard:cancel')]]))
    @dp.message(TariffWizard.description)
    async def tdesc(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        await state.update_data(description='' if m.text=='—' else (m.text or '')[:500]); await state.set_state(TariffWizard.price); await m.answer('✅ Описание сохранено.\n\nШаг 3 из 5 · Стоимость\nУкажи цену, например: 500 ₽ или 9.99 €.', reply_markup=ikb([[('✖️ Отменить создание','tariffwizard:cancel')]]))
    @dp.message(TariffWizard.price)
    async def tprice(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        if not (m.text or '').strip(): await m.answer('Укажи стоимость текстом.');return
        price=(m.text or '').strip()
        if len(price)>50: await m.answer('Цена слишком длинная. Укажи короткое значение, например 500 ₽.'); return
        await state.update_data(price=price); await state.set_state(TariffWizard.days); await m.answer('✅ Стоимость сохранена.\n\nШаг 4 из 5 · Срок подписки\nВыбери срок кнопкой или отправь своё количество дней числом (от 1 до 36500).', reply_markup=ikb(wizard_days_rows()))
    def wizard_days_rows():
        presets=[7,14,30,60,90,180,365]
        rows=[[(f'{n} дн.',f'twdays:{n}') for n in presets[i:i+3]] for i in range(0,len(presets),3)]
        rows.append([('✖️ Отменить создание','tariffwizard:cancel')])
        return rows
    async def wizard_days_done(target:Message,state:FSMContext,days:int):
        await state.update_data(days=days)
        await state.set_state(TariffWizard.channel)
        await target.answer(f'✅ Срок сохранён: {days} дн.\n\nШаг 5 из 5 · Канал подписки')
        await prompt_tariff_channel(target)
    @dp.message(TariffWizard.days)
    async def tdays(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        try: days=int(m.text or '')
        except ValueError: await m.answer('Введи целое число дней или выбери срок кнопкой.');return
        if not 1<=days<=36500: await m.answer('Срок должен быть от 1 до 36500 дней.');return
        await wizard_days_done(m,state,days)
    @dp.callback_query(F.data.startswith('twdays:'))
    async def tdays_button(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        if await state.get_state()!=TariffWizard.days.state:
            await c.answer('Этот шаг уже пройден или создание тарифа отменено.',show_alert=True);return
        try: days=int(c.data.split(':',1)[1]); assert 1<=days<=36500
        except Exception: await c.answer('Некорректный срок',show_alert=True);return
        await c.answer(f'Срок: {days} дн.')
        await wizard_days_done(c.message,state,days)

    @dp.callback_query(F.data.startswith('tariff:toggle:'))
    async def togglet(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        tid=int(c.data.rsplit(':',1)[1]); await DB.run('UPDATE tariffs SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?',(tid,)); await c.answer('Статус изменён'); await show_tariff_card(c.message, tid)
    @dp.callback_query(F.data.startswith('tariff:delete:'))
    async def deltariff(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        tid=int(c.data.rsplit(':',1)[1]); await c.message.answer('Удалить тариф? Уже выданные подписки сохранятся.',reply_markup=ikb([[('Да, удалить',f'tariff:deleteyes:{tid}'),('Отмена','menu:admin')]])); await c.answer()
    @dp.callback_query(F.data.startswith('tariff:deleteyes:'))
    async def deltariffyes(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        tid=int(c.data.rsplit(':',1)[1]); t=await DB.run('SELECT id FROM tariffs WHERE id=?',(tid,),one=True)
        if not t: await c.answer('Тариф уже удалён',show_alert=True); return
        await DB.run("UPDATE orders SET status='rejected',reviewed_at=CURRENT_TIMESTAMP,review_note='Тариф удалён администратором' WHERE tariff_id=? AND status='pending'",(tid,))
        await DB.run('DELETE FROM tariff_payment_methods WHERE tariff_id=?',(tid,))
        await DB.run('DELETE FROM tariffs WHERE id=?',(tid,)); await c.answer('Тариф удалён'); await c.message.answer('Тариф удалён. Ожидавшие заявки закрыты, активные подписки не затронуты.')
    @dp.callback_query(F.data=='tariff:list')
    async def tariff_list_callback(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await show_tariffs(c.message, True); await c.answer()

    @dp.callback_query(F.data.startswith('tariff:edit:'))
    async def editt(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        t=await DB.run('SELECT id FROM tariffs WHERE id=?',(tid,),one=True)
        if not t: await c.answer('Тариф не найден',show_alert=True);return
        await show_tariff_card(c.message, tid)
        await c.answer()
    @dp.callback_query(F.data.startswith('edit:'))
    async def editfield(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        _,field,tid=c.data.split(':')
        if field=='days':
            await state.clear()
            presets=[7,14,30,60,90,180,365]
            rows=[[(f'{n} дн.',f'tdays:set:{tid}:{n}') for n in presets[i:i+3]] for i in range(0,len(presets),3)]
            rows+=[[('✏️ Свой срок',f'tdays:custom:{tid}')],[('↩️ К карточке тарифа',f'tariff:edit:{tid}')]]
            await c.message.answer('📅 Выбери срок подписки или введи свой:',reply_markup=ikb(rows)); await c.answer(); return
        if field=='old_price':
            await state.update_data(edit_id=int(tid),edit_field=field); await state.set_state(EditTariff.value)
            await c.message.answer('🏷 Отправь старую цену, например 990 ₽. Она будет показана зачёркнутой рядом с текущей ценой, со скидкой в процентах. Старая цена должна быть выше текущей.',reply_markup=ikb([[('🧹 Убрать старую цену',f'oldprice:clear:{tid}')],[('↩️ К карточке тарифа',f'tariff:edit:{tid}')]])); await c.answer(); return
        await state.update_data(edit_id=int(tid),edit_field=field); await state.set_state(EditTariff.value); await c.message.answer('Отправь новое значение:'); await c.answer()
    @dp.callback_query(F.data.startswith('oldprice:clear:'))
    async def old_price_clear(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True);return
        await DB.run("UPDATE tariffs SET old_price='' WHERE id=?",(tid,)); await state.clear(); await cancel_promos([tid])
        await c.answer('Старая цена убрана'); await show_tariff_card(c.message,tid)
    @dp.callback_query(F.data.startswith('tdays:'))
    async def tariff_days_pick(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        parts=c.data.split(':')
        try: action=parts[1]; tid=int(parts[2])
        except Exception: await c.answer('Некорректная команда',show_alert=True);return
        if not await DB.run('SELECT id FROM tariffs WHERE id=?',(tid,),one=True): await c.answer('Тариф не найден',show_alert=True);return
        if action=='custom':
            await state.update_data(edit_id=tid,edit_field='days'); await state.set_state(EditTariff.value)
            await c.message.answer('Введи срок подписки в днях целым числом (от 1 до 36500):'); await c.answer(); return
        if action=='set':
            try: n=int(parts[3]); assert 1<=n<=36500
            except Exception: await c.answer('Некорректный срок',show_alert=True);return
            await DB.run('UPDATE tariffs SET days=? WHERE id=?',(n,tid)); await state.clear()
            await c.answer(f'Срок: {n} дн.'); await show_tariff_card(c.message,tid); return
        await c.answer('Некорректная команда',show_alert=True)
    @dp.message(EditTariff.value)
    async def saveedit(m:Message,state:FSMContext):
        if not admin(m.from_user.id):return
        d=await state.get_data(); field=d['edit_field']; tid=d['edit_id']; value=(m.text or '').strip()
        if field=='days':
            try: value=str(int(value)); assert 1<=int(value)<=36500
            except Exception: await m.answer('Укажи целое число дней от 1 до 36500.');return
        if field=='channel_id':
            try:
                value=str(int(value)); chat=await bot.get_chat(int(value)); me=await bot.get_chat_member(int(value),bot_id)
                if me.status not in ('administrator','creator') or (me.status=='administrator' and not getattr(me,'can_restrict_members',False)): raise ValueError('Нет прав блокировки')
                await DB.run('UPDATE tariffs SET channel_id=?,channel_title=? WHERE id=?',(int(value),chat.title or value,tid)); await state.clear(); await m.answer('Канал тарифа обновлён.'); await show_tariff_card(m, tid); return
            except Exception as e: await m.answer(f'Не удалось проверить канал или права: {e}');return
        if field not in {'name','description','price','days','old_price'}: await m.answer('Неизвестное поле.');return
        if field in {'price','old_price'} and len(value)>50: await m.answer('Слишком длинное значение. Укажи короткую цену, например 500 ₽.');return
        if field=='old_price':
            cur=await DB.run('SELECT price FROM tariffs WHERE id=?',(tid,),one=True)
            if not cur: await state.clear(); await m.answer('Тариф не найден.');return
            if not old_price_valid(cur['price'],value): await m.answer(f"Старая цена должна содержать число и быть выше текущей ({cur['price']}). Попробуй ещё раз.");return
        await DB.run(f'UPDATE tariffs SET {field}=? WHERE id=?',(value,tid)); await state.clear()
        note=''
        if field in ('price','old_price') and await cancel_promos([tid]): note=' Акция по таймеру отменена: цена изменена вручную.'
        if field=='price' and await drop_stale_old_price(tid): note+=' Старая цена убрана: она больше не выше новой цены.'
        await m.answer('Тариф обновлён.'+note); await show_tariff_card(m, tid)
    # ------------------------------------------------------------------
    # Bulk editing of tariffs
    # ------------------------------------------------------------------
    BULK_TITLES={'pct':'изменить цену на процент','price':'задать цену','old_set':'указать старую цену','old_clear':'убрать старую цену',
                 'days':'изменить срок','on':'включить тарифы','off':'отключить тарифы','pm':'способы оплаты'}
    async def safe_edit(message:Message,text:str,markup):
        try: await message.edit_text(text,reply_markup=markup)
        except TelegramBadRequest as e:
            if 'not modified' not in str(e).lower(): await message.answer(text,reply_markup=markup)
        except Exception: await message.answer(text,reply_markup=markup)
    async def bulk_ids(state:FSMContext):
        d=await state.get_data(); return [int(x) for x in d.get('bulk_ids',[])]
    async def bulk_select_screen(message:Message,state:FSMContext,edit:bool=True):
        tariffs=await DB.tariffs(None); sel=set(await bulk_ids(state))
        rows=[[(f"{'☑️' if t['id'] in sel else '⬜'} {t['name']} · {t['price']} · {t['days']} дн."[:60],f"bulk:t:{t['id']}")] for t in tariffs[:90]]
        rows.append([('✅ Выбрать все','bulk:all'),('⬜ Снять выбор','bulk:none')])
        rows.append([(f'➡️ Далее ({len(sel)})','bulk:next')]); rows.append([('↩️ К тарифам','bulk:exit')])
        text=f'🧰 МАССОВОЕ РЕДАКТИРОВАНИЕ\n\nОтметь тарифы, которые нужно изменить. Выбрано: {len(sel)} из {len(tariffs)}.'
        if len(tariffs)>90: text+='\n⚠️ Показаны первые 90 тарифов.'
        if edit: await safe_edit(message,text,ikb(rows))
        else: await message.answer(text,reply_markup=ikb(rows))
    async def bulk_menu_screen(message:Message,state:FSMContext,edit:bool=True):
        n=len(await bulk_ids(state))
        rows=[[('💰 Цена: скидка/наценка в %','bulk:a:pct'),('💰 Задать цену','bulk:a:price')],
              [('🏷 Старая цена','bulk:a:old'),('📅 Срок','bulk:a:days')],
              [('🟢 Включить','bulk:a:on'),('⏸ Отключить','bulk:a:off')],
              [('💳 Способы оплаты','bulk:a:pm'),('⏳ Акция на время','bulk:a:promo')],
              [('⬅️ К выбору тарифов','bulk:start_keep'),('✖️ Выйти','bulk:exit')]]
        text=f'🧰 МАССОВОЕ РЕДАКТИРОВАНИЕ\n\nВыбрано тарифов: {n}. Что изменить?'
        if edit: await safe_edit(message,text,ikb(rows))
        else: await message.answer(text,reply_markup=ikb(rows))
    def bulk_compute(op:dict,t:dict):
        """Pure function: (updates | None, line). None means the tariff is skipped, `line` explains why."""
        typ=op['type']; name=t['name']
        if typ in ('pct','price'):
            if typ=='pct':
                new=reprice(t['price'],op['percent'])
                if new is None: return None,f'{name}: в цене нет числа или цена стала бы нулевой'
            else: new=op['value']
            if new==t['price']: return None,f'{name}: цена не изменится'
            upd={'price':new}; old_cur=(t.get('old_price') or '').strip()
            if op.get('remember_old') and 0<parse_amount(new)<parse_amount(t['price']): upd['old_price']=t['price']
            elif old_cur and not old_price_valid(new,old_cur): upd['old_price']=''
            extra=f" · старая цена: {t['price']}" if upd.get('old_price')==t['price'] else (' · старая цена убрана' if upd.get('old_price')=='' else '')
            return upd,f"{name}: {t['price']} → {new}{extra}"
        if typ=='old_set':
            if not old_price_valid(t['price'],op['value']): return None,f"{name}: старая цена не выше текущей ({t['price']})"
            if (t.get('old_price') or '')==op['value']: return None,f'{name}: уже установлена'
            return {'old_price':op['value']},f"{name}: старая цена {op['value']}"
        if typ=='old_clear':
            if not (t.get('old_price') or '').strip(): return None,f'{name}: старой цены нет'
            return {'old_price':''},f'{name}: старая цена будет убрана'
        if typ=='days':
            if int(t['days'])==int(op['value']): return None,f"{name}: срок уже {t['days']} дн."
            return {'days':int(op['value'])},f"{name}: {t['days']} → {op['value']} дн."
        if typ in ('on','off'):
            target=1 if typ=='on' else 0
            if int(t['active'])==target: return None,f"{name}: уже {'включён' if target else 'отключён'}"
            return {'active':target},f"{name}: {'включить' if target else 'отключить'}"
        if typ=='pm': return {},f"{name}"
        return None,f'{name}: неизвестное действие'
    async def bulk_preview(message:Message,state:FSMContext,edit:bool=False):
        d=await state.get_data(); op=d.get('bulk_op'); ids=await bulk_ids(state)
        if not op or not ids: await message.answer('Сессия редактирования устарела. Начни заново: «💳 Тарифы» → «🧰 Массовое редактирование».'); return
        tariffs=[]
        for tid in ids:
            t=await DB.run('SELECT * FROM tariffs WHERE id=?',(tid,),one=True)
            if t: tariffs.append(t)
        changes=[];skips=[]
        for t in tariffs:
            upd,line=bulk_compute(op,t); (changes if upd is not None else skips).append(line)
        title=BULK_TITLES.get(op['type'],op['type'])
        text=f'🧰 ПРЕДПРОСМОТР: {title}\n\nБудет изменено тарифов: {len(changes)} из {len(tariffs)}.'
        if op['type']=='pm':
            names=await DB.run('SELECT name FROM payment_methods WHERE id IN (%s)'%','.join('?'*len(op['pm_ids'])),tuple(op['pm_ids']),all=True)
            text+=f"\nСпособы оплаты: {', '.join(x['name'] for x in names)}\nРежим: {'добавить к текущим' if op['mode']=='add' else 'заменить текущие'}"
        if changes: text+='\n\n'+'\n'.join('• '+x for x in changes[:12])+(f'\n… и ещё {len(changes)-12}' if len(changes)>12 else '')
        if skips: text+='\n\nПропущено:\n'+'\n'.join('• '+x for x in skips[:8])+(f'\n… и ещё {len(skips)-8}' if len(skips)>8 else '')
        rows=[]
        if op['type'] in ('pct','price'): rows.append([(f"🏷 Запоминать старую цену: {'ВКЛ' if op.get('remember_old') else 'ВЫКЛ'}",'bulk:rem')])
        if changes: rows.append([('✅ Применить','bulk:apply')])
        rows.append([('⬅️ Назад','bulk:menu'),('✖️ Выйти','bulk:exit')])
        if op['type'] in ('pct','price'): text+='\n\nСтарая цена сохраняется только при снижении цены и показывается зачёркнутой в карточке тарифа.'
        if edit: await safe_edit(message,text[:3900],ikb(rows))
        else: await message.answer(text[:3900],reply_markup=ikb(rows))
    async def bulk_guard(c:CallbackQuery,state:FSMContext,need_selection:bool=True):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return False
        if need_selection and not await bulk_ids(state):
            await c.answer('Сессия устарела или тарифы не выбраны. Начни заново.',show_alert=True); return False
        return True
    @dp.callback_query(F.data.in_({'bulk:start','bulk:start_keep'}))
    async def bulk_start(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        if c.data=='bulk:start': await state.clear(); await state.update_data(bulk_ids=[])
        else: await state.set_state(None)
        await bulk_select_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data.startswith('bulk:t:'))
    async def bulk_toggle(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state,False): return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        sel=await bulk_ids(state); sel=[x for x in sel if x!=tid] if tid in sel else sel+[tid]
        await state.update_data(bulk_ids=sel); await bulk_select_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data.in_({'bulk:all','bulk:none'}))
    async def bulk_all_none(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state,False): return
        ids=[t['id'] for t in (await DB.tariffs(None))[:90]] if c.data=='bulk:all' else []
        await state.update_data(bulk_ids=ids); await bulk_select_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data=='bulk:exit')
    async def bulk_exit(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.clear(); await show_tariffs(c.message,True); await c.answer()
    @dp.callback_query(F.data.in_({'bulk:next','bulk:menu'}))
    async def bulk_next(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state,False): return
        if not await bulk_ids(state):
            await c.answer('Выбери хотя бы один тариф' if c.data=='bulk:next' else 'Сессия устарела. Начни заново: «💳 Тарифы» → «🧰 Массовое редактирование».',show_alert=True); return
        await state.set_state(None); await bulk_menu_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data.startswith('bulk:a:'))
    async def bulk_action(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        act=c.data.split(':')[2]
        if act=='pct':
            rows=[[('−30%','bulk:pct:-30'),('−20%','bulk:pct:-20'),('−10%','bulk:pct:-10')],[('+10%','bulk:pct:10'),('+20%','bulk:pct:20')],[('✏️ Свой процент','bulk:pctc')],[('⬅️ Назад','bulk:menu')]]
            await safe_edit(c.message,'💰 На сколько процентов изменить цену выбранных тарифов? Минус — скидка, плюс — наценка. Валюта и формат цены сохранятся.',ikb(rows))
        elif act=='price':
            await state.set_state(BulkEdit.price); await c.message.answer('💰 Отправь новую цену для всех выбранных тарифов, например 500 ₽:',reply_markup=ikb([[('⬅️ Назад','bulk:menu')]]))
        elif act=='old':
            await safe_edit(c.message,'🏷 Старая цена показывается зачёркнутой рядом с текущей. Что сделать?',ikb([[('✏️ Указать старую цену','bulk:oldset')],[('🧹 Убрать старую цену','bulk:oldclear')],[('⬅️ Назад','bulk:menu')]]))
        elif act=='days':
            presets=[7,14,30,60,90,180,365]
            rows=[[(f'{n} дн.',f'bulk:days:{n}') for n in presets[i:i+3]] for i in range(0,len(presets),3)]
            rows+=[[('✏️ Свой срок','bulk:daysc')],[('⬅️ Назад','bulk:menu')]]
            await safe_edit(c.message,'📅 Выбери новый срок подписки для выбранных тарифов:',ikb(rows))
        elif act in ('on','off'):
            await state.update_data(bulk_op={'type':act}); await bulk_preview(c.message,state,edit=True)
        elif act=='pm':
            await state.update_data(bulk_pm=[]); await bulk_pm_screen(c.message,state)
        elif act=='promo':
            await promo_begin(c.message,state,await bulk_ids(state),'bulk')
        else: await c.answer('Неизвестное действие',show_alert=True); return
        await c.answer()
    @dp.callback_query(F.data.startswith('bulk:pct:'))
    async def bulk_pct_pick(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        try: pct=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный процент',show_alert=True); return
        await state.update_data(bulk_op={'type':'pct','percent':pct,'remember_old':pct<0}); await bulk_preview(c.message,state,edit=True); await c.answer()
    @dp.callback_query(F.data=='bulk:pctc')
    async def bulk_pct_custom(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        await state.set_state(BulkEdit.percent); await c.message.answer('Введи процент числом от −90 до +500, например -15 или +7.5:'); await c.answer()
    @dp.message(BulkEdit.percent)
    async def bulk_pct_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        raw=(m.text or '').strip().replace('−','-').replace('–','-').replace('%','').replace(',','.').replace(' ','')
        try: pct=float(raw); assert -90<=pct<=500 and pct!=0
        except Exception: await m.answer('Введи число от -90 до 500 (не 0), например -15 или +10.'); return
        pct=int(pct) if pct==int(pct) else pct
        await state.set_state(None); await state.update_data(bulk_op={'type':'pct','percent':pct,'remember_old':pct<0}); await bulk_preview(m,state)
    @dp.message(BulkEdit.price)
    async def bulk_price_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        value=(m.text or '').strip()
        if not value or len(value)>50: await m.answer('Укажи цену текстом до 50 символов, например 500 ₽.'); return
        await state.set_state(None); await state.update_data(bulk_op={'type':'price','value':value,'remember_old':False}); await bulk_preview(m,state)
    @dp.callback_query(F.data.in_({'bulk:oldset','bulk:oldclear'}))
    async def bulk_old_pick(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        if c.data=='bulk:oldclear': await state.update_data(bulk_op={'type':'old_clear'}); await bulk_preview(c.message,state,edit=True)
        else: await state.set_state(BulkEdit.old_price); await c.message.answer('🏷 Отправь старую цену, например 990 ₽. Она должна быть выше текущей цены тарифа (тарифы, где это не так, будут пропущены):')
        await c.answer()
    @dp.message(BulkEdit.old_price)
    async def bulk_old_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        value=(m.text or '').strip()
        if not value or len(value)>50 or parse_amount(value)<=0: await m.answer('Укажи старую цену с числом до 50 символов, например 990 ₽.'); return
        await state.set_state(None); await state.update_data(bulk_op={'type':'old_set','value':value}); await bulk_preview(m,state)
    @dp.callback_query(F.data.startswith('bulk:days:'))
    async def bulk_days_pick(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        try: n=int(c.data.rsplit(':',1)[1]); assert 1<=n<=36500
        except Exception: await c.answer('Некорректный срок',show_alert=True); return
        await state.update_data(bulk_op={'type':'days','value':n}); await bulk_preview(c.message,state,edit=True); await c.answer()
    @dp.callback_query(F.data=='bulk:daysc')
    async def bulk_days_custom(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        await state.set_state(BulkEdit.days); await c.message.answer('Введи срок подписки в днях целым числом (от 1 до 36500):'); await c.answer()
    @dp.message(BulkEdit.days)
    async def bulk_days_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        try: n=int((m.text or '').strip()); assert 1<=n<=36500
        except Exception: await m.answer('Укажи целое число дней от 1 до 36500.'); return
        await state.set_state(None); await state.update_data(bulk_op={'type':'days','value':n}); await bulk_preview(m,state)
    async def bulk_pm_screen(message:Message,state:FSMContext):
        d=await state.get_data(); sel=set(int(x) for x in d.get('bulk_pm',[]))
        methods=await DB.run('SELECT id,name FROM payment_methods WHERE active=1 ORDER BY id',all=True)
        if not methods:
            await safe_edit(message,'Нет включённых способов оплаты. Сначала добавь их в разделе «💰 Способы оплаты».',ikb([[('⬅️ Назад','bulk:menu')]])); return
        rows=[[(f"{'☑️' if x['id'] in sel else '⬜'} {x['name']}"[:60],f"bulk:pm:{x['id']}")] for x in methods]
        rows+=[[('➕ Добавить к текущим',"bulk:pmgo:add")],[('♻️ Заменить текущие','bulk:pmgo:replace')],[('⬅️ Назад','bulk:menu')]]
        await safe_edit(message,f'💳 Отметь способы оплаты и выбери режим. Выбрано: {len(sel)}.\n\n«Добавить» не трогает уже привязанные способы, «Заменить» оставит у тарифов только отмеченные.',ikb(rows))
    @dp.callback_query(F.data.startswith('bulk:pm:'))
    async def bulk_pm_toggle(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        try: pid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный способ',show_alert=True); return
        d=await state.get_data(); sel=[int(x) for x in d.get('bulk_pm',[])]; sel=[x for x in sel if x!=pid] if pid in sel else sel+[pid]
        await state.update_data(bulk_pm=sel); await bulk_pm_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data.startswith('bulk:pmgo:'))
    async def bulk_pm_go(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        d=await state.get_data(); sel=[int(x) for x in d.get('bulk_pm',[])]
        if not sel: await c.answer('Отметь хотя бы один способ оплаты',show_alert=True); return
        mode=c.data.rsplit(':',1)[1]
        if mode not in ('add','replace'): await c.answer('Неизвестный режим',show_alert=True); return
        await state.update_data(bulk_op={'type':'pm','mode':mode,'pm_ids':sel}); await bulk_preview(c.message,state,edit=True); await c.answer()
    @dp.callback_query(F.data=='bulk:rem')
    async def bulk_toggle_remember(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        d=await state.get_data(); op=d.get('bulk_op')
        if not op or op.get('type') not in ('pct','price'): await c.answer('Сессия устарела',show_alert=True); return
        op['remember_old']=not op.get('remember_old'); await state.update_data(bulk_op=op); await bulk_preview(c.message,state,edit=True); await c.answer()
    @dp.callback_query(F.data=='bulk:apply')
    async def bulk_apply(c:CallbackQuery,state:FSMContext):
        if not await bulk_guard(c,state): return
        d=await state.get_data(); op=d.get('bulk_op'); ids=await bulk_ids(state)
        if not op: await c.answer('Сессия устарела',show_alert=True); return
        changed=0; skipped=0
        async with aiosqlite.connect(DB.path, timeout=20) as db:
            db.row_factory=aiosqlite.Row
            await db.execute('BEGIN IMMEDIATE')
            try:
                for tid in ids:
                    cur=await db.execute('SELECT * FROM tariffs WHERE id=?',(tid,)); row=await cur.fetchone()
                    if not row: skipped+=1; continue
                    t=dict(row); upd,_line=bulk_compute(op,t)
                    if upd is None: skipped+=1; continue
                    if op['type']=='pm':
                        if op['mode']=='replace': await db.execute('DELETE FROM tariff_payment_methods WHERE tariff_id=?',(tid,))
                        for pid in op['pm_ids']: await db.execute('INSERT OR IGNORE INTO tariff_payment_methods(tariff_id,payment_method_id) VALUES(?,?)',(tid,pid))
                    elif upd:
                        assert set(upd)<= {'price','old_price','days','active'}
                        await db.execute(f"UPDATE tariffs SET {', '.join(k+'=?' for k in upd)} WHERE id=?",(*upd.values(),tid))
                        if 'price' in upd or 'old_price' in upd:   # a manual price change wins over a running timer
                            await db.execute("UPDATE price_promos SET status='cancelled',finished_at=CURRENT_TIMESTAMP WHERE tariff_id=? AND status='active'",(tid,))
                    changed+=1
                await db.commit()
            except Exception:
                await db.rollback(); log.exception('Bulk tariff update failed'); await c.answer('Не удалось применить изменения. Ничего не изменено.',show_alert=True); return
        await state.clear()
        await safe_edit(c.message,f'✅ Готово. Изменено тарифов: {changed}. Пропущено: {skipped}.',ikb([[('💳 К тарифам','tariff:list')],[('🧰 Ещё одно массовое действие','bulk:start')],[('↩️ Меню','menu:admin')]]))
        await c.answer('Изменения применены')

    # ------------------------------------------------------------------
    # Crypto Bot: admin setup, buyer checkout, automatic confirmation
    # ------------------------------------------------------------------
    crypto_alerts: dict = {}
    async def notify_admins(text:str, key=None, cooldown:float=3600, markup=None):
        if key is not None:
            now=asyncio.get_running_loop().time(); last=crypto_alerts.get(key)
            if last is not None and now-last<cooldown: return
            crypto_alerts[key]=now
        for aid in sorted(ADMIN_IDS|OWNER_IDS):
            try: await bot.send_message(aid,text,reply_markup=markup)
            except Exception: log.warning('Could not notify admin %s',aid)
    async def crypto_menu_payload():
        cfg=await crypto_config(); method=await crypto_method_row()
        if not cfg:
            text=('🪙 CRYPTO BOT\n\nПринимайте оплату криптовалютой (USDT, TON, BTC и др.). Покупатель платит по счёту в @CryptoBot, '
                  'доступ выдаётся автоматически, подтверждать чеки вручную не нужно.\n\nСтатус: не подключён.\n\n'
                  'Как подключить:\n1. Откройте @CryptoBot → Crypto Pay → Создать приложение.\n2. Скопируйте API Token.\n3. Нажмите «Подключить» и отправьте токен сюда.')
            return text, ikb([[('🔗 Подключить Crypto Bot','crypto:connect')],[('↩️ Способы оплаты','pay:back')]])
        app=await DB.setting('cryptobot_app','')
        net='тестовая сеть (@CryptoTestnetBot)' if cfg['network']=='test' else 'основная сеть'
        lines=['🪙 CRYPTO BOT','',f"Статус: подключён{(' · '+app) if app else ''}",f'Сеть: {net}',
               f"Валюта цен: {cfg['fiat']} (число из цены тарифа считается суммой в этой валюте; покупатель платит криптой по курсу)"]
        rows=[]
        if method:
            total=(await DB.run('SELECT COUNT(*) AS n FROM tariffs',one=True))['n']
            linked=(await DB.run('SELECT COUNT(*) AS n FROM tariff_payment_methods WHERE payment_method_id=?',(method['id'],),one=True))['n']
            lines.append(f"Способ оплаты для покупателей: {'🟢 включён' if method['active'] else '⏸ отключён'}")
            lines.append(f'Привязан к тарифам: {linked} из {total}')
            rows.append([(f"💱 Валюта: {cfg['fiat']}",'crypto:fiat')])
            rows.append([('🔗 Привязать ко всем тарифам','crypto:linkall')])
            rows.append([(('⏸ Отключить для покупателей' if method['active'] else '🟢 Включить для покупателей'),'crypto:toggle')])
        else:
            lines.append('⚠️ Способ оплаты удалён из списка, покупатели не видят Crypto Bot.')
            rows.append([('➕ Создать способ оплаты','crypto:ensure')])
        rows.append([('🌐 '+('Перейти на основную сеть' if cfg['network']=='test' else 'Перейти на тестовую сеть'),'crypto:net')])
        rows.append([('🔑 Сменить токен','crypto:connect'),('🔌 Отключить','crypto:disc')])
        rows.append([('↩️ Способы оплаты','pay:back')])
        return '\n'.join(lines), ikb(rows)
    async def crypto_show_menu(message:Message, edit:bool=True):
        text,markup=await crypto_menu_payload()
        if edit: await safe_edit(message,text,markup)
        else: await message.answer(text,reply_markup=markup)
    @dp.callback_query(F.data=='crypto:menu')
    async def crypto_menu_cb(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.clear(); await crypto_show_menu(c.message); await c.answer()
    @dp.callback_query(F.data=='crypto:connect')
    async def crypto_connect(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await state.set_state(CryptoSetup.token)
        await c.message.answer('🔑 Отправь API Token из @CryptoBot → Crypto Pay → твоё приложение.\n\nСообщение с токеном будет удалено. Для проверки на тестовой сети используй токен из @CryptoTestnetBot, бот определит сеть сам.',
                               reply_markup=ikb([[('✖️ Отмена','crypto:menu')]]))
        await c.answer()
    @dp.message(CryptoSetup.token)
    async def crypto_token_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        token=(m.text or '').strip()
        try: await m.delete()
        except Exception: pass
        if not CRYPTO_TOKEN_RE.match(token):
            await m.answer('Это не похоже на токен Crypto Pay. Он выглядит как 123456:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxx. Отправь токен ещё раз или нажми «Отмена».',reply_markup=ikb([[('✖️ Отмена','crypto:menu')]])); return
        info=None; network='main'; failure=''
        for net in ('main','test'):
            try: info=await crypto_request(token,net,'getMe'); network=net; break
            except CryptoError as e: failure=e.code
        if info is None:
            text='Не удалось проверить токен: Crypto Bot недоступен, попробуй позже.' if failure=='NETWORK' else 'Токен не подошёл ни к основной, ни к тестовой сети. Проверь, что скопировал токен приложения целиком, и отправь ещё раз.'
            await m.answer(text,reply_markup=ikb([[('✖️ Отмена','crypto:menu')]])); return
        first_time=not (await DB.setting('cryptobot_fiat','')).strip()
        await DB.set_setting('cryptobot_token',token); await DB.set_setting('cryptobot_network',network)
        await DB.set_setting('cryptobot_app',str((info or {}).get('name') or '')[:60])
        await crypto_ensure_method(); await state.clear()
        net_note=' (тестовая сеть)' if network=='test' else ''
        if first_time:
            rows=[[(code,f'crypto:setfiat:{code}') for code in CRYPTO_FIATS[i:i+4]] for i in range(0,len(CRYPTO_FIATS),4)]
            await m.answer(f'✅ Crypto Bot подключён{net_note}.\n\nВ какой валюте указаны цены тарифов? Число из цены (например 500 из «500 ₽») будет выставляться в счёте в этой валюте.',reply_markup=ikb(rows))
        else:
            await m.answer(f'✅ Токен обновлён{net_note}.'); await crypto_show_menu(m,edit=False)
    @dp.callback_query(F.data=='crypto:fiat')
    async def crypto_fiat_menu(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        rows=[[(code,f'crypto:setfiat:{code}') for code in CRYPTO_FIATS[i:i+4]] for i in range(0,len(CRYPTO_FIATS),4)]+[[('↩️ Назад','crypto:menu')]]
        await safe_edit(c.message,'💱 В какой валюте указаны цены тарифов? Число из цены будет выставляться в счёте в этой валюте. Меняй её вместе с ценами тарифов.',ikb(rows)); await c.answer()
    @dp.callback_query(F.data.startswith('crypto:setfiat:'))
    async def crypto_set_fiat(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        code=c.data.rsplit(':',1)[1]
        if code not in CRYPTO_FIATS: await c.answer('Неизвестная валюта',show_alert=True); return
        await DB.set_setting('cryptobot_fiat',code); await c.answer(f'Валюта: {code}')
        await crypto_show_menu(c.message)
    @dp.callback_query(F.data=='crypto:net')
    async def crypto_switch_network(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        cfg=await crypto_config()
        if not cfg: await c.answer('Crypto Bot не подключён',show_alert=True); return
        target='main' if cfg['network']=='test' else 'test'
        try: await crypto_request(cfg['token'],target,'getMe')
        except CryptoError: await c.answer('Этот токен не работает в выбранной сети. Подключи токен приложения из нужного бота (@CryptoBot или @CryptoTestnetBot).',show_alert=True); return
        await DB.set_setting('cryptobot_network',target); await c.answer('Сеть переключена'); await crypto_show_menu(c.message)
    @dp.callback_query(F.data=='crypto:linkall')
    async def crypto_link_all(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        method=await crypto_method_row()
        if not method: await c.answer('Сначала создай способ оплаты',show_alert=True); return
        before=(await DB.run('SELECT COUNT(*) AS n FROM tariff_payment_methods WHERE payment_method_id=?',(method['id'],),one=True))['n']
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.execute('INSERT OR IGNORE INTO tariff_payment_methods(tariff_id,payment_method_id) SELECT id,? FROM tariffs',(method['id'],)); await d.commit()
        after=(await DB.run('SELECT COUNT(*) AS n FROM tariff_payment_methods WHERE payment_method_id=?',(method['id'],),one=True))['n']
        await c.answer(f'Привязано новых тарифов: {after-before}',show_alert=True); await crypto_show_menu(c.message)
    @dp.callback_query(F.data=='crypto:toggle')
    async def crypto_toggle(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        method=await crypto_method_row()
        if not method: await c.answer('Способ оплаты не найден',show_alert=True); return
        await DB.run('UPDATE payment_methods SET active=? WHERE id=?',(0 if method['active'] else 1,method['id'])); await c.answer('Готово'); await crypto_show_menu(c.message)
    @dp.callback_query(F.data=='crypto:ensure')
    async def crypto_ensure_cb(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        if not await crypto_config(): await c.answer('Сначала подключи Crypto Bot',show_alert=True); return
        await crypto_ensure_method(); await c.answer('Способ оплаты создан'); await crypto_show_menu(c.message)
    @dp.callback_query(F.data=='crypto:disc')
    async def crypto_disconnect_ask(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        await safe_edit(c.message,'🔌 Отключить Crypto Bot?\n\nТокен будет удалён, способ оплаты пропадёт у всех тарифов. Счета, которые покупатели ещё не оплатили, перестанут отслеживаться: если кто-то оплатит такой счёт позже, подтверждай заявку вручную.',
                        ikb([[('Да, отключить','crypto:discyes')],[('Отмена','crypto:menu')]])); await c.answer()
    @dp.callback_query(F.data=='crypto:discyes')
    async def crypto_disconnect(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        method=await crypto_method_row()
        if method:
            await DB.run('DELETE FROM tariff_payment_methods WHERE payment_method_id=?',(method['id'],)); await DB.run('DELETE FROM payment_methods WHERE id=?',(method['id'],))
        for key in ('cryptobot_token','cryptobot_app'): await DB.set_setting(key,'')
        await state.clear(); await c.answer('Crypto Bot отключён'); await crypto_show_menu(c.message)

    async def crypto_checkout(c:CallbackQuery,t:dict,method:dict):
        """Create (or reuse) a Crypto Bot invoice for the selected tariff and show the pay button."""
        cfg=await crypto_config()
        if not cfg:
            await c.answer('Оплата через Crypto Bot временно недоступна',show_alert=True)
            await notify_admins('⚠️ Покупатель выбрал Crypto Bot, но токен не подключён. Откройте «💰 Способы оплаты» → «🪙 Crypto Bot».',key=('crypto-off',))
            return
        amount=parse_amount(t['price'])
        if amount<=0:
            await c.answer('Не удалось определить сумму тарифа. Сообщите администратору.',show_alert=True)
            await notify_admins(f"⚠️ В цене тарифа «{t['name']}» нет числа ({t['price']}), счёт Crypto Bot создать нельзя. Укажите цену числом, например 500 ₽.",key=('crypto-price',t['id']))
            return
        uid=c.from_user.id; now=datetime.now(timezone.utc)
        live=await DB.run("SELECT * FROM orders WHERE user_id=? AND tariff_id=? AND payment_method_id=? AND status='awaiting_crypto' AND crypto_invoice_url<>'' AND crypto_expires_at>? ORDER BY id DESC LIMIT 1",(uid,t['id'],method['id'],now.isoformat()),one=True)
        if live and abs(float(live['amount'])-amount)<1e-9:
            oid=live['id']; url=live['crypto_invoice_url']
        else:
            oid=await DB.run("INSERT INTO orders(user_id,tariff_id,payment_method_id,status,amount) VALUES(?,?,?,'awaiting_crypto',?)",(uid,t['id'],method['id'],amount))
            params={'currency_type':'fiat','fiat':cfg['fiat'],'amount':f'{amount:.2f}','description':f"{t['name']} · {t['days']} дн."[:1000],
                    'payload':f'order:{oid}','expires_in':CRYPTO_INVOICE_TTL,'allow_comments':False}
            if bot_info.username: params.update({'paid_btn_name':'openBot','paid_btn_url':f'https://t.me/{bot_info.username}'})
            try: inv=await crypto_call('createInvoice',params,cfg)
            except CryptoError as e:
                await DB.run("UPDATE orders SET status='cancelled' WHERE id=? AND status='awaiting_crypto'",(oid,))
                log.warning('Crypto Bot createInvoice failed for tariff=%s: %s',t['id'],e.code)
                await c.answer(crypto_error_text(e),show_alert=True)
                if e.code not in ('NETWORK','AMOUNT_TOO_SMALL','AMOUNT_TOO_LARGE'):
                    await notify_admins(f'⚠️ Crypto Bot отклонил создание счёта ({e.code}). Проверьте токен и настройки приложения в «💰 Способы оплаты» → «🪙 Crypto Bot».',key=('crypto-create',e.code))
                return
            url=inv.get('bot_invoice_url') or inv.get('pay_url') or ''
            if not url or inv.get('invoice_id') is None:
                await DB.run("UPDATE orders SET status='cancelled' WHERE id=? AND status='awaiting_crypto'",(oid,))
                await c.answer('Не удалось создать счёт в Crypto Bot. Попробуйте позже.',show_alert=True); return
            expires=(now+timedelta(seconds=CRYPTO_INVOICE_TTL)).isoformat()
            await DB.run('UPDATE orders SET crypto_invoice_id=?,crypto_invoice_url=?,crypto_expires_at=? WHERE id=?',(str(inv['invoice_id']),url,expires,oid))
        markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💎 Оплатить в Crypto Bot',url=url)],[InlineKeyboardButton(text='🔄 Проверить оплату',callback_data=f'crypto:check:{oid}')]])
        await c.message.answer(f"💎 Оплата через Crypto Bot\n\nТариф: {t['name']}\nСтоимость: {price_line(t['price'], t.get('old_price'))}\nК оплате: {amount:.2f} {cfg['fiat']} (в криптовалюте по курсу Crypto Bot)\nСчёт действует {CRYPTO_INVOICE_TTL//60} минут. Номер покупки: №{oid}.\n\nНажми «Оплатить», выбери монету и подтверди платёж. Доступ придёт сюда автоматически, обычно в течение минуты. Если не пришёл, нажми «Проверить оплату».",reply_markup=markup)
        await c.answer('Счёт создан')
    async def crypto_process_paid(o:dict, inv:dict)->str:
        """Grant access for a paid invoice. Returns 'ok', 'wait' (will retry) or 'skip'."""
        oid=o['id']
        if str(inv.get('payload') or '')!=f'order:{oid}' or abs(float(inv.get('amount') or 0)-float(o['amount']))>1e-6:
            log.error('Crypto invoice %s does not match order %s',inv.get('invoice_id'),oid)
            # Stop re-checking this order every poll: a person must resolve it.
            await DB.run("UPDATE orders SET status='crypto_mismatch' WHERE id=? AND status='awaiting_crypto'",(oid,))
            await notify_admins(f"⚠️ Счёт Crypto Bot №{inv.get('invoice_id')} оплачен, но не совпадает с заказом №{oid} (сумма или номер). Проверьте вручную, доступ не выдан.",key=('crypto-mismatch',oid))
            return 'skip'
        paid=' '.join(str(x) for x in (inv.get('paid_amount'),inv.get('paid_asset')) if x)
        result,data=await grant_order(oid,'awaiting_crypto',0,f"Crypto Bot invoice {inv.get('invoice_id')}{(': '+paid) if paid else ''}")
        if result=='ok':
            order,invites=data['o'],data['invites']
            delivered=await deliver_order_links(order,invites)
            who=await DB.run('SELECT username,first_name FROM users WHERE user_id=?',(order['user_id'],),one=True) or {}
            cfg=await crypto_config() or {'fiat':''}
            text=(f"💎 Crypto Bot: заказ №{oid} оплачен и подтверждён автоматически.\nПользователь: {who.get('first_name') or ''} (@{who.get('username') or '—'}, ID {order['user_id']})\n"
                  f"Тариф: {order['tariff_name']}\nСумма: {float(order['amount']):.2f} {cfg['fiat']}{(' · оплачено: '+paid) if paid else ''}")
            if not delivered: text+='\n\n⚠️ Подписка создана, но отправить ссылки покупателю не удалось.'
            await notify_admins(text,markup=None if delivered else ikb([[('📨 Повторить отправку ссылок',f'order:resend:{oid}')]]))
            return 'ok'
        if result=='busy': return 'skip'
        if result=='missing':
            cur=await DB.run('SELECT status FROM orders WHERE id=?',(oid,),one=True)
            if cur and cur['status']=='awaiting_crypto':
                await DB.run("UPDATE orders SET status='paid_unprocessed' WHERE id=? AND status='awaiting_crypto'",(oid,))
                await notify_admins(f"⚠️ Заказ №{oid} оплачен через Crypto Bot (счёт №{inv.get('invoice_id')}), но тариф удалён. Доступ не выдан, свяжитесь с покупателем (ID {o['user_id']}) и верните оплату.")
            return 'skip'
        await notify_admins(f"⚠️ Заказ №{oid} оплачен через Crypto Bot, но доступ выдать не удалось: {data}. Бот повторит попытку автоматически, исправьте причину (например, права бота в канале).",key=('crypto-grant',oid))
        return 'wait'
    async def crypto_expire_order(o:dict):
        changed=await DB.run("UPDATE orders SET status='cancelled' WHERE id=? AND status='awaiting_crypto'",(o['id'],))
        if changed==1:
            try: await bot.send_message(o['user_id'],f"⌛ Счёт по покупке №{o['id']} истёк и не был оплачен. Если хотите оформить подписку, выберите тариф заново.",reply_markup=MENU)
            except Exception: pass
    async def crypto_fetch_invoices(cfg:dict, ids:list)->dict:
        res=await crypto_call('getInvoices',{'invoice_ids':','.join(ids),'count':100},cfg)
        items=res.get('items') if isinstance(res,dict) else res
        return {str(i.get('invoice_id')):i for i in (items or []) if isinstance(i,dict)}
    async def crypto_poll_once():
        cfg=await crypto_config()
        if not cfg: return
        await DB.run("UPDATE orders SET status='cancelled' WHERE status='awaiting_crypto' AND crypto_invoice_id='' AND created_at<datetime('now','-15 minutes')")
        rows=await DB.run("SELECT * FROM orders WHERE status='awaiting_crypto' AND crypto_invoice_id<>'' ORDER BY id LIMIT 100",all=True)
        if not rows: return
        by_id=await crypto_fetch_invoices(cfg,[r['crypto_invoice_id'] for r in rows])
        now=datetime.now(timezone.utc)
        for o in rows:
            inv=by_id.get(o['crypto_invoice_id'])
            if inv is None:
                try: gone=datetime.fromisoformat(o['crypto_expires_at'])<now-timedelta(days=1)
                except (ValueError,TypeError): gone=True
                if gone: await crypto_expire_order(o)
                continue
            status=inv.get('status')
            if status=='paid': await crypto_process_paid(o,inv)
            elif status=='expired': await crypto_expire_order(o)
    async def crypto_worker():
        while True:
            try: await crypto_poll_once()
            except CryptoError as e: log.warning('Crypto Bot poll failed: %s',e.code)
            except Exception: log.exception('Crypto Bot worker failed')
            await asyncio.sleep(CRYPTO_POLL_SECONDS)
    @dp.callback_query(F.data.startswith('crypto:check:'))
    async def crypto_check(c:CallbackQuery):
        try: oid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректная покупка',show_alert=True); return
        o=await DB.run('SELECT * FROM orders WHERE id=? AND user_id=?',(oid,c.from_user.id),one=True)
        if not o or not o['crypto_invoice_id']: await c.answer('Покупка не найдена',show_alert=True); return
        if o['status']=='approved': await c.answer('Оплата уже подтверждена, ссылки отправлены выше.',show_alert=True); return
        if o['status']!='awaiting_crypto': await c.answer('Этот счёт уже закрыт. Выберите тариф заново.',show_alert=True); return
        try: inv=(await crypto_fetch_invoices(await crypto_config(),[o['crypto_invoice_id']])).get(o['crypto_invoice_id'])
        except CryptoError as e: await c.answer(crypto_error_text(e),show_alert=True); return
        if inv and inv.get('status')=='paid':
            result=await crypto_process_paid(o,inv)
            await c.answer('Оплата найдена, доступ выдаётся.' if result in ('ok','wait') else 'Оплата найдена, идёт обработка.',show_alert=True)
        elif inv and inv.get('status')=='expired':
            await crypto_expire_order(o); await c.answer('Счёт истёк. Выберите тариф заново.',show_alert=True)
        else: await c.answer('Оплата пока не найдена. Если вы уже заплатили, подождите минуту и проверьте снова.',show_alert=True)

    # ------------------------------------------------------------------
    # Timed promotions: change a price now, restore it automatically later
    # ------------------------------------------------------------------
    PROMO_PCT_PRESETS=[-10,-20,-30,-50,10,20]
    PROMO_DURATIONS=[(360,'6 ч'),(720,'12 ч'),(1440,'24 ч'),(4320,'3 дня'),(10080,'7 дней'),(20160,'14 дней')]
    async def promo_percent_screen(message:Message,state:FSMContext,edit:bool=True):
        d=await state.get_data(); n=len(d.get('promo_ids') or [])
        rows=[[(f'{p:+d}%'.replace('-','−'),f'promo:pct:{p}') for p in PROMO_PCT_PRESETS[i:i+3]] for i in range(0,len(PROMO_PCT_PRESETS),3)]
        rows+=[[('✏️ Свой процент','promo:pctc')],[('✖️ Отмена','promo:cancel')]]
        text=f'⏳ АКЦИЯ НА ВРЕМЯ\n\nТарифов: {n}. На сколько процентов изменить цену? Минус — скидка, плюс — наценка. Когда время выйдет, цена вернётся сама.'
        if edit: await safe_edit(message,text,ikb(rows))
        else: await message.answer(text,reply_markup=ikb(rows))
    async def promo_duration_screen(message:Message,state:FSMContext,edit:bool=True):
        d=await state.get_data(); pct=d.get('promo_pct')
        rows=[[(label,f'promo:dur:{m}') for m,label in PROMO_DURATIONS[i:i+3]] for i in range(0,len(PROMO_DURATIONS),3)]
        rows+=[[('✏️ Свой срок','promo:durc')],[('⬅️ Назад','promo:backpct'),('✖️ Отмена','promo:cancel')]]
        text=f'⏳ Изменение цены: {pct:+g}%'.replace('-','−')+'\n\nНа какое время включить акцию? После этого цена вернётся к прежней автоматически.'
        if edit: await safe_edit(message,text,ikb(rows))
        else: await message.answer(text,reply_markup=ikb(rows))
    async def promo_preview(message:Message,state:FSMContext,edit:bool=True):
        d=await state.get_data(); ids=d.get('promo_ids') or []; pct=d.get('promo_pct'); minutes=d.get('promo_minutes')
        if not ids or pct is None or not minutes:
            await message.answer('Сессия устарела. Начни заново из карточки тарифа или из массового редактирования.'); return
        ends=datetime.now(timezone.utc)+timedelta(minutes=int(minutes)); show_old=bool(d.get('promo_show_old'))
        lines=[]; skips=[]
        for tid in ids:
            t=await DB.run('SELECT * FROM tariffs WHERE id=?',(tid,),one=True)
            if not t: continue
            current=await active_promo(tid); base=current['original_price'] if current else t['price']
            new=reprice(base,pct)
            if new is None or new==base: skips.append(f"{t['name']}: в цене нет числа или цена не изменится")
            else: lines.append(f"{t['name']}: {base} → {new}"+(' (текущая акция будет заменена)' if current else ''))
        text=(f"⏳ АКЦИЯ НА ВРЕМЯ\n\nИзменение цены: {pct:+g}% на {format_minutes(int(minutes))}\n"
              f"До: {fmt_date(ends.isoformat())}, затем цена вернётся автоматически.\nТарифов в акции: {len(lines)} из {len(ids)}")
        if lines: text+='\n\n'+'\n'.join('• '+x for x in lines[:12])+(f'\n… и ещё {len(lines)-12}' if len(lines)>12 else '')
        if skips: text+='\n\nПропущено:\n'+'\n'.join('• '+x for x in skips[:8])
        rows=[[(f"🏷 Старая цена зачёркнутой: {'ВКЛ' if show_old else 'ВЫКЛ'}",'promo:old')]]
        if lines: rows.append([('✅ Запустить акцию','promo:go')])
        rows.append([('⬅️ Назад','promo:backdur'),('✖️ Отмена','promo:cancel')])
        if edit: await safe_edit(message,text[:3900],ikb(rows))
        else: await message.answer(text[:3900],reply_markup=ikb(rows))
    async def promo_guard(c:CallbackQuery,state:FSMContext)->bool:
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return False
        if not (await state.get_data()).get('promo_ids'):
            await c.answer('Сессия устарела. Начни заново.',show_alert=True); return False
        return True
    async def promo_begin(message:Message,state:FSMContext,ids:list,back:str):
        await state.set_state(None)
        await state.update_data(promo_ids=list(ids),promo_back=back,promo_pct=None,promo_minutes=None,promo_show_old=True)
        await promo_percent_screen(message,state)
    @dp.callback_query(F.data.startswith('promo:start:'))
    async def promo_start_card(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        if not await DB.run('SELECT id FROM tariffs WHERE id=?',(tid,),one=True): await c.answer('Тариф не найден',show_alert=True); return
        await promo_begin(c.message,state,[tid],f'card:{tid}'); await c.answer()
    @dp.callback_query(F.data.startswith('promo:pct:'))
    async def promo_pick_percent(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        try: pct=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный процент',show_alert=True); return
        await state.update_data(promo_pct=pct,promo_show_old=pct<0); await promo_duration_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data=='promo:pctc')
    async def promo_custom_percent(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        await state.set_state(PromoEdit.percent); await c.message.answer('Введи процент числом от −90 до +500, например -50 или +15:'); await c.answer()
    @dp.message(PromoEdit.percent)
    async def promo_percent_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        pct=parse_percent(m.text)
        if pct is None: await m.answer('Введи число от -90 до 500 (не 0), например -50 или +15.'); return
        await state.set_state(None); await state.update_data(promo_pct=pct,promo_show_old=pct<0); await promo_duration_screen(m,state,edit=False)
    @dp.callback_query(F.data.startswith('promo:dur:'))
    async def promo_pick_duration(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        try: minutes=int(c.data.rsplit(':',1)[1]); assert 60<=minutes<=365*1440
        except Exception: await c.answer('Некорректный срок',show_alert=True); return
        await state.update_data(promo_minutes=minutes); await promo_preview(c.message,state); await c.answer()
    @dp.callback_query(F.data=='promo:durc')
    async def promo_custom_duration(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        await state.set_state(PromoEdit.duration)
        await c.message.answer('Введи срок акции: число часов или дней, например 36 (часов), 12ч или 2д. От 1 часа до 365 дней:'); await c.answer()
    @dp.message(PromoEdit.duration)
    async def promo_duration_text(m:Message,state:FSMContext):
        if not admin(m.from_user.id): return
        minutes=parse_duration(m.text)
        if minutes is None: await m.answer('Не понял срок. Примеры: 36 (часов), 12ч, 2д. Допустимо от 1 часа до 365 дней.'); return
        await state.set_state(None); await state.update_data(promo_minutes=minutes); await promo_preview(m,state,edit=False)
    @dp.callback_query(F.data=='promo:old')
    async def promo_toggle_old(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        d=await state.get_data(); await state.update_data(promo_show_old=not d.get('promo_show_old')); await promo_preview(c.message,state); await c.answer()
    @dp.callback_query(F.data=='promo:backpct')
    async def promo_back_to_percent(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        await promo_percent_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data=='promo:backdur')
    async def promo_back_to_duration(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        await promo_duration_screen(c.message,state); await c.answer()
    @dp.callback_query(F.data=='promo:cancel')
    async def promo_cancel(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        d=await state.get_data(); back=d.get('promo_back') or ''
        await state.set_state(None); await state.update_data(promo_ids=None,promo_pct=None,promo_minutes=None,promo_show_old=None,promo_back=None)
        await c.answer()
        if back.startswith('card:'): await show_tariff_card(c.message,int(back.split(':')[1]),edit=True)
        elif back=='bulk' and await bulk_ids(state): await bulk_menu_screen(c.message,state)
        else: await show_tariffs(c.message,True)
    @dp.callback_query(F.data=='promo:go')
    async def promo_go(c:CallbackQuery,state:FSMContext):
        if not await promo_guard(c,state): return
        d=await state.get_data(); ids=d['promo_ids']; pct=d.get('promo_pct'); minutes=d.get('promo_minutes'); back=d.get('promo_back') or ''
        if pct is None or not minutes: await c.answer('Сессия устарела. Начни заново.',show_alert=True); return
        started=[]; failed=[]
        for tid in ids:
            info,why=await start_promo(tid,pct,int(minutes),bool(d.get('promo_show_old')))
            (started if info else failed).append(info or why)
        if not started: await c.answer('Акцию запустить не удалось: '+(failed[0] if failed else 'нет подходящих тарифов'),show_alert=True); return
        ends=started[0]['ends']; await state.clear(); await c.answer('Акция запущена')
        text=f"✅ Акция запущена на тарифах: {len(started)}. Цена {pct:+g}% действует до {fmt_date(ends)}, затем вернётся автоматически."
        if failed: text+=f'\nПропущено: {len(failed)}.'
        if back.startswith('card:'):
            await safe_edit(c.message,text,None); await show_tariff_card(c.message,int(back.split(':')[1]))
        else: await safe_edit(c.message,text,ikb([[('💳 К тарифам','tariff:list')],[('↩️ Меню','menu:admin')]]))
    @dp.callback_query(F.data.startswith('promo:end:'))
    async def promo_end_now(c:CallbackQuery,state:FSMContext):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True); return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        current=await active_promo(tid)
        if not current: await c.answer('Акции по этому тарифу уже нет',show_alert=True); await show_tariff_card(c.message,tid,edit=True); return
        result=await end_promo(current['id'],'manual')
        await c.answer(f"Акция завершена, цена: {result['to']}" if result and result.get('restored') else 'Акция завершена')
        await show_tariff_card(c.message,tid,edit=True)
    async def promo_pass():
        now=datetime.now(timezone.utc).isoformat()
        due=await DB.run("SELECT * FROM price_promos WHERE status='active' AND ends_at<=? ORDER BY id",(now,),all=True)
        results=[]
        for promo in due:
            result=await end_promo(promo['id'],'done')
            if result: results.append(result)
        if results:
            lines=[f"• {r['name']}: {r['from']} → {r['to']}" if r['restored'] else f"• {r['name']}: цену меняли вручную, она осталась как есть" for r in results]
            await notify_admins('⏳ Акция закончилась, цены вернулись:\n'+'\n'.join(lines))
    async def promo_worker():
        # Promotions live in the database: after a restart the bot restores prices that came due while it was down.
        while True:
            try: await promo_pass()
            except Exception: log.exception('Promo worker failed')
            await asyncio.sleep(PROMO_INTERVAL)

    @dp.callback_query(F.data.startswith('order:new:'))
    async def neworder(c:CallbackQuery):
        if admin(c.from_user.id): await c.answer('Администратору заявка не нужна'); return
        if await turnover_blocked():
            await c.answer('Лимит оборота исчерпан. Владелец бота должен продлить лицензию.',show_alert=True); return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        t=await DB.run('SELECT t.* FROM tariffs t LEFT JOIN channels ch ON ch.channel_id=t.channel_id WHERE t.id=? AND t.active=1 AND COALESCE(ch.active,0)=1',(tid,),one=True)
        if not t: await c.answer('Тариф больше недоступен',show_alert=True); return
        linked=await DB.run('SELECT pm.id,pm.name FROM tariff_payment_methods tp JOIN payment_methods pm ON pm.id=tp.payment_method_id WHERE tp.tariff_id=? AND pm.active=1 ORDER BY pm.id',(tid,),all=True)
        if not linked:
            await c.answer('Для этого тарифа не настроен способ оплаты',show_alert=True)
            await c.message.answer('Оплата для выбранного тарифа пока не настроена. Пожалуйста, сообщите администратору.')
            for aid in ADMIN_IDS:
                try: await bot.send_message(aid,f'⚠️ Пользователь {c.from_user.id} выбрал тариф «{t["name"]}», но у тарифа нет привязанных активных способов оплаты. Откройте тариф → «Способы оплаты».')
                except Exception: pass
            return
        rows=[[(x['name'][:55],f"order:pay:{tid}:{x['id']}")] for x in linked]
        rows.append([('↩️ К тарифам','order:back')])
        desc_line=f"\nОписание: {t['description'].strip()}" if (t.get('description') or '').strip() else ''
        promo_now=await active_promo(t['id'])
        promo_line_text=f"\n⏳ Акция до {fmt_date(promo_now['ends_at'])}" if promo_now else ''
        await c.message.answer(f"💳 Тариф: {t['name']}\nСтоимость: {price_line(t['price'], t.get('old_price'))}{promo_line_text}\nСрок: {t['days']} дней{desc_line}\n\nВыбери способ оплаты:",reply_markup=ikb(rows))
        await c.answer()

    @dp.callback_query(F.data=='order:back')
    async def order_back(c:CallbackQuery):
        await show_tariffs(c.message,False); await c.answer()

    @dp.callback_query(F.data.startswith('order:pay:'))
    async def order_choose_payment(c:CallbackQuery):
        if admin(c.from_user.id): await c.answer('Администратору заявка не нужна',show_alert=True); return
        if await turnover_blocked(): await c.answer('Лимит оборота исчерпан. Владелец бота должен продлить лицензию.',show_alert=True); return
        try: _,_,tid_s,mid_s=c.data.split(':'); tid=int(tid_s); mid=int(mid_s)
        except (ValueError,TypeError): await c.answer('Некорректный выбор оплаты',show_alert=True); return
        t=await DB.run('SELECT t.* FROM tariffs t LEFT JOIN channels ch ON ch.channel_id=t.channel_id WHERE t.id=? AND t.active=1 AND COALESCE(ch.active,0)=1',(tid,),one=True)
        method=await DB.run('SELECT pm.id,pm.name,pm.details,pm.kind FROM tariff_payment_methods tp JOIN payment_methods pm ON pm.id=tp.payment_method_id WHERE tp.tariff_id=? AND pm.id=? AND pm.active=1',(tid,mid),one=True)
        if not t or not method:
            await c.answer('Тариф или способ оплаты больше недоступен. Открой список тарифов заново.',show_alert=True); return
        # Keep the selected purchase as a private draft until the user uploads a receipt.
        # Drafts are not shown to admins and do not count as payment applications.
        await DB.run("UPDATE orders SET status='cancelled' WHERE user_id=? AND status='awaiting_proof'",(c.from_user.id,))
        if method['kind']=='cryptobot':
            await crypto_checkout(c,t,method); return
        amount=parse_amount(t['price'])
        oid=await DB.run("INSERT INTO orders(user_id,tariff_id,payment_method_id,status,amount) VALUES(?,?,?,'awaiting_proof',?)",(c.from_user.id,tid,mid,amount))
        await c.message.answer(f"Вы выбрали тариф «{t['name']}»\nСтоимость: {price_line(t['price'], t.get('old_price'))}\nСрок: {t['days']} дней\n\nСпособ оплаты: {method['name']}\n{method['details']}\n\nПосле оплаты загрузите фото или файл чека сюда. Покупка будет отправлена администратору только после загрузки чека. Номер покупки: №{oid}.",reply_markup=MENU)
        await c.answer('Способ оплаты выбран. Загрузите чек после оплаты.')
    @dp.message(F.photo | F.document | F.text)
    async def receiveproof(m:Message):
        if (m.text or '').startswith('/'):return
        # Only an actual uploaded photo/document counts as a receipt. Text messages must not create a request.
        if not m.photo and not m.document:
            draft=await DB.run("SELECT id FROM orders WHERE user_id=? AND status='awaiting_proof' ORDER BY id DESC LIMIT 1",(m.from_user.id,),one=True)
            if draft:
                await m.answer('Чтобы отправить заявку администратору, загрузите фото или файл чека. Текстовое сообщение не считается чеком.',reply_markup=MENU)
            return
        lp=await DB.run("SELECT * FROM license_payments WHERE user_id=? AND status='awaiting_proof' ORDER BY id DESC LIMIT 1",(m.from_user.id,),one=True)
        if admin(m.from_user.id) and not lp: return
        if lp:
            typ='photo' if m.photo else 'document'; fid=m.photo[-1].file_id if m.photo else m.document.file_id
            changed=await DB.run("UPDATE license_payments SET proof_type=?,proof_file_id=?,status='pending' WHERE id=? AND status='awaiting_proof'",(typ,fid,lp['id']))
            if changed!=1: await m.answer('Заявка на лицензию уже отправлена.',reply_markup=admin_menu_for(m.from_user.id)); return
            method=await DB.run('SELECT name FROM license_payment_methods WHERE id=?',(lp['payment_method_id'],),one=True)
            # Show the immutable checkout snapshot to the constructor owner so the
            # reviewed receipt is clear even if the central tariff was edited later.
            plan_name = lp.get('license_tariff_name') or 'Тариф лицензии'
            plan_limit = lp.get('license_turnover_limit')
            plan_days = lp.get('license_duration_days')
            caption = (
                f"🔓 Оплата лицензии L{lp['id']}\n"
                f"Администратор: {m.from_user.full_name} (ID {m.from_user.id})\n"
                f"Тариф: {plan_name}\n"
            )
            if plan_limit is not None:
                caption += f"Лимит оборота: {float(plan_limit):g}\n"
            if plan_days is not None:
                caption += f"Срок лицензии: {int(plan_days)} дней\n"
            caption += f"Сумма: {float(lp['amount']):g}\nСпособ: {method['name'] if method else '—'}"
            controls=ikb([[('✅ Подтвердить лицензию',f"license:approve:{lp['id']}"),('❌ Отклонить',f"license:reject:{lp['id']}")]])
            delivered = 0
            for aid in PLATFORM_OWNER_IDS:
                try:
                    if m.photo: await bot.send_photo(aid,fid,caption=caption,reply_markup=controls)
                    else: await bot.send_document(aid,fid,caption=caption,reply_markup=controls)
                    delivered += 1
                except Exception: log.exception('Could not forward license proof to owner=%s', aid)
            if delivered == 0:
                await DB.run("UPDATE license_payments SET status='awaiting_proof',proof_type=NULL,proof_file_id=NULL WHERE id=? AND status='pending'",(lp['id'],))
                await m.answer('Не удалось доставить чек владельцу. Заявка не отправлена. Проверьте, что бот может написать владельцу, и отправьте чек ещё раз.',reply_markup=admin_menu_for(m.from_user.id))
            else:
                await m.answer('Чек на лицензию получен. Владелец проверит оплату.',reply_markup=admin_menu_for(m.from_user.id))
            return
        o=await DB.run("SELECT * FROM orders WHERE user_id=? AND status='awaiting_proof' ORDER BY id DESC LIMIT 1",(m.from_user.id,),one=True)
        if not o: return
        typ='photo' if m.photo else 'document'; fid=m.photo[-1].file_id if m.photo else m.document.file_id; txt=m.caption
        # Promote the draft to a pending admin application only after receipt upload.
        unique_id=(m.photo[-1].file_unique_id if m.photo else m.document.file_unique_id) or ''
        changed=await DB.run("UPDATE orders SET proof_type=?,proof_file_id=?,proof_text=?,proof_unique_id=?,status='pending' WHERE id=? AND status='awaiting_proof'",(typ,fid,txt,unique_id,o['id']))
        if changed != 1:
            await m.answer('Эта покупка уже отправлена или отменена. Если нужно, выберите тариф заново.',reply_markup=MENU); return
        u=m.from_user
        tariff=await DB.run('SELECT name,price FROM tariffs WHERE id=?',(o['tariff_id'],),one=True)
        caption=(f"🛍 Новая покупка\nПользователь: {u.full_name} (@{u.username or '—'}, ID {u.id})\nТариф: {tariff['name'] if tariff else o['tariff_id']}\nСумма: {tariff['price'] if tariff else '—'}\nЧек приложен."+await duplicate_receipt_note(o['id'],unique_id))[:1020]
        controls=ikb([[('✅ Подтвердить оплату',f"order:approve:{o['id']}"),('❌ Отклонить',f"order:reject:{o['id']}")]])
        delivered = 0
        for aid in ADMIN_IDS:
            try:
                if m.photo: await bot.send_photo(aid,fid,caption=caption,reply_markup=controls)
                else: await bot.send_document(aid,fid,caption=caption,reply_markup=controls)
                delivered += 1
            except Exception: log.exception('Could not forward proof with controls for order %s to admin=%s',o['id'],aid)
        if delivered == 0:
            await DB.run("UPDATE orders SET status='awaiting_proof',proof_type=NULL,proof_file_id=NULL,proof_text=NULL WHERE id=? AND status='pending'",(o['id'],))
            await m.answer('Не удалось доставить чек администраторам. Покупка не отправлена. Попробуйте позже или свяжитесь с администратором, затем загрузите чек повторно.',reply_markup=MENU)
        else:
            await m.answer('Чек получен. Покупка отправлена администратору на проверку. Доступ появится после подтверждения оплаты.',reply_markup=MENU)
    @dp.callback_query(F.data.startswith('license:approve:'))
    async def license_approve(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец конструктора может активировать лицензию',show_alert=True); return
        pid=int(c.data.rsplit(':',1)[1]); lp=await DB.run("SELECT * FROM license_payments WHERE id=? AND status='pending' AND proof_file_id IS NOT NULL",(pid,),one=True)
        if not lp: await c.answer('Заявка уже обработана или чек отсутствует',show_alert=True); return
        plan = await DB.run('SELECT * FROM license_tariffs WHERE id=?',(lp.get('license_tariff_id'),),one=True) if lp.get('license_tariff_id') else None
        # Prefer the immutable checkout snapshot; fall back to the current tariff
        # for legacy rows created before this migration.
        plan_name = lp.get('license_tariff_name') or (plan.get('name') if plan else None)
        try:
            plan_limit = float(lp.get('license_turnover_limit')) if lp.get('license_turnover_limit') is not None else (float(plan['turnover_limit']) if plan else None)
        except (ValueError, TypeError, KeyError):
            plan_limit = float(plan['turnover_limit']) if plan else None
        try:
            duration=max(1,int(lp.get('license_duration_days') or (plan['duration_days'] if plan else await DB.setting('license_duration_days','30'))))
        except (ValueError, TypeError, KeyError): duration=30
        # Read the latest expiry and update the payment/license in one write transaction.
        # Renewals purchased early start after the existing expiry instead of losing remaining days.
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.execute('BEGIN IMMEDIATE')
            cur=await d.execute("UPDATE license_payments SET status='approved',reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending' AND proof_file_id IS NOT NULL",(pid,))
            if cur.rowcount != 1:
                await d.rollback()
                await c.answer('Заявка уже обработана',show_alert=True)
                return
            setting_row=await (await d.execute("SELECT value FROM settings WHERE key='license_expires_at'")).fetchone()
            current_expiry=setting_row[0] if setting_row else ''
            expiry=calculate_license_expiry(current_expiry, duration)
            if plan_limit is not None:
                await d.execute("INSERT INTO settings(key,value) VALUES('turnover_limit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(plan_limit),))
            await d.execute("INSERT INTO settings(key,value) VALUES('license_active','1') ON CONFLICT(key) DO UPDATE SET value='1'")
            await d.execute("INSERT INTO settings(key,value) VALUES('license_turnover_capped','0') ON CONFLICT(key) DO UPDATE SET value='0'")
            await d.execute("INSERT INTO settings(key,value) VALUES('license_expires_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(expiry,))
            await d.commit()
        # Payment/license state is already committed. Notification failure must not make
        # the owner think approval failed or prevent the callback from being acknowledged.
        try:
            notice = (f"✅ Оплата лицензии подтверждена. Тариф: {plan_name}. Лимит оборота: {plan_limit:g}. Срок: {duration} дней. Лицензия действует до {fmt_date(expiry)}." if plan_name and plan_limit is not None else f'✅ Оплата лицензии подтверждена. Лицензия действует до {fmt_date(expiry)}.')
            await bot.send_message(lp['user_id'], notice)
        except Exception:
            log.exception('License approved but user notification failed; payment_id=%s user_id=%s', pid, lp['user_id'])
        try:
            await c.message.edit_caption(caption=(c.message.caption or '')+'\n\n✅ Лицензия активирована')
        except Exception:
            log.exception('License approved but owner message could not be updated; payment_id=%s', pid)
        await c.answer('Лицензия активирована')

    @dp.callback_query(F.data.startswith('license:reject:'))
    async def license_reject(c:CallbackQuery):
        if c.from_user.id not in PLATFORM_OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        try:
            pid=int(c.data.rsplit(':',1)[1])
        except (ValueError, IndexError):
            await c.answer('Некорректный номер заявки',show_alert=True); return
        # Claim the rejection atomically so approve/reject callbacks cannot both win.
        async with aiosqlite.connect(DB.path, timeout=20) as d:
            await d.execute('BEGIN IMMEDIATE')
            row=await (await d.execute("SELECT user_id FROM license_payments WHERE id=? AND status='pending'",(pid,))).fetchone()
            if not row:
                await d.rollback()
                await c.answer('Заявка уже обработана',show_alert=True)
                return
            cur=await d.execute("UPDATE license_payments SET status='rejected',reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(pid,))
            if cur.rowcount != 1:
                await d.rollback()
                await c.answer('Заявка уже обработана',show_alert=True)
                return
            await d.commit()
        try:
            await bot.send_message(row[0],'❌ Оплата лицензии не подтверждена. Обратитесь к владельцу бота.')
        except Exception:
            log.exception('License payment rejected but user notification failed; payment_id=%s user_id=%s', pid, row[0])
        try:
            await c.message.edit_caption(caption=(c.message.caption or '')+'\n\n❌ Отклонено')
        except Exception:
            log.exception('License payment rejected but owner message could not be updated; payment_id=%s', pid)
        await c.answer('Заявка отклонена')

    async def grant_order(oid:int, from_status:str, reviewer_id:int, note:str=''):
        """Issue channel access for an order that is currently in `from_status`.

        Shared by manual approval (from_status='pending') and automatic Crypto Bot payments
        ('awaiting_crypto'). The order is claimed atomically, so it can be granted only once.
        Returns (state, data): ('ok', {'o': order, 'invites': [...]}), ('missing', None),
        ('busy', None), ('perm', text) or ('error', text). On 'perm'/'error' the order goes back
        to `from_status` and nothing is granted.
        """
        base="SELECT o.*,t.days,t.channel_id,t.channel_title,t.name AS tariff_name FROM orders o JOIN tariffs t ON t.id=o.tariff_id WHERE o.id=? AND o.status=?"
        if from_status=='pending': base+=" AND o.proof_file_id IS NOT NULL AND o.proof_type IN ('photo','document')"
        o=await DB.run(base,(oid,from_status),one=True)
        if not o: return 'missing',None
        # Atomically reserve the request so it cannot be approved twice.
        claimed=await DB.run("UPDATE orders SET status='processing' WHERE id=? AND status=?",(oid,from_status))
        if claimed != 1: return 'busy',None
        invites=[]
        committed=False
        try:
            # Verify bot permissions again before granting access.
            member=await bot.get_chat_member(o['channel_id'],bot_id)
            if member.status not in ('administrator','creator') or (member.status=='administrator' and (not getattr(member,'can_invite_users',False) or not getattr(member,'can_restrict_members',False))):
                await DB.run("UPDATE orders SET status=? WHERE id=? AND status='processing'",(from_status,oid))
                return 'perm','Боту нужны права приглашать пользователей и ограничивать участников канала'
            channels=await tariff_channels({'id':o['tariff_id'],'channel_id':o['channel_id'],'channel_title':o['channel_title']})
            for channel in channels:
                channel_id=int(channel['channel_id'])
                member=await bot.get_chat_member(channel_id,bot_id)
                if member.status not in ('administrator','creator') or (member.status=='administrator' and (not getattr(member,'can_invite_users',False) or not getattr(member,'can_restrict_members',False))):
                    raise RuntimeError(f'Нет прав бота в канале {channel.get("channel_title",channel_id)}')
                invite=await bot.create_chat_invite_link(chat_id=channel_id,member_limit=1,expire_date=int((datetime.now(timezone.utc)+timedelta(days=2)).timestamp()),name=f"sub-{o['user_id']}-{oid}")
                invites.append((channel,invite.invite_link,None))
            # Commit all channel subscriptions and the order status together.
            # BEGIN IMMEDIATE serializes approvals that renew the same user/channel.
            # Expiry is read INSIDE this write transaction so two nearly simultaneous
            # purchases cannot both extend from the same old expiry and lose one term.
            async with aiosqlite.connect(DB.path, timeout=20) as d:
                await d.execute('PRAGMA foreign_keys=ON')
                await d.execute('BEGIN IMMEDIATE')
                committed_invites=[]
                for channel, invite_link, _unused in invites:
                    channel_id=int(channel['channel_id'])
                    current_cur=await d.execute("SELECT id,expires_at FROM subscriptions WHERE user_id=? AND channel_id=? AND status='active' ORDER BY expires_at DESC LIMIT 1",(o['user_id'],channel_id))
                    current_row=await current_cur.fetchone()
                    now=datetime.now(timezone.utc)
                    if current_row:
                        try:
                            old=datetime.fromisoformat(current_row[1])
                            if old.tzinfo is None: old=old.replace(tzinfo=timezone.utc)
                            now=max(now,old.astimezone(timezone.utc))
                        except (ValueError, TypeError):
                            pass
                    expiry=now+timedelta(days=int(o['days']))
                    if current_row:
                        current_id=current_row[0]
                        await d.execute("UPDATE subscriptions SET status='renewed',removed_at=CURRENT_TIMESTAMP WHERE user_id=? AND channel_id=? AND status='active' AND id<>?",(o['user_id'],channel_id,current_id))
                        await d.execute("UPDATE subscriptions SET tariff_id=?,expires_at=? WHERE id=? AND status='active'",(o['tariff_id'],expiry.isoformat(),current_id))
                    else:
                        await d.execute("UPDATE subscriptions SET status='renewed',removed_at=CURRENT_TIMESTAMP WHERE user_id=? AND channel_id=? AND status='active'",(o['user_id'],channel_id))
                        await d.execute("INSERT INTO subscriptions(user_id,tariff_id,channel_id,expires_at,status) VALUES(?,?,?,?, 'active')",(o['user_id'],o['tariff_id'],channel_id,expiry.isoformat()))
                    committed_invites.append((channel,invite_link,expiry))
                saved_links = json.dumps([
                    {'channel_id': int(ch['channel_id']), 'channel_title': str(ch['channel_title']),
                     'invite_link': url, 'expires_at': expiry.isoformat()}
                    for ch, url, expiry in committed_invites
                ], ensure_ascii=False)
                cur=await d.execute("UPDATE orders SET status='approved',reviewed_at=CURRENT_TIMESTAMP,reviewed_by=?,review_note=COALESCE(NULLIF(?,''),review_note),invite_links_json=? WHERE id=? AND status='processing'",(reviewer_id,note,saved_links,oid))
                if cur.rowcount != 1: raise RuntimeError('Заявка изменила статус во время подтверждения')
                await d.commit()
                committed=True
                invites=committed_invites
            return 'ok',{'o':o,'invites':invites}
        except Exception as e:
            log.exception('Approval failed for order %s',oid)
            # If issuing links or committing subscriptions failed, revoke any links
            # created for this attempt so a retry does not leave stale invitations.
            # Never revoke after the DB commit: those links belong to an approved order.
            if not committed and invites:
                await revoke_created_invites(bot, invites)
            if not committed: await DB.run("UPDATE orders SET status=? WHERE id=? AND status='processing'",(from_status,oid))
            return 'error',str(e)
    async def deliver_order_links(o:dict, invites)->bool:
        """Send the personal invite links to the buyer. Returns False if Telegram refused to deliver."""
        link_text='\n\n'.join(f"{ch['channel_title']}: до {fmt_date(exp.isoformat())}\n{url}" for ch,url,exp in invites)
        try:
            await bot.send_message(o['user_id'],f"✅ Оплата подтверждена!\nТариф: {o['tariff_name']}\n\nПерсональные ссылки (каждая действует 48 часов и одно использование):\n{link_text}")
            return True
        except Exception:
            log.exception('Subscription created but invite delivery failed for order=%s user=%s',o['id'],o['user_id'])
            return False
    @dp.callback_query(F.data.startswith('order:approve:'))
    async def approve(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        # Turnover limit blocks starting/choosing new tariff purchases. Do not
        # block review of a receipt already submitted: the user may have paid
        # before the limit was reached, and holding that application would trap
        # a legitimate payment. The order must still be pending and have proof.
        oid=int(c.data.rsplit(':',1)[1])
        result,data=await grant_order(oid,'pending',c.from_user.id)
        if result=='missing': await c.answer('Покупка уже обработана или тариф удалён',show_alert=True);return
        if result=='busy': await c.answer('Покупка уже обрабатывается или обработана',show_alert=True);return
        if result=='perm': await c.answer(data,show_alert=True);return
        if result=='error': await c.answer(f'Не удалось выдать доступ: {data}',show_alert=True);return
        o,invites=data['o'],data['invites']
        await c.message.answer(f"✅ Покупка подтверждена: {o['tariff_name']}, покупатель ID {o['user_id']}. Доступы оформлены для {len(invites)} канал(ов).")
        if await deliver_order_links(o,invites): await c.message.answer('Ссылки отправлены пользователю.')
        else: await c.message.answer('⚠️ Подписки созданы, но отправить ссылки не удалось. Можно повторить отправку кнопкой ниже.', reply_markup=ikb([[('📨 Повторить отправку ссылок',f'order:resend:{oid}')]]))
        await c.answer('Оплата подтверждена')
    @dp.callback_query(F.data.startswith('order:resend:'))
    async def resend_order_links(c:CallbackQuery):
        if not admin(c.from_user.id):
            await c.answer('Нет доступа',show_alert=True); return
        try:
            oid=int(c.data.rsplit(':',1)[1])
        except (ValueError, IndexError):
            await c.answer('Некорректный номер заявки',show_alert=True); return
        order=await DB.run("SELECT o.*,t.name AS tariff_name FROM orders o LEFT JOIN tariffs t ON t.id=o.tariff_id WHERE o.id=? AND o.status='approved'",(oid,),one=True)
        if not order:
            await c.answer('Заявка не найдена или ещё не подтверждена',show_alert=True); return
        try:
            try:
                saved=json.loads(order.get('invite_links_json') or '[]')
            except (TypeError, json.JSONDecodeError):
                saved=[]
            # Create fresh links when possible so retry still works after the
            # original 48-hour invite links have expired.
            # Track links created in this retry; revoke them if persistence fails.
            created_invites=[]
            links_saved=False
            channels=[]
            for item in saved:
                cid=int(item['channel_id'])
                active=await DB.run("SELECT expires_at FROM subscriptions WHERE user_id=? AND channel_id=? AND status='active' ORDER BY expires_at DESC LIMIT 1",(order['user_id'],cid),one=True)
                if not active:
                    continue
                member=await bot.get_chat_member(cid,bot_id)
                if member.status not in ('administrator','creator') or (member.status=='administrator' and not getattr(member,'can_invite_users',False)):
                    raise RuntimeError(f"Нет права приглашать участников в канал {cid}")
                fresh=await bot.create_chat_invite_link(chat_id=cid,member_limit=1,expire_date=int((datetime.now(timezone.utc)+timedelta(days=2)).timestamp()),name=f"sub-{order['user_id']}-{oid}-retry")
                created_invites.append(({'channel_id': cid}, fresh.invite_link, active['expires_at']))
                channels.append({'channel_id':cid,'channel_title':str(item.get('channel_title') or cid),'invite_link':fresh.invite_link,'expires_at':active['expires_at']})
            if not channels:
                await c.answer('Нет активных подписок для повторной отправки ссылок',show_alert=True); return
            changed=await DB.run("UPDATE orders SET invite_links_json=? WHERE id=? AND status='approved'",(json.dumps(channels,ensure_ascii=False),oid))
            if changed != 1:
                raise RuntimeError('Не удалось сохранить новые ссылки: заявка уже изменила статус')
            links_saved=True
            body='\n\n'.join(f"{item['channel_title']}: доступ до {fmt_date(item['expires_at'])}\n{item['invite_link']}" for item in channels)
            await bot.send_message(order['user_id'],f"✅ Повторная отправка ссылок по заявке №{oid}\nТариф: {order.get('tariff_name') or 'тариф'}\n\nКаждая ссылка действует 48 часов и рассчитана на одно использование:\n{body}")
            await c.message.answer(f'Ссылки по заявке №{oid} отправлены повторно.')
            await c.answer('Отправлено')
        except Exception as e:
            log.exception('Invite resend failed for order=%s',oid)
            if not links_saved and created_invites:
                await revoke_created_invites(bot, created_invites)
            await c.answer(f'Не удалось отправить ссылки: {e}',show_alert=True)

    @dp.callback_query(F.data.startswith('order:reject:'))
    async def reject(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        oid=int(c.data.rsplit(':',1)[1]); o=await DB.run("SELECT user_id FROM orders WHERE id=? AND status='pending'",(oid,),one=True)
        if not o: await c.answer('Покупка уже обработана',show_alert=True);return
        changed=await DB.run("UPDATE orders SET status='rejected',reviewed_at=CURRENT_TIMESTAMP,reviewed_by=? WHERE id=? AND status='pending'",(c.from_user.id,oid))
        if changed != 1: await c.answer('Покупка уже обрабатывается или обработана',show_alert=True); return
        await bot.send_message(o['user_id'],'❌ Оплата вашей покупки не подтверждена. Если это ошибка, выберите тариф и отправьте чек повторно или свяжитесь с администратором.')
        await c.message.answer(f"❌ Покупка отклонена, покупатель ID {o['user_id']}."); await c.answer('Отклонено')
    @dp.callback_query(F.data.startswith('sub:revoke:'))
    async def revoke(c:CallbackQuery):
        if not admin(c.from_user.id): await c.answer('Нет доступа',show_alert=True);return
        sid=int(c.data.rsplit(':',1)[1]); s=await DB.run("SELECT * FROM subscriptions WHERE id=? AND status='active'",(sid,),one=True)
        if not s: await c.answer('Подписка не найдена',show_alert=True);return
        # Mark this subscription first, then remove the member only when no other
        # active subscription grants access to the same channel.
        changed=await DB.run("UPDATE subscriptions SET status='revoked',removed_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",(sid,))
        if changed != 1:
            await c.answer('Подписка уже обработана',show_alert=True); return
        remaining=await DB.run("SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=? AND channel_id=? AND status='active'",(s['user_id'],s['channel_id']),one=True)
        if not remaining or int(remaining['n']) == 0:
            try:
                await bot.ban_chat_member(s['channel_id'],s['user_id'])
                await bot.unban_chat_member(s['channel_id'],s['user_id'],only_if_banned=True)
            except Exception as e:
                await DB.run("UPDATE subscriptions SET status='active',removed_at=NULL WHERE id=? AND status='revoked'",(sid,))
                await c.answer(f'Не удалось удалить пользователя; статус подписки восстановлен: {e}',show_alert=True); return
        await c.message.answer(f"Доступ пользователя {s['user_id']} по подписке №{sid} отозван.")
        await c.answer('Готово')
    async def notify_license_expiry():
        # Notify each configured admin once at 7 days, 1 day, and on expiry.
        if await DB.setting('license_turnover_capped','0') == '1':
            return  # the free admin allowance has no fixed expiry date
        raw=await DB.setting('license_expires_at','')
        if not raw or await DB.setting('license_active','0') != '1':
            return
        try:
            expiry=datetime.fromisoformat(raw)
            if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
            expiry=expiry.astimezone(timezone.utc)
        except (TypeError,ValueError):
            return
        now=datetime.now(timezone.utc)
        remaining=expiry-now
        days=remaining.total_seconds()/86400
        kind='expired' if days <= 0 else ('1day' if days <= 1 else ('7days' if days <= 7 else ''))
        if not kind: return
        key=f'license_notice_{kind}_{expiry.date().isoformat()}'
        if await DB.setting(key,'') == 'sent': return
        if kind=='expired': msg=f'⛔ Лицензия бота истекла {fmt_date(expiry.isoformat())}. Необходимо продлить лицензию.'
        elif kind=='1day': msg=f'⚠️ Лицензия бота заканчивается менее чем через 24 часа ({fmt_date(expiry.isoformat())}). Продлите её заранее.'
        else: msg=f'⏳ Лицензия бота заканчивается в течение 7 дней ({fmt_date(expiry.isoformat())}). Не забудьте продлить её.'
        for aid in sorted(ADMIN_IDS | OWNER_IDS):
            try: await bot.send_message(aid,msg)
            except Exception: log.warning('Could not notify admin %s about license expiry',aid)
        await DB.set_setting(key,'sent')

    async def expiry_worker():
        while True:
            try:
                await notify_license_expiry()
                now=datetime.now(timezone.utc).isoformat()
                rows=await DB.run("SELECT * FROM subscriptions WHERE status='active' AND expires_at<=?",(now,),all=True)
                for s in rows:
                    try:
                        changed=await DB.run("UPDATE subscriptions SET status='expired',removed_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",(s['id'],))
                        if changed != 1:
                            continue
                        # A user may have overlapping subscriptions to the same channel.
                        # Expiring one tariff must not revoke access granted by another.
                        # Count only subscriptions that are still valid by time.
                        # The worker processes expired rows one by one, so another
                        # row may still say status='active' even though its deadline
                        # has already passed. Counting it would send a false message
                        # that channel access is preserved.
                        remaining=await DB.run("SELECT COUNT(*) AS n FROM subscriptions WHERE user_id=? AND channel_id=? AND status='active' AND expires_at>?",(s['user_id'],s['channel_id'],now),one=True)
                        if not remaining or int(remaining['n']) == 0:
                            try:
                                await bot.ban_chat_member(s['channel_id'],s['user_id'])
                                await bot.unban_chat_member(s['channel_id'],s['user_id'],only_if_banned=True)
                            except Exception:
                                await DB.run("UPDATE subscriptions SET status='active',removed_at=NULL WHERE id=? AND status='expired'",(s['id'],))
                                raise
                        # Avoid telling the user access may be closed when another
                        # active subscription still grants access to this same channel.
                        if remaining and int(remaining['n']) > 0:
                            notice = ('⏰ Срок действия одной из ваших подписок закончился. '
                                      'Доступ к каналу сохраняется благодаря другой активной подписке. '
                                      'Вы можете посмотреть остальные подписки в меню.')
                        else:
                            notice = ('⏰ Срок вашей подписки закончился, и других активных подписок '
                                      'на этот канал нет. Доступ закрыт. Вы можете выбрать тариф '
                                      'и продлить подписку.')
                        try: await bot.send_message(s['user_id'],notice,reply_markup=MENU)
                        except Exception: log.warning('Could not notify user %s about expired subscription %s',s['user_id'],s['id'])
                    except Exception as e:
                        log.exception('Expiry removal failed subscription=%s',s['id'])
                        await notify_admins(f"⚠️ Не удалось закрыть доступ пользователю {s['user_id']} в канале {s['channel_id']}: подписка закончилась, но он остаётся в канале. Проверьте, что у бота есть право блокировать участников. Бот будет повторять попытку ({type(e).__name__}).",key=('expiry-fail',s['channel_id']),cooldown=6*3600)
            except Exception: log.exception('Expiry worker failed')
            await asyncio.sleep(CHECK_INTERVAL)
    async def reminder_pass():
        if (await DB.setting('reminders_enabled','1'))!='1': return
        now=datetime.now(timezone.utc)
        rows=await DB.run("SELECT s.id,s.user_id,s.tariff_id,s.expires_at,t.name AS tariff_name,t.days AS tariff_days,t.active AS tariff_active FROM subscriptions s JOIN tariffs t ON t.id=s.tariff_id WHERE s.status='active' AND s.expires_at>? AND s.expires_at<=?",(now.isoformat(),(now+timedelta(days=3)).isoformat()),all=True)
        groups={}
        for r in rows: groups.setdefault((r['user_id'],r['tariff_id']),[]).append(r)
        for (uid,tid),subs in groups.items():
            try: first=min(datetime.fromisoformat(x['expires_at']) for x in subs)
            except (ValueError,TypeError): continue
            if first.tzinfo is None: first=first.replace(tzinfo=timezone.utc)
            left=first-now
            milestone,limit_days=('1d',1) if left<=timedelta(days=1) else ('3d',3)
            # Short tariffs would be reminded right after purchase: remind only if the tariff is longer than the threshold.
            if int(subs[0]['tariff_days'])<=limit_days: continue
            fresh=False
            for x in subs:
                seen=await DB.run('SELECT 1 AS n FROM subscription_reminders WHERE subscription_id=? AND expires_at=? AND milestone=?',(x['id'],x['expires_at'],milestone),one=True)
                if not seen: fresh=True
            if not fresh: continue
            when='меньше суток' if milestone=='1d' else f'{max(2,math.ceil(left.total_seconds()/86400))} дн.'
            text=(f"⏰ Подписка «{subs[0]['tariff_name']}» заканчивается {fmt_date(first.isoformat())} (осталось {when}).\n\n"
                  "Продлите заранее: новый срок прибавится к текущему, доступ не прервётся.")
            button=('🔄 Продлить',f'order:new:{tid}') if subs[0]['tariff_active'] else ('💳 Тарифы','order:back')
            mark=True
            try: await bot.send_message(uid,text,reply_markup=ikb([[button]]))
            except TelegramForbiddenError: pass  # the user blocked the bot: do not retry
            except Exception:
                log.warning('Could not send renewal reminder to %s',uid); mark=False
            if mark:
                for x in subs: await DB.run('INSERT OR IGNORE INTO subscription_reminders(subscription_id,expires_at,milestone) VALUES(?,?,?)',(x['id'],x['expires_at'],milestone))
            await asyncio.sleep(0.05)
        await DB.run("DELETE FROM subscription_reminders WHERE sent_at<datetime('now','-90 days')")
    async def reminder_worker():
        while True:
            try: await reminder_pass()
            except Exception: log.exception('Reminder worker failed')
            await asyncio.sleep(REMINDER_INTERVAL)
    expiry_task=asyncio.create_task(expiry_worker())
    promo_task=asyncio.create_task(promo_worker())
    broadcast_task=asyncio.create_task(broadcast_worker())
    reminder_task=asyncio.create_task(reminder_worker())
    crypto_task=asyncio.create_task(crypto_worker())
    log.info('Subscription bot started')
    await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
