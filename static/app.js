// static/app.js runs in the browser. It talks to the Flask routes in app.py:
//   GET  /api/tickets  -> load the inbox
//   POST /api/tickets  -> save a new ticket

// The page elements this script uses (their ids are in templates/index.html).
const form = document.getElementById("ticket-form");
const subjectInput = document.getElementById("subject");
const messageInput = document.getElementById("message");
const submitButton = document.getElementById("submit-button");
const formStatus = document.getElementById("form-status");
const inboxStatus = document.getElementById("inbox-status");
const ticketList = document.getElementById("ticket-list");

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

// Build the <li> for one ticket. We use textContent (never innerHTML), so whatever
// the customer typed is shown as plain text and something like <script> can't run.
function createTicketItem(ticket) {
  const item = document.createElement("li");
  item.className = "ticket";

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

  item.append(subject, details, message);
  return item;
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
