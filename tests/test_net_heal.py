"""
Тесты net_heal (авто-обход DNS-блокировок). Без реальной сети.
"""

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
import requests.exceptions as req_exc
import urllib3.connection
import urllib3.exceptions as ue

import db
import disk_api
import net_heal
from net_heal import (
    NetBlockError,
    NetIssue,
    classify_error,
    drain_block_events,
    record_block_event,
)


def _nre(host="blocked.test"):
    """urllib3 NameResolutionError, как при SERVFAIL/перехвате UDP53."""
    return ue.NameResolutionError(
        host, None, socket.gaierror(11001, "getaddrinfo failed"))


class TestClassifyError:
    def test_dns_via_name_resolution_error(self):
        # requests оборачивает urllib3-ошибку первым аргументом (не __cause__)
        exc = req_exc.ConnectionError(_nre())
        assert classify_error(exc) == NetIssue.DnsBlocked

    def test_dns_via_cause_chain(self):
        inner = _nre()
        outer = req_exc.ConnectionError("nope")
        outer.__cause__ = inner
        assert classify_error(outer) == NetIssue.DnsBlocked

    def test_dns_bare_gaierror(self):
        assert classify_error(socket.gaierror(11001, "getaddrinfo failed")) \
            == NetIssue.DnsBlocked

    def test_dns_by_message_marker(self):
        exc = req_exc.ConnectionError(
            "[WinError 11001] getaddrinfo failed for cloud-api.yandex.net")
        assert classify_error(exc) == NetIssue.DnsBlocked

    def test_tcp_connection_reset(self):
        assert classify_error(ConnectionResetError()) == NetIssue.TcpBlocked

    def test_tcp_wrapped_reset(self):
        assert classify_error(req_exc.ConnectionError(ConnectionResetError())) \
            == NetIssue.TcpBlocked

    def test_tcp_protocol_error(self):
        err = ue.ProtocolError("Connection aborted", ConnectionResetError())
        assert classify_error(err) == NetIssue.TcpBlocked

    def test_tcp_connect_timeout(self):
        assert classify_error(req_exc.ConnectTimeout("connect timeout")) \
            == NetIssue.TcpBlocked

    def test_none_for_http_error(self):
        assert classify_error(req_exc.HTTPError("404 Not Found")) is None

    def test_none_for_ssl_error(self):
        # SSLError наследует requests.ConnectionError — но это НЕ блокировка
        assert classify_error(req_exc.SSLError("cert verify failed")) is None

    def test_none_for_read_timeout(self):
        assert classify_error(req_exc.ReadTimeout("read timed out")) is None


class TestBlockEvents:
    def test_record_and_drain(self):
        record_block_event(NetIssue.DnsBlocked, "a.test")
        record_block_event(NetIssue.TcpBlocked, "b.test")
        events = drain_block_events()
        kinds = [k for _ts, k, _h in events]
        hosts = [h for _ts, _k, h in events]
        assert kinds == [NetIssue.DnsBlocked, NetIssue.TcpBlocked]
        assert hosts == ["a.test", "b.test"]
        # после drain очередь пуста
        assert drain_block_events() == []

    def test_net_block_error_carries_kind_and_host(self):
        err = NetBlockError(NetIssue.DnsBlocked, "x.test")
        assert err.kind == NetIssue.DnsBlocked
        assert err.host == "x.test"
        assert not isinstance(err, requests.RequestException)


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class TestIpCache:
    def _make(self):
        from net_heal import IpCache
        self.clock = FakeClock()
        return IpCache(time_fn=self.clock)

    def test_put_get_roundtrip(self):
        cache = self._make()
        cache.put("h.test", ["1.1.1.1", "2.2.2.2"], ttl_s=300)
        assert cache.get("h.test") == ["1.1.1.1", "2.2.2.2"]

    def test_ttl_expiry(self):
        cache = self._make()
        cache.put("h.test", ["1.1.1.1"], ttl_s=300)
        self.clock.now += 301
        assert cache.get("h.test") == []

    def test_ttl_clamped_to_bounds(self):
        cache = self._make()
        cache.put("low.test", ["1.1.1.1"], ttl_s=5)      # → минимум 60с
        self.clock.now += 61
        assert cache.get("low.test") == []
        cache.put("high.test", ["1.1.1.1"], ttl_s=99999)  # → максимум 3600с
        self.clock.now += 3601
        assert cache.get("high.test") == []

    def test_mark_bad_removes_ip_until_expiry(self):
        cache = self._make()
        cache.put("h.test", ["1.1.1.1", "2.2.2.2"], ttl_s=300)
        cache.mark_bad("h.test", "1.1.1.1")
        assert cache.get("h.test") == ["2.2.2.2"]
        cache.mark_bad("h.test", "2.2.2.2")
        assert cache.get("h.test") == []          # список пуст → перерезолв
        # после истечения TTL запись исчезла полностью
        cache.put("h.test", ["3.3.3.3"], ttl_s=300)
        self.clock.now += 301
        cache.mark_bad("h.test", "3.3.3.3")
        assert cache.get("h.test") == []

    def test_unknown_host_and_ip_are_noop(self):
        cache = self._make()
        cache.mark_bad("ghost.test", "1.1.1.1")
        assert cache.get("ghost.test") == []

    def test_thread_safety_smoke(self):
        import threading
        cache = self._make()
        errors: list[Exception] = []

        def worker(n):
            try:
                for i in range(200):
                    host = f"h{n}.test"
                    cache.put(host, [f"10.0.{n}.{i % 250}"], ttl_s=300)
                    cache.get(host)
                    cache.mark_bad(host, f"10.0.{n}.{i % 250}")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class FakeResp:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class FakeSession:
    """Подменяет requests.Session: очередь ответов по порядку вызовов."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _doh_answer(*answers, status=0):
    return {"Status": status, "Answer": [
        {"name": "s41nrg.storage.yandex.net.", "type": t, "TTL": ttl,
         "data": data} for (t, ttl, data) in answers]}


class TestDoHResolver:
    def _make(self, responses):
        from net_heal import DoHResolver
        session = FakeSession(responses)
        return DoHResolver(session=session), session

    def test_primary_endpoint_answer(self):
        resolver, session = self._make([
            FakeResp(_doh_answer((1, 300, "37.140.178.41"))),
        ])
        ips, ttl = resolver.resolve("s41nrg.storage.yandex.net")
        assert ips == ["37.140.178.41"]
        assert ttl == 300
        assert len(session.calls) == 1
        assert "name=s41nrg.storage.yandex.net" in session.calls[0]

    def test_fallback_to_second_endpoint(self):
        resolver, session = self._make([
            requests.Timeout("primary dead"),               # 1.1.1.1 упал
            FakeResp(_doh_answer((1, 120, "77.88.8.8"))),   # 8.8.8.8 ответил
        ])
        ips, ttl = resolver.resolve("h.test")
        assert ips == ["77.88.8.8"]
        assert ttl == 120
        assert len(session.calls) == 2

    def test_all_endpoints_failed_raises_net_block_error(self):
        resolver, session = self._make([
            requests.ConnectTimeout("t"), requests.ConnectionError("c"),
        ])
        with pytest.raises(NetBlockError) as ei:
            resolver.resolve("h.test")
        assert ei.value.kind == NetIssue.DnsBlocked
        assert len(session.calls) == 2

    def test_filters_non_a_records_and_bad_data(self):
        resolver, _ = self._make([
            FakeResp(_doh_answer(
                (5, 60, "cname.yandex.net."),        # CNAME — мимо
                (28, 60, "2a02:6b8::1"),             # AAAA — мимо (вне объёма)
                (1, 90, "37.140.178.41"),            # A — берём
                (1, 90, "not-an-ip"),                # мусор — мимо
            )),
        ])
        ips, ttl = resolver.resolve("h.test")
        assert ips == ["37.140.178.41"]
        assert ttl == 90

    def test_empty_answer_returns_empty_list(self):
        resolver, _ = self._make([FakeResp({"Status": 3})])  # NXDOMAIN
        ips, ttl = resolver.resolve("ghost.test")
        assert ips == []
        assert ttl == 60

    def test_ttl_clamped(self):
        resolver, _ = self._make([FakeResp(_doh_answer((1, 10, "1.1.1.1")))])
        _, ttl = resolver.resolve("h.test")
        assert ttl == 60
        resolver2, _ = self._make([FakeResp(_doh_answer((1, 99999, "1.1.1.1")))])
        _, ttl2 = resolver2.resolve("h.test")
        assert ttl2 == 3600

    def test_broken_json_treated_as_failure(self):
        resolver, session = self._make([
            FakeResp(ValueError("bad json")),
            FakeResp(_doh_answer((1, 60, "1.1.1.1"))),
        ])
        ips, _ = resolver.resolve("h.test")
        assert ips == ["1.1.1.1"]
        assert len(session.calls) == 2

    def test_non_dict_json_treated_as_failure(self):
        resolver, session = self._make([
            FakeResp([1, 2]),                               # мусор вместо JSON-объекта
            FakeResp(_doh_answer((1, 60, "1.1.1.1"))),
        ])
        ips, _ = resolver.resolve("h.test")
        assert ips == ["1.1.1.1"]
        assert len(session.calls) == 2

    def test_bad_ttl_value_treated_as_failure(self):
        resolver, session = self._make([
            FakeResp({"Status": 0, "Answer": [
                {"name": "h.test.", "type": 1, "TTL": "abc",
                 "data": "1.1.1.1"}]}),
            FakeResp(_doh_answer((1, 60, "1.1.1.1"))),
        ])
        ips, _ = resolver.resolve("h.test")
        assert ips == ["1.1.1.1"]
        assert len(session.calls) == 2


@pytest.fixture(autouse=True)
def _clear_events():
    """Очередь событий net_heal — общая на модуль; чистим после каждого теста."""
    yield
    drain_block_events()


@pytest.fixture
def heal_state():
    """Чистый STATE + стаб-резолвер на каждый тест."""
    from net_heal import HealState, IpCache
    saved = net_heal.STATE
    state = HealState()
    state.cache = IpCache()
    net_heal.STATE = state
    yield state
    net_heal.STATE = saved


class StubResolver:
    def __init__(self, result=None, exc=None):
        self.result = result or ([], 60)
        self.exc = exc
        self.calls: list[str] = []

    def resolve(self, host):
        self.calls.append(host)
        if self.exc:
            raise self.exc
        return self.result


class TestHealedConnection:
    def _conn(self, host="blocked.test"):
        return net_heal.HealedHTTPSConnection(host=host)

    def test_system_dns_first_then_cached_ip(self, heal_state, monkeypatch):
        """1-й вызов — системный DNS (падает), 2-й — коннект на IP из кэша."""
        heal_state.enabled_fn = lambda: True
        heal_state.resolver = StubResolver(result=(["93.184.216.34"], 300))
        calls = []

        def fake_new_conn(self):
            # urllib3 2.x: host — property над _dns_host, во время подмены
            # .host читает IP; оригинал гарантируется восстановлением в finally
            # (см. TestSniPreserved).
            calls.append(self._dns_host)
            if len(calls) == 1:
                raise _nre(self.host)
            return object()  # sentinel «сокет»

        monkeypatch.setattr(urllib3.connection.HTTPConnection,
                            "_new_conn", fake_new_conn)
        conn = self._conn()
        sock = conn._new_conn()
        assert isinstance(sock, object)
        assert calls[0] == "blocked.test"   # системный DNS
        assert calls[1] == "93.184.216.34"  # коннект на IP
        # подмена _dns_host откатана — последующие операции видят хост
        assert conn._dns_host == "blocked.test"
        # IP попал в кэш
        assert heal_state.cache.get("blocked.test") == ["93.184.216.34"]

    def test_disabled_flag_raises_immediately_and_records_event(
            self, heal_state, monkeypatch):
        heal_state.enabled_fn = lambda: False
        heal_state.resolver = StubResolver(result=(["1.1.1.1"], 60))
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn",
                            lambda self: (_ for _ in ()).throw(_nre()))
        conn = self._conn()
        with pytest.raises(NetBlockError) as ei:
            conn._new_conn()
        assert ei.value.kind == NetIssue.DnsBlocked
        assert ei.value.host == "blocked.test"
        events = drain_block_events()
        assert len(events) == 1
        assert events[0][1] == NetIssue.DnsBlocked
        # DoH даже не дёргали
        assert heal_state.resolver.calls == []

    def test_unclassified_error_passes_through(self, heal_state, monkeypatch):
        heal_state.enabled_fn = lambda: True
        ssl_err = req_exc.SSLError("cert verify failed")
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn",
                            lambda self: (_ for _ in ()).throw(ssl_err))
        conn = self._conn()
        with pytest.raises(req_exc.SSLError):
            conn._new_conn()
        assert drain_block_events() == []

    def test_all_ips_bad_raises_and_marks(self, heal_state, monkeypatch):
        heal_state.enabled_fn = lambda: True
        heal_state.resolver = StubResolver(result=(["1.1.1.1", "2.2.2.2"], 300))

        attempts = []

        def fake_new_conn(self):
            # 1-й вызов — системный DNS, далее — попытки по IP из кэша.
            attempts.append(self._dns_host)
            if len(attempts) == 1:
                raise _nre(self.host)
            raise ConnectionResetError("reset")

        monkeypatch.setattr(urllib3.connection.HTTPConnection,
                            "_new_conn", fake_new_conn)
        conn = self._conn()
        with pytest.raises(NetBlockError) as ei:
            conn._new_conn()
        assert ei.value.kind == NetIssue.DnsBlocked
        assert heal_state.cache.get("blocked.test") == []  # оба помечены плохими
        kinds = [k for _t, k, _h in drain_block_events()]
        assert kinds == [NetIssue.DnsBlocked]

    def test_doh_dead_gives_up_with_original_kind(self, heal_state,
                                                  monkeypatch):
        heal_state.enabled_fn = lambda: True
        heal_state.resolver = StubResolver(exc=NetBlockError(
            NetIssue.DnsBlocked, "blocked.test"))
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn",
                            lambda self: (_ for _ in ()).throw(_nre()))
        conn = self._conn()
        with pytest.raises(NetBlockError):
            conn._new_conn()

    def test_tcp_blocked_kind_preserved(self, heal_state, monkeypatch):
        heal_state.enabled_fn = lambda: False
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn",
                            lambda self: (_ for _ in ())
                            .throw(ConnectionResetError()))
        conn = self._conn()
        with pytest.raises(NetBlockError) as ei:
            conn._new_conn()
        assert ei.value.kind == NetIssue.TcpBlocked

    def test_proxy_traffic_not_healed(self, heal_state, monkeypatch):
        heal_state.enabled_fn = lambda: True
        heal_state.resolver = StubResolver(result=(["1.1.1.1"], 60))
        monkeypatch.setattr(urllib3.connection.HTTPConnection, "_new_conn",
                            lambda self: (_ for _ in ()).throw(_nre()))
        conn = self._conn()
        conn.proxy = "http://proxy.local:3128"
        with pytest.raises(ue.NameResolutionError):
            conn._new_conn()
        assert heal_state.resolver.calls == []


class TestSniPreserved:
    def _conn(self, host="blocked.test"):
        return net_heal.HealedHTTPSConnection(host=host)

    def test_wrap_socket_gets_original_hostname(self, heal_state, monkeypatch):
        """Полный connect(): wrap_socket получает server_hostname=исходный хост,
        хотя сокет открыт на IP из кэша."""
        captured = {}

        def fake_wrap(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(socket=MagicMock(), is_verified=True)

        monkeypatch.setattr(urllib3.connection,
                            "_ssl_wrap_socket_and_match_hostname", fake_wrap)
        monkeypatch.setattr(urllib3.connection.http2_probe,
                            "acquire_and_get", lambda **kw: True)

        first_attempt = []

        def fake_new_conn(self):
            if not first_attempt:
                # 1-й вызов — системный DNS по имени; далее — попытки по IP.
                first_attempt.append(self._dns_host)
                raise _nre(self.host)
            return MagicMock()  # «сокет», подключённый к IP

        monkeypatch.setattr(urllib3.connection.HTTPConnection,
                            "_new_conn", fake_new_conn)
        heal_state.enabled_fn = lambda: True
        heal_state.resolver = StubResolver(result=(["93.184.216.34"], 300))
        conn = self._conn()
        conn.connect()
        assert captured["server_hostname"] == "blocked.test"


class TestHealedAdapterWiring:
    def test_adapter_uses_healed_pool_for_https(self, heal_state):
        adapter = net_heal.HealedAdapter(pool_connections=2, pool_maxsize=2)
        pool = adapter.poolmanager.connection_from_url("https://x.test/")
        assert isinstance(pool, net_heal._HealedHTTPSConnectionPool)
        assert pool.ConnectionCls is net_heal.HealedHTTPSConnection

    def test_adapter_publishes_state(self, heal_state):
        cache = net_heal.IpCache()
        resolver = StubResolver()
        fn = lambda: True  # noqa: E731
        net_heal.HealedAdapter(enabled_fn=fn, cache=cache, resolver=resolver)
        assert net_heal.STATE.enabled_fn is fn
        assert net_heal.STATE.cache is cache
        assert net_heal.STATE.resolver is resolver


class TestDiskApiIntegration:
    def test_mounts_healed_adapter_when_fn_passed(self, heal_state):
        api = disk_api.YaDiskAPI("tok", net_heal_enabled_fn=lambda: True)
        adapter = api._session.get_adapter(
            "https://cloud-api.yandex.net/v1/disk")
        assert isinstance(adapter, net_heal.HealedAdapter)

    def test_plain_adapter_without_fn(self, heal_state):
        api = disk_api.YaDiskAPI("tok")
        adapter = api._session.get_adapter(
            "https://cloud-api.yandex.net/v1/disk")
        assert not isinstance(adapter, net_heal.HealedAdapter)
        assert isinstance(adapter, requests.adapters.HTTPAdapter)
