"""Start the Ticket Copilot demo on this Windows computer.

"Start Ticket Copilot.cmd" runs this file with the project's .venv Python, so it works when
double-clicked in File Explorer, without VS Code or a terminal:

1. Read the saved settings. The first time, ask for anything missing, then save it.
2. Start the app with Waitress on http://127.0.0.1:8080, using tickets.db next to app.py.
3. Open the default browser as soon as the sign-in page answers.

Close the window (or press Ctrl+C in it) to stop the app.

The settings are saved outside the project folder (so never in Git), in
%LOCALAPPDATA%\\AI Support Ticket Copilot\\settings.dat. The file is encrypted with Windows'
own data protection (DPAPI), so only your Windows account on this computer can read it.
"""

import ctypes
import getpass
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

from waitress import serve
from werkzeug.security import generate_password_hash

from app import create_app

HOST = "127.0.0.1"
PORT = 8080
PORTAL_URL = f"http://{HOST}:{PORT}/"
DEFAULT_N8N_URL = "http://localhost:18080/webhook/ticket-triage"
MIN_PASSWORD_LENGTH = 12

# The four settings the app reads from the environment (see README.md).
SETTING_NAMES = ["GEMINI_API_KEY", "N8N_TRIAGE_WEBHOOK_URL", "DEMO_PASSWORD_HASH", "SECRET_KEY"]


# ---------------------------------------------------------------------------
# Saved settings: one small file, encrypted for the current Windows account
# ---------------------------------------------------------------------------

def settings_file():
    """Where the settings are saved: in your Windows profile, outside the project folder."""
    return Path(os.environ["LOCALAPPDATA"]) / "AI Support Ticket Copilot" / "settings.dat"


def load_saved_settings(path):
    """Return the saved settings as a dict, or an empty dict before the first launch."""
    if not path.exists():
        return {}
    return json.loads(unprotect(path.read_bytes()).decode("utf-8"))


def save_settings(path, settings):
    """Encrypt the settings and write them to the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(protect(json.dumps(settings).encode("utf-8")))


# Windows' Data Protection API (DPAPI) encrypts bytes with a key tied to your Windows sign-in.
# Python has no built-in wrapper, so these call it through ctypes. A DATA_BLOB is just
# "a length and a pointer to the bytes".
class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x1  # never show a Windows dialog


def protect(data):
    """Encrypt bytes for the current Windows account."""
    return call_dpapi(ctypes.windll.crypt32.CryptProtectData, data)


def unprotect(data):
    """Decrypt bytes made by protect(). Fails (OSError) for another account or a damaged file."""
    return call_dpapi(ctypes.windll.crypt32.CryptUnprotectData, data)


def call_dpapi(function, data):
    buffer = ctypes.create_string_buffer(data, len(data))
    data_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    data_out = DataBlob()
    # Both functions take: input, description, extra entropy, reserved, prompt, flags, output.
    if not function(ctypes.byref(data_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(data_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(data_out.data, data_out.size)
    finally:
        ctypes.windll.kernel32.LocalFree(data_out.data)  # Windows allocated the output; give it back


# ---------------------------------------------------------------------------
# First launch: fill in whatever isn't saved yet
# ---------------------------------------------------------------------------

def fill_missing_settings(settings, environment):
    """Add each setting that isn't saved yet, and return the settings.

    A value already in the environment is reused (for example when you start the launcher
    from a PowerShell window that still has your old $env: settings). Otherwise the launcher
    asks for it, except SECRET_KEY, which it makes up: nobody needs to know it.
    """
    for name in SETTING_NAMES:
        if settings.get(name):
            continue
        existing = environment.get(name, "").strip()
        if existing:
            print(f"Reusing {name} from this window's environment.")
            settings[name] = existing
        elif name == "GEMINI_API_KEY":
            settings[name] = ask_gemini_key()
        elif name == "N8N_TRIAGE_WEBHOOK_URL":
            settings[name] = ask_n8n_url()
        elif name == "DEMO_PASSWORD_HASH":
            settings[name] = generate_password_hash(ask_demo_password())
        else:
            settings[name] = secrets.token_hex(32)
    return settings


def is_plain_text(value):
    """True if the value has only visible characters: no spaces, tabs, or invisible ones."""
    return value != "" and all("!" <= character <= "~" for character in value)


def ask_gemini_key():
    # The key is read from the clipboard instead of pasted: pasting into a hidden prompt
    # doesn't work in every Windows console, and it never shows whether anything arrived.
    print("\nDrafts need your Gemini API key (from https://aistudio.google.com/apikey).")
    while True:
        input("Copy the key, then press Enter here. The launcher reads it from the clipboard: ")
        key = read_clipboard().strip()
        if is_plain_text(key):
            print(f"Got a {len(key)}-character key (Gemini keys are usually 39 characters).")
            return key
        print("The clipboard doesn't hold a key: it's empty, or has spaces or more than one line.")
        print("Copy just the key (for example with AI Studio's copy button) and try again.")


def read_clipboard():
    """Return the text on the Windows clipboard ("" if there is none). It is never printed."""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                            capture_output=True, text=True)
    return result.stdout


def ask_n8n_url():
    print("\nThe priority check uses your n8n workflow's Production webhook URL.")
    print("The app sends tickets there; it isn't a page to open. (A browser gets n8n's")
    print("\"not registered for GET requests\" message, which is normal.)")
    while True:
        url = input(f"Press Enter to use {DEFAULT_N8N_URL}\nor type a different URL: ").strip()
        if url == "":
            return DEFAULT_N8N_URL
        if url.startswith(("http://", "https://")) and is_plain_text(url):
            return url
        print("The URL must start with http:// or https:// and have no spaces.")


def ask_demo_password():
    print(f"\nChoose the demo password for the sign-in page (at least {MIN_PASSWORD_LENGTH} characters).")
    print("Only a hash of it is saved, never the password itself.")
    while True:
        first = getpass.getpass("Demo password (hidden): ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if getpass.getpass("Type it again: ") == first:
            return first
        print("The two passwords were different. Try again.")


# ---------------------------------------------------------------------------
# Starting the app
# ---------------------------------------------------------------------------

def portal_is_running():
    """True if this app's sign-in page already answers on 127.0.0.1:8080."""
    try:
        with urllib.request.urlopen(PORTAL_URL + "sign-in", timeout=2) as response:
            return b"AI Support Ticket Copilot" in response.read()
    except OSError:  # nothing listening, or a different program answered with an error
        return False


def open_in_browser():
    """Open the portal in the default browser, without handing the settings to the browser.

    A program started from here gets a copy of this process's environment, which holds the
    Gemini key and the other settings (main() puts them there for the app). If the browser
    wasn't running yet, it would keep that copy while it's open. So it's started through
    cmd's "start" with an environment that leaves the settings out.
    """
    environment = {name: value for name, value in os.environ.items()
                   if name not in SETTING_NAMES + ["LOCALHOST_HTTP"]}
    subprocess.run(["cmd.exe", "/c", "start", "", PORTAL_URL], env=environment,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def open_browser_when_ready():
    """Wait (up to 30 seconds) for the sign-in page to answer, then open it in the default browser."""
    for _ in range(60):
        if portal_is_running():
            open_in_browser()
            print(f"Opened {PORTAL_URL} in your browser.")
            return
        time.sleep(0.5)
    print(f"The app didn't answer within 30 seconds. Try opening {PORTAL_URL} yourself.")


def n8n_is_listening(url):
    """True if something accepts connections at the webhook's host and port.

    This only opens and closes a connection: it doesn't run the workflow.
    """
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.create_connection((parts.hostname, port), timeout=1):
            return True
    except (OSError, ValueError):
        return False


def main():
    if portal_is_running():
        print("Ticket Copilot is already running. Opening it in your browser.")
        open_in_browser()
        return 0

    path = settings_file()
    try:
        saved = load_saved_settings(path)
    except (OSError, ValueError):
        print(f"Couldn't read the saved settings in {path}")
        print("Delete that file and start again to set them up fresh.")
        return 1

    settings = fill_missing_settings(dict(saved), os.environ)
    # The app and Waitress run in this process, so they read the settings from here.
    os.environ.update(settings)
    os.environ["LOCALHOST_HTTP"] = "1"  # plain http is fine on 127.0.0.1 (see README.md)
    try:
        app = create_app()  # checks SECRET_KEY and DEMO_PASSWORD_HASH, and opens the database
    except RuntimeError as error:
        print(error)
        print(f"To set them up fresh, delete {path} and start again.")
        return 1

    if settings != saved:
        save_settings(path, settings)
        print(f"\nSettings saved for next time (encrypted for your Windows account) in:\n{path}")

    if not n8n_is_listening(settings["N8N_TRIAGE_WEBHOOK_URL"]):
        print("\nNote: n8n isn't answering at the saved webhook address. Start n8n to see priority checks.")
        print("Everything else works, and Check priority can try again later.")

    print(f"\nStarting AI Support Ticket Copilot at {PORTAL_URL}")
    print("Leave this window open while you use the app. Close it (or press Ctrl+C) to stop.\n")
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    try:
        serve(app, listen=f"{HOST}:{PORT}")  # runs until Ctrl+C or the window closes
    except OSError as error:
        print(f"Couldn't start on port {PORT}. Another program may be using it. ({error})")
        return 1
    print("Ticket Copilot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
