# AI Support Ticket Copilot

A small support inbox built with Python, Flask, SQLite, and vanilla JavaScript, with AI draft replies from Google Gemini.

- **Milestone 1:** submit a customer ticket in the browser, see it in the inbox (newest first), and still see it after restarting the app.
- **Milestone 2:** click **Generate draft** on a ticket to get a category (Billing, Technical, Account, or Other) and a draft reply from Gemini. Both are saved under the ticket as **Draft for review**. Nothing is ever sent to the customer.

n8n automation comes in a later milestone.

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

Coming from milestone 1? Activate the venv and run the `pip install` line again to add the Gemini package.

## Gemini API key

Drafts need a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey). The app reads it on the server from the `GEMINI_API_KEY` environment variable, so it never reaches the browser.

Set it in the terminal you'll start the app from. This asks for the key without showing it, and keeps it only for that terminal window:

```powershell
$env:GEMINI_API_KEY = [Net.NetworkCredential]::new("", (Read-Host "Gemini API key" -AsSecureString)).Password
```

At the hidden prompt, **right-click to paste**. Ctrl+V can type an invisible character instead of pasting. To check the key without showing it (AI Studio keys are usually 39 characters and start with `AIza`):

```powershell
$k = $env:GEMINI_API_KEY; "length: $($k.Length), starts with AIza: $($k.StartsWith('AIza')), only normal characters: $($k -cmatch '^[!-~]+$')"
```

Never put the key in a project file or commit it. Without a key, everything else still works and **Generate draft** explains what's missing. The model (`gemini-3.1-flash-lite`) is set by `MODEL` in `ai_drafts.py`, and the Gemini API version (`v1beta`) by `API_VERSION`.

## Run the app

```powershell
.\.venv\Scripts\Activate.ps1   # needed in each new terminal
python app.py
```

Open <http://127.0.0.1:5000>. Press `Ctrl+C` in the terminal to stop the server.

Tickets and drafts are saved in `tickets.db`, which is created the first time the app runs. A milestone 1 database is upgraded automatically when the app starts: the draft columns are added and every ticket is kept. To start over with an empty inbox, stop the app and run `Remove-Item tickets.db`.

## Run the tests

```powershell
python -m pytest
```

The tests use a temporary database and a simulated Gemini, so they never touch `tickets.db`, need no API key, and never call the real API.

## API

| Request | Body | Success response |
| --- | --- | --- |
| `GET /api/tickets` | none | `200` with a list of tickets, newest first |
| `POST /api/tickets` | `{"subject": "...", "message": "..."}` | `201` with the saved ticket |
| `POST /api/tickets/<id>/draft` | none | `200` with the ticket, including its new draft |

A ticket looks like `{"id": 1, "subject": "...", "message": "...", "created_at": "2026-11-22T14:03:00+00:00", "category": "Billing", "draft_reply": "..."}`. `category` and `draft_reply` are `null` until a draft is generated.

Errors come back as `{"error": "..."}`, and a failed draft request never changes the saved draft:

| Status | Meaning |
| --- | --- |
| `400` | A ticket field is blank or whitespace-only |
| `404` | No ticket has that id |
| `429` | The Gemini quota or rate limit was reached |
| `500` | The database couldn't save |
| `502` | Gemini failed or gave an invalid answer |
| `503` | `GEMINI_API_KEY` isn't set |

## Project layout

| File | What it does |
| --- | --- |
| `templates/index.html` | The page: ticket form and inbox |
| `static/app.js` | Sends tickets and draft requests to the API, and draws the inbox |
| `static/style.css` | Page styles |
| `app.py` | Flask routes and validation |
| `database.py` | SQLite code: create and upgrade the table, save tickets and drafts, list tickets |
| `ai_drafts.py` | Gemini: the prompt, the model name, structured output, and checking the answer |
| `test_app.py` | Tests for tickets and drafts (Gemini is simulated) |
