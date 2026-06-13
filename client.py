import json
import os
import socket
import ssl
import threading

HOST = "localhost"
PORT = 12345
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CERT_FILE = os.path.join(PROJECT_DIR, "cert.pem")


def send_input(sock: ssl.SSLSocket, text: str) -> bool:
    packet = {"type": "input", "payload": {"text": text}}
    try:
        sock.sendall((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def read_packet(reader) -> dict | None:
    line = reader.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"type": "system", "payload": {"text": line.strip()}}


def print_packet(packet: dict) -> None:
    msg_type = packet.get("type")
    payload = packet.get("payload", {})

    if msg_type in {"system", "prompt"}:
        print(payload.get("text", ""))
    elif msg_type == "auth_result":
        print(payload.get("message", ""))
    elif msg_type == "chat":
        room = packet.get("room") or payload.get("room")
        sender = packet.get("sender") or payload.get("sender")
        timestamp = payload.get("timestamp") or packet.get("timestamp")
        print(f"[{timestamp}] {sender}@{room}: {payload.get('text', '')}")
    elif msg_type == "history":
        print(
            f"[history {payload.get('timestamp')}] "
            f"{payload.get('sender')}: {payload.get('text', '')}"
        )
    elif msg_type == "pm":
        print(
            f"[{payload.get('timestamp', '')}] "
            f"[PM from {payload.get('sender')}] {payload.get('text', '')}"
        )
    elif msg_type == "rooms":
        print("Available rooms:")
        for room in payload.get("rooms", []):
            print(
                f"  - {room.get('name')} ({room.get('members')} members) "
                f"| {room.get('topic', '-')}"
            )
    elif msg_type == "room_joined":
        print(payload.get("text", f"Joined {payload.get('room', '')}"))
    elif msg_type == "online_users":
        print("Online users:")
        for user in payload.get("users", []):
            print(
                f"  - {user.get('username')} [{user.get('room', 'lobby')}] "
                f"— {user.get('status', '-')}"
            )
    elif msg_type == "status_update":
        print(f"[STATUS] {payload.get('username')}: {payload.get('status')}")
    else:
        print(payload)


def receive_messages(reader) -> None:
    while True:
        try:
            packet = read_packet(reader)
            if packet is None:
                print("Disconnected from server.")
                break
            print_packet(packet)
        except (OSError, ValueError):
            break


def create_tls_socket() -> ssl.SSLSocket:
    if not os.path.exists(CERT_FILE):
        raise FileNotFoundError("cert.pem tidak ditemukan di folder project.")

    # Sertifikat lokal dijadikan trust anchor agar koneksi tetap diverifikasi.
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=CERT_FILE)
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    raw_socket = socket.create_connection((HOST, PORT), timeout=5)
    return context.wrap_socket(raw_socket, server_hostname=HOST)


def main() -> None:
    try:
        sock = create_tls_socket()
    except (OSError, ssl.SSLError, FileNotFoundError) as exc:
        print(f"Failed to connect securely: {exc}")
        return

    print("Connected securely to StudiBudi TLS Chat Server!")
    print(f"TLS version: {sock.version()}")
    reader = sock.makefile("r", encoding="utf-8")

    try:
        username_prompt = read_packet(reader)
        if username_prompt is None:
            print("Server closed the connection.")
            return
        print(username_prompt.get("payload", {}).get("text", "Username:"), end=" ")
        send_input(sock, input())

        password_prompt = read_packet(reader)
        if password_prompt is None:
            print("Server closed the connection.")
            return
        print(password_prompt.get("payload", {}).get("text", "Password:"), end=" ")
        send_input(sock, input())

        auth_packet = read_packet(reader)
        if auth_packet is None:
            print("Server closed the connection during authentication.")
            return

        if auth_packet.get("type") != "auth_result":
            print_packet(auth_packet)
            print("Authentication response is invalid.")
            return

        auth_payload = auth_packet.get("payload", {})
        print(auth_payload.get("message", ""))
        if not auth_payload.get("success", False):
            return

        threading.Thread(target=receive_messages, args=(reader,), daemon=True).start()
        print("Type your messages or /help for commands:\n")

        while True:
            msg = input("> ")
            if msg and not send_input(sock, msg):
                print("Failed to send message. Connection may be closed.")
                break

    except (KeyboardInterrupt, EOFError):
        print("\nDisconnected.")
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


if __name__ == "__main__":
    main()
