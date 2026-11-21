"""All the SQLite code for the app lives in this file.

Each function opens the database file, does one job, and closes it again.
`closing(...)` makes sure the connection is closed when the `with` block ends,
even if something goes wrong inside it.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def init_db(database_path):
    """Create the tickets table on the first run. Later runs keep the existing table and its tickets."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                subject    TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()


def create_ticket(database_path, subject, message):
    """Save one ticket and return it as a dictionary, including its new id."""
    # The current time in UTC, for example "2026-11-22T14:03:00+00:00".
    # The browser turns it into the viewer's local time.
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with closing(sqlite3.connect(database_path)) as connection:
        # The ? placeholders let sqlite3 insert the values safely (no SQL injection).
        cursor = connection.execute(
            "INSERT INTO tickets (subject, message, created_at) VALUES (?, ?, ?)",
            (subject, message, created_at),
        )
        connection.commit()  # Without this, the new row would be thrown away.
        ticket_id = cursor.lastrowid  # The id SQLite just gave the new ticket.

    return {
        "id": ticket_id,
        "subject": subject,
        "message": message,
        "created_at": created_at,
    }


def list_tickets(database_path):
    """Return every ticket as a list of dictionaries, newest first."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row  # Lets each row be turned into a dict below.
        rows = connection.execute(
            # Newest first. If two tickets were created in the same second,
            # the one with the higher id was saved later.
            "SELECT id, subject, message, created_at FROM tickets "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()

    return [dict(row) for row in rows]
