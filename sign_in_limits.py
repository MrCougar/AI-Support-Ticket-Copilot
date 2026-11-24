"""Slow down password guessing on the sign-in page.

Each address (the visitor's IP address, request.remote_addr) may enter MAX_FAILURES wrong
passwords within WINDOW_SECONDS. After that, the sign-in page doesn't check passwords from
that address at all until the oldest of those wrong attempts is WINDOW_SECONDS old. So a
guesser gets at most 5 tries every 15 minutes. A correct password clears the address's count.

The counts are kept in memory, so restarting the app resets them. That suits this demo,
which runs as a single process (the launcher, waitress-serve, or python app.py).
"""

import threading
import time

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


def now():
    """Seconds on a clock that only moves forward. The tests replace this to move time on."""
    return time.monotonic()


class FailedSignIns:
    """Remembers when each address entered a wrong password, for the last WINDOW_SECONDS."""

    def __init__(self):
        self.failures = {}  # address -> times of its wrong passwords, oldest first
        self.lock = threading.Lock()  # Waitress answers several requests at once, on different threads

    def seconds_until_allowed(self, address):
        """0 if this address may try a password now, otherwise how many seconds it must wait."""
        with self.lock:
            self.forget_old_failures()
            times = self.failures.get(address, [])
            if len(times) < MAX_FAILURES:
                return 0
            # Allowed again once enough of the wrong attempts are older than the window.
            allowed_at = times[-MAX_FAILURES] + WINDOW_SECONDS
            return max(1, round(allowed_at - now()))

    def record_failure(self, address):
        with self.lock:
            self.forget_old_failures()
            self.failures.setdefault(address, []).append(now())

    def clear(self, address):
        with self.lock:
            self.failures.pop(address, None)

    def forget_old_failures(self):
        """Drop attempts older than the window, so memory only holds recent ones."""
        cutoff = now() - WINDOW_SECONDS
        for address in list(self.failures):
            recent = [moment for moment in self.failures[address] if moment > cutoff]
            if recent:
                self.failures[address] = recent
            else:
                del self.failures[address]
