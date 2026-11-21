# AI Support Ticket Copilot

A small support inbox built with Python, Flask, SQLite, and vanilla JavaScript.

**Milestone 1:** submit a customer ticket in the browser, see it in the inbox (newest first), and still see it after restarting the app. AI features and n8n automation come in later milestones.

## Setup (Windows PowerShell)

Run these once, from the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `python` isn't recognized, use `py` for the first command. If PowerShell says *running scripts is disabled on this system*, allow local scripts for your user account, then run the `Activate.ps1` line again:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

## Run the app

```powershell
.\.venv\Scripts\Activate.ps1   # needed in each new terminal
python app.py
```

Open <http://127.0.0.1:5000>. Press `Ctrl+C` in the terminal to stop the server.

Tickets are saved in `tickets.db`, which is created the first time the app runs. To start over with an empty inbox, stop the app and run `Remove-Item tickets.db`.

## Run the tests

```powershell
python -m pytest
```

The tests use a temporary database, so they never touch `tickets.db`.

## API

| Request | Body | Success response |
| --- | --- | --- |
| `GET /api/tickets` | none | `200` with a list of tickets, newest first |
| `POST /api/tickets` | `{"subject": "...", "message": "..."}` | `201` with the saved ticket |

A saved ticket looks like `{"id": 1, "subject": "...", "message": "...", "created_at": "2026-11-22T14:03:00+00:00"}`. Errors come back as `{"error": "..."}`: status `400` when a field is blank or whitespace-only, `500` when the database can't save the ticket.

## Project layout

| File | What it does |
| --- | --- |
| `templates/index.html` | The page: ticket form and inbox |
| `static/app.js` | Sends the form to the API and draws the inbox |
| `static/style.css` | Page styles |
| `app.py` | Flask routes and validation |
| `database.py` | SQLite code: create the table, save a ticket, list tickets |
| `test_app.py` | Tests for creating, validating, and listing tickets |
