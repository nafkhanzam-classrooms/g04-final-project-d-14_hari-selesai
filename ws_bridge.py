"""Secure WebSocket + HTTPS + TLS TCP bridge for StudiBudi.

Run:
    python ws_bridge.py

Open:
    https://127.0.0.1:8443/index.html

Catatan:
- cert.pem dan key.pem dipakai untuk demo lokal.
- Browser mungkin menampilkan peringatan karena sertifikat self-signed.
"""

import asyncio
import functools
import http.server
import os
import queue
import socket as tcp_socket
import ssl
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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(PROJECT_DIR, "cert.pem")
KEY_FILE = os.path.join(PROJECT_DIR, "key.pem")

WS_HOST = "0.0.0.0"
WS_PORT = 8765
TCP_HOST = "0.0.0.0"
TCP_PORT = 12345
HTTPS_HOST = "0.0.0.0"
HTTPS_PORT = 8443
MAX_WS_MESSAGE_SIZE = 16 * 1024 * 1024


def require_tls_files() -> None:
    missing = [path for path in (CERT_FILE, KEY_FILE) if not os.path.exists(path)]
    if missing:
        print("ERROR: File TLS tidak ditemukan:")
        for path in missing:
            print(f"  - {path}")
        print("Gunakan cert.pem dan key.pem dari ZIP atau buat sertifikat baru.")
        sys.exit(1)


def create_server_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    return context


class FakeSocket:
    """Adapter agar logika socket lama dapat berjalan di atas WebSocket TLS."""

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
        return ("wss-client", 0)


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
    addr = getattr(websocket, "remote_address", None) or ("wss-client", 0)
    logger.info("[WSS] Koneksi TLS baru dari %s", addr)

    threading.Thread(
        target=handle_client,
        args=(fake_socket, addr),
        daemon=True,
    ).start()

    try:
        async for message in websocket:
            if isinstance(message, str):
                if not message.endswith("\n"):
                    message += "\n"
                fake_socket.push_line(message)
    except Exception as exc:
        logger.info("[WSS] Koneksi %s berakhir: %s", addr, exc)
    finally:
        fake_socket.push_eof()
        fake_socket.close()
        logger.info("[WSS] Terputus: %s", addr)


def start_tls_tcp(ssl_context: ssl.SSLContext) -> None:
    """Terminal client menggunakan TLS di port TCP 12345."""
    server = tcp_socket.socket(tcp_socket.AF_INET, tcp_socket.SOCK_STREAM)
    server.setsockopt(tcp_socket.SOL_SOCKET, tcp_socket.SO_REUSEADDR, 1)

    try:
        server.bind((TCP_HOST, TCP_PORT))
        server.listen(100)
        logger.info("[TLS-TCP] Server berjalan di port %d", TCP_PORT)

        while True:
            raw_socket, addr = server.accept()
            try:
                secure_socket = ssl_context.wrap_socket(raw_socket, server_side=True)
            except ssl.SSLError as exc:
                logger.warning("[TLS-TCP] Handshake gagal dari %s: %s", addr, exc)
                raw_socket.close()
                continue

            threading.Thread(
                target=handle_client,
                args=(secure_socket, addr),
                daemon=True,
            ).start()
    except OSError as exc:
        logger.error("[TLS-TCP] Server gagal berjalan: %s", exc)
    finally:
        server.close()


class NoCacheHTTPSRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTPS handler agar browser selalu mengambil index.html terbaru."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Strict-Transport-Security", "max-age=300")
        super().end_headers()


def start_https(ssl_context: ssl.SSLContext) -> None:
    handler = functools.partial(NoCacheHTTPSRequestHandler, directory=PROJECT_DIR)
    server = http.server.ThreadingHTTPServer((HTTPS_HOST, HTTPS_PORT), handler)
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    logger.info("[HTTPS] Web client: https://127.0.0.1:%d/index.html", HTTPS_PORT)

    try:
        server.serve_forever()
    except Exception as exc:
        logger.error("[HTTPS] Server gagal berjalan: %s", exc)
    finally:
        server.server_close()


async def run_wss(ssl_context: ssl.SSLContext) -> None:
    logger.info("[WSS] Secure WebSocket berjalan di wss://localhost:%d", WS_PORT)
    async with ws_serve(
        ws_handler,
        WS_HOST,
        WS_PORT,
        ssl=ssl_context,
        max_size=MAX_WS_MESSAGE_SIZE,
        ping_interval=20,
        ping_timeout=60,
        close_timeout=10,
    ):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    require_tls_files()
    tls_context = create_server_ssl_context()

    logger.info(
        "[MAIN] StudiBudi TLS — TCP-TLS :12345 | WSS :8765 | HTTPS :8443"
    )
    logger.info("[MAIN] Buka https://127.0.0.1:8443/index.html")
    logger.info("[MAIN] Tekan Ctrl+C untuk berhenti")

    threading.Thread(target=start_tls_tcp, args=(tls_context,), daemon=True).start()
    threading.Thread(target=start_https, args=(tls_context,), daemon=True).start()

    try:
        asyncio.run(run_wss(tls_context))
    except KeyboardInterrupt:
        logger.info("[MAIN] Server dihentikan.")
