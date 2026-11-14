from __future__ import annotations
import asyncio, logging, math, os, re, shutil, signal, sqlite3, subprocess, sys, time
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, MessageEntity
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv, dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent
# Load the configuration beside manager.py, even when launched from another working directory.
load_dotenv(PROJECT_DIR / '.env')
TOKEN = os.getenv('CONSTRUCTOR_BOT_TOKEN','').strip()
OWNER_IDS = {int(x.strip()) for x in os.getenv('OWNER_IDS','').split(',') if x.strip().isdigit()}
DB_PATH = Path(os.getenv('CONSTRUCTOR_DB', 'data/constructor.sqlite3')).expanduser()
INSTANCES = Path(os.getenv('INSTANCES_DIR', 'instances')).expanduser()
if not DB_PATH.is_absolute(): DB_PATH = PROJECT_DIR / DB_PATH
if not INSTANCES.is_absolute(): INSTANCES = PROJECT_DIR / INSTANCES
TEMPLATE_SCRIPT = PROJECT_DIR / 'template' / 'subscription_bot.py'
MAX_BOTS = max(1, int(os.getenv('MAX_BOTS_PER_USER','3')))
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'), format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
log = logging.getLogger('bot_constructor')

MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='➕ Создать бота'), KeyboardButton(text='🤖 Мои боты')], [KeyboardButton(text='ℹ️ Инструкция')]], resize_keyboard=True)
OWNER_MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='➕ Создать бота'), KeyboardButton(text='🤖 Мои боты')], [KeyboardButton(text='📊 Статистика'), KeyboardButton(text='💳 Оплата лицензий')]], resize_keyboard=True)
def menu_for(user_id: int): return OWNER_MENU if user_id in OWNER_IDS else MENU

async def answer_with_retry(message: Message, text: str, *, attempts: int = 3, **kwargs):
    """Retry a user-facing reply when Telegram's API connection briefly drops."""
    for attempt in range(1, attempts + 1):
        try:
            return await message.answer(text, **kwargs)
        except TelegramNetworkError as exc:
            if attempt >= attempts:
                log.warning(
                    'Could not send reply after %s attempts; chat_id=%s; error=%s',
                    attempts, message.chat.id, type(exc).__name__,
                )
                return None
            delay = min(2 ** (attempt - 1), 4)
            log.warning(
                'Telegram connection failed while sending reply; attempt=%s/%s; retry_in=%ss; error=%s',
                attempt, attempts, delay, type(exc).__name__,
            )
            await asyncio.sleep(delay)
class ConnectState(StatesGroup): token = State()
class LicenseMethodState(StatesGroup): name=State(); details=State()
class LicenseTariffState(StatesGroup): name=State(); turnover_limit=State(); price=State(); duration_days=State()
class ConfigState(StatesGroup): name=State(); description=State(); price=State(); days=State(); channel=State(); method_name=State(); method_details=State(); method_edit_name=State(); method_edit_details=State(); channel_wait=State(); broadcast_text=State()

def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(DB_PATH)
    con.row_factory=sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS bots(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, bot_id INTEGER NOT NULL UNIQUE, username TEXT NOT NULL, first_name TEXT NOT NULL, container_name TEXT NOT NULL UNIQUE, instance_dir TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, username TEXT, first_name TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS license_payment_methods(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, details TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS license_tariffs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        turnover_limit REAL NOT NULL, price REAL NOT NULL,
        duration_days INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS license_expiry_alerts(
        bot_id INTEGER NOT NULL, expiry TEXT NOT NULL, milestone TEXT NOT NULL,
        owner_id INTEGER NOT NULL, sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(bot_id, expiry, milestone, owner_id))''')
    con.execute('''CREATE TABLE IF NOT EXISTS constructor_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, message TEXT NOT NULL,
        bot_id INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS license_state_snapshots(
        bot_id INTEGER PRIMARY KEY, license_status TEXT NOT NULL, expiry TEXT NOT NULL,
        checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    con.execute('''CREATE TABLE IF NOT EXISTS bot_process_snapshots(
        bot_id INTEGER PRIMARY KEY, process_status TEXT NOT NULL,
        checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    if con.execute('SELECT COUNT(*) FROM license_tariffs').fetchone()[0] == 0:
        con.execute('INSERT INTO license_tariffs(name,turnover_limit,price,duration_days,active) VALUES(?,?,?,?,1)', ('Базовый', 5000, 990, 30))
    user_columns = {r[1] for r in con.execute('PRAGMA table_info(users)').fetchall()}
    if 'username' not in user_columns: con.execute('ALTER TABLE users ADD COLUMN username TEXT')
    if 'first_name' not in user_columns: con.execute('ALTER TABLE users ADD COLUMN first_name TEXT')
    con.commit(); return con

def safe_name(uid:int, bot_id:int): return f'clientbot_{uid}_{bot_id}'

def record_event(event_type: str, message: str, bot_id: int | None = None):
    """Store a compact owner-visible event without touching tenant databases."""
    try:
        with db() as con:
            con.execute('INSERT INTO constructor_events(event_type,message,bot_id) VALUES(?,?,?)',
                        (str(event_type)[:40], str(message)[:500], bot_id))
            con.execute('DELETE FROM constructor_events WHERE id NOT IN (SELECT id FROM constructor_events ORDER BY id DESC LIMIT 200)')
    except sqlite3.Error:
        log.exception('Could not record constructor event')

def pid_path(instance_dir: Path) -> Path:
    return instance_dir / 'bot.pid'

def read_pid(instance_dir: Path):
    try:
        return int(pid_path(instance_dir).read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None

_CHILDREN: dict = {}  # pid -> Popen of bots started by this constructor process

def _reaped(pid: int) -> bool:
    """Reap an exited child so it does not stay a zombie that still answers kill(pid, 0)."""
    proc = _CHILDREN.get(pid)
    if proc is not None and proc.poll() is not None:
        _CHILDREN.pop(pid, None)
        return True
    return False

def _is_our_bot(pid: int) -> bool:
    """Guard against PID reuse: the PID must belong to the personal-bot script (and not be a zombie)."""
    try:
        if os.name == 'nt':
            import psutil
            return TEMPLATE_SCRIPT.name in ' '.join(psutil.Process(pid).cmdline())
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists():
            if stat.read_text(errors='replace').rsplit(')', 1)[-1].split()[0] == 'Z':
                return False
            return TEMPLATE_SCRIPT.name.encode() in Path(f'/proc/{pid}/cmdline').read_bytes()
    except Exception:
        return True  # cannot tell (no /proc, no access): keep the plain liveness answer
    return True

def process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if _reaped(pid):
        return False
    try:
        if os.name == 'nt':
            import psutil
            if not psutil.pid_exists(pid):
                return False
        else:
            os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except ImportError:
        return False
    return _is_our_bot(pid)

def instance_status(instance_dir: Path) -> str:
    pid = read_pid(instance_dir)
    if process_alive(pid):
        return 'active'
    try:
        pid_path(instance_dir).unlink(missing_ok=True)
    except OSError:
        pass
    return 'stopped'

def stop_instance(instance_dir: Path, timeout: float = 10.0) -> bool:
    pid = read_pid(instance_dir)
    if not process_alive(pid):
        try: pid_path(instance_dir).unlink(missing_ok=True)
        except OSError: pass
        return True
    try:
        if os.name == 'nt':
            import psutil
            proc = psutil.Process(pid)
            proc.terminate()
            try: proc.wait(timeout=timeout)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        else:
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + timeout
            while time.time() < deadline and process_alive(pid):
                time.sleep(0.2)
            if process_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    except Exception as exc:
        log.warning('Could not stop process pid=%s: %s', pid, exc)
        return False
    try: pid_path(instance_dir).unlink(missing_ok=True)
    except OSError: pass
    return not process_alive(pid)

def start_instance(name: str, env_file: Path, data_dir: Path):
    instance_dir = env_file.parent
    if not TEMPLATE_SCRIPT.is_file():
        raise RuntimeError(f'Не найден файл персонального бота: {TEMPLATE_SCRIPT}')
    if instance_status(instance_dir) == 'active':
        stop_instance(instance_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key, value in dotenv_values(env_file).items():
        if value is not None:
            env[key] = str(value)
    env['DB_PATH'] = str((data_dir / 'subscriptions.sqlite3').resolve())
    env['PYTHONUNBUFFERED'] = '1'
    log_file = open(instance_dir / 'bot.log', 'a', encoding='utf-8')
    try:
        proc = subprocess.Popen(
            [sys.executable, str(TEMPLATE_SCRIPT)],
            cwd=str(instance_dir), env=env,
            stdin=subprocess.DEVNULL, stdout=log_file,
            stderr=subprocess.STDOUT, close_fds=True,
        )
    finally:
        log_file.close()
    _CHILDREN[proc.pid] = proc
    pid_path(instance_dir).write_text(str(proc.pid), encoding='utf-8')
    time.sleep(1.0)
    if proc.poll() is not None:
        _CHILDREN.pop(proc.pid, None)
        try: pid_path(instance_dir).unlink(missing_ok=True)
        except OSError: pass
        tail = ''
        try: tail = (instance_dir / 'bot.log').read_text(encoding='utf-8', errors='replace')[-1800:]
        except OSError: pass
        raise RuntimeError('Персональный бот завершился сразу после запуска. Последние строки bot.log: ' + (tail or 'лог пуст'))

def restart_instance(name: str, instance_dir: Path, env_file: Path, data_dir: Path):
    if not stop_instance(instance_dir):
        raise RuntimeError('Не удалось остановить процесс персонального бота.')
    start_instance(name, env_file, data_dir)

def bot_card_text(r) -> str:
    name = r['first_name'] or 'Бот'
    uname = f" (@{r['username']})" if r['username'] else ''
    return f"🤖 {name}{uname}\nID: {r['bot_id']}"

def bot_buttons(bot_id:int, status:str, username:str=''):
    rows=[[InlineKeyboardButton(text='🔄 Перезапустить',callback_data=f'b:restart:{bot_id}')],
          [InlineKeyboardButton(text='⏹ Остановить' if status=='active' else '▶️ Запустить',callback_data=f'b:toggle:{bot_id}')]]
    if username:
        rows.append([InlineKeyboardButton(text='⚙️ Открыть своего бота и настройки',url=f'https://t.me/{username.lstrip("@")}')])
    rows.append([InlineKeyboardButton(text='🗑 Удалить бота',callback_data=f'b:delete:{bot_id}')])
    rows.append([InlineKeyboardButton(text='⬅️ К списку ботов',callback_data='b:list:0')])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def main():
    if not TOKEN: raise RuntimeError(f'Не задан CONSTRUCTOR_BOT_TOKEN. Создай файл {PROJECT_DIR / ".env"} на основе .env.example и укажи токен первого бота-конструктора.')
    INSTANCES.mkdir(parents=True,exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not TEMPLATE_SCRIPT.is_file(): raise RuntimeError(f'Не найден шаблон персонального бота: {TEMPLATE_SCRIPT}')
    bot=Bot(TOKEN); dp=Dispatcher()

    @dp.message(CommandStart())
    async def start(m:Message):
        with db() as con: con.execute('INSERT INTO users(user_id,username,first_name) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name',(m.from_user.id,m.from_user.username or '',m.from_user.full_name or ''))
        await answer_with_retry(m, 'Привет! Я бот-конструктор. Отправь токен от @BotFather, и я запущу твоего персонального бота для выдачи подписок. После подключения открой именно своего бота и настрой тарифы, каналы и способы оплаты в его административном меню.\n\n⚠️ Отправляй токен только если ты владелец бота. После проверки сообщение будет удалено, если Telegram позволит.', reply_markup=menu_for(m.from_user.id))

    @dp.message(F.text=='ℹ️ Инструкция')
    async def instructions(m:Message):
        await m.answer('1. Создай своего бота через @BotFather.\n2. Нажми «➕ Создать бота» и отправь его токен.\n3. Открой подключённого бота кнопкой «Открыть своего бота и настройки».\n4. Нажми /start в своём боте: владелец увидит административное меню.\n5. Настрой тарифы, каналы, способы оплаты и рассылки в персональном боте.\n\nДля выдачи приглашений и удаления подписчиков добавь своего бота администратором в соответствующие каналы с необходимыми правами. Не отправляй токен посторонним.')

    @dp.message(F.text=='➕ Создать бота')
    async def create(m:Message,state:FSMContext):
        with db() as con: count=con.execute('SELECT COUNT(*) FROM bots WHERE owner_id=?',(m.from_user.id,)).fetchone()[0]
        if count>=MAX_BOTS:
            await m.answer(f'Достигнут лимит: {MAX_BOTS} бота(ов) на пользователя.'); return
        await state.set_state(ConnectState.token)
        await m.answer('Пришли токен бота от @BotFather одним сообщением. Не отправляй токен, если бот принадлежит другому человеку. Для отмены введи /cancel.')

    @dp.message(Command('cancel'))
    async def cancel(m:Message,state:FSMContext):
        await state.set_state(None); await m.answer('Действие отменено.',reply_markup=menu_for(m.from_user.id))

    connect_lock=asyncio.Lock()

    @dp.message(ConnectState.token, F.text)
    async def accept_token(m:Message,state:FSMContext):
        # Updates are handled concurrently. Provisioning must be serialized, otherwise a
        # double-sent token (or two users sending the same token) starts two processes and
        # the failing attempt deletes the folder of the successful one.
        if connect_lock.locked():
            try: await m.delete()
            except Exception: pass
        async with connect_lock:
            await accept_token_locked(m,state)

    async def accept_token_locked(m:Message,state:FSMContext):
        token=m.text.strip()
        # Best effort removal to avoid leaving the credential in the chat.
        try: await m.delete()
        except Exception: pass
        if len(token)>200 or not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,}',token):
            await state.clear(); await m.answer('Формат токена не похож на токен BotFather. Создай новый токен при необходимости и попробуй ещё раз.',reply_markup=menu_for(m.from_user.id)); return
        test_bot=Bot(token)
        try:
            me=await test_bot.get_me()
            if not me.is_bot: raise ValueError('Token does not belong to a bot')
        except Exception:
            await state.clear()
            await m.answer('Не удалось проверить токен. Убедись, что он действителен. Если ты уже отправлял его кому-то ещё, отзови его через @BotFather.',reply_markup=menu_for(m.from_user.id))
            try: await test_bot.session.close()
            except Exception: pass
            return
        finally:
            try: await test_bot.session.close()
            except Exception: pass
        if me.id== (await bot.get_me()).id:
            await state.clear(); await m.answer('Нельзя подключить самого бота-конструктора.',reply_markup=menu_for(m.from_user.id)); return
        with db() as con:
            existing=con.execute('SELECT owner_id FROM bots WHERE bot_id=?',(me.id,)).fetchone()
            count=con.execute('SELECT COUNT(*) FROM bots WHERE owner_id=?',(m.from_user.id,)).fetchone()[0]
        if existing:
            await state.clear(); await m.answer('Этот бот уже подключён в конструкторе.'); return
        if count>=MAX_BOTS:
            await state.clear(); await m.answer(f'Достигнут лимит: {MAX_BOTS} бота(ов) на пользователя.'); return
        name=safe_name(m.from_user.id,me.id)
        inst=INSTANCES/name; data=inst/'data'; data.mkdir(parents=True,exist_ok=True)
        env_file=inst/'.env'
        # Owner/admin is the user who connected this bot; never reuse platform admin IDs.
        env_text=f'BOT_TOKEN={token}\nADMIN_IDS={m.from_user.id}\nOWNER_IDS={m.from_user.id}\nCONSTRUCTOR_OWNER_IDS={",".join(str(x) for x in sorted(OWNER_IDS))}\nDB_PATH=/app/data/subscriptions.sqlite3\nSUBSCRIPTION_CHECK_INTERVAL=60\nLOG_LEVEL=INFO\n'
        fd=os.open(env_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as env_handle: env_handle.write(env_text)
        os.chmod(env_file,0o600)  # also fixes the mode of a pre-existing file
        try:
            await asyncio.to_thread(start_instance,name,env_file,data)
            with db() as con:
                con.execute('INSERT INTO bots(owner_id,bot_id,username,first_name,container_name,instance_dir,status) VALUES(?,?,?,?,?,?,?)',(m.from_user.id,me.id,me.username or '',me.first_name,name,str(inst),'active'))
                created_row = con.execute('SELECT * FROM bots WHERE bot_id=?',(me.id,)).fetchone()
            record_event('connection', f"Подключён бот {me.first_name} (@{me.username or 'нет username'}) владельцем {m.from_user.id}", me.id)
            await asyncio.to_thread(sync_license_methods_to_bot,created_row)
            await state.clear()
            await m.answer(f'✅ Бот подключён и запущен!\n\nИмя: {me.first_name}\nUsername: @{me.username or "не задан"}\n\nТеперь открой своего бота кнопкой ниже и отправь ему /start. Все настройки тарифов, каналов, способов оплаты и подписок находятся в административном меню персонального бота.',reply_markup=menu_for(m.from_user.id))
            await show_one(m,me.id)
        except Exception as e:
            log.exception('Failed to provision bot id=%s',me.id)
            try:
                await asyncio.to_thread(stop_instance,inst)
                shutil.rmtree(inst,ignore_errors=True)
            except Exception: pass
            await state.clear(); await m.answer('Не удалось запустить персонального бота. Проверь логи конструктора; токен не будет показан. Попробуй позже.',reply_markup=menu_for(m.from_user.id))

    async def show_one(m:Message, bot_id:int):
        with db() as con: r=con.execute('SELECT * FROM bots WHERE bot_id=? AND owner_id=?',(bot_id,m.from_user.id)).fetchone()
        if not r: return
        await m.answer(bot_card_text(r),reply_markup=bot_buttons(r['bot_id'],r['status'],r['username']))

    def my_bots_markup(user_id:int):
        with db() as con: rows=con.execute('SELECT * FROM bots WHERE owner_id=? ORDER BY id DESC',(user_id,)).fetchall()
        if not rows: return None
        kb=[[InlineKeyboardButton(text=f'🤖 {r["first_name"] or "Бот"} (@{r["username"]})'[:60] if r['username'] else f'🤖 {r["first_name"] or "Бот"}'[:60],callback_data=f'b:open:{r["bot_id"]}')] for r in rows]
        return InlineKeyboardMarkup(inline_keyboard=kb)

    @dp.message(F.text=='🤖 Мои боты')
    async def my_bots(m:Message):
        markup=my_bots_markup(m.from_user.id)
        if markup is None: await m.answer('У тебя пока нет подключённых ботов. Нажми «➕ Создать бота».'); return
        await m.answer('🤖 Твои боты. Выбери бота, чтобы открыть карточку управления:',reply_markup=markup)

    @dp.callback_query(F.data.regexp(r'^b:(status|restart|toggle|delete|confirmdel|cancel|open|list):'))
    async def bot_action(c:CallbackQuery):
        try: action,raw=c.data.split(':',2)[1:]; bot_id=int(raw)
        except Exception: await c.answer('Некорректная команда',show_alert=True); return
        if action=='list':
            markup=my_bots_markup(c.from_user.id)
            if markup is None: await c.answer('Ботов нет',show_alert=True); return
            try: await c.message.edit_text('🤖 Твои боты. Выбери бота, чтобы открыть карточку управления:',reply_markup=markup)
            except Exception: await c.message.answer('🤖 Твои боты. Выбери бота, чтобы открыть карточку управления:',reply_markup=markup)
            await c.answer(); return
        if action=='cancel': await c.answer('Отменено'); return
        with db() as con:
            row=con.execute('SELECT * FROM bots WHERE bot_id=? AND owner_id=?',(bot_id,c.from_user.id)).fetchone()
            if row is None and c.from_user.id in OWNER_IDS:
                row=con.execute('SELECT * FROM bots WHERE bot_id=?',(bot_id,)).fetchone()
        if not row: await c.answer('Нет доступа к этому боту',show_alert=True); return
        name=row['container_name']; inst=Path(row['instance_dir'])
        if action=='open':
            status=instance_status(inst)
            with db() as con: con.execute('UPDATE bots SET status=? WHERE bot_id=?',(status,bot_id))
            text=bot_card_text(row)
            markup=bot_buttons(bot_id,status,row['username'])
            try: await c.message.edit_text(text,reply_markup=markup)
            except Exception: await c.message.answer(text,reply_markup=markup)
            await c.answer(); return
        if action=='status':
            status=instance_status(inst)
            with db() as con: con.execute('UPDATE bots SET status=? WHERE bot_id=?',(status,bot_id))
            log_tail = ''
            try:
                log_tail = (inst / 'bot.log').read_text(encoding='utf-8', errors='replace')[-1200:]
                log_tail = re.sub(r'\b\d{5,15}:[A-Za-z0-9_-]{20,}\b', '[СКРЫТЫЙ_ТОКЕН]', log_tail)
            except OSError:
                log_tail = 'Файл bot.log пока отсутствует.'
            await c.message.answer(f'📋 Статус процесса: {status}\nPID: {read_pid(inst) or "нет"}\n\nПоследние строки журнала (до 1200 символов):\n' + (log_tail or 'Журнал пуст.'))
        elif action=='restart':
            try: await asyncio.to_thread(restart_instance,name,inst,inst/'.env',inst/'data')
            except Exception as exc:
                record_event('error', f"Ошибка перезапуска бота {row['first_name']}: {type(exc).__name__}", bot_id)
                log.exception('Could not restart bot_id=%s', bot_id)
                await c.answer(f'Не удалось перезапустить: {str(exc)[:180]}',show_alert=True); return
            with db() as con: con.execute('UPDATE bots SET status="active" WHERE bot_id=?',(bot_id,))
            record_event('restart', f"Бот {row['first_name']} перезапущен пользователем {c.from_user.id}", bot_id)
            await c.answer('Бот перезапущен')
        elif action=='toggle':
            running=instance_status(inst)=='active'
            if running:
                ok=await asyncio.to_thread(stop_instance,inst)
                status='stopped'
            else:
                try: await asyncio.to_thread(start_instance,name,inst/'.env',inst/'data'); ok=True
                except Exception as exc: await c.answer(f'Не удалось запустить: {str(exc)[:180]}',show_alert=True); return
                status='active'
            if not ok:
                record_event('error', f"Не удалось изменить статус бота {row['first_name']}", bot_id)
                await c.answer('Не удалось изменить статус',show_alert=True); return
            with db() as con: con.execute('UPDATE bots SET status=? WHERE bot_id=?',(status,bot_id))
            record_event('status', f"Бот {row['first_name']}: статус {status} (пользователь {c.from_user.id})", bot_id)
            await c.answer('Остановлен' if running else 'Запущен')
            try: await c.message.edit_reply_markup(reply_markup=bot_buttons(bot_id,status,row['username']))
            except Exception: pass
        elif action=='delete':
            kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Да, удалить',callback_data=f'b:confirmdel:{bot_id}'),InlineKeyboardButton(text='Отмена',callback_data='b:cancel:0')]])
            await c.message.answer('Удалить персонального бота? Его процесс, токен и база подписчиков будут удалены без возможности восстановления. Перед удалением сохрани резервную копию, если она нужна.',reply_markup=kb); await c.answer()
        elif action=='confirmdel':
            if not await asyncio.to_thread(stop_instance,inst):
                await c.answer('Не удалось остановить процесс персонального бота',show_alert=True); return
            # Only delete a path inside the configured instances root.
            resolved=inst.resolve(); root=INSTANCES.resolve()
            if resolved.parent==root and resolved.name.startswith('clientbot_'):
                shutil.rmtree(resolved,ignore_errors=True)
            with db() as con: con.execute('DELETE FROM bots WHERE bot_id=?',(bot_id,))
            await c.message.answer('Бот и его локальные данные удалены.'); await c.answer('Удалено')
        else: await c.answer('Отменено')

    async def selected(m:Message,state:FSMContext):
        data=await state.get_data()
        bid=data.get('selected_bot_id')
        if not bid:
            await m.answer('Открой «🤖 Мои боты» и перейди в свой персональный бот — настройки находятся там.')
            return None
        with db() as con: row=con.execute('SELECT * FROM bots WHERE bot_id=? AND owner_id=?',(bid,m.from_user.id)).fetchone()
        if not row: await m.answer('Бот не найден или нет доступа.'); return None
        return row

    async def selected_cb(c:CallbackQuery,state:FSMContext):
        data=await state.get_data(); bid=data.get('selected_bot_id')
        with db() as con: row=con.execute('SELECT * FROM bots WHERE bot_id=? AND owner_id=?',(bid,c.from_user.id)).fetchone() if bid else None
        if not row: await c.answer('Сначала выбери бота через «Мои боты».',show_alert=True); return None
        return row

    def tenant_db(row): return Path(row['instance_dir'])/'data'/'subscriptions.sqlite3'

    def sync_license_methods_to_bot(row):
        # Sync constructor-owned license payment methods and tariffs into a tenant database.
        instance_dir = Path(row['instance_dir'])
        env_path = instance_dir / '.env'
        owner_line = 'CONSTRUCTOR_OWNER_IDS=' + ','.join(str(x) for x in sorted(OWNER_IDS))
        env_changed = False
        if env_path.exists():
            env_lines = env_path.read_text(encoding='utf-8').splitlines()
            found = False
            updated = []
            for line in env_lines:
                if line.startswith('CONSTRUCTOR_OWNER_IDS='):
                    updated.append(owner_line); found = True; env_changed = (line != owner_line)
                else:
                    updated.append(line)
            if not found:
                updated.append(owner_line); env_changed = True
            if env_changed:
                env_path.write_text('\n'.join(updated) + '\n', encoding='utf-8')
                try: os.chmod(env_path, 0o600)
                except OSError: pass
                if instance_status(instance_dir) == 'active':
                    try:
                        restart_instance(row['container_name'], instance_dir, env_path, instance_dir / 'data')
                        log.info('Restarted bot_id=%s to apply constructor-owner permissions', row['bot_id'])
                    except Exception:
                        log.exception('Could not restart bot_id=%s after updating owner permissions', row['bot_id'])
        path = tenant_db(row)
        for _ in range(12):
            if path.exists(): break
            time.sleep(0.25)
        if not path.exists():
            log.warning('Tenant database not ready for license sync: bot_id=%s', row['bot_id'])
            return
        with db() as con:
            methods = con.execute('SELECT id,name,details,active,created_at FROM license_payment_methods ORDER BY id').fetchall()
            tariffs = con.execute('SELECT id,name,turnover_limit,price,duration_days,active,created_at FROM license_tariffs ORDER BY id').fetchall()
        with sqlite3.connect(path, timeout=10) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS license_payment_methods(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, details TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
            con.execute('''CREATE TABLE IF NOT EXISTS license_tariffs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, turnover_limit REAL NOT NULL,
                price REAL NOT NULL, duration_days INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, admin_free INTEGER NOT NULL DEFAULT 0)''')
            columns = {r[1] for r in con.execute('PRAGMA table_info(license_tariffs)').fetchall()}
            if 'constructor_tariff_id' not in columns:
                con.execute('ALTER TABLE license_tariffs ADD COLUMN constructor_tariff_id INTEGER')
            # Upsert constructor-owned methods instead of deleting/recreating them.
            # Existing license_payments keep their payment_method_id, including when
            # a method is removed centrally while a receipt is still awaiting review.
            con.execute('PRAGMA busy_timeout=5000')
            for method in methods:
                con.execute('''INSERT INTO license_payment_methods(id,name,details,active,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, details=excluded.details,
                        active=excluded.active, created_at=excluded.created_at''',
                    (method['id'], method['name'], method['details'], method['active'], method['created_at']))
            current_method_ids = {int(x['id']) for x in methods}
            if current_method_ids:
                method_marks = ','.join('?' for _ in current_method_ids)
                con.execute(
                    f'UPDATE license_payment_methods SET active=0 WHERE id NOT IN ({method_marks})',
                    tuple(current_method_ids),
                )
            else:
                con.execute('UPDATE license_payment_methods SET active=0')
            current_ids = {int(x['id']) for x in tariffs}
            for tariff in tariffs:
                existing = con.execute('SELECT id FROM license_tariffs WHERE constructor_tariff_id=?', (tariff['id'],)).fetchone()
                values = (tariff['name'], tariff['turnover_limit'], tariff['price'], tariff['duration_days'], tariff['active'], tariff['id'])
                if existing:
                    con.execute('UPDATE license_tariffs SET name=?,turnover_limit=?,price=?,duration_days=?,active=?,constructor_tariff_id=? WHERE id=?', values + (existing[0],))
                else:
                    con.execute('INSERT INTO license_tariffs(name,turnover_limit,price,duration_days,active,admin_free,constructor_tariff_id) VALUES(?,?,?,?,?,0,?)', values)
            # Removed central tariffs remain in history but cannot be purchased again.
            if current_ids:
                marks=','.join('?' for _ in current_ids)
                con.execute(f'UPDATE license_tariffs SET active=0 WHERE constructor_tariff_id IS NOT NULL AND constructor_tariff_id NOT IN ({marks})', tuple(current_ids))
            else:
                con.execute('UPDATE license_tariffs SET active=0 WHERE constructor_tariff_id IS NOT NULL')
            con.commit()

    def sync_license_methods_all():
        with db() as con: rows = con.execute('SELECT * FROM bots').fetchall()
        for row in rows:
            try: sync_license_methods_to_bot(row)
            except (sqlite3.Error, OSError): log.exception('Failed to sync license settings to bot_id=%s', row['bot_id'])

    async def tenant_ready(path:Path):
        path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path) as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS tariffs(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',price TEXT NOT NULL,days INTEGER NOT NULL,channel_id INTEGER NOT NULL,channel_title TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER NOT NULL UNIQUE,title TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,details TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS tariff_payment_methods(tariff_id INTEGER NOT NULL,payment_method_id INTEGER NOT NULL,PRIMARY KEY(tariff_id,payment_method_id));
            CREATE TABLE IF NOT EXISTS tariff_extra_channels(tariff_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,channel_title TEXT NOT NULL DEFAULT '',PRIMARY KEY(tariff_id,channel_id));
            """)

    @dp.callback_query(F.data.startswith('b:manage:'))
    async def manage_bot_legacy(c:CallbackQuery):
        bid=int(c.data.rsplit(':',1)[1])
        with db() as con: row=con.execute('SELECT * FROM bots WHERE bot_id=? AND owner_id=?',(bid,c.from_user.id)).fetchone()
        if not row: await c.answer('Нет доступа',show_alert=True); return
        if row['username']:
            await c.message.answer('Настраивай подписки непосредственно в своём боте:',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⚙️ Открыть своего бота',url=f'https://t.me/{row["username"].lstrip("@")}')]]))
            await c.answer()
        else:
            await c.answer('У этого бота пока нет username',show_alert=True)

    @dp.message(F.text=='⬅️ К списку ботов')
    async def unselect(m:Message,state:FSMContext):
        await state.update_data(selected_bot_id=None); await m.answer('Выбор бота сброшен. Открой «🤖 Мои боты».',reply_markup=menu_for(m.from_user.id))

    @dp.message(F.text=='💳 Тарифы')
    async def tariff_menu(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        path=tenant_db(row); await tenant_ready(path)
        with sqlite3.connect(path) as con: ts=con.execute('SELECT id,name,price,days,active FROM tariffs ORDER BY id DESC').fetchall()
        kb=InlineKeyboardBuilder(); kb.button(text='➕ Добавить тариф',callback_data='cfg:addtariff')
        for t in ts: kb.button(text=f'{"🟢" if t[4] else "⚪"} {t[1]} · {t[2]} ₽ · {t[3]} дн.',callback_data=f'cfg:tariff:{t[0]}')
        kb.adjust(1); await m.answer('Тарифы этого бота:',reply_markup=kb.as_markup())

    @dp.callback_query(F.data=='cfg:addtariff')
    async def tariff_begin(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        with sqlite3.connect(tenant_db(row)) as con: channels=con.execute('SELECT channel_id,title FROM channels WHERE active=1').fetchall()
        if not channels: await c.answer('Сначала добавь канал в разделе «Каналы».',show_alert=True); return
        await state.set_state(ConfigState.name); await c.message.answer('Название тарифа:'); await c.answer()

    @dp.callback_query(F.data.startswith('cfg:savechannel:'))
    async def tariff_save(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        cid=int(c.data.rsplit(':',1)[1]); d=await state.get_data(); path=tenant_db(row)
        with sqlite3.connect(path) as con:
            ch=con.execute('SELECT title FROM channels WHERE channel_id=? AND active=1',(cid,)).fetchone()
            if not ch: await c.answer('Канал не найден',show_alert=True); return
            tid=con.execute('INSERT INTO tariffs(name,description,price,days,channel_id,channel_title,active) VALUES(?,?,?,?,?,?,1)',(d['t_name'],d['t_desc'],d['t_price'],d['t_days'],cid,ch[0])).lastrowid
            methods=con.execute('SELECT id,name FROM payment_methods WHERE active=1').fetchall()
        await state.set_state(None)
        if methods:
            kb=InlineKeyboardBuilder()
            for mid,name in methods: kb.button(text=f'Привязать: {name}',callback_data=f'cfg:link:{tid}:{mid}')
            kb.adjust(1); await c.message.answer(f'Тариф создан (ID {tid}). Привяжи способы оплаты:',reply_markup=kb.as_markup())
        else: await c.message.answer(f'Тариф создан (ID {tid}). Теперь добавь способ оплаты и привяжи его к тарифу.')
        await c.answer('Создано')

    @dp.callback_query(F.data.startswith('cfg:link:'))
    async def tariff_link(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        _,_,tid,mid=c.data.split(':')
        with sqlite3.connect(tenant_db(row)) as con: con.execute('INSERT OR IGNORE INTO tariff_payment_methods(tariff_id,payment_method_id) VALUES(?,?)',(int(tid),int(mid)))
        await c.answer('Способ оплаты привязан')

    @dp.callback_query(F.data.startswith('cfg:tariff:'))
    async def tariff_card(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        tid=int(c.data.rsplit(':',1)[1]); path=tenant_db(row)
        with sqlite3.connect(path) as con: t=con.execute('SELECT id,name,price,days,active FROM tariffs WHERE id=?',(tid,)).fetchone()
        if not t: await c.answer('Не найден',show_alert=True); return
        with sqlite3.connect(path) as con:
            extras=con.execute('SELECT channel_id,channel_title FROM tariff_extra_channels WHERE tariff_id=? ORDER BY channel_title',(tid,)).fetchall()
            channels=con.execute('SELECT channel_id,title FROM channels WHERE active=1 ORDER BY title').fetchall()
        rows=[[InlineKeyboardButton(text='✏️ Название',callback_data=f'cfg:tedit:{tid}:name'),InlineKeyboardButton(text='💰 Цена',callback_data=f'cfg:tedit:{tid}:price')],
              [InlineKeyboardButton(text='📅 Срок',callback_data=f'cfg:tedit:{tid}:days')],
              [InlineKeyboardButton(text='➕ Доп. каналы',callback_data=f'cfg:extras:{tid}')],
              [InlineKeyboardButton(text='🔛 Вкл/выкл',callback_data=f'cfg:ttoggle:{tid}')],
              [InlineKeyboardButton(text='🗑 Удалить',callback_data=f'cfg:tdel:{tid}')]]
        extra_text='\nДополнительные каналы: '+(', '.join(x[1] for x in extras) if extras else 'нет')
        kb=InlineKeyboardMarkup(inline_keyboard=rows)
        await c.message.answer(f'{t[1]} · {t[2]} ₽ · {t[3]} дней · активен: {bool(t[4])}{extra_text}',reply_markup=kb); await c.answer()

    @dp.callback_query(F.data.startswith('cfg:extras:'))
    async def tariff_extras(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        tid=int(c.data.rsplit(':',1)[1]); path=tenant_db(row)
        with sqlite3.connect(path) as con:
            active=con.execute('SELECT channel_id,title FROM channels WHERE active=1 ORDER BY title').fetchall()
            extras={x[0] for x in con.execute('SELECT channel_id FROM tariff_extra_channels WHERE tariff_id=?',(tid,)).fetchall()}
        if not active:
            await c.answer('Сначала добавь каналы',show_alert=True); return
        kb=InlineKeyboardBuilder()
        for cid,title in active:
            kb.button(text=('✅ ' if cid in extras else '➕ ')+title,callback_data=f'cfg:extra:{tid}:{cid}')
        kb.button(text='↩️ К тарифам',callback_data=f'cfg:tariff:{tid}')
        kb.adjust(1)
        await c.message.answer('Нажимай на канал, чтобы добавить его к тарифу или убрать. Основной канал тарифа не меняется.',reply_markup=kb.as_markup())
        await c.answer()

    @dp.callback_query(F.data.startswith('cfg:extra:'))
    async def tariff_extra_toggle(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        _,_,raw_tid,raw_cid=c.data.split(':'); tid=int(raw_tid); cid=int(raw_cid); path=tenant_db(row)
        with sqlite3.connect(path) as con:
            t=con.execute('SELECT id,channel_id FROM tariffs WHERE id=?',(tid,)).fetchone()
            ch=con.execute('SELECT title FROM channels WHERE channel_id=? AND active=1',(cid,)).fetchone()
            if not t or not ch:
                await c.answer('Тариф или канал не найден',show_alert=True); return
            if cid==t[1]:
                await c.answer('Основной канал уже включён в тариф',show_alert=True); return
            exists=con.execute('SELECT 1 FROM tariff_extra_channels WHERE tariff_id=? AND channel_id=?',(tid,cid)).fetchone()
            if exists: con.execute('DELETE FROM tariff_extra_channels WHERE tariff_id=? AND channel_id=?',(tid,cid)); msg='Канал убран из тарифа'
            else: con.execute('INSERT INTO tariff_extra_channels(tariff_id,channel_id,channel_title) VALUES(?,?,?)',(tid,cid,ch[0])); msg='Канал добавлен к тарифу'
        await c.answer(msg)
        with sqlite3.connect(path) as con:
            active=con.execute('SELECT channel_id,title FROM channels WHERE active=1 ORDER BY title').fetchall()
            extras={x[0] for x in con.execute('SELECT channel_id FROM tariff_extra_channels WHERE tariff_id=?',(tid,)).fetchall()}
        kb=InlineKeyboardBuilder()
        for ch_id,title in active:
            kb.button(text=('✅ ' if ch_id in extras else '➕ ')+title,callback_data=f'cfg:extra:{tid}:{ch_id}')
        kb.button(text='↩️ К тарифу',callback_data=f'cfg:tariff:{tid}')
        kb.adjust(1)
        await c.message.answer('Дополнительные каналы тарифа (нажми, чтобы переключить):',reply_markup=kb.as_markup())
    @dp.callback_query(F.data.startswith('cfg:tedit:'))
    async def tariff_edit_begin(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        _,_,tid,field=c.data.split(':')
        await state.update_data(edit_tariff_id=int(tid),edit_tariff_field=field)
        await state.set_state(ConfigState.name if field=='name' else ConfigState.price if field=='price' else ConfigState.days)
        await c.message.answer({'name':'Введи новое название тарифа:','price':'Введи новую цену в рублях:','days':'Введи новый срок в днях:'}[field])
        await c.answer()

    @dp.message(ConfigState.name,F.text)
    async def tariff_name_or_edit(m:Message,state:FSMContext):
        data=await state.get_data()
        if data.get('edit_tariff_id'):
            row=await selected(m,state)
            if not row:return
            with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE tariffs SET name=? WHERE id=?',(m.text.strip()[:100],int(data['edit_tariff_id'])))
            await state.update_data(edit_tariff_id=None,edit_tariff_field=None); await state.set_state(None); await m.answer('Название тарифа изменено.'); return
        await state.update_data(t_name=m.text.strip()[:100]); await state.set_state(ConfigState.description); await m.answer('Описание тарифа (или точка, если без описания):')

    @dp.message(ConfigState.price,F.text)
    async def tariff_price_or_edit(m:Message,state:FSMContext):
        data=await state.get_data()
        if data.get('edit_tariff_id'):
            try: v=float(m.text.replace(',','.')); assert 0<=v<=10000000
            except Exception: await m.answer('Укажи корректную цену.'); return
            row=await selected(m,state)
            if not row:return
            with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE tariffs SET price=? WHERE id=?',(str(v),int(data['edit_tariff_id'])))
            await state.update_data(edit_tariff_id=None,edit_tariff_field=None); await state.set_state(None); await m.answer('Цена тарифа изменена.'); return
        try: v=float(m.text.replace(',','.')); assert 0<=v<=10000000
        except Exception: await m.answer('Укажи корректную цену.'); return
        await state.update_data(t_price=str(v)); await state.set_state(ConfigState.days); await m.answer('Срок в днях (1–3650):')

    @dp.message(ConfigState.days,F.text)
    async def tariff_days_or_edit(m:Message,state:FSMContext):
        data=await state.get_data()
        if data.get('edit_tariff_id'):
            try: v=int(m.text); assert 1<=v<=3650
            except Exception: await m.answer('Укажи число от 1 до 3650.'); return
            row=await selected(m,state)
            if not row:return
            with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE tariffs SET days=? WHERE id=?',(v,int(data['edit_tariff_id'])))
            await state.update_data(edit_tariff_id=None,edit_tariff_field=None); await state.set_state(None); await m.answer('Срок тарифа изменён.'); return
        try: v=int(m.text); assert 1<=v<=3650
        except Exception: await m.answer('Укажи число от 1 до 3650.'); return
        await state.update_data(t_days=v); row=await selected(m,state)
        if not row:return
        with sqlite3.connect(tenant_db(row)) as con: channels=con.execute('SELECT channel_id,title FROM channels WHERE active=1').fetchall()
        kb=InlineKeyboardBuilder()
        for cid,title in channels: kb.button(text=f'{title} ({cid})',callback_data=f'cfg:savechannel:{cid}')
        kb.adjust(1); await state.set_state(ConfigState.channel); await m.answer('Основной канал тарифа:',reply_markup=kb.as_markup())

    @dp.callback_query(F.data.startswith('cfg:ttoggle:'))
    async def tariff_toggle(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE tariffs SET active=1-active WHERE id=?',(int(c.data.rsplit(':',1)[1]),))
        await c.answer('Статус изменён')
    @dp.callback_query(F.data.startswith('cfg:tdel:'))
    async def tariff_delete(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        tid=int(c.data.rsplit(':',1)[1])
        with sqlite3.connect(tenant_db(row)) as con: con.execute('DELETE FROM tariff_payment_methods WHERE tariff_id=?',(tid,)); con.execute('DELETE FROM tariff_extra_channels WHERE tariff_id=?',(tid,)); con.execute('DELETE FROM tariffs WHERE id=?',(tid,))
        await c.answer('Тариф удалён')

    @dp.message(F.text=='📣 Каналы')
    async def channels_menu(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        path=tenant_db(row); await tenant_ready(path)
        with sqlite3.connect(path) as con: channels=con.execute('SELECT channel_id,title,active FROM channels ORDER BY id DESC').fetchall()
        kb=InlineKeyboardBuilder(); kb.button(text='➕ Добавить пересылкой',callback_data='cfg:addchannel')
        for cid,title,active in channels: kb.button(text=f'{"🟢" if active else "⚪"} {title}',callback_data=f'cfg:ctoggle:{cid}')
        kb.adjust(1); await m.answer('Добавь персонального бота администратором канала и перешли сюда сообщение из канала.',reply_markup=kb.as_markup())
    @dp.callback_query(F.data=='cfg:addchannel')
    async def channel_begin(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        await state.set_state(ConfigState.channel_wait); await c.message.answer('Перешли сообщение из канала, где персональный бот — администратор.'); await c.answer()
    @dp.message(ConfigState.channel_wait)
    async def channel_receive(m:Message,state:FSMContext):
        origin=getattr(m,'forward_origin',None)
        if not origin or getattr(origin,'type',None)!='channel': await m.answer('Нужна пересылка сообщения именно из канала.'); return
        row=await selected(m,state)
        if not row:return
        env=(Path(row['instance_dir'])/'.env').read_text(encoding='utf-8')
        token=next((line.split('=',1)[1] for line in env.splitlines() if line.startswith('BOT_TOKEN=')), '')
        child=Bot(token)
        try:
            me=await child.get_me(); member=await child.get_chat_member(origin.chat.id,me.id)
            if member.status not in ('administrator','creator'): await m.answer('Сначала добавь бота администратором канала.'); return
            with sqlite3.connect(tenant_db(row)) as con: con.execute('INSERT INTO channels(channel_id,title,active) VALUES(?,?,1) ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title,active=1',(origin.chat.id,origin.chat.title))
            await state.set_state(None); await m.answer(f'Канал «{origin.chat.title}» добавлен.')
        except Exception: await m.answer('Не удалось проверить канал. Проверь права бота, включая создание ссылок-приглашений.'); log.exception('channel verify failed')
        finally: await child.session.close()
    @dp.callback_query(F.data.startswith('cfg:ctoggle:'))
    async def channel_toggle(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE channels SET active=1-active WHERE channel_id=?',(int(c.data.rsplit(':',1)[1]),))
        await c.answer('Статус канала изменён')

    @dp.message(F.text=='💰 Способы оплаты')
    async def methods_menu(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        path=tenant_db(row); await tenant_ready(path)
        with sqlite3.connect(path) as con: methods=con.execute('SELECT id,name,details,active FROM payment_methods ORDER BY id DESC').fetchall()
        kb=InlineKeyboardBuilder(); kb.button(text='➕ Добавить способ оплаты',callback_data='cfg:addmethod')
        for mid,name,details,active in methods: kb.button(text=f'{"🟢" if active else "⚪"} {name}',callback_data=f'cfg:method:{mid}')
        kb.adjust(1); await m.answer('Способы оплаты. Нажми на способ, чтобы изменить название, инструкцию или статус:',reply_markup=kb.as_markup())
    @dp.callback_query(F.data=='cfg:addmethod')
    async def method_begin(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        await state.set_state(ConfigState.method_name); await c.message.answer('Название способа оплаты:'); await c.answer()
    @dp.message(ConfigState.method_name,F.text)
    async def method_name(m:Message,state:FSMContext): await state.update_data(method_name=m.text.strip()[:100]); await state.set_state(ConfigState.method_details); await m.answer('Инструкция для пользователя:')
    @dp.message(ConfigState.method_details,F.text)
    async def method_save(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        d=await state.get_data()
        with sqlite3.connect(tenant_db(row)) as con: con.execute('INSERT INTO payment_methods(name,details,active) VALUES(?,?,1)',(d['method_name'],m.text.strip()[:3000]))
        await state.set_state(None); await m.answer('Способ оплаты добавлен. Теперь открой тариф и привяжи его к способу оплаты.')
    @dp.callback_query(F.data.startswith('cfg:method:'))
    async def method_card(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        mid=int(c.data.rsplit(':',1)[1])
        with sqlite3.connect(tenant_db(row)) as con: method=con.execute('SELECT id,name,details,active FROM payment_methods WHERE id=?',(mid,)).fetchone()
        if not method: await c.answer('Способ оплаты не найден',show_alert=True); return
        kb=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✏️ Название',callback_data=f'cfg:medit:{mid}:name')],
            [InlineKeyboardButton(text='📝 Инструкция',callback_data=f'cfg:medit:{mid}:details')],
            [InlineKeyboardButton(text='🔛 Вкл/выкл',callback_data=f'cfg:mtoggle:{mid}')],
            [InlineKeyboardButton(text='🗑 Удалить',callback_data=f'cfg:mdel:{mid}')]])
        await c.message.answer(f'{method[1]}\nСтатус: {"активен" if method[3] else "выключен"}\n\n{method[2]}',reply_markup=kb)
        await c.answer()

    @dp.callback_query(F.data.startswith('cfg:medit:'))
    async def method_edit_begin(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        _,_,raw_mid,field=c.data.split(':'); mid=int(raw_mid)
        with sqlite3.connect(tenant_db(row)) as con: exists=con.execute('SELECT 1 FROM payment_methods WHERE id=?',(mid,)).fetchone()
        if not exists: await c.answer('Способ оплаты не найден',show_alert=True); return
        await state.update_data(edit_method_id=mid)
        if field=='name': await state.set_state(ConfigState.method_edit_name); await c.message.answer('Введи новое название способа оплаты:')
        else: await state.set_state(ConfigState.method_edit_details); await c.message.answer('Введи новую инструкцию по оплате:')
        await c.answer()

    @dp.message(ConfigState.method_edit_name,F.text)
    async def method_edit_name_save(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        d=await state.get_data(); value=m.text.strip()[:100]
        if not value: await m.answer('Название не может быть пустым.'); return
        with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE payment_methods SET name=? WHERE id=?',(value,int(d['edit_method_id'])))
        await state.update_data(edit_method_id=None); await state.set_state(None); await m.answer('Название способа оплаты изменено.')

    @dp.message(ConfigState.method_edit_details,F.text)
    async def method_edit_details_save(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        d=await state.get_data(); value=m.text.strip()[:3000]
        if not value: await m.answer('Инструкция не может быть пустой.'); return
        with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE payment_methods SET details=? WHERE id=?',(value,int(d['edit_method_id'])))
        await state.update_data(edit_method_id=None); await state.set_state(None); await m.answer('Инструкция по оплате изменена.')

    @dp.callback_query(F.data.startswith('cfg:mdel:'))
    async def method_delete(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        mid=int(c.data.rsplit(':',1)[1])
        with sqlite3.connect(tenant_db(row)) as con:
            con.execute('DELETE FROM tariff_payment_methods WHERE payment_method_id=?',(mid,))
            con.execute('DELETE FROM payment_methods WHERE id=?',(mid,))
        await c.answer('Способ оплаты удалён')

    @dp.callback_query(F.data.startswith('cfg:mtoggle:'))
    async def method_toggle(c:CallbackQuery,state:FSMContext):
        row=await selected_cb(c,state)
        if not row:return
        with sqlite3.connect(tenant_db(row)) as con: con.execute('UPDATE payment_methods SET active=1-active WHERE id=?',(int(c.data.rsplit(':',1)[1]),))
        await c.answer('Статус изменён')

    @dp.message(F.text=='📨 Рассылка')
    async def broadcast_begin(m:Message,state:FSMContext):
        row=await selected(m,state)
        if not row:return
        await state.set_state(ConfigState.broadcast_text)
        await m.answer('Отправь текст рассылки. Сначала я покажу предпросмотр и попрошу подтверждение. Для отмены введи /cancel.')

    @dp.message(ConfigState.broadcast_text)
    async def broadcast_preview(m:Message,state:FSMContext):
        if m.text and m.text.startswith('/'):
            await m.answer('Отправь текст, фото, видео или документ для рассылки. /cancel — отмена.'); return
        if not (m.text or m.photo or m.video or m.document or m.animation or m.audio or m.voice):
            await m.answer('Этот тип сообщения пока нельзя рассылать. Отправь текст, фото, видео, GIF, аудио, голосовое сообщение или документ.'); return
        media_type='text'; file_id=None
        if m.photo: media_type='photo'; file_id=m.photo[-1].file_id
        elif m.video: media_type='video'; file_id=m.video.file_id
        elif m.animation: media_type='animation'; file_id=m.animation.file_id
        elif m.document: media_type='document'; file_id=m.document.file_id
        elif m.audio: media_type='audio'; file_id=m.audio.file_id
        elif m.voice: media_type='voice'; file_id=m.voice.file_id
        content=m.text or m.caption or ''
        entities=m.entities if media_type=='text' else m.caption_entities
        entity_data=[e.model_dump(exclude_none=True) for e in (entities or [])]
        current=await state.get_data()
        await state.update_data(broadcast_type=media_type,broadcast_file_id=file_id,broadcast_content=content[:4000],broadcast_entities=entity_data,broadcast_target_bot_id=current.get('selected_bot_id'))
        await state.set_state(None)
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📨 Отправить всем',callback_data='cfg:broadcast:send'),InlineKeyboardButton(text='Отменить',callback_data='cfg:broadcast:cancel')]])
        await m.answer('Предпросмотр рассылки подготовлен. Будет отправлено сообщение такого типа: '+media_type+'\n\n'+(content[:500] or 'Без подписи')+'\n\nОтправить его всем пользователям выбранного бота?',reply_markup=kb)

    @dp.callback_query(F.data.startswith('cfg:broadcast:'))
    async def broadcast_execute(c:CallbackQuery,state:FSMContext):
        action=c.data.rsplit(':',1)[1]
        row=await selected_cb(c,state)
        if not row:return
        data=await state.get_data()
        if action=='cancel':
            await state.update_data(broadcast_type=None,broadcast_file_id=None,broadcast_content=None,broadcast_entities=None,broadcast_target_bot_id=None)
            try: await c.message.edit_reply_markup(reply_markup=None)
            except Exception: pass
            await c.message.answer('Рассылка отменена.'); await c.answer('Отменено'); return
        if data.get('broadcast_target_bot_id') != row['bot_id']:
            await c.answer('Выбран другой бот. Отмени эту рассылку и создай новую для нужного бота.',show_alert=True); return
        media_type=data.get('broadcast_type'); content=data.get('broadcast_content','')
        if not media_type: await c.answer('Сообщение для рассылки не найдено, создай её заново.',show_alert=True); return
        entities=[MessageEntity(**e) for e in data.get('broadcast_entities',[]) ]
        payload=None
        if media_type!='text':
            file_id=data.get('broadcast_file_id')
            if not file_id: await c.answer('Файл не найден, создай рассылку заново.',show_alert=True); return
            try:
                downloaded=await bot.download(file_id)
                raw=downloaded.getvalue() if hasattr(downloaded,'getvalue') else downloaded.read()
                payload=BufferedInputFile(raw,filename={'photo':'broadcast.jpg','video':'broadcast.mp4','animation':'broadcast.gif','document':'broadcast.bin','audio':'broadcast.mp3','voice':'broadcast.ogg'}.get(media_type,'broadcast.bin'))
            except Exception:
                log.exception('Unable to download broadcast media')
                await c.answer('Не удалось подготовить файл для рассылки.',show_alert=True); return
        env=(Path(row['instance_dir'])/'.env').read_text(encoding='utf-8')
        child_token=next((line.split('=',1)[1] for line in env.splitlines() if line.startswith('BOT_TOKEN=')), '')
        child=Bot(child_token)
        with sqlite3.connect(tenant_db(row)) as con: users=[r[0] for r in con.execute('SELECT user_id FROM users').fetchall()]
        ok=0; failed=0
        try:
            for uid in users:
                try:
                    if media_type=='text': await child.send_message(uid,content,entities=entities or None)
                    else:
                        sender={'photo':child.send_photo,'video':child.send_video,'animation':child.send_animation,'document':child.send_document,'audio':child.send_audio,'voice':child.send_voice}[media_type]
                        kwargs={'chat_id':uid,'caption':content or None,'caption_entities':entities or None}
                        kwargs[{'photo':'photo','video':'video','animation':'animation','document':'document','audio':'audio','voice':'voice'}[media_type]]=payload
                        await sender(**kwargs)
                    ok+=1
                except Exception as e:
                    failed+=1
                    log.debug('Broadcast delivery failed bot_id=%s user_id=%s error=%s',row['bot_id'],uid,type(e).__name__)
                await asyncio.sleep(0.05)
        finally: await child.session.close()
        await state.update_data(broadcast_type=None,broadcast_file_id=None,broadcast_content=None,broadcast_entities=None,broadcast_target_bot_id=None)
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        await c.message.answer(f'Рассылка завершена. Успешно: {ok}. Ошибок доставки: {failed}.'); await c.answer('Готово')

    def owner_only(user_id: int) -> bool:
        return user_id in OWNER_IDS

    def admin_users_page(page: int = 0):
        page = max(0, page)
        with db() as con:
            # Include owners of connected bots even if they predate the users table.
            rows = con.execute("""SELECT u.user_id, u.created_at, u.username, u.first_name,
                (SELECT COUNT(*) FROM bots b WHERE b.owner_id=u.user_id) AS bot_count
                FROM users u
                UNION
                SELECT b.owner_id AS user_id, MIN(b.created_at) AS created_at,
                NULL AS username, NULL AS first_name, COUNT(*) AS bot_count FROM bots b
                WHERE b.owner_id NOT IN (SELECT user_id FROM users) GROUP BY b.owner_id
                ORDER BY created_at DESC LIMIT 10 OFFSET ?""", (page * 10,)).fetchall()
            total = con.execute('SELECT COUNT(*) FROM (SELECT user_id FROM users UNION SELECT owner_id FROM bots)').fetchone()[0]
        buttons = []
        for r in rows:
            with db() as con:
                names = con.execute('SELECT first_name, username FROM bots WHERE owner_id=? ORDER BY id DESC LIMIT 1', (r['user_id'],)).fetchone()
            person_name = (r['first_name'] or '').strip()
            person_username = ('@' + r['username']) if r['username'] else ''
            label = f"👤 {person_name or person_username or r['user_id']} · ботов: {r['bot_count']}"
            if names:
                label = f"🤖 {str(names['first_name'] or 'Бот')[:16]} · ID {r['user_id']} · {r['bot_count']} бот."
            buttons.append([InlineKeyboardButton(text=label[:60], callback_data=f"adm:user:{r['user_id']}")])
        nav=[]
        if page > 0: nav.append(InlineKeyboardButton(text='⬅️ Назад', callback_data=f'adm:users:{page-1}'))
        if (page + 1) * 10 < total: nav.append(InlineKeyboardButton(text='Далее ➡️', callback_data=f'adm:users:{page+1}'))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text='↻ Обновить', callback_data=f'adm:users:{page}'), InlineKeyboardButton(text='⬅️ К статистике', callback_data='adm:dashboard')])
        return total, InlineKeyboardMarkup(inline_keyboard=buttons)

    def tenant_monitoring(row):
        """Return a read-only operational snapshot for one connected personal bot."""
        path = tenant_db(row)
        result = {'users': 0, 'active_subs': 0, 'tariffs': 0, 'channels': 0,
                  'methods': 0, 'pending_orders': 0, 'pending_license_payments': 0,
                  'license_status': 'база не создана', 'expiry': '—', 'limit': '—',
                  'turnover': 0.0, 'month_turnover': 0.0}
        if not path.exists():
            return result
        try:
            with sqlite3.connect(path) as con:
                con.row_factory = sqlite3.Row
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                def count(table, sql=None):
                    if table not in tables: return 0
                    return int(con.execute(sql or f'SELECT COUNT(*) FROM {table}').fetchone()[0] or 0)
                result['users'] = count('users')
                result['active_subs'] = count('subscriptions', "SELECT COUNT(*) FROM subscriptions WHERE status='active'")
                result['tariffs'] = count('tariffs', 'SELECT COUNT(*) FROM tariffs WHERE active=1')
                result['channels'] = count('channels', 'SELECT COUNT(*) FROM channels WHERE active=1')
                result['methods'] = count('payment_methods', 'SELECT COUNT(*) FROM payment_methods WHERE active=1')
                result['pending_orders'] = count('orders', "SELECT COUNT(*) FROM orders WHERE status IN ('pending','processing')")
                result['pending_license_payments'] = count('license_payments', "SELECT COUNT(*) FROM license_payments WHERE status IN ('awaiting_proof','pending')")
                settings = {}
                if 'settings' in tables:
                    settings = {r['key']: r['value'] for r in con.execute('SELECT key,value FROM settings')}
                result['limit'] = settings.get('turnover_limit', '—')
                expiry = settings.get('license_expires_at', '')
                result['expiry'] = expiry or ('без срока' if settings.get('license_turnover_capped') == '1' else 'не задан')
                active = settings.get('license_active') == '1'
                capped = settings.get('license_turnover_capped') == '1'
                if active and (capped or not expiry):
                    result['license_status'] = 'активна' if capped else 'активна (срок не задан)'
                elif active:
                    try:
                        exp = __import__('datetime').datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                        if exp.tzinfo is None: exp = exp.replace(tzinfo=__import__('datetime').timezone.utc)
                        result['license_status'] = 'активна' if exp > __import__('datetime').datetime.now(__import__('datetime').timezone.utc) else 'истекла'
                    except (ValueError, TypeError): result['license_status'] = 'активна (дата не распознана)'
                else:
                    result['license_status'] = 'не активна'
                if 'orders' in tables:
                    approved = con.execute("SELECT amount,tariff_id,reviewed_at,created_at FROM orders WHERE status='approved'").fetchall()
                    import datetime as _dt
                    now = _dt.datetime.now(_dt.timezone.utc)
                    for order in approved:
                        amount = float(order['amount'] or 0)
                        if amount <= 0 and 'tariffs' in tables:
                            tariff = con.execute('SELECT price FROM tariffs WHERE id=?', (order['tariff_id'],)).fetchone()
                            match = re.search(r'\d+(?:[.,]\d+)?', str(tariff['price'] if tariff else ''))
                            if match: amount = float(match.group(0).replace(',', '.'))
                        result['turnover'] += amount
                        raw = order['reviewed_at'] or order['created_at']
                        try:
                            dt = _dt.datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                            if dt.tzinfo is None: dt = dt.replace(tzinfo=_dt.timezone.utc)
                            dt = dt.astimezone(_dt.timezone.utc)
                            if (dt.year, dt.month) == (now.year, now.month): result['month_turnover'] += amount
                        except (ValueError, TypeError): pass
        except (sqlite3.Error, OSError) as exc:
            log.warning('Monitoring snapshot failed bot_id=%s: %s', row['bot_id'], type(exc).__name__)
            result['license_status'] = 'ошибка чтения базы'
        return result

    async def license_expiry_watch():
        """Notify constructor owners once per tenant license milestone and expiry date."""
        from datetime import datetime, timezone, timedelta
        while True:
            try:
                with db() as con:
                    rows = con.execute('SELECT * FROM bots ORDER BY id').fetchall()
                now = datetime.now(timezone.utc)
                for row in rows:
                    path = tenant_db(row)
                    if not path.exists():
                        continue
                    try:
                        with sqlite3.connect(path, timeout=5) as tenant:
                            tenant.row_factory = sqlite3.Row
                            names = {x[1] for x in tenant.execute('PRAGMA table_info(settings)').fetchall()}
                            if not names or not {'key', 'value'}.issubset(names):
                                continue
                            settings = {x['key']: x['value'] for x in tenant.execute('SELECT key,value FROM settings')}
                        if settings.get('license_active') != '1' or not settings.get('license_expires_at'):
                            continue
                        raw_expiry = str(settings['license_expires_at'])
                        expiry = datetime.fromisoformat(raw_expiry.replace('Z', '+00:00'))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=timezone.utc)
                        expiry = expiry.astimezone(timezone.utc)
                        remaining = expiry - now
                        if remaining <= timedelta(0):
                            milestone, title = 'expired', '⛔ Лицензия истекла'
                        elif remaining <= timedelta(hours=24):
                            milestone, title = '24h', '⚠️ Лицензия истекает в течение 24 часов'
                        elif remaining <= timedelta(days=7):
                            milestone, title = '7d', '🔔 До окончания лицензии осталось не более 7 дней'
                        else:
                            continue
                        expiry_text = expiry.strftime('%d.%m.%Y %H:%M UTC')
                        bot_uname = row['username'] or 'нет username'
                        message = (f"{title}\n\nБот: {row['first_name']} (@{bot_uname})\n"
                                   f"ID бота: {row['bot_id']}\nВладелец бота: {row['owner_id']}\n"
                                   f"Дата окончания: {expiry_text}\n\nОткрой карточку бота в разделе «📊 Статистика → Пользователи и боты», чтобы проверить лицензию.")
                        for owner_id in OWNER_IDS:
                            with db() as con:
                                exists = con.execute('SELECT 1 FROM license_expiry_alerts WHERE bot_id=? AND expiry=? AND milestone=? AND owner_id=?',
                                                     (row['bot_id'], raw_expiry, milestone, owner_id)).fetchone()
                            if exists:
                                continue
                            try:
                                await bot.send_message(owner_id, message)
                            except Exception as exc:
                                log.warning('License expiry alert failed bot_id=%s owner_id=%s milestone=%s error=%s',
                                            row['bot_id'], owner_id, milestone, type(exc).__name__)
                                continue
                            with db() as con:
                                con.execute('INSERT OR IGNORE INTO license_expiry_alerts(bot_id,expiry,milestone,owner_id) VALUES(?,?,?,?)',
                                            (row['bot_id'], raw_expiry, milestone, owner_id))
                    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                        log.warning('Could not inspect license expiry bot_id=%s error=%s', row['bot_id'], type(exc).__name__)
            except Exception:
                log.exception('Unexpected error in license expiry monitor')
            await asyncio.sleep(6 * 60 * 60)

    async def send_user_card(target, user_id: int):
        if not owner_only(target.from_user.id):
            await target.answer('Нет доступа.', show_alert=True) if isinstance(target, CallbackQuery) else await target.answer('Нет доступа.')
            return
        with db() as con:
            user = con.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            bots = con.execute('SELECT * FROM bots WHERE owner_id=? ORDER BY id DESC', (user_id,)).fetchall()
        created = user['created_at'] if user else (bots[0]['created_at'] if bots else '—')
        person_name = (user['first_name'] or '') if user else ''
        person_username = (('@' + user['username']) if user and user['username'] else '')
        display_name = person_name or 'неизвестно'
        lines = [f'👤 Карточка пользователя {user_id}', f'Имя: {display_name} {person_username}'.strip(), f'Зарегистрирован: {created}', f'Подключено ботов: {len(bots)}']
        kb=[]
        for r in bots:
            snap = tenant_monitoring(r)
            lines += ['', f"🤖 {r['first_name']} (@{r['username'] or 'нет username'})", f"ID: {r['bot_id']} · процесс: {instance_status(Path(r['instance_dir']))}", f"Лицензия: {snap['license_status']} · до: {snap['expiry']}", f"Оборот за месяц: {snap['month_turnover']:.2f} · за всё время: {snap['turnover']:.2f} · лимит: {snap['limit']}", f"Пользователи бота: {snap['users']} · активные подписки: {snap['active_subs']}", f"Активные тарифы: {snap['tariffs']} · каналы: {snap['channels']} · способы оплаты: {snap['methods']}", f"Заявки: {snap['pending_orders']} · заявки на лицензию: {snap['pending_license_payments']}", f"Подключён: {r['created_at']}"]
            kb.append([InlineKeyboardButton(text=f"📊 {str(r['first_name'])[:22]} · {r['bot_id']}", callback_data=f"adm:bot:{r['bot_id']}")])
        if not bots: lines.append('У пользователя пока нет подключённых ботов.')
        kb.append([InlineKeyboardButton(text='⬅️ К пользователям', callback_data='adm:users:0')])
        markup=InlineKeyboardMarkup(inline_keyboard=kb)
        if isinstance(target, CallbackQuery):
            await target.message.answer('\n'.join(lines), reply_markup=markup)
            await target.answer()
        else:
            await target.answer('\n'.join(lines), reply_markup=markup)

    def license_methods_markup(methods):
        rows=[]
        for method in methods:
            status = '🟢' if method['active'] else '⏸'
            rows.append([InlineKeyboardButton(text=f"{status} {method['name'][:35]} · #{method['id']}", callback_data=f"licmethod:toggle:{method['id']}"), InlineKeyboardButton(text='🗑', callback_data=f"licmethod:delete:{method['id']}")])
        rows.append([InlineKeyboardButton(text='➕ Добавить способ оплаты', callback_data='licmethod:add')])
        rows.append([InlineKeyboardButton(text='📋 Тарифы лицензии', callback_data='lictariff:list')])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_license_methods(target, edit=False):
        if target.from_user.id not in OWNER_IDS:
            if isinstance(target, CallbackQuery): await target.answer('Нет доступа', show_alert=True)
            else: await target.answer('Нет доступа.')
            return
        with db() as con:
            methods=con.execute('SELECT * FROM license_payment_methods ORDER BY id DESC').fetchall()
        await asyncio.to_thread(sync_license_methods_all)
        text='💳 Способы оплаты лицензии\n\nЭти способы используются только для покупки/продления лицензий и автоматически синхронизируются со всеми подключёнными ботами. Обычные способы оплаты товаров и подписок не затрагиваются.'
        if methods:
            text += '\n\n' + '\n'.join(f"{'🟢' if x['active'] else '⏸'} {x['name']}\n{x['details'][:160]}" for x in methods)
        else:
            text += '\n\nПока нет способов оплаты. Добавь первый способ.'
        markup=license_methods_markup(methods)
        if isinstance(target, CallbackQuery):
            await target.message.answer(text, reply_markup=markup); await target.answer()
        else:
            await target.answer(text, reply_markup=markup)

    @dp.message(F.text=='💳 Оплата лицензий')
    async def license_methods_button(m:Message):
        if m.from_user.id not in OWNER_IDS: await m.answer('Нет доступа.'); return
        await show_license_methods(m)

    @dp.callback_query(F.data=='licmethod:add')
    async def license_method_add(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        await state.clear(); await state.set_state(LicenseMethodState.name)
        await c.message.answer('Введи название способа оплаты лицензии:')
        await c.answer()

    @dp.message(LicenseMethodState.name, F.text)
    async def license_method_name(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        name=(m.text or '').strip()[:100]
        if not name: await m.answer('Название не может быть пустым.'); return
        await state.update_data(license_method_name=name); await state.set_state(LicenseMethodState.details)
        await m.answer('Введи реквизиты или инструкцию по оплате лицензии (до 3000 символов):')

    @dp.message(LicenseMethodState.details, F.text)
    async def license_method_details(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        details=(m.text or '').strip()[:3000]
        if not details: await m.answer('Инструкция не может быть пустой.'); return
        data=await state.get_data()
        with db() as con:
            con.execute('INSERT INTO license_payment_methods(name,details,active) VALUES(?,?,1)',(data['license_method_name'],details))
        await state.clear(); await asyncio.to_thread(sync_license_methods_all)
        await m.answer('Способ оплаты лицензии добавлен и синхронизирован с подключёнными ботами.',reply_markup=menu_for(m.from_user.id))
        await show_license_methods(m)

    @dp.callback_query(F.data.startswith('licmethod:toggle:'))
    async def license_method_toggle(c:CallbackQuery):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        try: mid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный способ оплаты',show_alert=True); return
        with db() as con: con.execute('UPDATE license_payment_methods SET active=1-active WHERE id=?',(mid,))
        await asyncio.to_thread(sync_license_methods_all); await c.answer('Статус изменён'); await show_license_methods(c)

    @dp.callback_query(F.data.startswith('licmethod:delete:'))
    async def license_method_delete(c:CallbackQuery):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        try: mid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный способ оплаты',show_alert=True); return
        with db() as con: con.execute('DELETE FROM license_payment_methods WHERE id=?',(mid,))
        await asyncio.to_thread(sync_license_methods_all); await c.answer('Способ удалён'); await show_license_methods(c)

    def license_tariffs_markup(tariffs):
        rows=[]
        for tariff in tariffs:
            status='🟢' if tariff['active'] else '⏸'
            rows.append([InlineKeyboardButton(text=f"{status} {tariff['name'][:24]} · {tariff['price']:g}", callback_data=f"lictariff:toggle:{tariff['id']}"), InlineKeyboardButton(text='🗑', callback_data=f"lictariff:delete:{tariff['id']}")])
        rows.append([InlineKeyboardButton(text='➕ Добавить тариф лицензии', callback_data='lictariff:add')])
        rows.append([InlineKeyboardButton(text='⬅️ Способы оплаты', callback_data='lictariff:methods')])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def show_license_tariffs(target):
        if target.from_user.id not in OWNER_IDS:
            if isinstance(target, CallbackQuery): await target.answer('Только владелец конструктора', show_alert=True)
            else: await target.answer('Только владелец конструктора.')
            return
        with db() as con: tariffs=con.execute('SELECT * FROM license_tariffs ORDER BY id DESC').fetchall()
        await asyncio.to_thread(sync_license_methods_all)
        text='📋 Тарифы лицензии\n\nТарифы централизованно применяются ко всем созданным ботам. Администраторы ботов не могут редактировать их.'
        if tariffs:
            text+='\n\n'+'\n'.join(f"{'🟢' if x['active'] else '⏸'} {x['name']} · {x['price']:g} · {x['duration_days']} дн. · лимит {x['turnover_limit']:g}" for x in tariffs)
        else: text+='\n\nТарифов пока нет.'
        markup=license_tariffs_markup(tariffs)
        if isinstance(target, CallbackQuery): await target.message.answer(text,reply_markup=markup); await target.answer()
        else: await target.answer(text,reply_markup=markup)

    @dp.callback_query(F.data=='lictariff:list')
    async def license_tariff_list(c:CallbackQuery):
        await show_license_tariffs(c)

    @dp.callback_query(F.data=='lictariff:methods')
    async def license_tariff_methods(c:CallbackQuery):
        await show_license_methods(c)

    @dp.callback_query(F.data=='lictariff:add')
    async def license_tariff_add(c:CallbackQuery,state:FSMContext):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        await state.clear(); await state.set_state(LicenseTariffState.name)
        await c.message.answer('Название тарифа лицензии:'); await c.answer()

    @dp.message(LicenseTariffState.name, F.text)
    async def license_tariff_name(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        value=(m.text or '').strip()[:80]
        if not value: await m.answer('Название не может быть пустым.'); return
        await state.update_data(license_tariff_name=value); await state.set_state(LicenseTariffState.turnover_limit)
        await m.answer('Укажи максимальный оборот для лицензии числом (например, 5000):')

    @dp.message(LicenseTariffState.turnover_limit, F.text)
    async def license_tariff_limit(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        try: value=float((m.text or '').replace(',','.'))
        except ValueError: await m.answer('Введи число, например 5000.'); return
        if not math.isfinite(value) or value <= 0 or value > 100000000000: await m.answer('Лимит должен быть конечным числом больше нуля и не превышать 100 000 000 000.'); return
        await state.update_data(license_tariff_limit=value); await state.set_state(LicenseTariffState.price)
        await m.answer('Цена лицензии (число, например 990):')

    @dp.message(LicenseTariffState.price, F.text)
    async def license_tariff_price(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        try: value=float((m.text or '').replace(',','.'))
        except ValueError: await m.answer('Введи цену числом, например 990.'); return
        if not math.isfinite(value) or value < 0 or value > 100000000000: await m.answer('Цена должна быть конечным числом от 0 до 100 000 000 000.'); return
        await state.update_data(license_tariff_price=value); await state.set_state(LicenseTariffState.duration_days)
        await m.answer('Срок лицензии в днях (целое число, например 30):')

    @dp.message(LicenseTariffState.duration_days, F.text)
    async def license_tariff_duration(m:Message,state:FSMContext):
        if m.from_user.id not in OWNER_IDS: await state.clear(); return
        try: value=int((m.text or '').strip())
        except ValueError: await m.answer('Введи целое количество дней, например 30.'); return
        if value < 1 or value > 3650: await m.answer('Срок должен быть от 1 до 3650 дней.'); return
        data=await state.get_data()
        with db() as con:
            con.execute('INSERT INTO license_tariffs(name,turnover_limit,price,duration_days,active) VALUES(?,?,?,?,1)',(data['license_tariff_name'],data['license_tariff_limit'],data['license_tariff_price'],value))
        await state.clear(); await asyncio.to_thread(sync_license_methods_all)
        await m.answer('Тариф лицензии создан и синхронизирован со всеми подключёнными ботами.')
        await show_license_tariffs(m)

    @dp.callback_query(F.data.startswith('lictariff:toggle:'))
    async def license_tariff_toggle(c:CallbackQuery):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        with db() as con: con.execute('UPDATE license_tariffs SET active=1-active WHERE id=?',(tid,))
        await asyncio.to_thread(sync_license_methods_all); await c.answer('Статус тарифа изменён'); await show_license_tariffs(c)

    @dp.callback_query(F.data.startswith('lictariff:delete:'))
    async def license_tariff_delete(c:CallbackQuery):
        if c.from_user.id not in OWNER_IDS: await c.answer('Только владелец конструктора',show_alert=True); return
        try: tid=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный тариф',show_alert=True); return
        with db() as con: con.execute('UPDATE license_tariffs SET active=0 WHERE id=?',(tid,))
        await asyncio.to_thread(sync_license_methods_all); await c.answer('Тариф отключён; история заявок сохранена'); await show_license_tariffs(c)

    @dp.message(Command('admin'))
    async def admin_panel(m:Message):
        if not owner_only(m.from_user.id): await m.answer('Нет доступа.'); return
        with db() as con:
            total=con.execute('SELECT COUNT(*) FROM bots').fetchone()[0]
            users=con.execute('SELECT COUNT(*) FROM (SELECT user_id FROM users UNION SELECT owner_id FROM bots)').fetchone()[0]
            active=con.execute("SELECT COUNT(*) FROM bots WHERE status='active'").fetchone()[0]
        _, user_markup = admin_users_page(0)
        await m.answer(f'👑 Панель владельца конструктора\nПользователей: {users}\nПодключённых ботов: {total}\nАктивны по базе: {active}\n\nВыбери пользователя для подробной карточки:', reply_markup=user_markup)

    async def owner_dashboard(target, *, edit=False):
        """Show live process and license overview; available only to constructor owners."""
        if not owner_only(target.from_user.id):
            if isinstance(target, CallbackQuery):
                await target.answer('Нет доступа', show_alert=True)
            else:
                await target.answer('Нет доступа.')
            return
        with db() as con:
            rows = con.execute('SELECT * FROM bots ORDER BY id DESC').fetchall()
            user_count = con.execute(
                'SELECT COUNT(*) FROM (SELECT user_id FROM users UNION SELECT owner_id FROM bots)'
            ).fetchone()[0]
            recent_events = con.execute('SELECT event_type,message,created_at FROM constructor_events ORDER BY id DESC LIMIT 5').fetchall()
        total = len(rows)
        running = stopped = failed = active_licenses = inactive_licenses = expired_licenses = expiring = 0
        pending_orders = pending_license = 0
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        for row in rows:
            inst_path = Path(row['instance_dir'])
            if not inst_path.exists() or not (inst_path / '.env').exists():
                failed += 1
            elif instance_status(inst_path) == 'active':
                running += 1
            else:
                stopped += 1
            snap = tenant_monitoring(row)
            # Detect license status/expiry changes between dashboard refreshes.
            try:
                license_event = None
                with db() as con:
                    previous = con.execute('SELECT license_status,expiry FROM license_state_snapshots WHERE bot_id=?', (row['bot_id'],)).fetchone()
                    if previous and (previous['license_status'] != snap['license_status'] or previous['expiry'] != snap['expiry']):
                        license_event = f"Лицензия бота {row['first_name']}: {previous['license_status']} / до {previous['expiry']} → {snap['license_status']} / до {snap['expiry']}"
                    con.execute('INSERT INTO license_state_snapshots(bot_id,license_status,expiry,checked_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(bot_id) DO UPDATE SET license_status=excluded.license_status,expiry=excluded.expiry,checked_at=CURRENT_TIMESTAMP', (row['bot_id'], snap['license_status'], snap['expiry']))
                if license_event:
                    record_event('license', license_event, row['bot_id'])
            except sqlite3.Error:
                log.exception('Could not update license snapshot for bot_id=%s', row['bot_id'])
            pending_orders += snap['pending_orders']
            pending_license += snap['pending_license_payments']
            status = snap['license_status']
            if status.startswith('активна'):
                active_licenses += 1
                raw_expiry = snap['expiry']
                if raw_expiry not in ('—', 'без срока', 'не задан'):
                    try:
                        expiry = datetime.fromisoformat(str(raw_expiry).replace('Z', '+00:00'))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=timezone.utc)
                        remaining = expiry.astimezone(timezone.utc) - now
                        if remaining <= timedelta(0):
                            expired_licenses += 1
                            active_licenses -= 1
                        elif remaining <= timedelta(days=7):
                            expiring += 1
                    except (ValueError, TypeError):
                        pass
            elif status == 'истекла':
                expired_licenses += 1
            elif status not in ('база не создана', 'ошибка чтения базы'):
                inactive_licenses += 1
        body = (
            '📊 Статистика конструктора\n\n'
            f'👥 Пользователей: {user_count}\n'
            f'🤖 Подключённых ботов: {total}\n'
            f'🟢 Процесс запущен: {running}\n'
            f'🔴 Процесс остановлен: {stopped}\n'
            f'⚠️ Ошибка / неполная установка: {failed}\n\n'
            f'🔐 Лицензии активны: {active_licenses}\n'
            f'⏳ Истекают в течение 7 дней: {expiring}\n'
            f'⛔ Истекли: {expired_licenses}\n'
            f'⚪ Не активны: {inactive_licenses}\n\n'
            f'🧾 Ожидают решения заявки на подписку: {pending_orders}\n'
            f'💳 Ожидают решения заявки на лицензию: {pending_license}\n\n'
            '🕘 Последние события\n' + ('\n'.join(f"• {e['created_at']} — {e['message']}" for e in recent_events) if recent_events else 'Пока событий нет.') + '\n\n'
            'Статусы процессов проверяются по PID-файлам; ошибки установки — по наличию папки и .env. Лицензии и заявки читаются из баз персональных ботов.'
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='👥 Пользователи и боты', callback_data='adm:users:0')],
        ])
        if isinstance(target, CallbackQuery):
            try:
                await target.message.edit_text(body, reply_markup=markup)
            except Exception:
                await target.message.answer(body, reply_markup=markup)
            await target.answer()
        else:
            await target.answer(body, reply_markup=markup)

    @dp.message(F.text == '📊 Статистика')
    async def owner_dashboard_message(m: Message):
        await owner_dashboard(m)

    @dp.message(F.text.in_({'📊 Сводка', '👥 Пользователи'}))
    async def legacy_owner_buttons(m: Message):
        # Old reply keyboards stay in Telegram until replaced: refresh the menu and open statistics.
        if not owner_only(m.from_user.id): return
        await m.answer('Меню обновлено.', reply_markup=OWNER_MENU)
        await owner_dashboard(m)

    @dp.callback_query(F.data == 'adm:dashboard')
    async def owner_dashboard_refresh(c: CallbackQuery):
        await owner_dashboard(c, edit=True)

    @dp.callback_query(F.data.startswith('adm:users:'))
    async def admin_users(c:CallbackQuery):
        if not owner_only(c.from_user.id): await c.answer('Нет доступа', show_alert=True); return
        try: page=max(0,int(c.data.rsplit(':',1)[1]))
        except ValueError: page=0
        total, markup=admin_users_page(page)
        try: await c.message.edit_text(f'👥 Пользователи конструктора\nВсего: {total}\nСтраница: {page+1}', reply_markup=markup)
        except Exception: await c.message.answer(f'👥 Пользователи конструктора\nВсего: {total}\nСтраница: {page+1}', reply_markup=markup)
        await c.answer()

    @dp.callback_query(F.data.startswith('adm:user:'))
    async def admin_user_detail(c:CallbackQuery):
        if not owner_only(c.from_user.id): await c.answer('Нет доступа', show_alert=True); return
        try: user_id=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный ID', show_alert=True); return
        await send_user_card(c,user_id)

    @dp.callback_query(F.data.startswith('adm:bot:'))
    async def admin_bot_detail(c:CallbackQuery):
        if not owner_only(c.from_user.id): await c.answer('Нет доступа', show_alert=True); return
        try: bot_id=int(c.data.rsplit(':',1)[1])
        except ValueError: await c.answer('Некорректный ID', show_alert=True); return
        with db() as con: row=con.execute('SELECT * FROM bots WHERE bot_id=?',(bot_id,)).fetchone()
        if not row: await c.answer('Бот не найден',show_alert=True); return
        snap=tenant_monitoring(row)
        text=(f"📊 Мониторинг бота {row['first_name']}\n@{row['username'] or 'нет username'} · ID {row['bot_id']}\nВладелец: {row['owner_id']}\nСоздан: {row['created_at']}\nПроцесс: {instance_status(Path(row['instance_dir']))}\n\nЛицензия: {snap['license_status']}\nДействует до: {snap['expiry']}\nЛимит оборота: {snap['limit']}\nОборот за месяц: {snap['month_turnover']:.2f}\nОборот за всё время: {snap['turnover']:.2f}\n\nПользователи: {snap['users']}\nАктивные подписки: {snap['active_subs']}\nАктивные тарифы: {snap['tariffs']}\nКаналы: {snap['channels']}\nСпособы оплаты: {snap['methods']}\nЗаявки на подписку: {snap['pending_orders']}\nЗаявки на лицензию: {snap['pending_license_payments']}")
        process_status = instance_status(Path(row['instance_dir']))
        log_tail = ''
        try:
            log_tail = (Path(row['instance_dir']) / 'bot.log').read_text(encoding='utf-8', errors='replace')[-900:]
            log_tail = re.sub(r'\b\d{5,15}:[A-Za-z0-9_-]{20,}\b', '[СКРЫТЫЙ_ТОКЕН]', log_tail)
        except OSError:
            log_tail = 'Журнал отсутствует.'
        text += f'\n\nПоследний фрагмент журнала:\n{log_tail or "Журнал пуст."}'
        markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🔄 Перезапустить',callback_data=f'b:restart:{bot_id}'), InlineKeyboardButton(text='📋 Статус и журнал',callback_data=f'b:status:{bot_id}')],
            [InlineKeyboardButton(text='▶️ Запустить' if process_status != 'active' else '⏹ Остановить',callback_data=f'b:toggle:{bot_id}')],
            [InlineKeyboardButton(text='👤 Карточка владельца',callback_data=f"adm:user:{row['owner_id']}")],
            [InlineKeyboardButton(text='↻ Обновить',callback_data=f'adm:bot:{bot_id}')],
            [InlineKeyboardButton(text='⬅️ К статистике',callback_data='adm:dashboard'), InlineKeyboardButton(text='⬅️ К пользователям',callback_data='adm:users:0')]
        ])
        await c.message.answer(text,reply_markup=markup); await c.answer()

    async def bot_process_watch():
        """Detect unexpected process exits and notify constructor owners once per crash."""
        while True:
            try:
                with db() as con:
                    rows = con.execute('SELECT * FROM bots ORDER BY id').fetchall()
                for row in rows:
                    inst = Path(row['instance_dir'])
                    status = 'failed' if not inst.exists() or not (inst / '.env').is_file() else instance_status(inst)
                    with db() as con:
                        db_status = con.execute('SELECT status FROM bots WHERE bot_id=?', (row['bot_id'],)).fetchone()
                        was_expected_running = bool(db_status and db_status['status'] == 'active')
                        con.execute('INSERT INTO bot_process_snapshots(bot_id,process_status,checked_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(bot_id) DO UPDATE SET process_status=excluded.process_status,checked_at=CURRENT_TIMESTAMP', (row['bot_id'], status))
                        if status in ('stopped', 'failed') and was_expected_running:
                            con.execute('UPDATE bots SET status=? WHERE bot_id=?', (status, row['bot_id']))
                    if status in ('stopped', 'failed') and was_expected_running:
                        message = f"⚠️ Персональный бот неожиданно остановился\n\nБот: {row['first_name']} (@{row['username'] or 'нет username'})\nID: {row['bot_id']}\nСтатус: {status}\nПроверь журнал через карточку бота в разделе «📊 Статистика»."
                        record_event('error', f"Бот {row['first_name']} неожиданно остановился ({status})", row['bot_id'])
                        for owner_id in OWNER_IDS:
                            try:
                                await bot.send_message(owner_id, message, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📊 Открыть карточку бота', callback_data=f"adm:bot:{row['bot_id']}")]]))
                            except Exception as exc:
                                log.warning('Could not notify owner about stopped bot_id=%s error=%s', row['bot_id'], type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Unexpected error in bot process monitor')
            await asyncio.sleep(60)

    await asyncio.to_thread(sync_license_methods_all)
    expiry_task = asyncio.create_task(license_expiry_watch(), name='license-expiry-watch')
    process_watch_task = asyncio.create_task(bot_process_watch(), name='bot-process-watch')
    log.info('Constructor started; python=%s; template=%s; instances=%s',sys.executable,TEMPLATE_SCRIPT,INSTANCES)
    try:
        await dp.start_polling(bot)
    finally:
        expiry_task.cancel()
        process_watch_task.cancel()
        for task in (expiry_task, process_watch_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

if __name__=='__main__': asyncio.run(main())
