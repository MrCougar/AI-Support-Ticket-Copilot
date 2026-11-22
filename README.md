# AI Support Ticket Copilot

A small support inbox built with Python, Flask, SQLite, and vanilla JavaScript. Google Gemini drafts the replies, and a person reviews and approves them.

- **Milestone 1:** submit a customer ticket in the browser, see it in the inbox (newest first), and still see it after restarting the app.
- **Milestone 2:** click **Generate draft** on a ticket to get a category (Billing, Technical, Account, or Other) and a draft reply from Gemini. Both are saved under the ticket as **Draft for review**. Nothing is ever sent to the customer.
- **Milestone 3:** edit the draft right in the inbox, then click **Approve reply**. The text is saved exactly as typed and the ticket is marked **Approved**. Gemini's original draft is kept separately for comparison. An approved reply is read-only, and the server refuses new drafts for that ticket. Approving only updates the app's database: it doesn't call Gemini or send anything to the customer.

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

Tickets, drafts, and approvals are saved in `tickets.db`, which is created the first time the app runs. A database from an earlier milestone is upgraded automatically when the app starts: the new columns are added, every ticket and draft is kept, and saved drafts open ready for review. To start over with an empty inbox, stop the app and run `Remove-Item tickets.db`.

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
| `POST /api/tickets/<id>/approve` | `{"approved_reply": "..."}` | `200` with the ticket, now `Approved` |

A ticket looks like `{"id": 1, "subject": "...", "message": "...", "created_at": "2026-11-22T14:03:00+00:00", "category": "Billing", "draft_reply": "...", "status": "Open", "approved_reply": null}`.

- `category` and `draft_reply` are `null` until a draft is generated.
- `status` is `"Open"` until the reply is approved, then `"Approved"`.
- `approved_reply` holds the approved text, exactly as the agent saved it. `draft_reply` keeps Gemini's original draft.

Errors come back as `{"error": "..."}`. A failed request changes nothing, so saved drafts and approvals stay as they were:

| Status | Meaning |
| --- | --- |
| `400` | A ticket field or the reply to approve is blank or whitespace-only |
| `404` | No ticket has that id |
| `409` | The ticket is already approved (no new draft, no second approval), or it has no draft to approve yet |
| `429` | The Gemini quota or rate limit was reached |
| `500` | The database couldn't save |
| `502` | Gemini failed or gave an invalid answer |
| `503` | `GEMINI_API_KEY` isn't set, or contains an invisible character |

## Project layout

| File | What it does |
| --- | --- |
| `templates/index.html` | The page: ticket form and inbox |
| `static/app.js` | Sends tickets, draft requests, and approvals to the API, and draws the inbox |
| `static/style.css` | Page styles |
| `app.py` | Flask routes and validation |
| `database.py` | SQLite code: create and upgrade the table, save tickets, drafts, and approvals, list tickets |
| `ai_drafts.py` | Gemini: the prompt, the model name, structured output, and checking the answer |
| `test_app.py` | Tests for tickets, drafts, and approvals (Gemini is simulated) |
