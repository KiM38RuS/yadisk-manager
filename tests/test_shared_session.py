"""
Тесты общей requests.Session (disk_api.get_shared_session).

См. PLANS.md 2.15: голые requests.get() в updater.py и net_heal.DoHResolver
создавали дефолтную сессию с maxsize=1 — при параллельных обращениях
urllib3 лог-засорял 'Connection pool is full, discarding connection'.
get_shared_session() возвращает singleton с HTTPAdapter(pool_maxsize=30).

Проверяет:
- singleton: повторные вызовы возвращают тот же объект
- pool_maxsize=30 на обоих схемах (http:// и https://)
- при явной передаче session в DoHResolver — он используется как есть
- при session=None — DoHResolver всё равно создаёт сессию с пулом > 1
- при многократном вызове get_shared_session() — НЕ создаются новые адаптеры
- YaDiskAPI НЕ делит сессию с updater/DoH: OAuth-токен Яндекса остаётся
  в приватной сессии и не уходит на api.github.com
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# PySide6 нужен для импорта disk_api (YaDiskAPI наследует не от QObject, но
# модуль использует requests — offscreen не нужен, ставим на всякий случай)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import disk_api
import net_heal


class TestGetSharedSession(unittest.TestCase):
    def setUp(self):
        """Сбросить singleton перед каждым тестом (иначе тесты зависят
        от порядка запуска)."""
        disk_api._shared_session = None

    def test_returns_session(self):
        s = disk_api.get_shared_session()
        self.assertIsInstance(s, __import__("requests").Session)

    def test_singleton(self):
        """Повторные вызовы возвращают тот же объект (иначе теряется смысл)."""
        a = disk_api.get_shared_session()
        b = disk_api.get_shared_session()
        self.assertIs(a, b)

    def test_singleton_thread_safe(self):
        """Параллельные вызовы из разных потоков дают один и тот же объект."""
        import threading
        results = []
        def worker():
            results.append(disk_api.get_shared_session())
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {id(s) for s in results}
        self.assertEqual(len(ids), 1, "Все потоки должны получить одну сессию")

    def test_https_adapter_pool_size(self):
        """HTTPS-адаптер имеет pool_maxsize = POOL_SIZE (по умолчанию 30)."""
        s = disk_api.get_shared_session()
        adapter = s.get_adapter("https://cloud-api.yandex.net/v1/disk")
        self.assertEqual(adapter.poolmanager.connection_pool_kw.get("maxsize"),
                         disk_api.POOL_SIZE)

    def test_http_adapter_pool_size(self):
        """HTTP-адаптер тоже смонтирован с тем же POOL_SIZE."""
        s = disk_api.get_shared_session()
        adapter = s.get_adapter("http://example.com")
        self.assertEqual(adapter.poolmanager.connection_pool_kw.get("maxsize"),
                         disk_api.POOL_SIZE)

    def test_pool_size_constant_is_30(self):
        """POOL_SIZE = 30 — то же значение, что в YaDiskAPI для согласованности."""
        # Если кто-то поменяет в одном месте — тест напомнит про второй.
        self.assertEqual(disk_api.POOL_SIZE, 30)


class TestDoHResolverPool(unittest.TestCase):
    """net_heal.DoHResolver при session=None должен создать сессию с пулом > 1,
    а не с дефолтным maxsize=1 (иначе при сбое резолвера несколько потоков
    сериализуются на одном коннекте к DoH-эндпоинтам)."""

    def test_default_session_has_pool_above_one(self):
        resolver = net_heal.DoHResolver()
        adapter = resolver._session.get_adapter("https://1.1.1.1/")
        maxsize = adapter.poolmanager.connection_pool_kw.get("maxsize")
        self.assertIsNotNone(maxsize, "Адаптер DoH-сессии не имеет maxsize")
        self.assertGreater(maxsize, 1,
                           f"DoH пул = {maxsize} — это создаёт 'Connection "
                           f"pool is full' warning при параллельных резолвах")

    def test_explicit_session_is_used_as_is(self):
        """Если session передан — DoHResolver НЕ создаёт свою сессию."""
        import requests
        own = requests.Session()
        resolver = net_heal.DoHResolver(session=own)
        self.assertIs(resolver._session, own)


class TestYaDiskApiSessionIsolation(unittest.TestCase):
    """YaDiskAPI держит собственную сессию.

    Общая сессия (get_shared_session) обслуживает updater (api.github.com) и
    DoHResolver. Если YaDiskAPI ставит на неё 'Authorization: OAuth <token>',
    токен Яндекса уходит на GitHub. Здесь проверяем, что сессии раздельные,
    а пул приватной сессии при этом не деградировал до urllib3-дефолта 1.
    """

    def setUp(self):
        """Сбросить singleton, чтобы тесты не зависели от порядка запуска."""
        disk_api._shared_session = None

    def test_api_session_is_not_shared_session(self):
        api = disk_api.YaDiskAPI("tok", net_heal_enabled_fn=lambda: True)
        self.assertIsNot(api._session, disk_api.get_shared_session())

    def test_shared_session_has_no_oauth_token(self):
        disk_api.YaDiskAPI("tok", net_heal_enabled_fn=lambda: True)
        self.assertNotIn("Authorization",
                         disk_api.get_shared_session().headers)

    def test_private_session_carries_oauth_token(self):
        api = disk_api.YaDiskAPI("tok", net_heal_enabled_fn=lambda: True)
        auth = api._session.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("OAuth "),
                        f"Приватная сессия должна носить OAuth-токен, "
                        f"а несёт: {auth!r}")

    def test_heal_adapter_pool_matches_pool_size(self):
        """Реальный maxsize пула heal-адаптера равен POOL_SIZE.

        Регресс: пул = 1 → спам 'Connection pool is full, discarding
        connection: cloud-api.yandex.net. Connection pool size: 1'.
        """
        api = disk_api.YaDiskAPI("tok", net_heal_enabled_fn=lambda: True)
        url = "https://cloud-api.yandex.net/v1/disk"
        adapter = api._session.get_adapter(url)
        pool = adapter.poolmanager.connection_from_url(url)
        self.assertEqual(pool.pool.maxsize, disk_api.POOL_SIZE)

    def test_plain_adapter_pool_above_one(self):
        """Без heal-обхода адаптер тоже получает пул > 1 (не дефолт urllib3)."""
        api = disk_api.YaDiskAPI("tok")
        url = "https://cloud-api.yandex.net/v1/disk"
        adapter = api._session.get_adapter(url)
        pool = adapter.poolmanager.connection_from_url(url)
        self.assertGreater(pool.pool.maxsize, 1)


if __name__ == "__main__":
    unittest.main()
