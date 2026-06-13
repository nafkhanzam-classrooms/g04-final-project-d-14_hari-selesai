"""
ws_bridge.py — WebSocket bridge for StudiBudi
=============================================
Menjalankan TCP server pada port 12345 dan WebSocket server pada port 8765.
Web client menggunakan WebSocket, sedangkan seluruh logika aplikasi tetap
menggunakan handle_client() dari oldserver.py.

Run:
    python ws_bridge.py
"""

import asyncio
import logging
import queue
import socket as _socket
import sys
import threading

try:
    import websockets

    try:
        from websockets.asyncio.server import serve as ws_serve

        WS_LEGACY = False
    except ImportError:
        ws_serve = websockets.serve
        WS_LEGACY = True
except ImportError:
    print("=" * 55)
    print("ERROR: library 'websockets' belum terinstall.")
    print("Jalankan perintah berikut lalu coba lagi:")
    print()
    print("    pip install -r requirements.txt")
    print()
    print("Atau: pip install websockets")
    print("=" * 55)
    sys.exit(1)

# Nama file asli dipertahankan agar struktur project tidak berubah.
try:
    from oldserver import handle_client, logger
except ImportError as exc:
    print(f"ERROR: gagal mengimpor oldserver.py: {exc}")
    sys.exit(1)

WS_HOST = "0.0.0.0"
WS_PORT = 8765
TCP_HOST = "0.0.0.0"
TCP_PORT = 12345


class FakeSocket:
    """Adapter agar WebSocket dapat dipakai oleh logika TCP yang sudah ada."""

    def __init__(self, websocket, loop: asyncio.AbstractEventLoop):
        self._ws = websocket
        self._loop = loop
        self._recv_queue: queue.Queue[str] = queue.Queue()
        self._closed = False

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("WebSocket is closed")

        text = data.decode("utf-8")
        future = asyncio.run_coroutine_threadsafe(
            self._ws.send(text),
            self._loop,
        )
        try:
            future.result(timeout=5)
        except Exception as exc:
            self._closed = True
            raise OSError("Failed to send WebSocket message") from exc

    def push_line(self, line: str) -> None:
        if not self._closed:
            self._recv_queue.put(line)

    def push_eof(self) -> None:
        self._recv_queue.put("")

    def makefile(self, mode="r", encoding="utf-8", **kwargs):
        return _FakeReader(self._recv_queue)

    def close(self) -> None:
        self._closed = True

    def setsockopt(self, *args) -> None:
        return None

    def getpeername(self):
        return ("ws-client", 0)


class _FakeReader:
    def __init__(self, receive_queue: queue.Queue[str]):
        self._queue = receive_queue
        self._buffer = ""

    def readline(self) -> str:
        while "\n" not in self._buffer:
            chunk = self._queue.get()
            if chunk == "":
                return ""
            self._buffer += chunk

        index = self._buffer.index("\n") + 1
        line = self._buffer[:index]
        self._buffer = self._buffer[index:]
        return line


async def ws_handler(websocket) -> None:
    loop = asyncio.get_running_loop()
    fake_socket = FakeSocket(websocket, loop)

    try:
        addr = websocket.remote_address or ("ws-client", 0)
    except Exception:
        addr = ("ws-client", 0)

    logger.info("[WS] Koneksi baru dari %s", addr)

    client_thread = threading.Thread(
        target=handle_client,
        args=(fake_socket, addr),
        daemon=True,
    )
    client_thread.start()

    try:
        async for message in websocket:
            if not isinstance(message, str):
                continue
            if not message.endswith("\n"):
                message += "\n"
            fake_socket.push_line(message)
    except Exception as exc:
        logger.info("[WS] Koneksi %s berakhir: %s", addr, exc)
    finally:
        fake_socket.push_eof()
        fake_socket.close()
        logger.info("[WS] Terputus: %s", addr)


def start_tcp() -> None:
    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)

    try:
        server.bind((TCP_HOST, TCP_PORT))
        server.listen(100)
        logger.info("[TCP] Server TCP berjalan di port %d", TCP_PORT)

        while True:
            client_socket, addr = server.accept()
            logger.info("[TCP] Koneksi baru dari %s", addr)
            thread = threading.Thread(
                target=handle_client,
                args=(client_socket, addr),
                daemon=True,
            )
            thread.start()
    except OSError as exc:
        logger.error("[TCP] Server gagal berjalan: %s", exc)
    finally:
        server.close()


async def run_ws() -> None:
    logger.info("[WS] WebSocket server berjalan di ws://%s:%d", WS_HOST, WS_PORT)
    async with ws_serve(ws_handler, WS_HOST, WS_PORT):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    logger.info("[MAIN] StudiBudi — TCP :12345 | WebSocket :8765")
    logger.info("[MAIN] Buka index.html di browser untuk UI web")
    logger.info("[MAIN] Jalankan client.py di terminal untuk UI terminal")
    logger.info("[MAIN] Tekan Ctrl+C untuk berhenti")

    tcp_thread = threading.Thread(target=start_tcp, daemon=True)
    tcp_thread.start()

    try:
        asyncio.run(run_ws())
    except KeyboardInterrupt:
        logger.info("[MAIN] Server dihentikan.")
    except OSError as exc:
        logging.getLogger("StudyBuddyServer").error(
            "[MAIN] WebSocket server gagal berjalan: %s",
            exc,
        )
