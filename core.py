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
