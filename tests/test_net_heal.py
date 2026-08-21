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
