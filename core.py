"""Pure helpers for KK Robot, independently testable without Discord."""
import ipaddress
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import re

MODES = {'mention', 'all', 'hybrid'}
MODELS = {'qwen3.5:9b', 'qwen3.5:35b-a3b'}


def should_reply(mode, selected_channels, channel_id, parent_id, mentioned):
    """An empty channel selection NEVER triggers automatic replies."""
    selected = str(channel_id) in selected_channels or (parent_id is not None and str(parent_id) in selected_channels)
    if mode == 'mention':
        return bool(mentioned)
    if mode == 'all':
        return selected
    if mode == 'hybrid':
        return selected or bool(mentioned)
    return False


def safe_http_url(url):
    try:
        parts = urlparse(url)
        if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
            return None
        host = parts.hostname.lower()
        if host in ('localhost', 'localhost.localdomain') or host.endswith(('.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return url
    except (ValueError, TypeError):
        return None


class SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_link = False
        self.title = []
        self.target = None
        self.results = []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        attrs = dict(attrs)
        if 'result__a' not in attrs.get('class', '').split():
            return
        url = attrs.get('href', '')
        if url.startswith('//'):
            url = 'https:' + url
        parsed = urlparse(url)
        if 'duckduckgo.com' in (parsed.hostname or ''):
            url = parse_qs(parsed.query).get('uddg', [url])[0]
        url = safe_http_url(unquote(url))
        if url:
            self.in_link = True
            self.target = url
            self.title = []

    def handle_data(self, data):
        if self.in_link:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.in_link:
            name = re.sub(r'\s+', ' ', ''.join(self.title)).strip()
            if name and self.target and len(self.results) < 6:
                self.results.append({'title': name[:180], 'url': self.target})
            self.in_link = False
            self.target = None


def clean_text_for_discord(text, limit=1900):
    """Prevent mass mentions and return chunks below Discord's message limit."""
    text = (text or '').strip() or '（模型沒有產生文字）'
    text = text.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
    return [text[i:i+limit] for i in range(0, min(len(text), 7600), limit)]


def parse_gpu_csv(raw):
    """Parse nvidia-smi --format=csv,noheader,nounits; may be unavailable under WSL."""
    try:
        name, util, used, total = [x.strip() for x in raw.strip().splitlines()[0].split(',', 3)]
        return {'name': name, 'utilization_percent': float(util), 'memory_used_mib': float(used), 'memory_total_mib': float(total)}
    except (ValueError, IndexError):
        return None


LIVE_KEYWORDS = (
    '幾度', '氣溫', '溫度', '天氣', '氣象', '即時', '目前', '現在',
    '今天', '今日', '明天', '明日', '最新', '新聞', '現價', '股價', '匯率',
    '營業時間', '開門時間', '有開嗎', '目前價格', '即時比分', '賽程',
)


def needs_live_search(text):
    """Questions that require fresh data must not rely on model memory."""
    return any(keyword in (text or '') for keyword in LIVE_KEYWORDS)


def is_taiping_current_temp_question(text):
    """Match the specific Taiping District example, not another city called Taiping."""
    text = (text or '').replace('臺', '台').replace(' ', '')
    return ('台中' in text and '太平' in text and
            any(word in text for word in ('幾度', '氣溫', '溫度', '現在天氣', '目前天氣')))


def format_taiping_weather(data):
    """Only report a fresh, explicit temperature reading from the weather API."""
    from datetime import datetime, timezone, timedelta
    current = data.get('current') if isinstance(data, dict) else None
    units = data.get('current_units') if isinstance(data, dict) else None
    if not isinstance(current, dict) or not isinstance(units, dict):
        return None
    temperature = current.get('temperature_2m')
    timestamp = current.get('time')
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        return None
    if not isinstance(timestamp, str) or units.get('temperature_2m') != '°C':
        return None
    try:
        sample_time = datetime.fromisoformat(timestamp)
        if sample_time.tzinfo is None:
            sample_time = sample_time.replace(tzinfo=timezone(timedelta(hours=8)))
        now_taiwan = datetime.now(timezone(timedelta(hours=8)))
        if abs((now_taiwan - sample_time).total_seconds()) > 7200:
            return None
    except ValueError:
        return None
    source = ('https://api.open-meteo.com/v1/forecast?latitude=24.1265&'
              'longitude=120.7185&current=temperature_2m&timezone=Asia%2FTaipei')
    return (f'台中市太平區附近目前約 **{temperature:g}°C**（{timestamp}，台灣時間）。'
            '\\n這是 Open-Meteo 對附近網格的即時估算，並非太平區各地的實測溫度。'
            f'\\n資料來源：{source}')
