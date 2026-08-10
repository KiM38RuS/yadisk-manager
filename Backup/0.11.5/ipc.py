"""
IPC — управление YaDisk Manager из командной строки.

Сервер (внутри процесса):
    start_ipc_server(window_ref) — запускает фоновый поток, слушающий TCP 127.0.0.1:43210

Клиент (из любого shell/терминала):
    send_ipc_command('restart')  → перезапускает приложение
    send_ipc_command('raise')    → поднимает окно на передний план
    send_ipc_command('shutdown') → корректно завершает программу (без мёртвого значка в трее)
"""

import json
import logging
import socket
import threading
from queue import Queue

logger = logging.getLogger("ipc")

# ⚠️ Перед релизом/компиляцией выставить False
# В разработке держать True — позволяет перезагружать программу
# через: python -c "from ipc import send_ipc_command; send_ipc_command('restart')"
IPC_ENABLED = True

IPC_PORT = 43210

_command_queue: Queue = Queue()


# ── Server ─────────────────────────────────────────────────

def start_ipc_server(window_ref) -> None:
    """Запустить IPC-сервер в фоновом daemon-потоке."""
    _window_ref = window_ref  # замыкание для доступа из потока

    def _serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(('127.0.0.1', IPC_PORT))
        except OSError as e:
            logger.warning("IPC port %d busy: %s — сервер не запущен", IPC_PORT, e)
            return
        srv.listen(5)
        srv.settimeout(1.0)
        logger.info("IPC server listening on 127.0.0.1:%d", IPC_PORT)
        while True:
            try:
                conn, addr = srv.accept()
                with conn:
                    raw = conn.recv(4096).decode('utf-8').strip()
                    cmd = json.loads(raw)
                    action = cmd.get("action", "")
                    logger.info("IPC command: %s", action)
                    _command_queue.put(action)
                    conn.sendall(b'{"status":"ok"}')
            except socket.timeout:
                continue
            except Exception as e:
                logger.error("IPC error: %s", e)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()


def poll_command() -> str:
    """Проверить очередь команд (вызывать из главного потока Qt по таймеру).

    Возвращает команду или пустую строку.
    """
    try:
        return _command_queue.get_nowait()
    except Exception:
        return ""


# ── Client ─────────────────────────────────────────────────

def send_ipc_command(action: str) -> dict:
    """Отправить команду в запущенный YaDisk Manager.

    Пример::
        send_ipc_command('restart')
        send_ipc_command('raise')
        send_ipc_command('shutdown')
    """
    try:
        s = socket.create_connection(('127.0.0.1', IPC_PORT), timeout=2.0)
        with s:
            s.sendall(json.dumps({"action": action}).encode('utf-8'))
            resp = s.recv(4096).decode('utf-8')
            return json.loads(resp)
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── CLI-entry (опционально: python -m ipc restart) ───────

if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "restart"
    result = send_ipc_command(action)
    print(json.dumps(result, ensure_ascii=False, indent=2))
