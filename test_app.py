"""Tests for creating, validating, and listing tickets.

Run them with:  python -m pytest

Each test gets a fresh app with its own empty database in a temporary folder,
so the tests never touch your real tickets.db.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import database
from app import create_app


@pytest.fixture
def client(tmp_path):
    """A test client for an app with a brand-new, empty database."""
    app = create_app(tmp_path / "test.db")
    app.config["TESTING"] = True
    return app.test_client()


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
