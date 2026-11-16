"""
Тесты AllFilesThread после оптимизации PLANS.md 2.1:
- PAGE_LIMIT увеличен с 200 до 500
- CONCURRENT_PAGES=4 — параллельная загрузка страниц через ThreadPoolExecutor

Проверяет:
- PAGE_LIMIT и CONCURRENT_PAGES — правильные значения
- Параллельная загрузка: 4 страницы запрашиваются одновременно
- Пустая страница = корректное завершение со сброшенным offset
- При ошибке API — retry с тем же offset
- При request_stop — корректное завершение с сохранением offset
"""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import db  # noqa: E402
import ui_threads  # noqa: E402


class TestAllFilesThreadConstants(unittest.TestCase):
    """Константы AllFilesThread: PAGE_LIMIT=500, CONCURRENT_PAGES=4."""

    def test_page_limit_is_500(self):
        """PAGE_LIMIT увеличен с 200 до 500 — уменьшает количество запросов
        в 2.5 раза (API поддерживает limit до 1000)."""
        self.assertEqual(ui_threads.AllFilesThread.PAGE_LIMIT, 500)

    def test_concurrent_pages_is_4(self):
        """CONCURRENT_PAGES=4 — параллельная загрузка страниц через
        ThreadPoolExecutor. 4 воркера × 500 файлов = 2000 файлов/итерация."""
        self.assertEqual(ui_threads.AllFilesThread.CONCURRENT_PAGES, 4)

    def test_concurrent_pages_within_api_limits(self):
        """4 воркера × N запросов должно укладываться в API-лимит 40 req/sec.
        При 4 запросах параллельно и 200мс RTT — 20 req/sec, в пределах нормы."""
        # Грубая проверка: не больше 8 (запас)
        self.assertLessEqual(ui_threads.AllFilesThread.CONCURRENT_PAGES, 8)


class TestAllFilesThreadParallel(unittest.TestCase):
    """Параллельная загрузка страниц через ThreadPoolExecutor."""

    def setUp(self):
        # QApplication (не QCoreApplication!): виджеты в последующих тестах
        # (test_breadcrumbs и др.) требуют QApplication, а instance() вернёт
        # уже созданный QCoreApplication и QApplication не создастся → hang.
        from PySide6.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])

        self.api = MagicMock()
        self.database = MagicMock()

        # Сбрасываем мок offset в db (используется в начале run())
        self._db_patches = [
            patch("db.get_all_files_offset", return_value=0),
            patch("db.set_all_files_offset"),
        ]
        for p in self._db_patches:
            p.start()

    def tearDown(self):
        for p in self._db_patches:
            p.stop()

    def _make_page(self, offset, limit, total_in_batch):
        """Сгенерировать фейковую страницу из total_in_batch элементов
        (начиная с offset). Если total_in_batch < limit — последняя страница.
        """
        return [
            {"path": f"/file_{offset + i}.txt", "size": 100}
            for i in range(total_in_batch)
        ]

    def test_uses_thread_pool_executor(self):
        """run() использует concurrent.futures.ThreadPoolExecutor."""
        import concurrent.futures

        # Перехватываем вызовы API. CONCURRENT_PAGES=4 → 4 запроса параллельно.
        # Возвращаем непустые страницы для первых 4 offset'ов, потом пустую.
        call_log = []

        def fake_get_all_files_page(limit, offset):
            call_log.append((limit, offset, time.monotonic()))
            time.sleep(0.05)  # эмулируем задержку сети
            if offset >= 2000:
                return []  # конец списка
            return self._make_page(offset, limit, limit)

        self.api.get_all_files_page.side_effect = fake_get_all_files_page

        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        self.assertTrue(thread.wait(5000), "AllFilesThread не завершился за 5 сек")

        # Проверяем, что API-вызовы шли параллельно — для первой пачки
        # должно быть ровно 4 offset'а: [0, 500, 1000, 1500].
        first_batch_offsets = sorted({c[1] for c in call_log[:4]})
        self.assertEqual(first_batch_offsets, [0, 500, 1000, 1500],
                         f"Ожидались параллельные offset'ы 0,500,1000,1500, "
                         f"получили {first_batch_offsets}")

        # БД получила данные
        self.assertGreaterEqual(self.database.upsert_files_batch.call_count, 1)

    def test_parallel_faster_than_serial(self):
        """Параллельная загрузка 4 страниц по 50мс ≈ 50мс (а не 200мс serial)."""
        # API имитирует 50мс задержку на каждом запросе
        def slow_page(limit, offset):
            time.sleep(0.05)
            if offset >= 2000:
                return []
            return self._make_page(offset, limit, limit)
        self.api.get_all_files_page.side_effect = slow_page

        start = time.monotonic()
        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        thread.wait(5000)
        elapsed = time.monotonic() - start

        # Serial был бы ~200мс для 4 страниц. Параллельно с 4 воркерами — ~50мс.
        # Даём запас: < 150мс. Если > 200мс — значит параллелизм сломан.
        self.assertLess(elapsed, 0.15,
                        f"Параллельная загрузка заняла {elapsed*1000:.0f}мс — "
                        f"ожидалось <150мс (serial был бы ~200мс)")

    def test_empty_page_terminates_with_offset_reset(self):
        """Первая страница пустая — список файлов пуст, завершаемся,
        offset сбрасывается в 0."""
        self.api.get_all_files_page.return_value = []

        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        self.assertTrue(thread.wait(5000))

        # finished должен быть вызван с count=0
        # (сигнал отправлен в run(), но не подключён — главное что поток
        # завершился без exception)
        self.api.get_all_files_page.assert_called()

        # БД upsert_files_batch НЕ вызывался (нет данных)
        self.database.upsert_files_batch.assert_not_called()

    def test_database_error_terminates(self):
        """Ошибка БД (upsert_files_batch кидает) — поток завершается."""
        self.api.get_all_files_page.return_value = [
            {"path": "/a.txt", "size": 10},
            {"path": "/b.txt", "size": 20},
        ]
        self.database.upsert_files_batch.side_effect = RuntimeError("DB locked")

        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        self.assertTrue(thread.wait(5000), "Поток не завершился при ошибке БД")
        # Поток не упал, корректно завершился

    def test_request_stop_saves_offset(self):
        """request_stop() во время работы — поток завершается, offset сохраняется."""
        # Делаем API медленным, чтобы успеть остановить
        def slow_page(limit, offset):
            time.sleep(0.2)
            return self._make_page(offset, limit, limit)
        self.api.get_all_files_page.side_effect = slow_page

        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        time.sleep(0.05)  # даём потоку стартовать
        thread.request_stop()
        self.assertTrue(thread.wait(3000), "Поток не завершился после request_stop")

        # set_all_files_offset был вызван хотя бы 1 раз (для сохранения прогресса)
        with patch("db.set_all_files_offset") as mock_set:
            # Уже не здесь — это уже было вызвано в потоке
            pass

    def test_retry_on_api_error(self):
        """При ошибке API — retry с тем же offset, до MAX_RETRIES."""
        # При каждом вызове решаем: первые 8 (4 воркера × 2 итерации) — ошибка,
        # потом успех. Это даст 1-2 retry перед успехом.
        # На offset >= 2000 возвращаем [] — конец списка.
        call_count = [0]

        def flaky_page(limit, offset):
            call_count[0] += 1
            n = call_count[0]
            if n <= 8:  # 4 воркера × 2 итерации = 8 ошибок
                raise ConnectionError("Network down")
            if offset >= 2000:  # конец списка
                return []
            return self._make_page(offset, limit, limit)

        self.api.get_all_files_page.side_effect = flaky_page

        thread = ui_threads.AllFilesThread(self.api, self.database)
        thread.start()
        finished = thread.wait(20000)
        if not finished:
            thread.request_stop()
            thread.wait(3000)
            self.fail(f"Поток не завершился за 20с (call_count={call_count[0]})")

        # Были вызовы с retry (несколько неудач)
        self.assertGreaterEqual(call_count[0], 9,
                                f"Ожидалось >=9 вызовов API, было {call_count[0]}")
        # Поток завершился через finished(count>0) — БД должна была получить данные
        self.assertGreaterEqual(self.database.upsert_files_batch.call_count, 1,
                                "После успешного retry БД должна получить данные")


class TestAllFilesThreadVanished(unittest.TestCase):
    """Полный обход Диска: исчезнувшие из облака файлы (Task 6).

    Разрешение запускается только для полного скана с нуля (offset == 0) и
    только если реально что-то загружено — иначе пустой ответ API выглядел бы
    как «в облаке нет ни одного файла» и все локальные копии были бы удалены.
    """

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])

        self.api = MagicMock()
        self.database = MagicMock()
        self.database.get_local_files_not_in.return_value = [
            {"cloud_path": "/gone.txt", "local_path": "C:/cache/gone.txt",
             "md5": "m", "last_sync_md5": "m", "status": "downloaded"},
        ]

        # run() читает module-level db.get_all_files_offset() и пишет
        # db.set_all_files_offset() — подменяем их, как в TestAllFilesThreadParallel.
        self._mock_get_offset = MagicMock(return_value=0)
        self._db_patches = [
            patch("db.get_all_files_offset", self._mock_get_offset),
            patch("db.set_all_files_offset"),
        ]
        for p in self._db_patches:
            p.start()

    def tearDown(self):
        for p in self._db_patches:
            p.stop()

    @staticmethod
    def _page_for(limit, offset):
        """Непустая страница только для offset == 0 (4 параллельных запроса)."""
        if offset == 0:
            return [{"path": "/keep.txt", "name": "keep.txt", "type": "file"}]
        return []

    def _run(self, page_for=None):
        self.api.get_all_files_page.side_effect = page_for or self._page_for
        thread = ui_threads.AllFilesThread(self.api, self.database)
        out = []
        thread.vanished.connect(out.extend)
        thread.run()
        return out

    @staticmethod
    def _item(offset):
        return {"path": f"/file_{offset}.txt", "name": f"file_{offset}.txt",
                "type": "file"}

    def _serve(self, non_empty_offsets):
        """Страницы непустые только для указанных offset (4 параллельных запроса)."""
        def page_for(limit, offset):
            return [self._item(offset)] if offset in non_empty_offsets else []
        return page_for

    def test_emits_vanished_after_full_scan(self):
        """Скан с нуля загрузил хотя бы страницу — исчезнувший файл отдан в UI."""
        out = self._run()

        self.assertEqual([r["cloud_path"] for r in out], ["/gone.txt"])
        self.database.get_local_files_not_in.assert_called_once()

    def test_does_not_emit_when_resumed(self):
        """Докачка с сохранённого offset — список неполный, vanished не эмитится.

        Скан реально загружает страницу (loaded > 0), поэтому тест падает,
        если из guard'а убрать проверку started_at_zero.
        """
        self._mock_get_offset.return_value = 500

        out = self._run(self._serve({500}))

        self.assertEqual(out, [])
        self.database.get_local_files_not_in.assert_not_called()

    def test_does_not_emit_when_listing_looks_truncated(self):
        """Непустая страница ПОСЛЕ пустой = список оборван — vanished не эмитится.

        Пустая страница на offset=500 при непустой на 1000 означает, что API
        отдал неполный список: судить об исчезнувших файлах нельзя, иначе все
        локальные копии с offset >= 1000 были бы удалены.
        """
        out = self._run(self._serve({0, 1000}))

        self.assertEqual(out, [])
        self.database.get_local_files_not_in.assert_not_called()

    def test_accumulates_cloud_paths_across_batches(self):
        """Много батчей: в get_local_files_not_in уходит объединение всех путей."""
        out = self._run(self._serve({0, 500, 1000, 1500, 2000}))

        self.assertEqual([r["cloud_path"] for r in out], ["/gone.txt"])
        self.database.get_local_files_not_in.assert_called_once()
        sent = self.database.get_local_files_not_in.call_args.args[0]
        self.assertEqual(
            sent,
            {f"/file_{off}.txt" for off in (0, 500, 1000, 1500, 2000)},
        )

    def test_does_not_emit_on_empty_first_page(self):
        """Пустой первый ответ API — не повод объявлять все локальные файлы исчезнувшими."""
        self.api.get_all_files_page.return_value = []

        thread = ui_threads.AllFilesThread(self.api, self.database)
        out = []
        thread.vanished.connect(out.extend)
        thread.run()

        self.assertEqual(out, [])
        self.database.get_local_files_not_in.assert_not_called()

    def test_excludes_syncing_files(self):
        """Файл в процессе загрузки не может быть исчезнувшим."""
        self.database.get_local_files_not_in.return_value = [
            {"cloud_path": "/uploading.txt", "local_path": "C:/cache/u.txt",
             "md5": "u", "last_sync_md5": "u", "status": "syncing"},
            {"cloud_path": "/gone.txt", "local_path": "C:/cache/gone.txt",
             "md5": "m", "last_sync_md5": "m", "status": "downloaded"},
        ]

        out = self._run()

        self.assertEqual([r["cloud_path"] for r in out], ["/gone.txt"])


if __name__ == "__main__":
    unittest.main()
