"""Queue handlers of the profile module. There are none yet: plan 03-05 adds profile.parse (a resume's text to
unconfirmed facts), registered inside register() with queue.handler."""


def register() -> None:
    """Called once by the worker at start-up, so the handlers exist before the first job is claimed."""
