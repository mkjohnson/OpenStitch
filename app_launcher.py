from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

from viewer_server import ViewerHandler


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def available_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((host, port)) != 0:
                return port
    raise RuntimeError("Could not find an available local port.")


def main() -> int:
    os.chdir(app_dir())
    host = "127.0.0.1"
    port = available_port(host, 8765)
    url = f"http://{host}:{port}/"
    server = ThreadingHTTPServer((host, port), ViewerHandler)

    def open_browser() -> None:
        time.sleep(0.8)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"OpenStitch is running at {url}")
    print("Close this window to stop the app.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
