"""Small helpers shared by the end-to-end tests."""

from __future__ import annotations

from lab import stack


def subjects(lab: stack.Lab) -> list[str]:
    return [m["Subject"] for m in lab.mails()]


def mails_with(lab: stack.Lab, text: str) -> int:
    return sum(text in s for s in subjects(lab))


def drive_retries(lab: stack.Lab, done, timeout: float = 60):
    return lab.drive_retries(done, timeout)
