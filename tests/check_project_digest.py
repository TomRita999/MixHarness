import hashlib
import socket
import sys
from pathlib import Path


def send_digest(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.connect(("127.0.0.1", port))
        with Path("pyproject.toml").open("rb") as source:
            connection.sendall(hashlib.file_digest(source, "sha256").digest())
        # 保持验证进程运行，直到控制端确认收到摘要或停止本次验证。
        connection.recv(1)


if __name__ == "__main__":
    send_digest(int(sys.argv[1]))
