# -*- coding: utf-8 -*-
"""V2.9.29.8 - only verified contacts to THIS HTTP proxy are presence proof.

ARP, mDNS, saved plist, old binding, or Apple pairing are never proof that the
phone still uses this PC:port. This is an inactivity lease, not a way to read
iOS proxy settings. A proxy heartbeat/companion would be needed for exact instant
proxy-settings changes and idle phones.
"""
import threading
import time
import ipaddress

DEFAULT_TTL_SECONDS = 90.0


def is_candidate_client_ip(ip, local_ip='', gateway_ip=''):
    try:
        addr = ipaddress.ip_address(str(ip))
        return (addr.version == 4 and not addr.is_loopback and not addr.is_multicast
                and str(addr) not in (str(local_ip), str(gateway_ip)))
    except (TypeError, ValueError):
        return False


class GatewayPresence:
    def __init__(self, ttl=DEFAULT_TTL_SECONDS, clock=None):
        self.ttl = float(ttl)
        self.clock = clock or time.monotonic
        self._last = {}
        self._lock = threading.RLock()

    def touch(self, ip):
        if not is_candidate_client_ip(ip):
            return False
        with self._lock:
            self._last[str(ip)] = self.clock()
        return True

    def known(self, ip):
        # 是否曾经真正访问过本机代理（即使已超过 TTL）；线程安全。
        with self._lock:
            return str(ip) in self._last

    def active(self, ip):
        with self._lock:
            last = self._last.get(str(ip))
            return last is not None and self.clock() - last < self.ttl

    def visible_ips(self):
        now = self.clock()
        with self._lock:
            return {ip for ip, last in self._last.items() if now-last < self.ttl}

    def expire(self):
        now = self.clock()
        with self._lock:
            expired = [ip for ip, last in self._last.items() if now-last >= self.ttl]
            for ip in expired:
                self._last.pop(ip, None)
            return expired

    def forget(self, ip):
        with self._lock:
            self._last.pop(str(ip), None)

    def clear(self):
        with self._lock:
            self._last.clear()
