#!/usr/bin/env python3
"""A simple script to detect if the backend and frontend are ready."""

import socket
import time

BACKEND_PORT = 8000
FRONTEND_PORT = 5173


def connectable(host: str, port: int) -> bool:
    """Check if a port is connectable.

    Args:
        host: The host to connect to.
        port: The port to connect to.

    Returns:
        True if the port is connectable, False otherwise.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def detect():
    """Detect if the backend and frontend are ready."""
    while True:
        backend = connectable("localhost", BACKEND_PORT)
        frontend = connectable("localhost", FRONTEND_PORT)
        if backend and frontend:
            print("Both backend and frontend are ready.")
            break
        else:
            print("Waiting for backend and frontend to be ready...")
            time.sleep(1)


if __name__ == "__main__":
    detect()
