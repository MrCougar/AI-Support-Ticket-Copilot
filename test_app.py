"""Tests for creating, validating, and listing tickets, generating drafts, and approving replies.

Run them with:  python -m pytest

Each test gets a fresh app with its own empty database in a temporary folder,
so the tests never touch your real tickets.db. Gemini is always simulated:
the tests need no API key and never call the real API.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import errors, types

import ai_drafts
import database
from app import create_app

FAKE_KEY = "fake-key-for-tests"


@pytest.fixture
def client(tmp_path):
    """A test client for an app with a brand-new, empty database."""
    app = create_app(tmp_path / "test.db")
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def never_call_the_real_gemini(monkeypatch):
    """Safety net for every test: use a fake API key, and refuse to create a real Gemini client."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)

    def refuse(**options):
        raise AssertionError("A test tried to create a real Gemini client. Use the fake_gemini fixture.")

    monkeypatch.setattr(ai_drafts.genai, "Client", refuse)


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

    first_run = create_app(database_path).test_client()
    first_run.post("/api/tickets", json={"subject": "Refund", "message": "Please refund order 1042."})

    # A brand-new app using the same database file is the same as restarting the server.
    second_run = create_app(database_path).test_client()
    tickets = second_run.get("/api/tickets").get_json()

    assert len(tickets) == 1
    assert tickets[0]["subject"] == "Refund"
    assert tickets[0]["message"] == "Please refund order 1042."


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
    client = create_app(database_path).test_client()  # starting it again must not fail or lose data

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
    client = create_app(database_path).test_client()  # starting it again must not fail or lose data

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
        },
    ]
    # The saved draft is ready for review: it can be approved straight away.
    response = client.post("/api/tickets/1/approve", json={"approved_reply": "Hi there, a saved draft."})
    assert response.get_json()["status"] == "Approved"
    assert fake_gemini.requests == []  # none of this called Gemini
