"""WebSocket + HTTP bridge for StudiBudi.

Run:
    python ws_bridge.py

Open:
    http://127.0.0.1:8000/index.html
"""

import asyncio
import functools
import http.server
import os
import queue
import socket as tcp_socket
import sys
import threading

try:
    import websockets
    try:
        from websockets.asyncio.server import serve as ws_serve
    except ImportError:
        ws_serve = websockets.serve
except ImportError:
    print("Library 'websockets' belum terinstall.")
    print("Jalankan: pip install -r requirements.txt")
    sys.exit(1)

try:
    from oldserver import handle_client, logger
except ImportError as exc:
    print(f"Gagal mengimpor oldserver.py: {exc}")
    sys.exit(1)

WS_HOST = "0.0.0.0"
WS_PORT = 8765
TCP_HOST = "0.0.0.0"
TCP_PORT = 12345
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8000
MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024


class FakeSocket:
    def __init__(self, websocket, loop: asyncio.AbstractEventLoop):
        self._ws = websocket
        self._loop = loop
        self._recv_queue = queue.Queue()
        self._closed = False

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise OSError("WebSocket is closed")
        text = data.decode("utf-8")
        future = asyncio.run_coroutine_threadsafe(self._ws.send(text), self._loop)
        try:
            future.result(timeout=60)
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
    def __init__(self, receive_queue):
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
    addr = getattr(websocket, "remote_address", None) or ("ws-client", 0)
    logger.info("[WS] Koneksi baru dari %s", addr)

    threading.Thread(target=handle_client, args=(fake_socket, addr), daemon=True).start()

    try:
        async for message in websocket:
            if isinstance(message, str):
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
    server = tcp_socket.socket(tcp_socket.AF_INET, tcp_socket.SOCK_STREAM)
    server.setsockopt(tcp_socket.SOL_SOCKET, tcp_socket.SO_REUSEADDR, 1)
    try:
        server.bind((TCP_HOST, TCP_PORT))
        server.listen(100)
        logger.info("[TCP] Server TCP berjalan di port %d", TCP_PORT)
        while True:
            client_socket, addr = server.accept()
            threading.Thread(target=handle_client, args=(client_socket, addr), daemon=True).start()
    except OSError as exc:
        logger.error("[TCP] Server gagal berjalan: %s", exc)
    finally:
        server.close()


class NoCacheHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler agar browser selalu mengambil index.html versi terbaru."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def start_http() -> None:
    project_dir = os.path.dirname(os.path.abspath(__file__))
    handler = functools.partial(NoCacheHTTPRequestHandler, directory=project_dir)
    server = http.server.ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), handler)
    logger.info("[HTTP] Web client: http://%s:%d/index.html", HTTP_HOST, HTTP_PORT)
    try:
        server.serve_forever()
    except Exception as exc:
        logger.error("[HTTP] Server gagal berjalan: %s", exc)
    finally:
        server.server_close()


async def run_ws() -> None:
    logger.info("[WS] WebSocket server berjalan di ws://%s:%d", WS_HOST, WS_PORT)
    async with ws_serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        max_size=MAX_WS_MESSAGE_SIZE,
        ping_interval=20,
        ping_timeout=60,
        close_timeout=10,
    ):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    logger.info("[MAIN] StudiBudi — TCP :12345 | WebSocket :8765 | HTTP :8000")
    logger.info("[MAIN] Buka http://127.0.0.1:8000/index.html")
    logger.info("[MAIN] Tekan Ctrl+C untuk berhenti")

    threading.Thread(target=start_tcp, daemon=True).start()
    threading.Thread(target=start_http, daemon=True).start()

    try:
        asyncio.run(run_ws())
    except KeyboardInterrupt:
        logger.info("[MAIN] Server dihentikan.")
