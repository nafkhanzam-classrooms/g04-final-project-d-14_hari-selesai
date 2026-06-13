import json
import socket
import threading


def send_input(sock: socket.socket, text: str) -> bool:
    packet = {"type": "input", "payload": {"text": text}}
    try:
        sock.sendall((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def read_packet(reader):
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
        print(f"[history {payload.get('timestamp', '')}] {payload.get('sender')}: {payload.get('text', '')}")
    elif msg_type == "voice":
        duration = int(payload.get("duration_ms", 0)) / 1000
        sender = packet.get("sender", "Unknown")
        print(f"[VOICE from {sender}, {duration:.1f}s] Buka web client untuk memutar audio.")
    elif msg_type == "file":
        sender = packet.get("sender", "Unknown")
        filename = payload.get("filename", "file")
        size = int(payload.get("size", 0))
        print(f"[FILE from {sender}] {filename} ({size} bytes). Buka web client untuk mengunduh.")
    elif msg_type == "reaction_update":
        reactions = payload.get("reactions", {})
        summary = " ".join(f"{emoji} {len(users)}" for emoji, users in reactions.items())
        print(f"[REACTION] {summary or 'reaction dihapus'}")
    elif msg_type == "pm":
        print(f"[{payload.get('timestamp', '')}] [PM from {payload.get('sender')}] {payload.get('text', '')}")
    elif msg_type == "rooms":
        print("Available rooms:")
        for room in payload.get("rooms", []):
            print(f"  - {room.get('name')} ({room.get('members')} members) | {room.get('topic', '-')}")
    elif msg_type == "room_joined":
        print(payload.get("text", f"Joined {payload.get('room', '')}"))
    elif msg_type == "online_users":
        print("Online users:")
        for user in payload.get("users", []):
            print(f"  - {user.get('username')} [{user.get('room', 'lobby')}] — {user.get('status', '-')}")
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


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", 12345))
    except OSError as exc:
        print(f"Failed to connect: {exc}")
        return

    print("Connected to StudiBudi Chat Server!")
    reader = sock.makefile("r", encoding="utf-8")

    try:
        username_prompt = read_packet(reader)
        if username_prompt is None:
            return
        print(username_prompt.get("payload", {}).get("text", "Username:"), end=" ")
        send_input(sock, input())

        password_prompt = read_packet(reader)
        if password_prompt is None:
            return
        print(password_prompt.get("payload", {}).get("text", "Password:"), end=" ")
        send_input(sock, input())

        auth_packet = read_packet(reader)
        if auth_packet is None or auth_packet.get("type") != "auth_result":
            print("Authentication response is invalid.")
            return

        auth_payload = auth_packet.get("payload", {})
        print(auth_payload.get("message", ""))
        if not auth_payload.get("success", False):
            return

        threading.Thread(target=receive_messages, args=(reader,), daemon=True).start()
        print("Type your messages or /help for commands:\n")

        while True:
            message = input("> ")
            if message and not send_input(sock, message):
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
