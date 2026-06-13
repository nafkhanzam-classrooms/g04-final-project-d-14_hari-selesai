"""
ws_bridge.py — WebSocket bridge for StudiBudi
=============================================
Runs a WebSocket server on port 8765.
Web clients connect here; this module reuses ALL existing server logic
by injecting a fake socket-like object into handle_client().

Run:
    python ws_bridge.py          # starts TCP (12345) + WS (8765) together
"""

import asyncio
import threading
import socket as _socket
import queue
import sys
import logging

# ── Dependency check dengan pesan error yang jelas ───────────────────
try:
    import websockets
    # Support websockets v10-v13 (legacy) AND v14+ (asyncio API)
    try:
        from websockets.asyncio.server import serve as ws_serve
        WS_LEGACY = False
    except ImportError:
        import websockets.server as _wss
        ws_serve = websockets.serve
        WS_LEGACY = True
except ImportError:
    print("=" * 55)
    print("ERROR: library 'websockets' belum terinstall.")
    print("Jalankan perintah berikut lalu coba lagi:")
    print()
    print("    pip install websockets")
    print()
    print("Jika pakai venv, pastikan venv sudah aktif dulu.")
    print("=" * 55)
    sys.exit(1)

# Import dari server.py (harus ada di folder yang sama)
try:
    from server import handle_client, logger
except ImportError:
    print("ERROR: server.py tidak ditemukan di folder ini.")
    sys.exit(1)

WS_HOST = "0.0.0.0"
WS_PORT = 8765


# ── FakeSocket: bungkus WebSocket agar tampak seperti TCP socket ──────

class FakeSocket:
    def __init__(self, ws, loop: asyncio.AbstractEventLoop):
        self._ws = ws
        self._loop = loop
        self._recv_queue: queue.Queue = queue.Queue()
        self._closed = False

    def sendall(self, data: bytes) -> None:
        if self._closed:
            return
        text = data.decode("utf-8")
        fut = asyncio.run_coroutine_threadsafe(self._ws.send(text), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    def push_line(self, line: str) -> None:
        self._recv_queue.put(line)

    def push_eof(self) -> None:
        self._recv_queue.put("")

    def makefile(self, mode="r", encoding="utf-8", **kwargs):
        return _FakeReader(self._recv_queue)

    def close(self) -> None:
        self._closed = True

    def setsockopt(self, *args): pass
    def getpeername(self): return ("ws-client", 0)


class _FakeReader:
    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def readline(self) -> str:
        if "\n" in self._buf:
            idx = self._buf.index("\n") + 1
            line, self._buf = self._buf[:idx], self._buf[idx:]
            return line
        while True:
            chunk = self._q.get()
            if chunk == "":
                return ""
            self._buf += chunk
            if "\n" in self._buf:
                idx = self._buf.index("\n") + 1
                line, self._buf = self._buf[:idx], self._buf[idx:]
                return line


# ── WebSocket handler ─────────────────────────────────────────────────

async def ws_handler(websocket):
    loop = asyncio.get_event_loop()
    fake_sock = FakeSocket(websocket, loop)

    # Kompatibilitas: websockets lama pakai .remote_address, baru sama
    try:
        addr = websocket.remote_address or ("ws-client", 0)
    except Exception:
        addr = ("ws-client", 0)

    logger.info("[WS] Koneksi baru dari %s", addr)

    def run_client():
        handle_client(fake_sock, addr)

    client_thread = threading.Thread(target=run_client, daemon=True)
    client_thread.start()

    try:
        async for message in websocket:
            if not message.endswith("\n"):
                message += "\n"
            fake_sock.push_line(message)
    except Exception:
        pass
    finally:
        fake_sock.push_eof()
        logger.info("[WS] Terputus: %s", addr)


# ── TCP server ────────────────────────────────────────────────────────

def start_tcp():
    server = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 12345))
    server.listen(10)
    logger.info("[TCP] Server TCP berjalan di port 12345")
    try:
        while True:
            client_socket, addr = server.accept()
            logger.info("[TCP] Koneksi baru dari %s", addr)
            t = threading.Thread(
                target=handle_client, args=(client_socket, addr), daemon=True
            )
            t.start()
    except Exception:
        pass
    finally:
        server.close()


# ── Main ──────────────────────────────────────────────────────────────

async def run_ws():
    logger.info("[WS] WebSocket server berjalan di ws://%s:%d", WS_HOST, WS_PORT)
    if WS_LEGACY:
        # websockets v10-v13
        async with ws_serve(ws_handler, WS_HOST, WS_PORT):
            await asyncio.get_running_loop().create_future()
    else:
        # websockets v14+
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