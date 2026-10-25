"""Zugriff auf die System-Zwischenablage, auch wenn flash unter sudo läuft."""

from __future__ import annotations

import os
import subprocess
from urllib.parse import unquote, urlparse

# Nacheinander probiert; GNOME spiegelt die Wayland-Zwischenablage auch nach XWayland.
CLIPBOARD_COMMANDS = [
    ["wl-paste", "--no-newline"],
    ["xclip", "-o", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--output"],
]


def clipboard_env() -> dict[str, str]:
    """Umgebung für die Zwischenablage-Programme.

    bin/flash reicht DISPLAY, XAUTHORITY und WAYLAND_DISPLAY durch sudo; das
    Laufzeitverzeichnis mit dem Wayland-Socket gehört dem aufrufenden Benutzer.
    """
    env = dict(os.environ)
    uid = os.environ.get("SUDO_UID")
    if uid and "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    return env


def read_clipboard() -> str | None:
    env = clipboard_env()
    for argv in CLIPBOARD_COMMANDS:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=2, env=env)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
    return None


def clean_path(text: str) -> str:
    """Macht aus eingefügtem Text einen Pfad: erste Zeile, ohne Anführungszeichen, file://-URIs aufgelöst."""
    line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "'\"":
        line = line[1:-1]
    if line.startswith("file://"):
        line = unquote(urlparse(line).path)
    return line


def looks_like_path(text: str) -> bool:
    return text.startswith(("/", "~"))
