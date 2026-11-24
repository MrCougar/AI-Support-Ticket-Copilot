"""Tests for launcher.py, the Windows double-click launcher.

Nothing here reads or writes your real saved settings, opens a browser, or uses port 8080:
the prompts, the browser, and Waitress are simulated, and files go in pytest's tmp_path.
Run with:  python -m pytest
"""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("The launcher uses Windows' data protection (DPAPI).", allow_module_level=True)

from werkzeug.security import check_password_hash

import launcher

TEST_PASSWORD = "a long test-only password"
TEST_SETTINGS = {
    "GEMINI_API_KEY": "test-only-gemini-key",
    "N8N_TRIAGE_WEBHOOK_URL": "http://localhost:59999/webhook/ticket-triage",
    "DEMO_PASSWORD_HASH": "pbkdf2:sha256:1000$salt$" + "0" * 64,
    "SECRET_KEY": "test-only-secret-key-" * 3,
}


@pytest.fixture(autouse=True)
def no_prompts_or_settings(monkeypatch):
    """Any question the test didn't plan for fails the test, and the app's settings start unset."""
    def unexpected_question(prompt=""):
        raise AssertionError(f"Unexpected question: {prompt}")

    monkeypatch.setattr(launcher.getpass, "getpass", unexpected_question)
    monkeypatch.setattr("builtins.input", unexpected_question)
    monkeypatch.setattr(launcher, "read_clipboard", lambda: unexpected_question("(clipboard)"))
    # main() writes the settings into os.environ; monkeypatch puts them back afterwards.
    for name in launcher.SETTING_NAMES + ["LOCALHOST_HTTP", "DATABASE_PATH"]:
        monkeypatch.delenv(name, raising=False)


def answer_with(monkeypatch, getpass_answers=(), input_answers=(), clipboard=()):
    """Make the hidden prompts, the normal prompts, and the clipboard answer in this order."""
    hidden = list(getpass_answers)
    visible = list(input_answers)
    copied = list(clipboard)
    monkeypatch.setattr(launcher.getpass, "getpass", lambda prompt="": hidden.pop(0))
    monkeypatch.setattr("builtins.input", lambda prompt="": visible.pop(0))
    monkeypatch.setattr(launcher, "read_clipboard", lambda: copied.pop(0))
    return hidden, visible, copied


def test_saved_settings_are_encrypted_and_read_back(tmp_path):
    path = tmp_path / "settings" / "settings.dat"
    assert launcher.load_saved_settings(path) == {}  # before the first launch

    launcher.save_settings(path, TEST_SETTINGS)

    assert b"test-only-gemini-key" not in path.read_bytes()
    assert b"GEMINI_API_KEY" not in path.read_bytes()
    assert launcher.load_saved_settings(path) == TEST_SETTINGS


def test_saved_settings_are_used_without_asking():
    assert launcher.fill_missing_settings(dict(TEST_SETTINGS), {}) == TEST_SETTINGS


def test_settings_in_the_environment_are_reused_without_asking():
    environment = {name: f" {value} " for name, value in TEST_SETTINGS.items()}
    assert launcher.fill_missing_settings({}, environment) == TEST_SETTINGS


def test_first_launch_asks_for_what_is_missing(monkeypatch):
    hidden, visible, copied = answer_with(
        monkeypatch,
        getpass_answers=[
            "too short",  # refused: under 12 characters
            TEST_PASSWORD, "a different password",  # refused: the two didn't match
            TEST_PASSWORD, TEST_PASSWORD,
        ],
        input_answers=[
            "", "", "",  # Enter after copying the key, three times
            "localhost:18080/no-scheme", "",  # refused, then Enter for the default n8n URL
        ],
        clipboard=[
            "",  # refused: nothing copied
            "Dear customer,\r\nsome other text",  # refused: not a key
            "test-only-gemini-key\r\n",  # the copied key, with a line break after it
        ],
    )

    settings = launcher.fill_missing_settings({}, {})

    assert hidden == [] and visible == [] and copied == []
    assert settings["GEMINI_API_KEY"] == "test-only-gemini-key"
    assert settings["N8N_TRIAGE_WEBHOOK_URL"] == "http://localhost:18080/webhook/ticket-triage"
    assert TEST_PASSWORD not in settings["DEMO_PASSWORD_HASH"]
    assert check_password_hash(settings["DEMO_PASSWORD_HASH"], TEST_PASSWORD)
    assert len(settings["SECRET_KEY"]) == 64  # made up, not asked


def test_only_the_missing_setting_is_asked_for(monkeypatch):
    answer_with(monkeypatch, input_answers=["http://127.0.0.1:5678/webhook/ticket-triage"])
    saved = {name: value for name, value in TEST_SETTINGS.items() if name != "N8N_TRIAGE_WEBHOOK_URL"}

    settings = launcher.fill_missing_settings(saved, {})

    assert settings == dict(TEST_SETTINGS, N8N_TRIAGE_WEBHOOK_URL="http://127.0.0.1:5678/webhook/ticket-triage")


@pytest.fixture
def fake_server(monkeypatch, tmp_path):
    """Simulate Waitress and the browser, and keep the settings and database in tmp_path."""
    calls = {"served": [], "opened": []}
    monkeypatch.setattr(launcher, "settings_file", lambda: tmp_path / "settings.dat")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "launcher-test.db"))
    monkeypatch.setattr(launcher, "portal_is_running", lambda: False)
    monkeypatch.setattr(launcher, "open_browser_when_ready", lambda: None)
    monkeypatch.setattr(launcher, "open_in_browser", lambda: calls["opened"].append(launcher.PORTAL_URL))
    monkeypatch.setattr(launcher, "serve", lambda app, listen: calls["served"].append((app, listen)))
    return calls


def test_launch_with_saved_settings_serves_on_localhost_8080(fake_server, tmp_path):
    launcher.save_settings(tmp_path / "settings.dat", TEST_SETTINGS)

    assert launcher.main() == 0

    (app, listen), = fake_server["served"]
    assert listen == "127.0.0.1:8080"
    assert app.config["SESSION_COOKIE_SECURE"] is False  # LOCALHOST_HTTP=1 for plain http
    assert (tmp_path / "launcher-test.db").exists()  # DATABASE_PATH still chooses the database
    assert launcher.os.environ["GEMINI_API_KEY"] == "test-only-gemini-key"


def test_first_launch_saves_the_answers(fake_server, tmp_path, monkeypatch):
    environment = {name: value for name, value in TEST_SETTINGS.items() if name != "GEMINI_API_KEY"}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    answer_with(monkeypatch, input_answers=[""], clipboard=["copied-test-key"])

    assert launcher.main() == 0

    saved = launcher.load_saved_settings(tmp_path / "settings.dat")
    assert saved == dict(environment, GEMINI_API_KEY="copied-test-key")


def test_bad_saved_sign_in_settings_stop_the_launch(fake_server, tmp_path):
    launcher.save_settings(tmp_path / "settings.dat", dict(TEST_SETTINGS, DEMO_PASSWORD_HASH="plain password"))

    assert launcher.main() == 1
    assert fake_server["served"] == []


def test_unreadable_settings_file_stops_the_launch(fake_server, tmp_path):
    (tmp_path / "settings.dat").write_bytes(b"not encrypted by DPAPI")

    assert launcher.main() == 1
    assert fake_server["served"] == []


def test_the_browser_does_not_get_the_settings(monkeypatch):
    for name, value in TEST_SETTINGS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LOCALHOST_HTTP", "1")
    started = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **options: started.append((command, options)))

    launcher.open_in_browser()

    (command, options), = started
    assert command == ["cmd.exe", "/c", "start", "", "http://127.0.0.1:8080/"]
    assert "PATH" in {name.upper() for name in options["env"]}  # the rest of the environment is kept
    for name in launcher.SETTING_NAMES + ["LOCALHOST_HTTP"]:
        assert name not in options["env"]
    assert "test-only-gemini-key" not in repr(options["env"])


def test_second_launch_just_opens_the_browser(fake_server, monkeypatch):
    monkeypatch.setattr(launcher, "portal_is_running", lambda: True)

    assert launcher.main() == 0

    assert fake_server["opened"] == ["http://127.0.0.1:8080/"]
    assert fake_server["served"] == []
