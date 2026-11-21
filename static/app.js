// static/app.js runs in the browser. It talks to the Flask routes in app.py:
//   GET  /api/tickets             -> load the inbox
//   POST /api/tickets             -> save a new ticket
//   POST /api/tickets/<id>/draft  -> have the server ask Gemini for a draft reply

// The page elements this script uses (their ids are in templates/index.html).
const form = document.getElementById("ticket-form");
const subjectInput = document.getElementById("subject");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const formStatus = document.getElementById("form-status");
const inboxStatus = document.getElementById("inbox-status");
const ticketList = document.getElementById("ticket-list");

// Ids of tickets whose draft is being generated right now. createTicketItem() checks this,
// so a ticket's button stays disabled even if the inbox is redrawn while Gemini works.
const draftsInProgress = new Set();

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

// Build the <li> for one ticket. We use textContent (never innerHTML), so whatever the
// customer typed, or Gemini wrote, is shown as plain text and something like <script> can't run.
function createTicketItem(ticket) {
  const item = document.createElement("li");
  item.className = "ticket";
  item.id = `ticket-${ticket.id}`; // lets generateDraft() find this ticket's card later

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

  item.append(subject, details, message, createDraftControls(ticket));

  // A saved draft comes back with the ticket from GET /api/tickets,
  // so refreshing the page shows it again without calling Gemini.
  if (ticket.draft_reply) {
    item.append(createDraftBox(ticket));
  }
  return item;
}

// The Generate draft button and the status line next to it.
function createDraftControls(ticket) {
  const controls = document.createElement("div");
  controls.className = "draft-controls";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "draft-button";
  button.textContent = ticket.draft_reply ? "Regenerate draft" : "Generate draft";
  button.addEventListener("click", () => generateDraft(ticket.id));

  const status = document.createElement("p");
  status.className = "status";
  status.setAttribute("role", "status");

  if (draftsInProgress.has(ticket.id)) {
    button.disabled = true;
    showStatus(status, "Generating draft…", "");
  }

  controls.append(button, status);
  return controls;
}

// The saved category and draft, labelled "Draft for review". It is only shown here:
// nothing in this app sends it to the customer.
function createDraftBox(ticket) {
  const box = document.createElement("div");
  box.className = "draft";

  const label = document.createElement("p");
  label.className = "draft-label";
  label.textContent = "Draft for review";

  const category = document.createElement("span");
  category.className = "draft-category";
  category.textContent = `Category: ${ticket.category}`;
  label.append(category);

  const reply = document.createElement("p");
  reply.className = "draft-reply";
  reply.textContent = ticket.draft_reply;

  box.append(label, reply);
  return box;
}

// Runs when a ticket's Generate draft button is clicked.
async function generateDraft(ticketId) {
  draftsInProgress.add(ticketId);
  showDraftStatus(ticketId, "Generating draft…", ""); // this also disables the button

  try {
    const response = await fetch(`/api/tickets/${ticketId}/draft`, { method: "POST" });
    const result = await response.json();
    draftsInProgress.delete(ticketId);

    if (!response.ok) {
      // For example: no API key, quota reached, or Gemini failed. The server saved
      // nothing, so the card still shows the earlier draft (if there was one).
      showDraftStatus(ticketId, result.error || "The draft could not be generated.", "error");
      return;
    }

    // Saved. Draw this ticket's card again so it shows the new draft.
    const card = document.getElementById(`ticket-${ticketId}`);
    if (card) {
      card.replaceWith(createTicketItem(result));
    }
    showDraftStatus(ticketId, "Draft saved for review. Nothing was sent to the customer.", "success");
  } catch (error) {
    // No usable reply (for example, the server is stopped). Any earlier draft stays as it was.
    console.error(error);
    draftsInProgress.delete(ticketId);
    showDraftStatus(
      ticketId,
      "Something went wrong, so no new draft was saved. Check that the server is running and try again.",
      "error",
    );
  }
}

// Update the button and the status line on one ticket's card.
function showDraftStatus(ticketId, text, kind) {
  const card = document.getElementById(`ticket-${ticketId}`);
  if (!card) {
    return; // the ticket isn't on the page right now
  }
  card.querySelector(".draft-button").disabled = draftsInProgress.has(ticketId);
  showStatus(card.querySelector(".draft-controls .status"), text, kind);
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
