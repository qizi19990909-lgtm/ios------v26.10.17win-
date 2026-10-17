# -*- coding: utf-8 -*-
"""城市名称翻译缓存。只发送城市文本，不上传出口IP/账号/代理参数。"""
from __future__ import annotations
import json
import os
import re
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ZH = re.compile(r'[\u4e00-\u9fff]')


def translate_city(city):
    """Best effort remote translation. Never guess/replace real city identities."""
    city = str(city or '').strip()
    if not city or len(city) > 100 or _ZH.search(city):
        return city
    # Google public translation endpoint, no account needed; results are untrusted,
    # validated, and never used for geo coordinates or proxy routing.
    url = ('https://translate.googleapis.com/translate_a/single?' + urlencode({
        'client': 'gtx', 'sl': 'en', 'tl': 'zh-CN', 'dt': 't', 'q': city}))
    try:
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 QiziCityTranslator/1.0'})
        with urlopen(request, timeout=4.0) as response:
            raw = response.read(16384)
        payload = json.loads(raw.decode('utf-8'))
        translated = ''.join(str(x[0]) for x in payload[0] if isinstance(x, list) and x and x[0])
        translated = translated.strip()
        if translated and len(translated) < 100 and _ZH.search(translated):
            return translated
    except (OSError, ValueError, TypeError, IndexError, KeyError):
        pass
    return ''


class CityTranslationCache:
    def __init__(self, path):
        self.path = Path(path)
        self.cache = {}
        self.pending = set()
        self.retry_after = {}
        self.jobs = Queue(maxsize=300)
        self.results = Queue()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(raw, dict): self.cache = raw
        except (OSError, ValueError):
            pass
        self.lock = threading.Lock()
        threading.Thread(target=self._work, name='qizi-city-translate', daemon=True).start()

    @staticmethod
    def key(city, country=''):
        return str(country or '').strip().casefold() + '\u0001' + str(city or '').strip().casefold()

    def get(self, city, country=''):
        city = str(city or '').strip()
        key = self.key(city, country)
        if not city or _ZH.search(city): return city
        if key in self.cache: return self.cache[key]
        with self.lock:
            if key not in self.pending and time.time() >= self.retry_after.get(key, 0):
                self.pending.add(key)
                try: self.jobs.put_nowait((key, city))
                except Exception: self.pending.discard(key)
        return city  # Always display the real original until translation is verified.

    def _work(self):
        while True:
            key, city = self.jobs.get()
            result = translate_city(city)
            self.results.put((key, result))
            time.sleep(0.75)  # Soft rate limit, background only.

    def drain(self, limit=30):
        changed = False
        for _ in range(limit):
            try: key, result = self.results.get_nowait()
            except Empty: break
            with self.lock:
                self.pending.discard(key)
                if result:
                    if self.cache.get(key) != result:
                        self.cache[key] = result
                        changed = True
                else:
                    self.retry_after[key] = time.time() + 3600
        if changed:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temp, self.path)
            except OSError:
                pass  # Cache failure must not affect connectivity.
        return changed
