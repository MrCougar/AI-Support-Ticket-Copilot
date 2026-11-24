"""Ask the n8n triage workflow whether a ticket needs priority review.

After a draft is saved, or when the agent clicks Check priority, Flask sends n8n three
fields as JSON:

    {"ticket_id": 7, "category": "Billing", "approved": false}

and n8n answers with:

    {"ticket_id": 7, "priority_review": true}

Only those three fields are sent, never the customer's message or the reply. The answer
is checked before anything is saved: it must be about the same ticket, and priority_review
must be true or false.

The webhook address comes from the N8N_TRIAGE_WEBHOOK_URL environment variable on the
server. It is never sent to the browser or written to the logs. urllib is part of Python,
so this needs no extra package.
"""

import http.client
import json
import os
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 5  # a short wait: if n8n is down, the agent finds out quickly and can retry


class TriageError(Exception):
    """The priority check didn't work. The message is written to be shown in the browser."""

    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code  # the HTTP status app.py sends back (502 = "Bad Gateway")


def check_priority(ticket_id, category, approved):
    """Send one ticket to the n8n webhook and return its answer: True if it needs priority review.

    Raises TriageError with a helpful message if n8n can't be reached or its answer isn't valid.
    """
    url = os.environ.get("N8N_TRIAGE_WEBHOOK_URL", "").strip()
    if not url:
        raise TriageError(
            "N8N_TRIAGE_WEBHOOK_URL isn't set, so priority can't be checked. "
            "Set it in the terminal that runs the app, then restart the app.",
            status_code=503,
        )
    # Only web addresses (urllib would also open files on this computer), with no spaces
    # or invisible characters picked up while pasting.
    if not url.startswith(("http://", "https://")) or not all("!" <= character <= "~" for character in url):
        raise TriageError(
            "N8N_TRIAGE_WEBHOOK_URL must be the webhook's full address, starting with http:// or https:// "
            "and without spaces. Set it again, then restart the app.",
            status_code=503,
        )

    body = json.dumps({"ticket_id": ticket_id, "category": category, "approved": approved})
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer_text = response.read()
    except urllib.error.HTTPError as error:
        # n8n answered, but with an error status (for example 404 when the workflow isn't active).
        raise TriageError(explain_http_error(error.code)) from error
    except (OSError, http.client.HTTPException) as error:
        # No answer at all: n8n is stopped, the address is wrong, or it took too long.
        if is_timeout(error):
            raise TriageError(
                f"n8n didn't answer within {TIMEOUT_SECONDS} seconds. "
                "Check that n8n is running and that the workflow finishes quickly.",
                status_code=504,  # 504 = "Gateway Timeout"
            ) from error
        raise TriageError(
            "Couldn't reach n8n. Check that n8n is running and that N8N_TRIAGE_WEBHOOK_URL is right."
        ) from error

    return parse_answer(answer_text, ticket_id)


def parse_answer(answer_text, ticket_id):
    """Check n8n's JSON answer and return priority_review. Nothing is saved unless the answer is valid."""
    try:
        answer = json.loads(answer_text)
    except ValueError:  # an empty answer, or not JSON
        answer = None
    if not isinstance(answer, dict):
        raise TriageError(
            'n8n\'s answer wasn\'t a JSON object like {"ticket_id": 7, "priority_review": true}, '
            "so it wasn't saved. Check what the workflow responds with."
        )

    # type(...) is int, rather than isinstance(): Python counts True as the number 1,
    # and "7" (text) isn't the number 7 either.
    answer_id = answer.get("ticket_id")
    if type(answer_id) is not int or answer_id != ticket_id:
        raise TriageError(
            f"n8n's answer had ticket_id {json.dumps(answer_id)}, but it must be the number {ticket_id} "
            "(the ticket that was sent), so it wasn't saved."
        )

    priority_review = answer.get("priority_review")
    if not isinstance(priority_review, bool):
        raise TriageError(
            f"n8n's answer had priority_review {json.dumps(priority_review)}, but it must be true or false, "
            "so it wasn't saved."
        )
    return priority_review


def explain_http_error(status_code):
    """A message for an error status from n8n. It never repeats the webhook address."""
    if status_code == 404:
        return (
            "n8n has no webhook at that address (HTTP 404). Check that N8N_TRIAGE_WEBHOOK_URL is the "
            "Production URL, that the workflow is active, and that the Webhook node accepts POST."
        )
    if status_code >= 500:
        return f"The n8n workflow failed (HTTP {status_code}). Check its Executions list in n8n for the error."
    return f"n8n refused the request (HTTP {status_code}). Check the Webhook node's settings in n8n."


def is_timeout(error):
    """urllib reports a timeout either directly, or wrapped inside a URLError's reason."""
    return isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
