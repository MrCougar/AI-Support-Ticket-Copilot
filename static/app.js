// static/app.js runs in the browser. It talks to the Flask routes in app.py:
//   GET  /api/tickets               -> load the inbox
//   POST /api/tickets               -> save a new ticket
//   POST /api/tickets/<id>/draft    -> have the server ask Gemini for a draft reply (then n8n for priority)
//   POST /api/tickets/<id>/priority -> have the server ask n8n again, using the saved category
//   POST /api/tickets/<id>/approve  -> save the agent's edited reply and mark the ticket Approved

// The page elements this script uses (their ids are in templates/index.html).
const form = document.getElementById("ticket-form");
const subjectInput = document.getElementById("subject");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const formStatus = document.getElementById("form-status");
const inboxStatus = document.getElementById("inbox-status");
const ticketList = document.getElementById("ticket-list");

// Tickets waiting for the server, and the message to show meanwhile (for example
// 3 -> "Generating draft…"). createTicketItem() checks this, so a busy ticket keeps its
// buttons disabled even if the inbox is redrawn while it waits.
const ticketsInProgress = new Map();

// Replies the agent has edited but not approved yet (ticket id -> text). The inbox is
// redrawn after every new ticket, and this keeps the typed text from being lost.
const unsavedReplies = new Map();

// Ask the server for every ticket and draw them in the inbox.
async function loadTickets() {
  try {
    const response = await fetch("/api/tickets");
    if (!response.ok) {
      throw new Error(`GET /api/tickets failed with status ${response.status}`);
    }
    const tickets = await response.json(); // the server already sorted them newest first
    showTickets(tickets);
  } catch (error) {
    console.error(error);
    showStatus(inboxStatus, "Could not load tickets. Is the server running?", "error");
  }
}

// Replace whatever the inbox showed before with this list of tickets.
function showTickets(tickets) {
  ticketList.replaceChildren(); // remove the tickets drawn last time
  for (const ticket of tickets) {
    ticketList.append(createTicketItem(ticket));
  }

  if (tickets.length === 0) {
    showStatus(inboxStatus, "No tickets yet. Submit one to get started.", "");
  } else {
    showStatus(inboxStatus, "", ""); // an empty status line is hidden by style.css
  }
}

// Build the <li> for one ticket. We use textContent and .value (never innerHTML), so whatever
// the customer typed, Gemini wrote, or the agent edited is shown as plain text and can't run.
function createTicketItem(ticket) {
  const item = document.createElement("li");
  item.className = "ticket";
  item.id = `ticket-${ticket.id}`; // lets generateDraft() and approveReply() find this card later

  const subject = document.createElement("h3");
  subject.textContent = ticket.subject;

  const createdAt = new Date(ticket.created_at).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const details = document.createElement("p");
  details.className = "ticket-details";
  details.textContent = `#${ticket.id} · ${createdAt}`;

  const message = document.createElement("p");
  message.className = "ticket-message";
  message.textContent = ticket.message;

  // n8n's answer is saved with the ticket, so the label comes back after a refresh too.
  // Approving clears the flag on the server, and approved tickets never show the label.
  if (ticket.priority_review && ticket.status !== "Approved") {
    item.classList.add("priority");
    const badge = document.createElement("p");
    badge.className = "priority-badge";
    badge.textContent = "Priority review";
    item.append(badge);
  }

  item.append(subject, details, message);

  // Saved drafts and approvals come back with the ticket from GET /api/tickets,
  // so refreshing the page shows them again without calling Gemini.
  if (ticket.status === "Approved") {
    item.append(createApprovedBox(ticket));
  } else if (ticket.draft_reply) {
    item.append(createReviewBox(ticket));
  }
  item.append(createTicketActions(ticket));

  // Still waiting for the server (the inbox was redrawn meanwhile), so stay disabled.
  if (ticketsInProgress.has(ticket.id)) {
    setTicketBusy(item, true);
    showStatus(item.querySelector(".ticket-actions .status"), ticketsInProgress.get(ticket.id), "");
  }
  return item;
}

// A draft waiting for review. The agent can edit the reply here, then approve it.
function createReviewBox(ticket) {
  const box = document.createElement("div");
  box.className = "draft";

  const editor = document.createElement("textarea");
  editor.className = "reply-editor";
  editor.rows = 8;
  editor.setAttribute("aria-label", `Reply for ticket #${ticket.id}`);
  // Start from the agent's unsaved edits if there are any, otherwise from Gemini's draft.
  editor.value = unsavedReplies.has(ticket.id) ? unsavedReplies.get(ticket.id) : ticket.draft_reply;
  editor.addEventListener("input", () => unsavedReplies.set(ticket.id, editor.value));

  box.append(createReplyLabel("Draft for review", ticket.category), editor);
  return box;
}

// An approved reply: read-only text labelled "Approved". Approving only saved it in this app.
function createApprovedBox(ticket) {
  const box = document.createElement("div");
  box.className = "draft approved";

  const reply = document.createElement("p");
  reply.className = "draft-reply";
  reply.textContent = ticket.approved_reply;

  const note = document.createElement("p");
  note.className = "reply-note";
  note.textContent = "Approved in this app only. Nothing has been sent to the customer.";

  box.append(createReplyLabel("Approved", ticket.category), reply, note);
  return box;
}

// The top line of a reply box: its label on the left, the category on the right.
function createReplyLabel(text, category) {
  const label = document.createElement("p");
  label.className = "draft-label";
  label.textContent = text;

  const categoryTag = document.createElement("span");
  categoryTag.className = "draft-category";
  categoryTag.textContent = `Category: ${category}`;
  label.append(categoryTag);
  return label;
}

// The buttons under a ticket, and its status line. Approved tickets get no buttons:
// their reply is final, and the server refuses new drafts for them too.
function createTicketActions(ticket) {
  const actions = document.createElement("div");
  actions.className = "ticket-actions";

  if (ticket.status !== "Approved") {
    if (ticket.draft_reply) {
      actions.append(
        createButton("Approve reply", "approve-button", () => approveReply(ticket.id)),
        createButton("Check priority", "priority-button", () => checkPriority(ticket.id)),
      );
    }
    const draftText = ticket.draft_reply ? "Regenerate draft" : "Generate draft";
    actions.append(createButton(draftText, "draft-button", () => generateDraft(ticket.id)));
  }

  const statusLine = document.createElement("p");
  statusLine.className = "status";
  statusLine.setAttribute("role", "status");
  actions.append(statusLine);
  return actions;
}

// One button in a ticket's action row. onClick runs when it's clicked.
function createButton(text, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", onClick);
  return button;
}

// Runs when a ticket's Generate draft (or Regenerate draft) button is clicked.
async function generateDraft(ticketId) {
  // A new draft replaces the text in the editor, so ask before throwing away the agent's edits.
  if (unsavedReplies.has(ticketId) && !confirm("Replace your edited reply with a new draft?")) {
    return;
  }

  ticketsInProgress.set(ticketId, "Generating draft…");
  showTicketStatus(ticketId, "Generating draft…", ""); // this also disables the ticket's buttons

  try {
    const response = await fetch(`/api/tickets/${ticketId}/draft`, { method: "POST" });
    const result = await response.json();
    ticketsInProgress.delete(ticketId);

    if (!response.ok) {
      // For example: no API key, quota reached, Gemini failed, or the ticket is already approved.
      // The server saved nothing, so the card still shows what it showed before.
      showTicketStatus(ticketId, result.error || "The draft could not be generated.", "error");
      return;
    }

    // Saved. Draw this ticket's card again so the editor shows the new draft.
    unsavedReplies.delete(ticketId);
    replaceTicketCard(result);

    if (result.priority_error) {
      // The draft is saved, but n8n couldn't check it (for example, n8n isn't running).
      showTicketStatus(
        ticketId,
        `Draft saved for review, but the priority check failed: ${result.priority_error} ` +
          "Click Check priority to try again, or approve the reply.",
        "error",
      );
    } else if (result.priority_review) {
      showTicketStatus(
        ticketId,
        "Draft saved for review and flagged for priority review. Nothing was sent to the customer.",
        "success",
      );
    } else {
      showTicketStatus(
        ticketId,
        "Draft saved for review. No priority review needed. Nothing was sent to the customer.",
        "success",
      );
    }
  } catch (error) {
    // No usable reply (for example, the server is stopped). Any earlier draft stays as it was.
    console.error(error);
    ticketsInProgress.delete(ticketId);
    showTicketStatus(
      ticketId,
      "Something went wrong, so no new draft was saved. Check that the server is running and try again.",
      "error",
    );
  }
}

// Runs when a ticket's Check priority button is clicked. The server sends the saved category
// to n8n, so Gemini isn't called and the draft (and any unsaved edits) stay as they are.
async function checkPriority(ticketId) {
  ticketsInProgress.set(ticketId, "Checking priority…");
  showTicketStatus(ticketId, "Checking priority…", ""); // disables the buttons while n8n answers

  try {
    const response = await fetch(`/api/tickets/${ticketId}/priority`, { method: "POST" });
    const result = await response.json();
    ticketsInProgress.delete(ticketId);

    if (!response.ok) {
      // For example: n8n isn't running, took too long, or sent an invalid answer. Nothing was
      // saved, and the buttons work again, so the agent can retry or approve the reply.
      showTicketStatus(ticketId, result.error || "Priority could not be checked.", "error");
      return;
    }

    // Saved. Draw the card again to show or hide the Priority review label.
    replaceTicketCard(result);
    const text = result.priority_review
      ? "n8n flagged this ticket for priority review."
      : "n8n says this ticket doesn't need priority review.";
    showTicketStatus(ticketId, text, "success");
  } catch (error) {
    // No usable reply (for example, the server is stopped). The draft and edits are still here.
    console.error(error);
    ticketsInProgress.delete(ticketId);
    showTicketStatus(
      ticketId,
      "Something went wrong, so priority wasn't checked. Check that the server is running and try again.",
      "error",
    );
  }
}

// Runs when a ticket's Approve reply button is clicked: save exactly what is in its editor.
async function approveReply(ticketId) {
  const card = document.getElementById(`ticket-${ticketId}`);
  const approvedReply = card.querySelector(".reply-editor").value;

  ticketsInProgress.set(ticketId, "Approving reply…");
  showTicketStatus(ticketId, "Approving reply…", ""); // disables the buttons and locks the editor

  try {
    const response = await fetch(`/api/tickets/${ticketId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved_reply: approvedReply }),
    });
    const result = await response.json();
    ticketsInProgress.delete(ticketId);

    if (!response.ok) {
      // For example, a blank reply. The editor still holds the agent's text, ready to fix and retry.
      showTicketStatus(ticketId, result.error || "The reply could not be approved.", "error");
      return;
    }

    // Approved. Draw the card again: the reply is now read-only and labelled "Approved".
    unsavedReplies.delete(ticketId);
    replaceTicketCard(result);
    showTicketStatus(ticketId, "Reply approved and saved.", "success");
  } catch (error) {
    // No usable reply (for example, the server is stopped). The editor still holds the text.
    console.error(error);
    ticketsInProgress.delete(ticketId);
    showTicketStatus(
      ticketId,
      "Something went wrong, so the reply wasn't approved. Your text is still here: " +
        "check that the server is running and try again.",
      "error",
    );
  }
}

// Swap one ticket's card for a freshly drawn one, using the ticket the server sent back.
function replaceTicketCard(ticket) {
  const card = document.getElementById(`ticket-${ticket.id}`);
  if (card) {
    card.replaceWith(createTicketItem(ticket));
  }
}

// Update one ticket's card: enable or disable its buttons and editor, and set its status line.
function showTicketStatus(ticketId, text, kind) {
  const card = document.getElementById(`ticket-${ticketId}`);
  if (!card) {
    return; // the ticket isn't on the page right now
  }
  setTicketBusy(card, ticketsInProgress.has(ticketId));
  showStatus(card.querySelector(".ticket-actions .status"), text, kind);
}

// While a ticket waits for the server, its buttons are disabled and its editor is locked,
// so the text being saved can't change halfway.
function setTicketBusy(card, busy) {
  for (const button of card.querySelectorAll(".ticket-actions button")) {
    button.disabled = busy;
  }
  const editor = card.querySelector(".reply-editor");
  if (editor) {
    editor.readOnly = busy;
  }
}

// Runs when the Submit ticket button is clicked: send the form to the server as JSON.
async function submitTicket(event) {
  event.preventDefault(); // stop the browser's normal form submit, which reloads the page

  submitButton.disabled = true; // no double submits while we wait
  showStatus(formStatus, "Saving ticket…", "");

  try {
    const response = await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subject: subjectInput.value,
        message: messageInput.value,
      }),
    });
    const result = await response.json();

    if (!response.ok) {
      // The server refused the ticket (for example, a blank field).
      // We don't clear the form, so the text is still there to fix.
      showStatus(formStatus, result.error || "The ticket could not be saved.", "error");
      return;
    }

    form.reset(); // saved, so clear the fields
    showStatus(formStatus, `Ticket #${result.id} submitted.`, "success");
    await loadTickets(); // redraw the inbox so the new ticket shows at the top
  } catch (error) {
    // No usable reply (for example, the server is stopped). The form still holds the text.
    console.error(error);
    showStatus(
      formStatus,
      "Something went wrong, so the ticket was not saved. Check that the server is running and try again.",
      "error",
    );
  } finally {
    submitButton.disabled = false;
  }
}

// Put a message in a status line. kind is "success", "error", or "" for plain text.
function showStatus(element, text, kind) {
  element.textContent = text;
  element.className = `status ${kind}`;
}

form.addEventListener("submit", submitTicket);
loadTickets(); // fill the inbox as soon as the page opens
