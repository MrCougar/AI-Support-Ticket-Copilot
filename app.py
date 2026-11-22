"""Flask web server for the AI Support Ticket Copilot.

Start it with:  python app.py
Then open:      http://127.0.0.1:5000
"""

import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import ai_drafts
import database

# tickets.db sits next to this file, whichever folder you start the app from.
DEFAULT_DATABASE_PATH = Path(__file__).parent / "tickets.db"


def clean_text(value):
    """Return the text with spaces and line breaks trimmed from both ends, or "" if it isn't text."""
    if isinstance(value, str):
        return value.strip()
    return ""


def create_app(database_path=DEFAULT_DATABASE_PATH):
    """Build the Flask app. The tests pass in a temporary database_path."""
    app = Flask(__name__)
    database.init_db(database_path)

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
        """Ask Gemini for a category and draft reply, save both, and send back the updated ticket.

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

        return jsonify(database.get_ticket(database_path, ticket_id))

    @app.post("/api/tickets/<int:ticket_id>/approve")
    def api_approve_reply(ticket_id):
        """Save the agent's reply exactly as typed, mark the ticket Approved, and send it back.

        This only updates SQLite. It doesn't call Gemini or send anything to the customer.
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
