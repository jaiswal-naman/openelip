"""Reasons in words (AI_SYSTEM 7.8): one template per reason code, filled by code.

A model never writes an explanation. The parameters are values our code supplies, plus the quoted phrase it names (a
sentence of the candidate's own draft, or a sentence of the posting that tripped a scam rule). A template that needs a
parameter the caller leaves out raises KeyError, so a missing value shows in a test and never as a half-written line.
"""

TEMPLATES: dict[str, str] = {
    # key one, the policy engine
    "dial_never": "You set this kind of action to never, so it was not attempted.",
    "campaign_paused": "This campaign is paused, so nothing was prepared or sent.",
    "campaign_closed": "This campaign is closed, so nothing was prepared or sent.",
    "account_inactive": "Your account is not active, so nothing was prepared or sent.",
    "type_not_available": "This kind of action is not available yet.",
    # key two, the checks
    "citation_unresolved": (
        "The draft cites something that is not in your confirmed record or in the company's sources, or says something "
        "it does not cite."
    ),
    "unsupported_claim": 'The draft says more than its sources support: "{quote}"',
    "citation_check_missing": "The check that compares a draft with its sources is not available, so it was held.",
    "consent_missing": "You have not allowed AI processing, so a draft written with it cannot be used.",
    "constraint_conflict": "The job now breaks one of your hard constraints, so nothing was sent for it.",
    "employer_blocked": "You blocked this employer, so nothing was sent to them.",
    "restricted_data": "The draft holds {detail}, which only you may state, so it was held.",
    "budget_exhausted": "This campaign reached its model budget, so work stopped until you raise it or cancel.",
    "injection_suspected": (
        "Text the agent read carried an instruction or an address that does not come from your record or the "
        "employer's own site, so the draft was held."
    ),
    "limit_reached": (
        "A limit was reached, so this was held: two follow-ups for a campaign, or 40 questions or 50 cents of model "
        "cost for one Twin conversation."
    ),
    "topic_not_open": "You have not opened this topic to the Twin, so it was not answered.",
    "session_closed": "This conversation with the employer is closed, so nothing was sent.",
    # pauses
    "candidate": "You paused this campaign.",
    "scam_suspected": 'The posting looks like a scam, so work stopped before anything was shared: "{quote}"',
    # what happened to an action after it was proposed
    "expired": "You did not answer in time, so the request expired.",
    "undone": "You undid this before it was sent.",
    "cancelled": "The campaign was cancelled before this was sent.",
    "action_not_scheduled": "This was no longer scheduled when its time came, so it was not sent.",
    "preview_changed": "The draft changed after you saw it; look at the new version before deciding.",
}


def explain(code: str, **params: str) -> str:
    """The sentence for a reason code. Unknown codes and missing parameters raise KeyError."""
    return TEMPLATES[code].format(**params)
