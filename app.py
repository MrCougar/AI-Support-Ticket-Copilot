"""Flask web server for the AI Support Ticket Copilot (milestone 1).

Start it with:  python app.py
Then open:      http://127.0.0.1:5000
"""

import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

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

    return app


if __name__ == "__main__":
    # debug=True restarts the server when you edit a .py file and shows detailed errors.
    create_app().run(debug=True)
