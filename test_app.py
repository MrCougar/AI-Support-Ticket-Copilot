"""Tests for signing in, creating, validating, and listing tickets, generating drafts,
approving replies, and checking priority with n8n.

Run them with:  python -m pytest

Each test gets a fresh app with its own empty database in a temporary folder,
so the tests never touch your real tickets.db. Gemini and the n8n webhook are always
simulated: the tests need no API key or n8n, and never call the real services.
The sign-in settings are test-only values, never your real password or secret key.
"""

import http.client
import io
import json
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.genai import errors, types
from werkzeug.security import check_password_hash, generate_password_hash

import ai_drafts
import app as app_module
import database
import sign_in_limits
from app import create_app

FAKE_KEY = "fake-key-for-tests"
FAKE_WEBHOOK_URL = "http://n8n.test/webhook/ticket-triage"
TEST_PASSWORD = "correct horse battery staple"
TEST_SECRET_KEY = "test-only-secret-key-" * 3  # 63 characters
# A quick-to-check hash keeps the tests fast. A real hash (scrypt, Werkzeug's default) is slower on purpose.
TEST_PASSWORD_HASH = generate_password_hash(TEST_PASSWORD, method="pbkdf2:sha256:1000")


@pytest.fixture(autouse=True)
def sign_in_settings(monkeypatch):
    """Every app in the tests starts with test-only sign-in settings, whatever is set in your terminal."""
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("DEMO_PASSWORD_HASH", TEST_PASSWORD_HASH)
    monkeypatch.delenv("LOCALHOST_HTTP", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)


def csrf_token_in(response):
    """The CSRF token in a page: the sign-in form's hidden field, or the inbox's <meta> tag."""
    page = response.get_data(as_text=True)
    return re.search(r'name="csrf[-_]token" (?:value|content)="([^"]+)"', page).group(1)


def sign_in(test_client, password=TEST_PASSWORD):
    """Sign in through the real sign-in form, the way a browser does, and return the response."""
    token = csrf_token_in(test_client.get("/sign-in"))
    return test_client.post("/sign-in", data={"password": password, "csrf_token": token})


def signed_in_client(app):
    """A test client that has signed in, and sends the inbox's CSRF token with every request,
    like static/app.js does."""
    test_client = app.test_client()
    sign_in(test_client)
    test_client.environ_base["HTTP_X_CSRFTOKEN"] = csrf_token_in(test_client.get("/"))
    return test_client


@pytest.fixture
def client(tmp_path):
    """A signed-in test client for an app with a brand-new, empty database."""
    app = create_app(tmp_path / "test.db")
    app.config["TESTING"] = True
    return signed_in_client(app)


@pytest.fixture(autouse=True)
def never_call_the_real_gemini(monkeypatch):
    """Safety net for every test: use a fake API key, and refuse to create a real Gemini client."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    def refuse(**options):
        raise AssertionError("A test tried to create a real Gemini client. Use the fake_gemini fixture.")

    monkeypatch.setattr(ai_drafts.genai, "Client", refuse)


@pytest.fixture(autouse=True)
def fake_n8n(monkeypatch):
    """Every test gets a simulated n8n webhook (FakeN8n, below), so no test makes a real HTTP request,
    even if N8N_TRIAGE_WEBHOOK_URL is set in your terminal. Tests that change what "n8n" answers
    ask for fake_n8n by name."""
    monkeypatch.setenv("N8N_TRIAGE_WEBHOOK_URL", FAKE_WEBHOOK_URL)
    fake = FakeN8n()
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def test_home_page_has_the_form_and_the_inbox(client):
    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="ticket-form"' in html
    assert "Submit ticket" in html
    assert 'id="ticket-list"' in html


def test_inbox_is_empty_at_first(client):
    response = client.get("/api/tickets")

    assert response.status_code == 200
    assert response.get_json() == []


def test_creating_a_ticket_saves_it_with_an_id_and_creation_time(client):
    response = client.post(
        "/api/tickets",
        json={"subject": "Can't log in", "message": "My password reset email never arrived."},
    )

    assert response.status_code == 201
    ticket = response.get_json()
    assert ticket["id"] == 1
    assert ticket["subject"] == "Can't log in"
    assert ticket["message"] == "My password reset email never arrived."

    created_at = datetime.fromisoformat(ticket["created_at"])
    assert created_at.tzinfo is not None  # saved with a time zone (UTC)
    assert datetime.now(timezone.utc) - created_at < timedelta(minutes=1)

    # Loading the inbox returns the same ticket.
    assert client.get("/api/tickets").get_json() == [ticket]


def test_inbox_lists_the_newest_ticket_first(client):
    for subject in ["First", "Second", "Third"]:
        client.post("/api/tickets", json={"subject": subject, "message": "Hello"})

    tickets = client.get("/api/tickets").get_json()

    assert [ticket["subject"] for ticket in tickets] == ["Third", "Second", "First"]
    assert [ticket["id"] for ticket in tickets] == [3, 2, 1]


def test_spaces_around_the_text_are_trimmed_before_saving(client):
    response = client.post(
        "/api/tickets",
        json={"subject": "  Refund request  ", "message": "\n  Line one\nLine two  \n"},
    )

    ticket = response.get_json()
    assert ticket["subject"] == "Refund request"
    assert ticket["message"] == "Line one\nLine two"  # line breaks inside the text are kept


@pytest.mark.parametrize(
    "subject, message, expected_error",
    [
        ("", "My order never arrived.", "Subject cannot be blank."),
        ("   ", "My order never arrived.", "Subject cannot be blank."),
        ("Missing order", "", "Message cannot be blank."),
        ("Missing order", " \n\t ", "Message cannot be blank."),
        (" ", " ", "Subject cannot be blank. Message cannot be blank."),
    ],
)
def test_blank_or_whitespace_only_fields_are_rejected(client, subject, message, expected_error):
    response = client.post("/api/tickets", json={"subject": subject, "message": message})

    assert response.status_code == 400
    assert response.get_json() == {"error": expected_error}
    assert client.get("/api/tickets").get_json() == []  # nothing was saved


@pytest.mark.parametrize(
    "body",
    [
        {},  # no fields at all
        {"subject": 123, "message": None},  # fields that aren't text
    ],
)
def test_missing_or_non_text_fields_are_rejected(client, body):
    response = client.post("/api/tickets", json=body)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Subject cannot be blank. Message cannot be blank."}
    assert client.get("/api/tickets").get_json() == []


def test_a_body_that_is_not_a_json_object_is_rejected(client):
    html_form_post = client.post(
        "/api/tickets",
        data="subject=Hi&message=Hello",
        content_type="application/x-www-form-urlencoded",
    )
    broken_json = client.post("/api/tickets", data="{not json", content_type="application/json")
    json_list = client.post("/api/tickets", json=["Hi", "Hello"])

    for response in [html_form_post, broken_json, json_list]:
        assert response.status_code == 400
        assert response.get_json() == {"error": "Send the ticket as a JSON object."}
    assert client.get("/api/tickets").get_json() == []


def test_a_database_failure_returns_an_error_message(client, monkeypatch):
    def broken_create_ticket(*args):
        raise sqlite3.OperationalError("database is locked")

    # Swap in a create_ticket that always fails, just for this test.
    monkeypatch.setattr(database, "create_ticket", broken_create_ticket)

    response = client.post("/api/tickets", json={"subject": "Hi", "message": "Hello"})

    assert response.status_code == 500
    assert response.get_json() == {"error": "The ticket could not be saved. Please try again."}


def test_tickets_are_still_there_after_restarting_the_app(tmp_path):
    database_path = tmp_path / "tickets.db"

    first_run = signed_in_client(create_app(database_path))
    first_run.post("/api/tickets", json={"subject": "Refund", "message": "Please refund order 1042."})

    # A brand-new app using the same database file is the same as restarting the server.
    second_run = signed_in_client(create_app(database_path))
    tickets = second_run.get("/api/tickets").get_json()

    assert len(tickets) == 1
    assert tickets[0]["subject"] == "Refund"
    assert tickets[0]["message"] == "Please refund order 1042."


# ---------- Choosing the database file (hosting) ----------


def test_database_path_chooses_the_file_when_the_app_is_started_without_one(tmp_path, monkeypatch):
    chosen_file = tmp_path / "hosted-tickets.db"
    monkeypatch.setenv("DATABASE_PATH", str(chosen_file))

    # create_app() with no arguments is how waitress-serve --call app:create_app starts the app.
    signed_in_client(create_app()).post("/api/tickets", json={"subject": "Hosted", "message": "Saved on the host."})
    restarted = signed_in_client(create_app())  # a restart with the same setting

    assert chosen_file.exists()
    assert [ticket["subject"] for ticket in restarted.get("/api/tickets").get_json()] == ["Hosted"]


def test_a_database_path_passed_in_wins_over_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "from-setting.db"))

    create_app(tmp_path / "passed-in.db")  # what the tests do

    assert (tmp_path / "passed-in.db").exists()
    assert not (tmp_path / "from-setting.db").exists()


def test_without_the_setting_the_app_uses_tickets_db_beside_app_py(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    assert app_module.DEFAULT_DATABASE_PATH == Path(app_module.__file__).parent / "tickets.db"

    # Point the default at a temporary file, so this test never opens your real tickets.db.
    monkeypatch.setattr(app_module, "DEFAULT_DATABASE_PATH", tmp_path / "default.db")
    create_app()

    assert (tmp_path / "default.db").exists()


# ---------- Sign-in (private demo) ----------


@pytest.mark.parametrize(
    "method, url",
    [
        ("GET", "/api/tickets"),
        ("POST", "/api/tickets"),
        ("POST", "/api/tickets/1/draft"),
        ("POST", "/api/tickets/1/priority"),
        ("POST", "/api/tickets/1/approve"),
    ],
)
def test_signed_out_api_requests_get_401_before_anything_happens(
    tmp_path, fake_gemini, fake_n8n, monkeypatch, method, url
):
    app = create_app(tmp_path / "test.db")
    agent = signed_in_client(app)
    agent.post("/api/tickets", json={"subject": "Charged twice", "message": "Order 1042."})
    agent.post("/api/tickets/1/draft")  # ticket 1 has a draft, so every route would have work to do
    before = agent.get("/api/tickets").get_json()
    calls_before = (len(fake_gemini.requests), len(fake_n8n.requests))

    with monkeypatch.context() as patch:
        # If any route got as far as the database, this would fail the test.
        def refuse(*args):
            raise AssertionError("A signed-out request reached the database.")

        for name in ["list_tickets", "get_ticket", "create_ticket", "save_draft", "save_priority_review", "approve_reply"]:
            patch.setattr(database, name, refuse)

        visitor = app.test_client()  # never signed in, so it has no session or CSRF token
        response = visitor.open(url, method=method, json={"subject": "Hi", "message": "Hi", "approved_reply": "Hi"})

    assert response.status_code == 401
    assert response.get_json() == {"error": "Sign in first."}
    assert (len(fake_gemini.requests), len(fake_n8n.requests)) == calls_before  # neither was called
    assert agent.get("/api/tickets").get_json() == before  # nothing changed


def test_signed_out_visitors_to_the_inbox_see_the_sign_in_page(tmp_path):
    visitor = create_app(tmp_path / "test.db").test_client()

    response = visitor.get("/")
    page = visitor.get("/", follow_redirects=True).get_data(as_text=True)

    assert response.status_code == 302
    assert response.headers["Location"] == "/sign-in"
    assert 'type="password"' in page
    assert 'id="ticket-list"' not in page  # no inbox before signing in


def test_an_incorrect_password_is_refused(tmp_path, caplog):
    visitor = create_app(tmp_path / "test.db").test_client()

    response = sign_in(visitor, password="not the password")

    assert response.status_code == 401
    assert "That password isn" in response.get_data(as_text=True)
    assert visitor.get("/api/tickets").status_code == 401  # still signed out
    assert visitor.get("/").status_code == 302
    # The attempt is logged, but no password ever is.
    assert "Failed sign-in attempt" in caplog.text
    assert "not the password" not in caplog.text and TEST_PASSWORD not in caplog.text


class FakeClock:
    """Stands in for sign_in_limits.now(), so a test can move time forward."""

    def __init__(self):
        self.seconds = 1000.0

    def __call__(self):
        return self.seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(sign_in_limits, "now", fake)
    return fake


@pytest.fixture
def password_checks(monkeypatch):
    """Counts how many times the app actually checks a password."""
    calls = []

    def counting_check(password_hash, password):
        calls.append(password)
        return check_password_hash(password_hash, password)

    monkeypatch.setattr(app_module, "check_password_hash", counting_check)
    return calls


def test_too_many_wrong_passwords_block_that_address(tmp_path, clock, password_checks, caplog):
    app = create_app(tmp_path / "test.db")
    guesser = app.test_client()

    for attempt in range(sign_in_limits.MAX_FAILURES - 1):
        assert sign_in(guesser, password=f"guess {attempt}").status_code == 401

    # The last allowed wrong password says how long to wait.
    last_try = sign_in(guesser, password="last guess")
    assert last_try.status_code == 429
    assert "Too many wrong passwords. Try again in 15 minutes." in last_try.get_data(as_text=True)
    assert last_try.headers["Retry-After"] == str(sign_in_limits.WINDOW_SECONDS)

    # Now even the right password isn't checked, so guessing gets nowhere.
    clock.seconds += 60
    checks_before = len(password_checks)
    blocked = sign_in(guesser)
    assert blocked.status_code == 429
    assert "Try again in 14 minutes." in blocked.get_data(as_text=True)
    assert len(password_checks) == checks_before
    assert guesser.get("/api/tickets").status_code == 401  # still signed out
    assert "Sign-in refused for 127.0.0.1: too many wrong passwords" in caplog.text

    # Other addresses can still sign in.
    agent = app.test_client()
    agent.environ_base["REMOTE_ADDR"] = "10.0.0.2"
    assert sign_in(agent).status_code == 302


def test_the_block_ends_when_the_wrong_passwords_are_old_enough(tmp_path, clock):
    guesser = create_app(tmp_path / "test.db").test_client()
    for attempt in range(sign_in_limits.MAX_FAILURES):
        sign_in(guesser, password=f"guess {attempt}")
        clock.seconds += 10

    # The guesses were 10 seconds apart. Once the first is over 15 minutes old, one more try is
    # allowed. It's wrong, so the block starts again, until the next oldest guess ages out too.
    clock.seconds += sign_in_limits.WINDOW_SECONDS - 49
    assert sign_in(guesser, password="one more guess").status_code == 429
    assert sign_in(guesser).status_code == 429
    clock.seconds += 60
    assert sign_in(guesser).status_code == 302


def test_a_correct_password_clears_the_count(tmp_path, clock):
    agent = create_app(tmp_path / "test.db").test_client()
    for attempt in range(sign_in_limits.MAX_FAILURES - 1):
        sign_in(agent, password=f"typo {attempt}")
    assert sign_in(agent).status_code == 302
    agent.post("/sign-out", data={"csrf_token": csrf_token_in(agent.get("/"))})

    # The earlier typos no longer count.
    for attempt in range(sign_in_limits.MAX_FAILURES - 1):
        assert sign_in(agent, password=f"typo {attempt}").status_code == 401


def test_old_wrong_passwords_are_forgotten(clock):
    failed = sign_in_limits.FailedSignIns()
    failed.record_failure("10.0.0.3")
    clock.seconds += sign_in_limits.WINDOW_SECONDS + 1
    failed.record_failure("10.0.0.4")

    assert list(failed.failures) == ["10.0.0.4"]  # memory only holds recent attempts


def test_signing_in_opens_the_inbox_and_signing_out_closes_it(tmp_path):
    agent = create_app(tmp_path / "test.db").test_client()

    # Successful sign-in: back to the inbox, with a protected session cookie.
    response = sign_in(agent)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie

    inbox = agent.get("/")
    assert inbox.status_code == 200
    assert 'id="ticket-list"' in inbox.get_data(as_text=True)
    assert "Sign out" in inbox.get_data(as_text=True)

    # An authenticated action, sent the way static/app.js sends it: with the page's CSRF token.
    token = csrf_token_in(inbox)
    created = agent.post(
        "/api/tickets", json={"subject": "Signed in", "message": "Saved after signing in."}, headers={"X-CSRFToken": token}
    )
    assert created.status_code == 201
    assert [ticket["subject"] for ticket in agent.get("/api/tickets").get_json()] == ["Signed in"]

    # Sign out: back to the sign-in page, and the API is closed again.
    signed_out = agent.post("/sign-out", data={"csrf_token": token})
    assert signed_out.status_code == 302
    assert signed_out.headers["Location"] == "/sign-in"
    assert "signed out" in agent.get("/sign-in").get_data(as_text=True)
    assert agent.get("/api/tickets").status_code == 401
    assert agent.get("/").status_code == 302


def test_changes_without_the_csrf_token_are_refused_even_when_signed_in(tmp_path):
    agent = create_app(tmp_path / "test.db").test_client()
    sign_in(agent)
    ticket = {"subject": "Hi", "message": "Hello"}

    no_token = agent.post("/api/tickets", json=ticket)
    wrong_token = agent.post("/api/tickets", json=ticket, headers={"X-CSRFToken": "made-up-token"})
    sign_out_without_token = agent.post("/sign-out")

    for response in [no_token, wrong_token]:
        assert response.status_code == 400
        assert "security token" in response.get_json()["error"]
    assert sign_out_without_token.status_code == 400
    assert agent.get("/api/tickets").get_json() == []  # nothing was saved, and still signed in


def test_the_sign_in_form_needs_its_csrf_token(tmp_path):
    visitor = create_app(tmp_path / "test.db").test_client()

    response = visitor.post("/sign-in", data={"password": TEST_PASSWORD})  # the right password, but no token

    assert response.status_code == 400
    assert visitor.get("/api/tickets").status_code == 401


@pytest.mark.parametrize(
    "name, value",
    [
        ("SECRET_KEY", None),
        ("SECRET_KEY", "too-short"),
        ("DEMO_PASSWORD_HASH", None),
        ("DEMO_PASSWORD_HASH", TEST_PASSWORD),  # the password itself instead of its hash
    ],
    ids=["no-secret-key", "short-secret-key", "no-password-hash", "plain-password"],
)
def test_the_app_refuses_to_start_without_its_sign_in_settings(tmp_path, monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match=name) as error:
        create_app(tmp_path / "test.db")

    assert TEST_PASSWORD not in str(error.value)  # the message never repeats a setting's value
    assert not (tmp_path / "test.db").exists()  # it stopped before opening the database


@pytest.mark.parametrize("localhost_http, secure", [(None, True), ("1", False)], ids=["hosted", "localhost-http"])
def test_the_session_cookie_is_https_only_unless_testing_on_localhost(tmp_path, monkeypatch, localhost_http, secure):
    if localhost_http:
        monkeypatch.setenv("LOCALHOST_HTTP", localhost_http)

    cookie = sign_in(create_app(tmp_path / "test.db").test_client()).headers["Set-Cookie"]

    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert ("; Secure" in cookie) == secure


# ---------- Generate draft (milestone 2) ----------
#
# FakeGemini stands in for genai.Client. It gives back a simulated Gemini response,
# or raises a simulated API error, so no test ever calls the real API.

SIMULATED_DRAFT = {
    "category": "Billing",
    "draft_reply": (
        "Hi there,\n\nSorry to hear you were charged twice for order 1042. Could you reply with "
        "the dates of both charges so the team can look into it?\n\nBest regards,\nThe Support Team"
    ),
}


class FakeGemini:
    """Stands in for genai.Client. Set reply_text or error to choose what "Gemini" does."""

    def __init__(self):
        self.reply_text = json.dumps(SIMULATED_DRAFT)
        self.error = None
        self.client_options = []  # what the app passed to genai.Client(...)
        self.requests = []  # what the app passed to generate_content(...)
        self.while_generating = None  # optional function to run while "Gemini" is writing
        self.models = self  # so that client.models.generate_content(...) works

    def make_client(self, **options):
        self.client_options.append(options)
        return self

    def __enter__(self):  # lets the app write "with genai.Client(...) as client:"
        return self

    def __exit__(self, *args):
        pass

    def generate_content(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        if self.while_generating:
            self.while_generating()
        if self.error:
            raise self.error
        # A real response object from the SDK, so response.text works exactly like it does live.
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(role="model", parts=[types.Part(text=self.reply_text)]))
            ]
        )


@pytest.fixture
def fake_gemini(monkeypatch):
    """Replace genai.Client with a FakeGemini for one test."""
    fake = FakeGemini()
    monkeypatch.setattr(ai_drafts.genai, "Client", fake.make_client)
    return fake


def add_ticket(client, subject="Charged twice", message="I was charged twice for order 1042."):
    """Create a ticket through the API and return its id."""
    response = client.post("/api/tickets", json={"subject": subject, "message": message})
    return response.get_json()["id"]


def simulated_api_error(code, status, message):
    """An error shaped like the ones the Gemini API sends back."""
    error_class = errors.ClientError if code < 500 else errors.ServerError
    return error_class(code, {"error": {"code": code, "message": message, "status": status}})


def test_generating_a_draft_saves_the_category_and_reply(client, fake_gemini):
    ticket_id = add_ticket(client)

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 200
    ticket = response.get_json()
    assert ticket["id"] == ticket_id
    assert ticket["category"] == "Billing"
    assert ticket["draft_reply"] == SIMULATED_DRAFT["draft_reply"]

    # How the app called Gemini.
    assert fake_gemini.client_options[0]["api_key"] == FAKE_KEY  # read from GEMINI_API_KEY
    assert fake_gemini.client_options[0]["http_options"].api_version == "v1beta"  # not the SDK's "v1" default
    request = fake_gemini.requests[0]
    assert request["model"] == "gemini-3.1-flash-lite"
    assert request["config"].response_mime_type == "application/json"  # structured JSON output
    assert request["config"].response_json_schema == ai_drafts.RESPONSE_SCHEMA
    assert request["config"].system_instruction == ai_drafts.SYSTEM_INSTRUCTION
    # The ticket is sent as JSON data, separate from the instructions.
    assert '"subject": "Charged twice"' in request["contents"]
    assert '"message": "I was charged twice for order 1042."' in request["contents"]

    # Refreshing the inbox shows the saved draft without calling Gemini again.
    assert client.get("/api/tickets").get_json() == [ticket]
    assert len(fake_gemini.requests) == 1


@pytest.mark.parametrize(
    "simulated_error, expected_status, expected_message",
    [
        (simulated_api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded."), 429, "quota"),
        (simulated_api_error(400, "INVALID_ARGUMENT", "API key not valid."), 502, "didn't accept the API key"),
        (simulated_api_error(404, "NOT_FOUND", "Model not found."), 502, "couldn't find the model"),
        (simulated_api_error(503, "UNAVAILABLE", "The model is overloaded."), 502, "unavailable or overloaded"),
        (
            simulated_api_error(400, "INVALID_ARGUMENT", "Request contains an invalid argument."),
            502,
            "Gemini rejected the request (400 INVALID_ARGUMENT): Request contains an invalid argument.",
        ),
        (
            errors.ClientError(400, {"message": "<html><title>Error 400 (Bad Request)!!1</title></html>"}),
            502,
            "wasn't pasted correctly",
        ),
        (ConnectionError("Network is unreachable"), 502, "network problem or timeout"),
    ],
    ids=[
        "quota-reached",
        "bad-api-key",
        "unknown-model",
        "gemini-overloaded",
        "other-gemini-error",
        "html-error-page",
        "no-network",
    ],
)
def test_a_gemini_failure_keeps_the_existing_draft(
    client, fake_gemini, caplog, simulated_error, expected_status, expected_message
):
    ticket_id = add_ticket(client)
    first_draft = client.post(f"/api/tickets/{ticket_id}/draft").get_json()

    fake_gemini.error = simulated_error
    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == expected_status
    assert expected_message in response.get_json()["error"]
    # Nothing new was saved, so the earlier draft is still there.
    assert client.get("/api/tickets").get_json() == [first_draft]
    # The failure is logged for you, but the API key never reaches the browser or the log.
    assert f"Draft for ticket {ticket_id} failed" in caplog.text
    assert FAKE_KEY not in response.get_data(as_text=True)
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    "reply_text",
    [
        "Sure! Here is a draft reply...",  # not JSON
        json.dumps(["Billing", "Hi there"]),  # JSON, but not an object
        json.dumps({"category": "Shipping", "draft_reply": "Hi there"}),  # not an allowed category
        json.dumps({"category": "Billing"}),  # no draft_reply
        json.dumps({"category": "Billing", "draft_reply": "   "}),  # blank draft_reply
        None,  # no text at all (for example, the answer was blocked)
    ],
    ids=["not-json", "not-an-object", "unknown-category", "missing-reply", "blank-reply", "no-text"],
)
def test_an_invalid_gemini_answer_is_not_saved(client, fake_gemini, reply_text):
    ticket_id = add_ticket(client)
    fake_gemini.reply_text = reply_text

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 502
    assert "nothing was saved" in response.get_json()["error"]
    ticket = client.get("/api/tickets").get_json()[0]
    assert ticket["category"] is None
    assert ticket["draft_reply"] is None


def test_a_missing_api_key_gives_a_helpful_message(client, fake_gemini, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    ticket_id = add_ticket(client)

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 503
    assert "GEMINI_API_KEY isn't set" in response.get_json()["error"]
    assert fake_gemini.requests == []  # Gemini was never called


def test_a_key_with_an_invisible_character_is_not_sent(client, fake_gemini, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "\x16" + FAKE_KEY)  # what Ctrl+V can type into a hidden prompt
    ticket_id = add_ticket(client)

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 503
    assert "invisible or unusual character" in response.get_json()["error"]
    assert fake_gemini.requests == []  # the bad key was never sent


def test_drafting_a_ticket_that_does_not_exist_returns_404(client, fake_gemini):
    response = client.post("/api/tickets/999/draft")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Ticket #999 doesn't exist."}
    assert fake_gemini.requests == []


def test_a_milestone_1_database_is_upgraded_without_losing_tickets(tmp_path, fake_gemini):
    database_path = tmp_path / "tickets.db"
    # Build a database exactly the way milestone 1 did: no draft columns.
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "subject TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO tickets (subject, message, created_at) VALUES (?, ?, ?)",
            ("Old ticket", "Saved before drafts existed.", "2026-11-01T09:00:00+00:00"),
        )
        connection.commit()

    create_app(database_path)  # starting the app adds the new columns
    client = signed_in_client(create_app(database_path))  # starting it again must not fail or lose data

    assert client.get("/api/tickets").get_json() == [
        {
            "id": 1,
            "subject": "Old ticket",
            "message": "Saved before drafts existed.",
            "created_at": "2026-11-01T09:00:00+00:00",
            "category": None,
            "draft_reply": None,
            "status": "Open",
            "approved_reply": None,
            "priority_review": None,
        }
    ]
    # The upgraded ticket can get a draft like any other.
    assert client.post("/api/tickets/1/draft").status_code == 200


# ---------- Review and approval (milestone 3) ----------


def add_ticket_with_draft(client):
    """Create a ticket, give it a simulated Gemini draft, and return its id."""
    ticket_id = add_ticket(client)
    client.post(f"/api/tickets/{ticket_id}/draft")
    return ticket_id


def test_approving_saves_the_edited_text_exactly_and_keeps_the_gemini_draft(client, fake_gemini):
    ticket_id = add_ticket_with_draft(client)
    # The agent's edit, ending with a line break: it must be saved exactly as typed, not trimmed.
    edited_reply = (
        "Hi Sam,\n\nSorry about the double charge on order 1042. Could you send the dates of both "
        "charges so the billing team can check them?\n\nBest regards,\nThe Support Team\n"
    )

    response = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": edited_reply})

    assert response.status_code == 200
    ticket = response.get_json()
    assert ticket["status"] == "Approved"
    assert ticket["approved_reply"] == edited_reply
    # Gemini's original draft and category are kept, so they can be compared later.
    assert ticket["draft_reply"] == SIMULATED_DRAFT["draft_reply"]
    assert ticket["category"] == "Billing"
    # Refreshing the inbox shows the approval, and approving never called Gemini.
    assert client.get("/api/tickets").get_json() == [ticket]
    assert len(fake_gemini.requests) == 1  # only the request that made the draft


@pytest.mark.parametrize(
    "body, expected_error",
    [
        ({"approved_reply": ""}, "Reply cannot be blank."),
        ({"approved_reply": " \n\t \n"}, "Reply cannot be blank."),
        ({}, "Reply cannot be blank."),
        ({"approved_reply": 42}, "Reply cannot be blank."),
        (["Hi there"], "Send the reply as a JSON object."),
    ],
    ids=["empty", "whitespace-only", "missing", "not-text", "not-an-object"],
)
def test_a_blank_reply_is_rejected(client, fake_gemini, body, expected_error):
    ticket_id = add_ticket_with_draft(client)
    before = client.get("/api/tickets").get_json()

    response = client.post(f"/api/tickets/{ticket_id}/approve", json=body)

    assert response.status_code == 400
    assert response.get_json() == {"error": expected_error}
    assert client.get("/api/tickets").get_json() == before  # still Open, with its draft unchanged


def test_an_approved_ticket_cannot_get_a_new_draft(client, fake_gemini):
    ticket_id = add_ticket_with_draft(client)
    approved = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Approved text"})
    fake_gemini.reply_text = json.dumps({"category": "Technical", "draft_reply": "A different draft"})

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 409
    assert response.get_json() == {"error": f"Ticket #{ticket_id} is already approved, so it can't get a new draft."}
    assert len(fake_gemini.requests) == 1  # Gemini wasn't asked again
    assert client.get("/api/tickets").get_json() == [approved.get_json()]  # nothing changed


def test_a_draft_that_finishes_after_approval_does_not_replace_it(client, fake_gemini, tmp_path):
    ticket_id = add_ticket_with_draft(client)
    fake_gemini.reply_text = json.dumps({"category": "Technical", "draft_reply": "A newer draft"})
    # Someone approves the reply (say, in another browser tab) while Gemini is still writing.
    fake_gemini.while_generating = lambda: database.approve_reply(tmp_path / "test.db", ticket_id, "Approved text")

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 409
    assert "was approved while Gemini was writing" in response.get_json()["error"]
    ticket = client.get("/api/tickets").get_json()[0]
    assert ticket["status"] == "Approved"
    assert ticket["approved_reply"] == "Approved text"
    assert ticket["draft_reply"] == SIMULATED_DRAFT["draft_reply"]  # the newer draft wasn't saved
    assert ticket["category"] == "Billing"


def test_a_reply_is_approved_once_and_only_after_a_draft(client, fake_gemini):
    missing = client.post("/api/tickets/999/approve", json={"approved_reply": "Hello"})
    assert missing.status_code == 404
    assert missing.get_json() == {"error": "Ticket #999 doesn't exist."}

    ticket_id = add_ticket(client)
    no_draft = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Hello"})
    assert no_draft.status_code == 409
    assert "has no draft to approve yet" in no_draft.get_json()["error"]

    client.post(f"/api/tickets/{ticket_id}/draft")
    first = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "First approval"})
    second = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Second approval"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "is already approved" in second.get_json()["error"]
    assert client.get("/api/tickets").get_json() == [first.get_json()]  # the first approval stands


def test_a_database_failure_while_approving_returns_an_error_message(client, fake_gemini, monkeypatch):
    ticket_id = add_ticket_with_draft(client)

    def broken_approve_reply(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "approve_reply", broken_approve_reply)

    response = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Hello"})

    assert response.status_code == 500
    assert response.get_json() == {"error": "The approval could not be saved. Please try again."}


def test_a_milestone_2_database_keeps_its_drafts_ready_for_review(tmp_path, fake_gemini):
    database_path = tmp_path / "tickets.db"
    # Build a database exactly the way milestone 2 did: drafts, but no approval columns.
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "subject TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute("ALTER TABLE tickets ADD COLUMN category TEXT")
        connection.execute("ALTER TABLE tickets ADD COLUMN draft_reply TEXT")
        connection.execute(
            "INSERT INTO tickets (subject, message, created_at, category, draft_reply) VALUES (?, ?, ?, ?, ?)",
            ("Charged twice", "Order 1042.", "2026-11-20T09:00:00+00:00", "Billing", "Hi there, a saved draft."),
        )
        connection.execute(
            "INSERT INTO tickets (subject, message, created_at) VALUES (?, ?, ?)",
            ("No draft yet", "Saved without a draft.", "2026-11-21T09:00:00+00:00"),
        )
        connection.commit()

    create_app(database_path)  # starting the app adds the approval columns
    client = signed_in_client(create_app(database_path))  # starting it again must not fail or lose data

    assert client.get("/api/tickets").get_json() == [
        {
            "id": 2,
            "subject": "No draft yet",
            "message": "Saved without a draft.",
            "created_at": "2026-11-21T09:00:00+00:00",
            "category": None,
            "draft_reply": None,
            "status": "Open",
            "approved_reply": None,
            "priority_review": None,
        },
        {
            "id": 1,
            "subject": "Charged twice",
            "message": "Order 1042.",
            "created_at": "2026-11-20T09:00:00+00:00",
            "category": "Billing",
            "draft_reply": "Hi there, a saved draft.",
            "status": "Open",
            "approved_reply": None,
            "priority_review": None,
        },
    ]
    # The saved draft is ready for review: it can be approved straight away.
    response = client.post("/api/tickets/1/approve", json={"approved_reply": "Hi there, a saved draft."})
    assert response.get_json()["status"] == "Approved"
    assert fake_gemini.requests == []  # none of this called Gemini


# ---------- Priority check with n8n (milestone 4) ----------
#
# FakeN8n stands in for urllib.request.urlopen, which n8n_triage.py uses to call the webhook.
# The fake_n8n fixture at the top of this file puts it in place for every test.


class FakeN8n:
    """By default "n8n" answers {"ticket_id": <the id it was sent>, "priority_review": true}.

    Set priority_review, answer_text (the exact text to answer with), or error to change that.
    """

    def __init__(self):
        self.priority_review = True
        self.answer_text = None
        self.error = None
        self.requests = []  # what the app sent to the webhook
        self.while_checking = None  # optional function to run while "n8n" is deciding

    def urlopen(self, request, timeout):
        sent = json.loads(request.data)
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "content_type": request.get_header("Content-type"),
                "json": sent,
                "timeout": timeout,
            }
        )
        if self.while_checking:
            self.while_checking()
        if self.error:
            raise self.error
        answer_text = self.answer_text
        if answer_text is None:
            answer_text = json.dumps({"ticket_id": sent["ticket_id"], "priority_review": self.priority_review})
        # BytesIO behaves like a real response here: the app reads it inside a "with" block.
        return io.BytesIO(answer_text.encode("utf-8"))


def test_a_new_draft_is_sent_to_n8n_and_its_priority_flag_is_saved(client, fake_gemini, fake_n8n):
    ticket_id = add_ticket(client)

    response = client.post(f"/api/tickets/{ticket_id}/draft")

    assert response.status_code == 200
    ticket = response.get_json()
    assert ticket["priority_review"] is True
    assert "priority_error" not in ticket
    # What Flask sent: only the ticket id, the saved category, and approved, as JSON, with a short timeout.
    assert fake_n8n.requests == [
        {
            "url": FAKE_WEBHOOK_URL,
            "method": "POST",
            "content_type": "application/json",
            "json": {"ticket_id": ticket_id, "category": "Billing", "approved": False},
            "timeout": 5,
        }
    ]
    assert fake_n8n.requests[0]["json"]["approved"] is False  # JSON false, not 0
    # Refreshing the inbox reads the saved flag without asking n8n again.
    assert client.get("/api/tickets").get_json() == [ticket]
    assert len(fake_n8n.requests) == 1

    # A new draft is checked again, and n8n's new answer replaces the old one.
    fake_n8n.priority_review = False
    assert client.post(f"/api/tickets/{ticket_id}/draft").get_json()["priority_review"] is False
    assert client.get("/api/tickets").get_json()[0]["priority_review"] is False


# The ticket in this test is ticket 1, and each wrong answer would clear its flag if it were saved.
@pytest.mark.parametrize(
    "answer_text, expected_message",
    [
        (json.dumps({"ticket_id": 2, "priority_review": False}), "had ticket_id 2, but it must be the number 1"),
        (json.dumps({"ticket_id": "1", "priority_review": False}), 'had ticket_id "1", but it must be the number 1'),
        (json.dumps({"ticket_id": True, "priority_review": False}), "had ticket_id true, but it must be the number 1"),
        (json.dumps({"ticket_id": 1, "priority_review": "false"}), 'had priority_review "false", but it must be true'),
        (json.dumps({"ticket_id": 1, "priority_review": 0}), "had priority_review 0, but it must be true or false"),
        (json.dumps({"ticket_id": 1}), "had priority_review null, but it must be true or false"),
        (json.dumps({"message": "Workflow was started"}), "had ticket_id null"),
        (json.dumps([{"ticket_id": 1, "priority_review": False}]), "wasn't a JSON object"),
        ("", "wasn't a JSON object"),
    ],
    ids=[
        "another-ticket",
        "id-as-text",
        "id-true",
        "flag-as-text",
        "flag-as-number",
        "no-flag",
        "responded-immediately",
        "a-list",
        "empty",
    ],
)
def test_an_invalid_n8n_answer_is_not_saved(client, fake_gemini, fake_n8n, answer_text, expected_message):
    ticket_id = add_ticket_with_draft(client)  # n8n flagged it, so priority_review is True
    before = client.get("/api/tickets").get_json()
    fake_n8n.answer_text = answer_text

    response = client.post(f"/api/tickets/{ticket_id}/priority")

    assert response.status_code == 502
    assert expected_message in response.get_json()["error"]
    assert client.get("/api/tickets").get_json() == before  # the flag and the draft are unchanged


@pytest.mark.parametrize(
    "simulated_error, expected_status, expected_message",
    [
        (TimeoutError("timed out"), 504, "didn't answer within 5 seconds"),
        (urllib.error.URLError(TimeoutError("timed out")), 504, "didn't answer within 5 seconds"),
        (urllib.error.URLError(ConnectionRefusedError(10061, "Connection refused")), 502, "Couldn't reach n8n"),
        (http.client.RemoteDisconnected("Remote end closed connection"), 502, "Couldn't reach n8n"),
        (urllib.error.HTTPError(FAKE_WEBHOOK_URL, 404, "Not Found", {}, None), 502, "no webhook at that address"),
        (urllib.error.HTTPError(FAKE_WEBHOOK_URL, 500, "Server Error", {}, None), 502, "workflow failed (HTTP 500)"),
    ],
    ids=["slow-answer", "slow-connection", "n8n-stopped", "connection-dropped", "workflow-inactive", "workflow-failed"],
)
def test_when_n8n_is_unavailable_the_draft_is_kept_and_the_check_can_be_retried(
    client, fake_gemini, fake_n8n, caplog, simulated_error, expected_status, expected_message
):
    ticket_id = add_ticket(client)
    fake_n8n.error = simulated_error

    # The draft is still saved, and the response says why priority wasn't checked.
    drafted = client.post(f"/api/tickets/{ticket_id}/draft")
    assert drafted.status_code == 200
    assert drafted.get_json()["draft_reply"] == SIMULATED_DRAFT["draft_reply"]
    assert drafted.get_json()["priority_review"] is None
    assert expected_message in drafted.get_json()["priority_error"]

    # Check priority fails the same way and changes nothing.
    checked = client.post(f"/api/tickets/{ticket_id}/priority")
    assert checked.status_code == expected_status
    assert expected_message in checked.get_json()["error"]
    saved = client.get("/api/tickets").get_json()[0]
    assert saved["draft_reply"] == SIMULATED_DRAFT["draft_reply"]
    assert saved["priority_review"] is None

    # The failure is logged for you, but the webhook URL never reaches the browser or the log.
    assert f"Priority check for ticket {ticket_id} failed" in caplog.text
    for text in [drafted.get_data(as_text=True), checked.get_data(as_text=True), caplog.text]:
        assert FAKE_WEBHOOK_URL not in text

    # Once n8n is back, Check priority works, using the saved draft (Gemini isn't asked again).
    fake_n8n.error = None
    retried = client.post(f"/api/tickets/{ticket_id}/priority")
    assert retried.status_code == 200
    assert retried.get_json()["priority_review"] is True
    assert len(fake_gemini.requests) == 1


@pytest.mark.parametrize(
    "webhook_url, expected_message",
    [
        (None, "N8N_TRIAGE_WEBHOOK_URL isn't set"),
        ("localhost:5678/webhook/ticket-triage", "must be the webhook's full address"),
        ("file:///C:/Windows/win.ini", "must be the webhook's full address"),
        ("http://localhost:5678/webhook/\x16ticket-triage", "must be the webhook's full address"),
    ],
    ids=["not-set", "no-http", "not-a-web-address", "invisible-character"],
)
def test_without_a_usable_webhook_url_n8n_is_not_called_and_the_reply_can_be_approved(
    client, fake_gemini, fake_n8n, monkeypatch, webhook_url, expected_message
):
    if webhook_url is None:
        monkeypatch.delenv("N8N_TRIAGE_WEBHOOK_URL")
    else:
        monkeypatch.setenv("N8N_TRIAGE_WEBHOOK_URL", webhook_url)
    ticket_id = add_ticket(client)

    drafted = client.post(f"/api/tickets/{ticket_id}/draft").get_json()
    checked = client.post(f"/api/tickets/{ticket_id}/priority")

    assert expected_message in drafted["priority_error"]
    assert checked.status_code == 503
    assert expected_message in checked.get_json()["error"]
    assert fake_n8n.requests == []  # nothing was sent anywhere
    # The draft was saved, and the agent can approve it without n8n.
    approved = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": drafted["draft_reply"]})
    assert approved.status_code == 200
    assert approved.get_json()["status"] == "Approved"


def test_approving_clears_the_priority_flag(client, fake_gemini, fake_n8n):
    ticket_id = add_ticket_with_draft(client)
    assert client.get("/api/tickets").get_json()[0]["priority_review"] is True

    response = client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Approved text"})

    ticket = response.get_json()
    assert ticket["status"] == "Approved"
    assert ticket["priority_review"] is None
    assert client.get("/api/tickets").get_json() == [ticket]  # still cleared after a refresh
    assert len(fake_n8n.requests) == 1  # approving didn't call n8n


@pytest.mark.parametrize("route, expected_status", [("draft", 200), ("priority", 409)])
def test_a_late_n8n_answer_does_not_bring_the_flag_back_after_approval(
    client, fake_gemini, fake_n8n, tmp_path, route, expected_status
):
    ticket_id = add_ticket_with_draft(client)
    # The agent approves the reply (say, in another browser tab) while n8n is still deciding,
    # and then n8n answers "priority_review": true.
    fake_n8n.while_checking = lambda: database.approve_reply(tmp_path / "test.db", ticket_id, "Approved text")

    response = client.post(f"/api/tickets/{ticket_id}/{route}")

    assert response.status_code == expected_status
    body = response.get_json()
    message = body["priority_error"] if route == "draft" else body["error"]
    assert "was approved or got a new draft while n8n was checking it" in message
    ticket = client.get("/api/tickets").get_json()[0]
    assert ticket["status"] == "Approved"
    assert ticket["approved_reply"] == "Approved text"
    assert ticket["priority_review"] is None  # the late answer wasn't saved


def test_an_answer_about_an_older_draft_is_not_saved(client, fake_gemini, fake_n8n, tmp_path):
    ticket_id = add_ticket_with_draft(client)  # a Billing draft
    # While n8n checks the Billing draft, a Technical draft replaces it (say, from another tab).
    fake_n8n.while_checking = lambda: database.save_draft(tmp_path / "test.db", ticket_id, "Technical", "Newer")

    response = client.post(f"/api/tickets/{ticket_id}/priority")

    assert response.status_code == 409
    ticket = client.get("/api/tickets").get_json()[0]
    assert ticket["category"] == "Technical"
    assert ticket["priority_review"] is None  # the answer about the Billing draft wasn't saved


def test_check_priority_needs_an_open_ticket_with_a_draft(client, fake_gemini, fake_n8n):
    missing = client.post("/api/tickets/999/priority")
    assert missing.status_code == 404
    assert missing.get_json() == {"error": "Ticket #999 doesn't exist."}

    ticket_id = add_ticket(client)
    no_draft = client.post(f"/api/tickets/{ticket_id}/priority")
    assert no_draft.status_code == 409
    assert "has no draft yet" in no_draft.get_json()["error"]

    client.post(f"/api/tickets/{ticket_id}/draft")
    client.post(f"/api/tickets/{ticket_id}/approve", json={"approved_reply": "Approved text"})
    approved = client.post(f"/api/tickets/{ticket_id}/priority")
    assert approved.status_code == 409
    assert "is already approved" in approved.get_json()["error"]

    assert len(fake_n8n.requests) == 1  # only the check that came with the draft


def test_a_milestone_3_database_keeps_its_tickets_and_saved_drafts_can_be_checked(tmp_path, fake_gemini, fake_n8n):
    database_path = tmp_path / "tickets.db"
    # Build a database exactly the way milestone 3 did: drafts and approvals, but no priority column.
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "subject TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute("ALTER TABLE tickets ADD COLUMN category TEXT")
        connection.execute("ALTER TABLE tickets ADD COLUMN draft_reply TEXT")
        connection.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'Open'")
        connection.execute("ALTER TABLE tickets ADD COLUMN approved_reply TEXT")
        connection.execute(
            "INSERT INTO tickets (subject, message, created_at, category, draft_reply, status, approved_reply) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("Charged twice", "Order 1042.", "2026-11-20T09:00:00+00:00", "Billing", "Hi, a draft.", "Approved", "Hi!"),
        )
        connection.execute(
            "INSERT INTO tickets (subject, message, created_at, category, draft_reply) VALUES (?, ?, ?, ?, ?)",
            ("App crashes", "On export.", "2026-11-21T09:00:00+00:00", "Technical", "Hi, a saved draft."),
        )
        connection.commit()

    create_app(database_path)  # starting the app adds the priority column
    client = signed_in_client(create_app(database_path))  # starting it again must not fail or lose data

    assert client.get("/api/tickets").get_json() == [
        {
            "id": 2,
            "subject": "App crashes",
            "message": "On export.",
            "created_at": "2026-11-21T09:00:00+00:00",
            "category": "Technical",
            "draft_reply": "Hi, a saved draft.",
            "status": "Open",
            "approved_reply": None,
            "priority_review": None,
        },
        {
            "id": 1,
            "subject": "Charged twice",
            "message": "Order 1042.",
            "created_at": "2026-11-20T09:00:00+00:00",
            "category": "Billing",
            "draft_reply": "Hi, a draft.",
            "status": "Approved",
            "approved_reply": "Hi!",
            "priority_review": None,
        },
    ]

    # Check priority on the saved draft: n8n gets the saved category, and Gemini isn't called.
    response = client.post("/api/tickets/2/priority")

    assert response.status_code == 200
    assert response.get_json()["priority_review"] is True
    assert response.get_json()["draft_reply"] == "Hi, a saved draft."
    assert fake_n8n.requests[0]["json"] == {"ticket_id": 2, "category": "Technical", "approved": False}
    assert fake_gemini.requests == []
