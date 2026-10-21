#!/usr/bin/env python3
"""KK Robot: Discord/Ollama bot with a local-only admin interface.

Standalone deployment alongside an unchanged llmcord checkout. The patterns of
Discord reply-chain messaging were informed by llmcord (MIT license).
"""
import asyncio
import base64
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

from core import MODES, MODELS, SearchResultParser, clean_text_for_discord, parse_gpu_csv, should_reply

ROOT = Path(__file__).resolve().parent


def load_dotenv():
    p = ROOT / '.env'
    if p.is_file():
        for line in p.read_text(encoding='utf-8').splitlines():
            k, sep, v = line.strip().partition('=')
            if sep and re.fullmatch(r'[A-Z_][A-Z_0-9]*', k):
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))


load_dotenv()
TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '')
PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
ADMIN_HOST = '127.0.0.1'
ADMIN_PORT = int(os.environ.get('ADMIN_PORT', '8765'))
OLLAMA_BASE = os.environ.get('OLLAMA_BASE', 'http://127.0.0.1:11555')
DB_PATH = ROOT / 'kk_robot.sqlite3'
DOCKER_IMAGE = 'python:3.12-slim'
SYS_PROMPT = (
    '你是 Discord 伺服器裡的 KK Robot，使用自然、直接的繁體中文回答。'
    '根據可讀取的頻道歷史理解對話，但不要透露其他頻道內容。'
    '遇到需要即時資訊的問題，如果有 web_search 工具可以用，就先搜尋再回答並附來源。'
    '如果有 run_python 工具且使用者要求執行程式，使用工具實際執行，不要假裝執行過。'
    '不要洩漏主機 IP、路徑、管理員帳密、Discord Token 或其他機密。'
    '對使用者尊重，不要嘲諷；不確定就直說。'
)
DEFAULT = {
    'mode': 'all', 'channels': [], 'model': 'qwen3.5:9b',
    'system_prompt': SYS_PROMPT, 'web_search': False,
    'python_enabled': False, 'python_allowed_user_ids': []
}
CONTEXT_MESSAGES = 50
CONTEXT_CHAR_LIMIT = 18000
MAX_REQUEST_CHARS = 3500
MAX_REPLY_TOKENS = 768
MAX_PENDING = 16
PYTHON_MAX_CHARS = 2400
PYTHON_MAX_SECONDS = 12
SEARCH_MAX_RESULTS = 5


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute('CREATE TABLE IF NOT EXISTS guild_settings (guild_id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS usage (guild_id TEXT, at INTEGER, model TEXT, '
               'prompt_tokens INTEGER, output_tokens INTEGER, seconds REAL, error TEXT)')
    db.commit()
    return db


def load_guild(db, guild_id):
    row = db.execute('SELECT data FROM guild_settings WHERE guild_id=?', (str(guild_id),)).fetchone()
    return {**DEFAULT, **json.loads(row[0])} if row else dict(DEFAULT)


def save_guild(db, guild_id, settings):
    db.execute('INSERT INTO guild_settings (guild_id,data) VALUES (?,?) '
               'ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data',
               (str(guild_id), json.dumps(settings, ensure_ascii=False)))
    db.commit()


def record_usage(db, guild_id, model, prompt_count=0, eval_count=0, duration=0., error=''):
    db.execute('INSERT INTO usage VALUES (?,?,?,?,?,?,?)',
               (str(guild_id), int(time.time()), model, prompt_count, eval_count, duration, error[:120]))
    db.commit()


def get_usage(db, guild_id):
    row = db.execute('SELECT COUNT(*),COALESCE(SUM(prompt_tokens),0),COALESCE(SUM(output_tokens),0),'
                     'COALESCE(AVG(CASE WHEN output_tokens>0 AND seconds>0 THEN output_tokens/seconds END),0),'
                     'COALESCE(SUM(CASE WHEN error != "" THEN 1 ELSE 0 END),0) FROM usage WHERE guild_id=?',
                     (str(guild_id),)).fetchone()
    return dict(requests=row[0], input_tokens=row[1], output_tokens=row[2],
                avg_tokens_per_sec=round(row[3], 1), errors=row[4])


def authorized(request):
    header = request.headers.get('Authorization', '')
    if not header.startswith('Basic '):
        return False
    try:
        plain = base64.b64decode(header[6:], validate=True).decode('utf-8')
        user, sep, password = plain.partition(':')
    except (UnicodeError, ValueError):
        return False
    return bool(sep) and hmac.compare_digest(user, 'admin') and hmac.compare_digest(password, PASSWORD)


def require_auth(request):
    if not authorized(request):
        raise web.HTTPUnauthorized(headers={'WWW-Authenticate': 'Basic realm="KK Robot"',
                                            'Cache-Control': 'no-store'})


def require_write(request):
    require_auth(request)
    origin = request.headers.get('Origin')
    if origin and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden(text='Cross-origin write forbidden')
    if request.content_type != 'application/json':
        raise web.HTTPUnsupportedMediaType(text='JSON required')


async def search_web(session, query):
    """Public search with no API key. May be rate-limited or blocked by DuckDuckGo."""
    async with session.get('https://html.duckduckgo.com/html/',
                           params={'q': query[:220]},
                           headers={'User-Agent': 'Mozilla/5.0 (compatible; KKRobot/1.0)'},
                           timeout=aiohttp.ClientTimeout(total=12),
                           allow_redirects=False) as response:
        if response.status != 200:
            return f'搜尋暫時不可用 (HTTP {response.status})。'
        html = await response.text(errors='replace')
    parser = SearchResultParser()
    parser.feed(html[:700000])
    results = parser.results[:SEARCH_MAX_RESULTS]
    if not results:
        return '搜尋沒有可用結果，或搜尋引擎限制了這次請求。'
    return '\n'.join(f'{i}. {item["title"]}\n{item["url"]}' for i, item in enumerate(results, 1))


async def run_python(code):
    """Run code ONLY in a constrained, isolated Docker container. Disabled by default.

    Operator MUST obtain approval to use Docker on a shared host. Membership in
    the Docker group effectively grants root-level access to the Docker host.
    """
    if not isinstance(code, str) or not code.strip() or len(code) > PYTHON_MAX_CHARS:
        return '程式碼是空的，或超過 2400 字元。'
    docker = os.environ.get('DOCKER_BIN', '/usr/bin/docker')
    name = 'kk-robot-' + secrets.token_hex(6)
    args = [docker, 'run', '--rm', '--name', name, '--init',
            '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '48',
            '--memory', '128m', '--memory-swap', '128m', '--cpus', '0.5',
            '--user', '65534:65534', '--ulimit', 'nofile=64:64',
            '--tmpfs', '/tmp:rw,nosuid,noexec,size=16m', '--workdir', '/tmp',
            '-i', DOCKER_IMAGE, 'python', '-I', '-']
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        proc.stdin.write(code.encode('utf-8'))
        await proc.stdin.drain()
        proc.stdin.close()
        # Cap data read as well as time; no unbounded communicate() buffer.
        async def collect():
            buff = bytearray()
            while True:
                chunk = await proc.stdout.read(1024)
                if not chunk:
                    break
                buff.extend(chunk)
                if len(buff) > 9000:
                    return buff[:9000], True
            return buff, False
        output, truncated = await asyncio.wait_for(collect(), timeout=PYTHON_MAX_SECONDS)
        if truncated:
            proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=2)
        result = output.decode('utf-8', errors='replace').strip()[:4000]
        return ('輸出過長，已截斷。\n' if truncated else '') + (result or '程式已結束（沒有輸出）。')
    except asyncio.TimeoutError:
        if proc and proc.returncode is None:
            proc.kill()
        return f'執行超過 {PYTHON_MAX_SECONDS} 秒，已中止。'
    except (OSError, RuntimeError) as exc:
        return f'隔離的 Python 執行環境不可用：{type(exc).__name__}。請管理員確認 Docker 已核准、映像檔已預先下載。'
    finally:
        # Prevent a detached container from persisting after a timed out or
        # forcibly terminated docker client. Never use --privileged or mounts.
        if proc:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            try:
                cleaner = await asyncio.create_subprocess_exec(
                    docker, 'rm', '-f', name, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(cleaner.wait(), timeout=3)
            except (OSError, asyncio.TimeoutError):
                pass


async def cpu_percent():
    """Read system CPU utilization over a short interval from /proc/stat."""
    try:
        def snapshot():
            values = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:]))
            return sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        total1, idle1 = snapshot()
        await asyncio.sleep(0.15)
        total2, idle2 = snapshot()
        return round(100.0 * (1.0 - (idle2 - idle1) / max(total2 - total1, 1)), 1)
    except (OSError, ValueError, IndexError):
        return None


async def gpu_metrics():
    try:
        proc = await asyncio.create_subprocess_exec(
            'nvidia-smi', '--query-gpu=name,utilization.gpu,memory.used,memory.total',
            '--format=csv,noheader,nounits',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        return parse_gpu_csv(stdout.decode()) if proc.returncode == 0 else None
    except (OSError, asyncio.TimeoutError):
        return None


def system_metrics():
    # Requires neither psutil nor privileged access; load averages are not CPU %.
    result = {'cpu_load_1m': None, 'ram_used_mib': None, 'ram_total_mib': None}
    try:
        result['cpu_load_1m'] = round(os.getloadavg()[0], 2)
        meminfo = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, val = line.partition(':')
            meminfo[key] = int(val.strip().split()[0])
        result['ram_total_mib'] = round(meminfo['MemTotal'] / 1024, 1)
        result['ram_used_mib'] = round((meminfo['MemTotal'] - meminfo['MemAvailable']) / 1024, 1)
    except (FileNotFoundError, KeyError, ValueError, OSError):
        pass
    return result


class KKRobot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         allowed_mentions=discord.AllowedMentions.none())
        self.db = open_db()
        self.http_session = None
        self.admin_runner = None
        self.inflight = asyncio.Semaphore(1)
        self.pending = 0
        self.locks = {}

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=150))
        app = web.Application(client_max_size=8192)
        app.router.add_get('/', self.dashboard)
        app.router.add_get('/api/state', self.api_state)
        app.router.add_get('/api/metrics', self.api_metrics)
        app.router.add_post('/api/save', self.api_save)
        self.admin_runner = web.AppRunner(app)
        await self.admin_runner.setup()
        await web.TCPSite(self.admin_runner, ADMIN_HOST, ADMIN_PORT).start()
        print(f'管理後台僅在 http://127.0.0.1:{ADMIN_PORT} 開放；遠端請使用 SSH port forward。')
        # Global commands may take time to appear. Limit tree.sync() to startup.
        try:
            await self.tree.sync()
        except discord.HTTPException:
            print('提醒：slash commands 尚未同步，稍後再試。', file=sys.stderr)

    async def close(self):
        if self.admin_runner:
            await self.admin_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        self.db.close()
        await super().close()

    async def on_ready(self):
        print(f'已登入 Discord：{self.user}；伺服器數 {len(self.guilds)}。')

    async def dashboard(self, request):
        # HTML contains no secrets. APIs are protected by authentication.
        resp = web.FileResponse(ROOT / 'admin.html')
        resp.headers.update({'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                             'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
        return resp

    def guild_channels(self, guild):
        if not guild.me:
            return []
        return [{'id': str(channel.id), 'name': channel.name,
                 'can_reply': channel.permissions_for(guild.me).send_messages} for channel in guild.text_channels
                if channel.permissions_for(guild.me).view_channel]

    async def api_state(self, request):
        require_auth(request)
        guilds = [{'id': str(g.id), 'name': g.name, 'channels': self.guild_channels(g),
                   'settings': load_guild(self.db, g.id), 'usage': get_usage(self.db, g.id)}
                  for g in self.guilds]
        r = web.json_response({'guilds': guilds, 'connected': self.is_ready(),
                               'ollama_base': OLLAMA_BASE})
        r.headers['Cache-Control'] = 'no-store'
        return r

    async def api_metrics(self, request):
        require_auth(request)
        cpu, gpu = await asyncio.gather(cpu_percent(), gpu_metrics())
        r = web.json_response({**system_metrics(), 'cpu_percent': cpu, 'gpu': gpu, 'timestamp': int(time.time())})
        r.headers['Cache-Control'] = 'no-store'
        return r

    async def api_save(self, request):
        require_write(request)
        try:
            raw = await request.json()
            guild = self.get_guild(int(raw['guild_id']))
            if guild is None:
                raise ValueError('找不到 Discord 伺服器')
            mode = raw['mode']
            if mode not in MODES:
                raise ValueError('模式不正確')
            visible = {x['id'] for x in self.guild_channels(guild) if x['can_reply']}
            selected = list(dict.fromkeys(str(int(x)) for x in raw.get('channels', [])))
            if len(selected) > 200 or not set(selected).issubset(visible):
                raise ValueError('包含無法回覆的頻道')
            model = str(raw['model'])
            if model not in MODELS:
                raise ValueError('不支援此模型；請選 Qwen3.5-9B 或 35B-A3B')
            prompt = str(raw['system_prompt']).strip()
            if not 1 <= len(prompt) <= 2500:
                raise ValueError('系統提示詞必須為 1～2500 字')
            user_ids = list(dict.fromkeys(str(int(x)) for x in raw.get('python_allowed_user_ids', [])))
            if len(user_ids) > 50:
                raise ValueError('Python 使用者上限 50 人')
            settings = dict(mode=mode, channels=selected, model=model,
                            system_prompt=prompt, web_search=raw.get('web_search') is True,
                            python_enabled=raw.get('python_enabled') is True,
                            python_allowed_user_ids=user_ids)
            save_guild(self.db, guild.id, settings)
            return web.json_response({'ok': True, 'settings': settings})
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    def can_execute_python(self, user, settings):
        return bool(settings['python_enabled']) and (
            str(user.id) in settings['python_allowed_user_ids'] or
            getattr(getattr(user, 'guild_permissions', None), 'administrator', False))

    async def history_for(self, message):
        """History is read from Discord on demand; never answers old messages."""
        msgs = []
        try:
            async for item in message.channel.history(limit=CONTEXT_MESSAGES, before=message, oldest_first=False):
                if item.author.bot and item.author != self.user:
                    continue
                if not item.content:
                    continue
                content = item.content[:1200]
                if not content.strip():
                    continue
                if item.author == self.user:
                    msgs.append({'role': 'assistant', 'content': content})
                else:
                    msgs.append({'role': 'user', 'content': f'{item.author.display_name}: {content}'})
        except (discord.Forbidden, discord.HTTPException):
            pass
        msgs.reverse()
        total = 0
        chosen = []
        for msg in reversed(msgs):
            total += len(msg['content'])
            if total > CONTEXT_CHAR_LIMIT:
                break
            chosen.append(msg)
        return list(reversed(chosen))

    async def ollama_request(self, payload):
        async with self.http_session.post(OLLAMA_BASE.rstrip('/') + '/api/chat', json=payload) as resp:
            if resp.status != 200:
                msg = (await resp.text())[:200]
                raise RuntimeError(f'Ollama HTTP {resp.status}: {msg}')
            return await resp.json()

    async def query(self, message, input_text, settings):
        tools = []
        if settings['web_search']:
            tools.append({'type': 'function', 'function': {
                'name': 'web_search', 'description': '搜尋即時公開網頁，回傳標題與來源網址。遇到最新消息、網站、當日資訊時使用。',
                'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}})
        if self.can_execute_python(message.author, settings):
            tools.append({'type': 'function', 'function': {
                'name': 'run_python', 'description': '在隔離的 Python 3.12 容器執行程式碼，真實回傳輸出。僅在使用者要求執行程式時呼叫。',
                'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']}}})
        msgs = [{'role': 'system', 'content': settings['system_prompt']}]
        msgs.extend(await self.history_for(message))
        msgs.append({'role': 'user', 'content': f'{message.author.display_name}: {input_text[:MAX_REQUEST_CHARS]}'})
        body = {'model': settings['model'], 'messages': msgs, 'stream': False,
                'think': False, 'options': {'num_ctx': 8192, 'num_predict': MAX_REPLY_TOKENS}}
        if tools:
            body['tools'] = tools
        started = time.monotonic()
        async with self.inflight:
            first = await self.ollama_request(body)
            tool_calls = (first.get('message') or {}).get('tool_calls') or []
            if tool_calls:
                # Explicitly capped at one tool call per interaction.
                call = tool_calls[0].get('function') or {}
                name = call.get('name', '')
                args = call.get('arguments', {})
                if not isinstance(args, dict):
                    args = {}
                if name == 'web_search' and settings['web_search']:
                    try:
                        output = await search_web(self.http_session, str(args.get('query', '')))
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                        output = '搜尋服務暫時無法使用，請不要編造來源。'
                elif name == 'run_python' and self.can_execute_python(message.author, settings):
                    output = await run_python(args.get('code', ''))
                else:
                    output = '此工具未授權或未啟用。'
                msgs.append(first['message'])
                msgs.append({'role': 'tool', 'tool_name': name, 'content': output})
                body['messages'] = msgs
                body.pop('tools', None)
                second = await self.ollama_request(body)
                result = (second.get('message') or {}).get('content', '')
                eval_count = int(first.get('eval_count', 0)) + int(second.get('eval_count', 0))
                prompt_count = int(first.get('prompt_eval_count', 0)) + int(second.get('prompt_eval_count', 0))
                if name == 'web_search' and 'http' in output and not any(u in result for u in ('http://', 'https://')):
                    result = (result or '搜尋結果如下。') + '\n\n資料來源：\n' + output[:1500]
            else:
                result = (first.get('message') or {}).get('content', '')
                eval_count = int(first.get('eval_count', 0))
                prompt_count = int(first.get('prompt_eval_count', 0))
        seconds = max(time.monotonic() - started, 0.001)
        record_usage(self.db, message.guild.id, settings['model'], prompt_count, eval_count, seconds)
        return result

    async def send_reply(self, message, text, settings):
        if self.pending >= MAX_PENDING:
            await message.reply('目前排隊太多，請稍後再試。', mention_author=False)
            return
        self.pending += 1
        placeholder = None
        try:
            placeholder = await message.reply('🤔 思考中…', mention_author=False,
                                              allowed_mentions=discord.AllowedMentions.none())
            # If another user message arrives during inference, serialize per channel
            # so conversation order stays stable.
            lock = self.locks.setdefault(message.channel.id, asyncio.Lock())
            async with lock, message.channel.typing():
                answer = await self.query(message, text, settings)
            chunks = clean_text_for_discord(answer)
            await placeholder.edit(content=chunks[0], allowed_mentions=discord.AllowedMentions.none())
            for chunk in chunks[1:]:
                await message.reply(chunk, mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, discord.HTTPException) as exc:
            record_usage(self.db, message.guild.id, settings['model'], error=type(exc).__name__)
            print(f'回覆失敗：{type(exc).__name__}', file=sys.stderr)
            try:
                if placeholder:
                    await placeholder.edit(content='模型暫時無法回覆，請稍後再試。')
            except discord.HTTPException:
                pass
        finally:
            self.pending -= 1

    async def on_message(self, message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        settings = load_guild(self.db, message.guild.id)
        mentioned = self.user in message.mentions if self.user else False
        parent_id = getattr(message.channel, 'parent_id', None)
        if not should_reply(settings['mode'], settings['channels'], message.channel.id, parent_id, mentioned):
            return
        if not message.channel.permissions_for(message.guild.me).send_messages:
            return
        content = re.sub(fr'<@!?{self.user.id}>', '', message.content or '').strip()
        if content:
            await self.send_reply(message, content, settings)


bot = KKRobot()


@bot.tree.command(name='chat', description='向 KK Robot 提問，不受自動聊天頻道勾選限制')
@app_commands.describe(text='要詢問的內容')
@app_commands.guild_only()
async def chat(interaction: discord.Interaction, text: str):
    # Slash interactions have no native message to reply to; use followup.
    if interaction.guild is None or interaction.channel is None:
        return
    await interaction.response.defer(thinking=True)
    settings = load_guild(bot.db, interaction.guild.id)
    # Uses the slash invocation's context by fetching up to 50 prior messages.
    proxy = type('MessageProxy', (), {})()
    proxy.guild, proxy.author, proxy.channel = interaction.guild, interaction.user, interaction.channel
    proxy.id = interaction.id
    try:
        answer = await bot.query(proxy, text, settings)
        for i, chunk in enumerate(clean_text_for_discord(answer)):
            await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())
    except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError):
        await interaction.followup.send('AI 暫時無法回答，請確認 Ollama 正常運行。', ephemeral=True)


@bot.tree.command(name='search', description='搜尋網頁（需管理員啟用網路搜尋）')
@app_commands.guild_only()
async def search(interaction: discord.Interaction, query: str):
    settings = load_guild(bot.db, interaction.guild.id)
    if not settings['web_search']:
        await interaction.response.send_message('管理員尚未啟用網路搜尋。', ephemeral=True)
        return
    await interaction.response.defer()
    try:
        result = await search_web(bot.http_session, query)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        result = '搜尋服務暫時不可用。'
    await interaction.followup.send(result[:1900], allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='python', description='隔離執行 Python（需管理員開啟並授權）')
@app_commands.guild_only()
async def python(interaction: discord.Interaction, code: str):
    settings = load_guild(bot.db, interaction.guild.id)
    if not bot.can_execute_python(interaction.user, settings):
        await interaction.response.send_message('Python 執行未啟用，或你沒有權限。', ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    output = await run_python(code)
    await interaction.followup.send('```text\n' + output[:1800].replace('```', '` ` `') + '\n```')


@bot.tree.command(name='forget', description='清除本頻道的 AI 訊息快取說明')
@app_commands.guild_only()
async def forget(interaction: discord.Interaction):
    await interaction.response.send_message(
        'AI 的最近 50 則上下文直接從 Discord 讀取，不在本機儲存快取。'
        '若不希望某則訊息進入上下文，請使用 Discord 權限刪除該訊息。', ephemeral=True)


if __name__ == '__main__':
    if not TOKEN or TOKEN == 'replace_me':
        sys.exit('缺少 DISCORD_BOT_TOKEN，請建立 .env 並設定 Token')
    if len(PASSWORD) < 16:
        sys.exit('ADMIN_PASSWORD 至少要 16 個字元')
    if not OLLAMA_BASE.startswith('http://127.0.0.1:'):
        sys.exit('安全限制：OLLAMA_BASE 必須使用本機回環位址 http://127.0.0.1:PORT')
    bot.run(TOKEN, log_handler=None)
