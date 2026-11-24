"""Flask web server for the AI Support Ticket Copilot.

For development (debug mode):  python app.py
Then open:                      http://127.0.0.1:5000

To serve it with Waitress (debugging off), see "Serve with Waitress" in README.md:
    waitress-serve --listen=127.0.0.1:8080 --call app:create_app
"""

import os
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import ai_drafts
import database
import n8n_triage

# tickets.db sits next to this file, whichever folder you start the app from.
# To keep the database somewhere else (for example on a host's persistent disk),
# set the DATABASE_PATH environment variable to the SQLite file's full path.
DEFAULT_DATABASE_PATH = Path(__file__).parent / "tickets.db"


def clean_text(value):
    """Return the text with spaces and line breaks trimmed from both ends, or "" if it isn't text."""
    if isinstance(value, str):
        return value.strip()
    return ""


def create_app(database_path=None):
    """Build the Flask app.

    The tests pass in a temporary database_path. Waitress calls create_app() with no
    arguments, so then the DATABASE_PATH environment variable chooses the SQLite file,
    and without it the app uses tickets.db next to this file.
    """
    if database_path is None:
        database_path = os.environ.get("DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH
    app = Flask(__name__)
    database.init_db(database_path)

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
        """Send the page with the ticket form and the inbox."""
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
