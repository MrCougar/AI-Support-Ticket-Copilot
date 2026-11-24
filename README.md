# AI Support Ticket Copilot

A small support inbox built with Python, Flask, SQLite, and vanilla JavaScript. Google Gemini drafts the replies, an n8n workflow flags tickets that need priority review, and a person reviews and approves the replies.

- **Milestone 1:** submit a customer ticket in the browser, see it in the inbox (newest first), and still see it after restarting the app.
- **Milestone 2:** click **Generate draft** on a ticket to get a category (Billing, Technical, Account, or Other) and a draft reply from Gemini. Both are saved under the ticket as **Draft for review**. Nothing is ever sent to the customer.
- **Milestone 3:** edit the draft right in the inbox, then click **Approve reply**. The text is saved exactly as typed and the ticket is marked **Approved**. Gemini's original draft is kept separately for comparison. An approved reply is read-only, and the server refuses new drafts for that ticket. Approving only updates the app's database: it doesn't call Gemini or send anything to the customer.
- **Milestone 4:** after a draft is saved, Flask asks your n8n workflow whether the ticket needs priority review. Flagged tickets that aren't approved yet show a **Priority review** label. Approving the reply clears the flag. **Check priority** asks n8n again about a saved draft, without generating a new one.

## Setup (Windows PowerShell)

You need Python 3 (tested with Python 3.14) and Git. Get the code:

```powershell
git clone https://github.com/MrCougar/AI-Support-Ticket-Copilot.git
cd AI-Support-Ticket-Copilot
```

Then run these once, from the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `python` isn't recognized, use `py` for the first command. If PowerShell says *running scripts is disabled on this system*, allow local scripts for your user account, then run the `Activate.ps1` line again:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Updating an existing copy? Activate the venv and run the `pip install` line again to add new packages (the Gemini package from milestone 2, and Waitress for serving the app).

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

## n8n priority check

Flask sends each saved draft's ticket to an n8n webhook. The workflow is included as `workflows/Support Ticket Triage.json`. It flags `Billing` tickets that aren't approved yet, and answers `false` for everything else.

To use it:
1. In n8n, import the file (**Import from File** in the workflow editor's menu).
2. Make sure the workflow is active.
3. Copy the **Production URL** from its Webhook node.

Set that address in the terminal you'll start the app from. On Windows, with n8n running locally on port 18080, it is:

```powershell
$env:N8N_TRIAGE_WEBHOOK_URL = "http://localhost:18080/webhook/ticket-triage"
```

Use your own n8n address and port if they differ. The path `ticket-triage` comes from the included workflow's Webhook node.

If the webhook has no authentication, anyone with the URL can run the workflow, so keep it out of project files. The app never sends it to the browser or writes it to the logs.

Flask sends a `POST` with only these three fields, never the customer's message or the reply:

```json
{"ticket_id": 7, "category": "Billing", "approved": false}
```

`approved` is always `false`, because only tickets that aren't approved yet get checked. n8n must answer with JSON like this:

```json
{"ticket_id": 7, "priority_review": true}
```

The answer is checked before it's saved:
- `ticket_id` must be the same number that was sent. `"7"` (text) doesn't match.
- `priority_review` must be `true` or `false`. `"true"` (text) doesn't count.

Anything else is shown as an error and nothing is saved. In n8n, make sure that:
- the Webhook node accepts `POST`;
- the workflow is active, because the Production URL only answers then;
- the Webhook node answers with that JSON, either when the last node finishes or through a **Respond to Webhook** node. If it responds **Immediately**, n8n sends "Workflow was started" instead.

Flask waits up to 5 seconds for an answer (`TIMEOUT_SECONDS` in `n8n_triage.py`). If n8n is stopped, slow, or answers with an error, the draft is still saved, the inbox shows why, and the agent can click **Check priority** to try again or simply approve the reply. Without `N8N_TRIAGE_WEBHOOK_URL`, everything else still works and the priority check explains what's missing.

## Run the app

In the terminal you'll run the app from, set `GEMINI_API_KEY` and `N8N_TRIAGE_WEBHOOK_URL` first (see above). Environment variables are read when the app starts, so restart the app after changing one.

```powershell
.\.venv\Scripts\Activate.ps1   # needed in each new terminal
python app.py                  # Flask's development server, with debugging on
```

Open <http://127.0.0.1:5000>. Press `Ctrl+C` in the terminal to stop the server. This development server is only for your own computer. To serve the app, use Waitress (see [Serve with Waitress](#serve-with-waitress)).

Tickets, drafts, approvals, and n8n's answers are saved in `tickets.db` in the project folder, next to `app.py`, whichever folder you start the app from. `DATABASE_PATH` can choose another file (see [Database location](#database-location)). The database is created the first time the app runs, and Git ignores it. A database from an earlier milestone is upgraded automatically when the app starts. The new columns are added, and every ticket, draft, and approval is kept. Saved drafts open ready for review, and **Check priority** works on them straight away. To start over with an empty inbox, stop the app and run `Remove-Item tickets.db`.

## Serve with Waitress

[Waitress](https://docs.pylonsproject.org/projects/waitress/) is a production web server for Python apps, installed with the other requirements. It builds the app by calling `create_app()` in `app.py`, and it never turns on Flask's debugger.

Run `waitress-serve` from the project folder, because it looks for `app.py` in the current folder. Set the environment variables first, in the same terminal or in your host's settings.

**Test it on this computer (Windows PowerShell):**

```powershell
.\.venv\Scripts\Activate.ps1
waitress-serve --listen=127.0.0.1:8080 --call app:create_app
```

Open <http://127.0.0.1:8080>. Press `Ctrl+C` to stop it.

**On a host** that gives the app its port in the `PORT` environment variable, use this start command:

```bash
waitress-serve --host=0.0.0.0 --port=$PORT --call app:create_app
```

In PowerShell, write `--port=$env:PORT` instead.

Set these in the host's environment settings, never in project files:

| Variable | Example | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | `your-gemini-api-key` | Generates drafts |
| `N8N_TRIAGE_WEBHOOK_URL` | `https://n8n.example.com/webhook/ticket-triage` | The priority check. It must be an address the host can reach: `localhost` only works when n8n runs on the same machine as the app |
| `DATABASE_PATH` | `/var/data/tickets.db` | Where the SQLite file is kept (see below) |

### Database location

Without `DATABASE_PATH`, the app uses `tickets.db` next to `app.py`. To keep the database somewhere else, set `DATABASE_PATH` to the file's full path before starting the app:

```powershell
$env:DATABASE_PATH = "D:\ticket-data\tickets.db"
```

The folder must already exist. The file is created and upgraded on the first start, and the same path keeps the same tickets across restarts.

On a host, use a path on a persistent disk. Many hosts reset the app's own folder on every deploy, which would delete the tickets. Run a single copy of the app, because SQLite can't be shared between servers.

The app has no sign-in. Anyone who can open its address can read tickets, approve replies, and use your Gemini quota. Don't make it public without putting access control in front of it.

## Run the tests

```powershell
python -m pytest
```

The tests use a temporary database, a simulated Gemini, and a simulated n8n webhook. They never touch `tickets.db`, need no API key or n8n, and never call the real services.

## API

| Request | Body | Success response |
| --- | --- | --- |
| `GET /api/tickets` | none | `200` with a list of tickets, newest first |
| `POST /api/tickets` | `{"subject": "...", "message": "..."}` | `201` with the saved ticket |
| `POST /api/tickets/<id>/draft` | none | `200` with the ticket, including its new draft and n8n's answer |
| `POST /api/tickets/<id>/priority` | none | `200` with the ticket, including n8n's new answer |
| `POST /api/tickets/<id>/approve` | `{"approved_reply": "..."}` | `200` with the ticket, now `Approved` |

A ticket looks like `{"id": 1, "subject": "...", "message": "...", "created_at": "2026-11-22T14:03:00+00:00", "category": "Billing", "draft_reply": "...", "status": "Open", "approved_reply": null, "priority_review": true}`.

- `category` and `draft_reply` are `null` until a draft is generated.
- `status` is `"Open"` until the reply is approved, then `"Approved"`.
- `approved_reply` holds the approved text, exactly as the agent saved it. `draft_reply` keeps Gemini's original draft.
- `priority_review` is `null` until n8n answers, then `true` or `false`. A new draft sets it back to `null` until n8n checks it, because the category may have changed. Approving the reply also sets it to `null`.

Errors come back as `{"error": "..."}`. A failed request changes nothing, so saved drafts, approvals, and n8n's earlier answers stay as they were.

The draft route is the one exception. If Gemini succeeds but the n8n check fails, the draft is still saved, and the `200` response adds `"priority_error": "..."` with the reason.

| Status | Meaning |
| --- | --- |
| `400` | A ticket field or the reply to approve is blank or whitespace-only |
| `404` | No ticket has that id |
| `409` | The ticket is already approved (no new draft, no second approval, no priority check). Or it has no draft yet. Or it was approved or got a new draft while n8n was checking it, so n8n's late answer was dropped |
| `429` | The Gemini quota or rate limit was reached |
| `500` | The database couldn't save |
| `502` | Gemini or n8n failed, couldn't be reached, or gave an invalid answer |
| `503` | `GEMINI_API_KEY` or `N8N_TRIAGE_WEBHOOK_URL` isn't set or isn't valid |
| `504` | n8n didn't answer within 5 seconds |

## Project layout

| File | What it does |
| --- | --- |
| `templates/index.html` | The page: ticket form and inbox |
| `static/app.js` | Sends tickets, draft requests, priority checks, and approvals to the API, and draws the inbox |
| `static/style.css` | Page styles |
| `app.py` | Flask routes and validation |
| `database.py` | SQLite code: create and upgrade the table, save tickets, drafts, priority flags, and approvals, list tickets |
| `ai_drafts.py` | Gemini: the prompt, the model name, structured output, and checking the answer |
| `n8n_triage.py` | n8n: send the ticket to the webhook, wait up to 5 seconds, and check the answer |
| `test_app.py` | Tests for tickets, drafts, approvals, and priority checks (Gemini and n8n are simulated) |
