"""The wind tunnel's tables in the deletion registry (EVL-03; DATA_MODEL 3.13 and 7.2).

Registered on import; the worker imports this module at start-up (the registry is filled by registration, not read from
the schema). A synthetic candidate is an account like any other: erasing it keeps the attempt and clears the reference.
The scenario definitions and grader results are invented and hold no personal data.
"""

from app.trust.privacy import register

register(
    "evals",
    {
        "sim_runs": "set_null",  # candidate_user_id (a synthetic account) is cleared; the attempt stays
        "sim_scenarios": "no_personal_data",  # synthetic scenario definitions; DATA_MODEL 7.2
        "sim_results": "no_personal_data",  # grader results: codes, counts and row references, never text
    },
)
