"""Flask web server for the AI Support Ticket Copilot.

For development (debug mode):  python app.py
Then open:                      http://127.0.0.1:5000

To serve it with Waitress (debugging off), see "Serve with Waitress" in README.md:
    waitress-serve --listen=127.0.0.1:8080 --call app:create_app
"""

import math
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.security import check_password_hash

import ai_drafts
import database
import n8n_triage
import sign_in_limits

# tickets.db sits next to this file, whichever folder you start the app from.
# To keep the database somewhere else (for example on a host's persistent disk),
# set the DATABASE_PATH environment variable to the SQLite file's full path.
DEFAULT_DATABASE_PATH = Path(__file__).parent / "tickets.db"


def clean_text(value):
    """Return the text with spaces and line breaks trimmed from both ends, or "" if it isn't text."""
    if isinstance(value, str):
        return value.strip()
    return ""


def read_sign_in_settings():
    """Read SECRET_KEY and DEMO_PASSWORD_HASH from the environment.

    The app refuses to start without both, so it can never run unprotected. Returns both values.
    """
    secret_key = os.environ.get("SECRET_KEY", "").strip()
    password_hash = os.environ.get("DEMO_PASSWORD_HASH", "").strip()

    problems = []
    if len(secret_key) < 32:
        problems.append("SECRET_KEY must be set to a random value of at least 32 characters.")
    # A Werkzeug hash looks like "scrypt:32768:8:1$<salt>$<hash>". The password itself would not.
    hash_method = password_hash.split(":", 1)[0]
    if password_hash.count("$") != 2 or hash_method not in ("scrypt", "pbkdf2"):
        problems.append(
            "DEMO_PASSWORD_HASH must be set to a password hash made with Werkzeug "
            "(it starts with scrypt: or pbkdf2:), never the password itself."
        )
    if problems:
        raise RuntimeError(" ".join(problems) + ' See "Sign-in settings" in README.md.')
    return secret_key, password_hash


def create_app(database_path=None):
    """Build the Flask app.

    The tests pass in a temporary database_path. Waitress calls create_app() with no
    arguments, so then the DATABASE_PATH environment variable chooses the SQLite file,
    and without it the app uses tickets.db next to this file.
    """
    if database_path is None:
        database_path = os.environ.get("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH
    secret_key, password_hash = read_sign_in_settings()

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,  # signs the session cookie, so it can't be forged or edited
        # The session cookie can't be read by JavaScript or sent along with other sites' requests,
        # and it is only sent over HTTPS. LOCALHOST_HTTP=1 lifts the HTTPS rule for testing on
        # http://127.0.0.1 on your own computer. Never set it on a host.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("LOCALHOST_HTTP", "").strip() != "1",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),  # then the agent signs in again
        # CSRF tokens are checked in require_sign_in() below, after the sign-in check, so a
        # signed-out request always gets 401. A token lasts as long as the sign-in.
        WTF_CSRF_CHECK_DEFAULT=False,
        WTF_CSRF_TIME_LIMIT=None,
    )
    csrf = CSRFProtect(app)  # also gives the templates csrf_token()
    failed_sign_ins = sign_in_limits.FailedSignIns()  # wrong passwords per address, for rate limiting
    database.init_db(database_path)

    @app.before_request
    def require_sign_in():
        """Runs before every request. Only the sign-in page and static files are open to everyone.

        A signed-out API request gets a 401 here, before any route reads tickets, changes data,
        or calls Gemini or n8n. Every POST must also carry the page's CSRF token.
        """
        if request.endpoint not in ("sign_in", "static") and not session.get("signed_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sign in first."}), 401  # 401 = "Unauthorized"
            return redirect(url_for("sign_in"))
        if request.method == "POST":
            csrf.protect()  # raises CSRFError (handled below) if the token is missing or wrong

    @app.errorhandler(CSRFError)
    def csrf_failed(error):
        """A POST without a valid CSRF token, for example from another site or an old page."""
        message = "This page's security token is missing or out of date. Refresh the page and try again."
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 400
        return render_template("sign_in.html", error=message), 400

    @app.route("/sign-in", methods=["GET", "POST"])
    def sign_in():
        """Show the sign-in page, and check the demo password when the form is sent."""
        if session.get("signed_in"):
            return redirect(url_for("home"))
        if request.method == "GET":
            return render_template("sign_in.html")

        # Rate limiting: after too many wrong passwords from this address, don't even check
        # the password, so guessing gets nowhere (see sign_in_limits.py).
        address = request.remote_addr
        if failed_sign_ins.seconds_until_allowed(address):
            app.logger.warning("Sign-in refused for %s: too many wrong passwords", address)
            return too_many_attempts(address)

        # check_password_hash() hashes the typed password the same way and compares the results.
        if not check_password_hash(password_hash, request.form.get("password", "")):
            app.logger.warning("Failed sign-in attempt from %s", address)
            failed_sign_ins.record_failure(address)
            if failed_sign_ins.seconds_until_allowed(address):
                return too_many_attempts(address)  # that was the last allowed try
            return render_template("sign_in.html", error="That password isn't right. Try again."), 401

        failed_sign_ins.clear(address)
        session.clear()  # start a fresh session, with a new CSRF token
        session["signed_in"] = True
        session.permanent = True  # keeps the sign-in for PERMANENT_SESSION_LIFETIME
        return redirect(url_for("home"))

    def too_many_attempts(address):
        """The sign-in page saying how long to wait. 429 = "Too Many Requests"."""
        wait_seconds = max(1, failed_sign_ins.seconds_until_allowed(address))
        minutes = math.ceil(wait_seconds / 60)
        message = f"Too many wrong passwords. Try again in {minutes} minute{'' if minutes == 1 else 's'}."
        # Retry-After tells browsers and scripts how many seconds to wait.
        return render_template("sign_in.html", error=message), 429, {"Retry-After": str(wait_seconds)}

    @app.post("/sign-out")
    def sign_out():
        """Forget the sign-in in this browser and go back to the sign-in page."""
        session.clear()
        flash("You're signed out.")
        return redirect(url_for("sign_in"))

    def run_priority_check(ticket):
        """Send a saved ticket's id, category, and approval status to n8n, and save n8n's answer.

        Used by both routes below that check priority. Raises n8n_triage.TriageError, with a
        message for the browser, if n8n can't answer or its answer can't be saved.
        """
        try:
            priority_review = n8n_triage.check_priority(ticket["id"], ticket["category"], ticket["status"] == "Approved")
        except n8n_triage.TriageError as error:
            # The terminal log shows the cause (for example a timeout) but never the webhook URL.
            app.logger.warning("Priority check for ticket %s failed: %s Cause: %r", ticket["id"], error, error.__cause__)
            raise

        try:
            saved = database.save_priority_review(database_path, ticket["id"], ticket["category"], priority_review)
        except sqlite3.Error:
            app.logger.exception("Saving a priority check failed")
            raise n8n_triage.TriageError("n8n answered, but the result could not be saved. Please try again.", 500)
        if not saved:
            # The ticket was approved, or got a new draft, while n8n was checking it. The late
            # answer is dropped, so it can't bring the flag back after an approval.
            message = (
                f"Ticket #{ticket['id']} was approved or got a new draft while n8n was checking it, "
                "so n8n's answer wasn't saved. Refresh the page."
            )
            raise n8n_triage.TriageError(message, 409)

    @app.get("/")
    def home():
        """Send the page with the ticket form and the inbox (only after signing in)."""
        return render_template("index.html")

    @app.get("/api/tickets")
    def api_list_tickets():
        """Send every ticket as JSON, newest first."""
        tickets = database.list_tickets(database_path)
        return jsonify(tickets)

    @app.post("/api/tickets")
    def api_create_ticket():
        """Check the submitted ticket, save it, and send the saved ticket back as JSON."""
        data = request.get_json(silent=True)  # None if the body isn't valid JSON
        if not isinstance(data, dict):
            return jsonify({"error": "Send the ticket as a JSON object."}), 400

        subject = clean_text(data.get("subject"))
        message = clean_text(data.get("message"))

        # After trimming, a blank or whitespace-only field is "", so it is rejected here.
        errors = []
        if not subject:
            errors.append("Subject cannot be blank.")
        if not message:
            errors.append("Message cannot be blank.")
        if errors:
            return jsonify({"error": " ".join(errors)}), 400  # 400 = "Bad Request"

        try:
            ticket = database.create_ticket(database_path, subject, message)
        except sqlite3.Error:
            app.logger.exception("Saving a ticket failed")  # full details in the terminal
            return jsonify({"error": "The ticket could not be saved. Please try again."}), 500

        return jsonify(ticket), 201  # 201 = "Created"

    @app.post("/api/tickets/<int:ticket_id>/draft")
    def api_generate_draft(ticket_id):
        """Ask Gemini for a category and draft reply, save both, check priority with n8n,
        and send back the updated ticket.

        The draft is only saved for review. Nothing is sent to the customer.
        """
        ticket = database.get_ticket(database_path, ticket_id)
        if ticket is None:
            return jsonify({"error": f"Ticket #{ticket_id} doesn't exist."}), 404
        if ticket["status"] == "Approved":
            # An approved reply is final, so Gemini isn't even asked. 409 = "Conflict".
            return jsonify({"error": f"Ticket #{ticket_id} is already approved, so it can't get a new draft."}), 409

        try:
            draft = ai_drafts.generate_draft(ticket["subject"], ticket["message"])
        except ai_drafts.DraftError as error:
            # Nothing gets saved, so any earlier draft stays as it was.
            # The terminal log shows the cause (for example Google's error) but never the API key.
            app.logger.warning("Draft for ticket %s failed: %s Cause: %r", ticket_id, error, error.__cause__)
            return jsonify({"error": str(error)}), error.status_code

        try:
            saved = database.save_draft(database_path, ticket_id, draft["category"], draft["draft_reply"])
        except sqlite3.Error:
            app.logger.exception("Saving a draft failed")
            return jsonify({"error": "The draft could not be saved. Please try again."}), 500
        if not saved:
            # Someone approved the reply while Gemini was writing. The approval wins.
            message = f"Ticket #{ticket_id} was approved while Gemini was writing, so the new draft wasn't saved."
            return jsonify({"error": message}), 409

        # The draft is saved. Now ask n8n whether the ticket needs priority review. If that fails,
        # the draft stays saved, and the agent can click Check priority to try again, or approve.
        priority_error = None
        try:
            run_priority_check(database.get_ticket(database_path, ticket_id))
        except n8n_triage.TriageError as error:
            priority_error = str(error)

        ticket = database.get_ticket(database_path, ticket_id)
        if priority_error:
            ticket["priority_error"] = priority_error  # the browser shows it under the saved draft
        return jsonify(ticket)

    @app.post("/api/tickets/<int:ticket_id>/priority")
    def api_check_priority(ticket_id):
        """Ask n8n whether a ticket needs priority review, using its saved category, and send it back.

        This uses the draft that's already saved: it doesn't call Gemini or change the draft.
        """
        ticket = database.get_ticket(database_path, ticket_id)
        if ticket is None:
            return jsonify({"error": f"Ticket #{ticket_id} doesn't exist."}), 404
        if ticket["status"] == "Approved":
            return jsonify({"error": f"Ticket #{ticket_id} is already approved, so its priority isn't checked."}), 409
        if ticket["category"] is None:
            message = f"Ticket #{ticket_id} has no draft yet, so there's no category to check. Generate a draft first."
            return jsonify({"error": message}), 409

        try:
            run_priority_check(ticket)
        except n8n_triage.TriageError as error:
            # Nothing was saved, so the draft and any earlier answer from n8n stay as they were.
            return jsonify({"error": str(error)}), error.status_code

        return jsonify(database.get_ticket(database_path, ticket_id))

    @app.post("/api/tickets/<int:ticket_id>/approve")
    def api_approve_reply(ticket_id):
        """Save the agent's reply exactly as typed, mark the ticket Approved, and send it back.

        Approving also clears the ticket's Priority review flag. This only updates SQLite:
        it doesn't call Gemini or n8n, or send anything to the customer.
        """
        ticket = database.get_ticket(database_path, ticket_id)
        if ticket is None:
            return jsonify({"error": f"Ticket #{ticket_id} doesn't exist."}), 404

        data = request.get_json(silent=True)  # None if the body isn't valid JSON
        if not isinstance(data, dict):
            return jsonify({"error": "Send the reply as a JSON object."}), 400

        approved_reply = data.get("approved_reply")
        # clean_text() is only used to check for blank text. The reply itself is saved exactly as typed.
        if not clean_text(approved_reply):
            return jsonify({"error": "Reply cannot be blank."}), 400

        if ticket["draft_reply"] is None:
            return jsonify({"error": f"Ticket #{ticket_id} has no draft to approve yet. Generate a draft first."}), 409

        try:
            approved = database.approve_reply(database_path, ticket_id, approved_reply)
        except sqlite3.Error:
            app.logger.exception("Approving a reply failed")
            return jsonify({"error": "The approval could not be saved. Please try again."}), 500
        if not approved:
            # approve_reply() found the ticket already Approved, so its reply stays as it was.
            message = f"Ticket #{ticket_id} is already approved. Refresh the page to see the approved reply."
            return jsonify({"error": message}), 409

        return jsonify(database.get_ticket(database_path, ticket_id))

    return app


if __name__ == "__main__":
    # debug=True restarts the server when you edit a .py file and shows detailed errors.
    create_app().run(debug=True)
