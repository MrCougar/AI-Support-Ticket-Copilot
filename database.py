"""All the SQLite code for the app lives in this file.

Each function opens the database file, does one job, and closes it again.
`closing(...)` makes sure the connection is closed when the `with` block ends,
even if something goes wrong inside it.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone


def init_db(database_path):
    """Create the tickets table on the first run, and add newer columns to older databases.

    Existing tickets are never deleted: ALTER TABLE only adds new, empty columns.
    """
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

        # Milestone 2 added AI drafts. Databases made before that don't have these columns,
        # so add them now. Old tickets keep all their data; the new columns start empty (NULL).
        # PRAGMA table_info returns one row per column, and row[1] is the column's name.
        column_names = [row[1] for row in connection.execute("PRAGMA table_info(tickets)")]
        if "category" not in column_names:
            connection.execute("ALTER TABLE tickets ADD COLUMN category TEXT")
        if "draft_reply" not in column_names:
            connection.execute("ALTER TABLE tickets ADD COLUMN draft_reply TEXT")

        # Milestone 3 added human approval. Existing tickets start as "Open" with no approved
        # reply, so their saved drafts are ready for review. draft_reply keeps Gemini's original
        # draft, and approved_reply will hold the agent's final text, so the two can be compared.
        if "status" not in column_names:
            connection.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'Open'")
        if "approved_reply" not in column_names:
            connection.execute("ALTER TABLE tickets ADD COLUMN approved_reply TEXT")

        # Milestone 4 added the n8n priority check. priority_review is NULL until n8n answers,
        # then 1 (needs priority review) or 0. Existing tickets, drafts, and approvals are unchanged.
        if "priority_review" not in column_names:
            connection.execute("ALTER TABLE tickets ADD COLUMN priority_review INTEGER")

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

    # Read the new ticket back so it has the same fields as every other ticket
    # (status starts as "Open", and the draft, approval, and priority fields are None).
    return get_ticket(database_path, ticket_id)


def get_ticket(database_path, ticket_id):
    """Return one ticket as a dictionary, or None if no ticket has that id."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row  # Lets the row be turned into a dict below.
        row = connection.execute(
            "SELECT id, subject, message, created_at, category, draft_reply, status, approved_reply, "
            "priority_review FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()

    if row is None:
        return None
    return row_to_ticket(row)


def list_tickets(database_path):
    """Return every ticket as a list of dictionaries, newest first."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row  # Lets each row be turned into a dict below.
        rows = connection.execute(
            # Newest first. If two tickets were created in the same second,
            # the one with the higher id was saved later.
            "SELECT id, subject, message, created_at, category, draft_reply, status, approved_reply, "
            "priority_review FROM tickets ORDER BY created_at DESC, id DESC"
        ).fetchall()

    return [row_to_ticket(row) for row in rows]


def row_to_ticket(row):
    """Turn one database row into a ticket dictionary.

    SQLite saves True and False as 1 and 0, so priority_review is turned back into
    True or False here (None means n8n hasn't answered).
    """
    ticket = dict(row)
    if ticket["priority_review"] is not None:
        ticket["priority_review"] = bool(ticket["priority_review"])
    return ticket


def save_draft(database_path, ticket_id, category, draft_reply):
    """Save a category and draft reply on a ticket, replacing any earlier draft.

    Returns True if it was saved. Returns False, and changes nothing, if the ticket is Approved.
    """
    with closing(sqlite3.connect(database_path)) as connection:
        # One UPDATE writes both fields at once, so a category and its draft always belong together.
        # "status != 'Approved'" protects an approval, even one made while Gemini was still writing.
        # n8n's earlier answer was about the old draft's category, so priority_review goes back to
        # NULL until n8n checks the new one.
        cursor = connection.execute(
            "UPDATE tickets SET category = ?, draft_reply = ?, priority_review = NULL "
            "WHERE id = ? AND status != 'Approved'",
            (category, draft_reply, ticket_id),
        )
        connection.commit()
        return cursor.rowcount == 1  # rowcount is how many rows the UPDATE changed


def save_priority_review(database_path, ticket_id, category, priority_review):
    """Save n8n's answer (True or False) on a ticket. category is the one that was sent to n8n.

    Returns True if it was saved. Returns False, and changes nothing, if the ticket was approved,
    or got a new draft with another category, while n8n was checking it.
    """
    with closing(sqlite3.connect(database_path)) as connection:
        # "status != 'Approved'" stops a late answer from bringing the flag back after an approval.
        # "category = ?" stops an answer about an older draft from replacing the newest draft's answer.
        cursor = connection.execute(
            "UPDATE tickets SET priority_review = ? WHERE id = ? AND status != 'Approved' AND category = ?",
            (priority_review, ticket_id, category),
        )
        connection.commit()
        return cursor.rowcount == 1


def approve_reply(database_path, ticket_id, approved_reply):
    """Save the agent's final reply exactly as given, mark the ticket Approved, and clear its priority flag.

    Gemini's original draft_reply is left as it was. Returns True if it was saved.
    Returns False, and changes nothing, if the ticket has no draft or is already Approved.
    """
    with closing(sqlite3.connect(database_path)) as connection:
        # One UPDATE sets the reply and the status together, so an approved ticket always has its reply.
        # It also clears priority_review: an approved ticket no longer needs priority review.
        cursor = connection.execute(
            "UPDATE tickets SET approved_reply = ?, status = 'Approved', priority_review = NULL "
            "WHERE id = ? AND draft_reply IS NOT NULL AND status != 'Approved'",
            (approved_reply, ticket_id),
        )
        connection.commit()
        return cursor.rowcount == 1
