"""
Авто-обход сетевых блокировок (DNS-over-HTTPS + кэш IP).

Чистая логика без Qt. HealedAdapter монтируется на сессию
disk_api.YaDiskAPI и вызывается из воркер-потоков.

Схема:
  1. Системный DNS пробуется всегда первым (пока он работает — обход невидим).
  2. При ошибке резолвинга/TCP — классификация (classify_error).
  3. Если пользователь включил обход — IP берутся из IpCache или DoHResolver.
  4. Соединение открывается на IP, TLS SNI и проверка сертификата —
     по исходному имени хоста.
  5. Если помочь не удалось — наружу уходит NetBlockError, а факт
     блокировки пишется в очередь событий для UI (drain_block_events).
"""

import collections
import enum
import logging
import socket
import threading
import time
from typing import Callable, Iterator, NoReturn, Optional
from urllib.parse import quote

import requests
import urllib3.exceptions
from requests.adapters import HTTPAdapter

logger = logging.getLogger("net_heal")

DOH_TIMEOUT_S = 5.0
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://8.8.8.8/resolve",
)
TTL_MIN_S = 60
TTL_MAX_S = 3600


class NetIssue(enum.Enum):
    DnsBlocked = "dns"
    TcpBlocked = "tcp"


class NetBlockError(Exception):
    """Исчерпаны все способы достучаться до хоста (блокировка сети)."""

    def __init__(self, kind: NetIssue, host: str):
        super().__init__(f"{kind.value}-blocked: {host}")
        self.kind = kind
        self.host = host


_DNS_MARKERS = (
    "getaddrinfo failed",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
)


def _iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """exc + вложенные исключения (args, __cause__, __context__) без циклов.

    requests оборачивает ошибки urllib3 первым аргументом (не через
    __cause__), поэтому обходим и args.
    """
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for arg in cur.args:
            if isinstance(arg, BaseException):
                stack.append(arg)
        for attr in ("__cause__", "__context__"):
            nxt = getattr(cur, attr, None)
            if isinstance(nxt, BaseException):
                stack.append(nxt)


def classify_error(exc: BaseException) -> Optional[NetIssue]:
    """Классифицировать сетевое исключение requests/urllib3.

    DnsBlocked — имя не резолвится (NameResolutionError / gaierror);
    TcpBlocked — TCP reset / protocol error / таймаут соединения;
    None — прочее (HTTP-коды, SSL, таймаут чтения — обычные ошибки).
    """
    for cur in _iter_exception_chain(exc):
        if isinstance(cur, (urllib3.exceptions.NameResolutionError,
                            socket.gaierror)):
            return NetIssue.DnsBlocked
        if isinstance(cur, (ConnectionResetError,
                            urllib3.exceptions.ProtocolError)):
            return NetIssue.TcpBlocked
        if isinstance(cur, requests.exceptions.ConnectTimeout):
            return NetIssue.TcpBlocked
        msg = str(cur).lower()
        if any(marker in msg for marker in _DNS_MARKERS):
            return NetIssue.DnsBlocked
    return None


# ── события блокировок для UI ────────────────────────────

_events_lock = threading.Lock()
_events: collections.deque = collections.deque(maxlen=50)


def record_block_event(kind: NetIssue, host: str) -> None:
    """Записать факт блокировки (потокобезопасно, из воркер-потоков)."""
    with _events_lock:
        _events.append((time.monotonic(), kind, host))


def drain_block_events() -> list[tuple[float, NetIssue, str]]:
    """Забрать накопившиеся события (вызывается из GUI-потока)."""
    with _events_lock:
        out = list(_events)
        _events.clear()
    return out


# ── кэш IP ───────────────────────────────────────────────


class IpCache:
    """Потокобезопасный кэш «хост → список рабочих IP» с TTL.

    Отрицательное кэширование: IP, на котором соединение упало, удаляется
    до конца TTL (mark_bad) — следующий запрос возьмёт другой IP или
    перерезолвит через DoH (ротация CDN-адресов Яндекса).
    """

    def __init__(self, time_fn: Callable[[], float] = time.monotonic):
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._data: dict[str, tuple[list[str], float]] = {}

    def get(self, host: str) -> list[str]:
        """Свежие IP хоста (копия списка) или []."""
        with self._lock:
            entry = self._data.get(host)
            if entry is None:
                return []
            ips, expires_at = entry
            if self._time_fn() >= expires_at:
                del self._data[host]
                return []
            return list(ips)

    def put(self, host: str, ips: list[str], ttl_s: int) -> None:
        ttl = max(TTL_MIN_S, min(int(ttl_s), TTL_MAX_S))
        with self._lock:
            self._data[host] = (list(ips), self._time_fn() + ttl)

    def mark_bad(self, host: str, ip: str) -> None:
        """Пометить IP нерабочим до конца TTL."""
        with self._lock:
            entry = self._data.get(host)
            if entry is None:
                return
            ips, expires_at = entry
            remaining = [x for x in ips if x != ip]
            if remaining:
                self._data[host] = (remaining, expires_at)
            else:
                del self._data[host]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
