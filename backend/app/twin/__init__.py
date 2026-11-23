"""The Twin: a candidate's agent that answers an employer's questions from the confirmed record (layer 6).

Importing the package teaches the Guardian about Twin sessions and replies (subject), registers the live `twin.message`
stream source (stream) and the Twin's entries in the audit log (audit_source), in the API process and in the worker
alike.
"""

from . import audit_source, stream, subject  # noqa: F401
