"""python -m app.platform.llm ping --email <address>: one costed call through the gateway, as a self-test.

It runs as the worker role (the API role cannot write the ledger), so it needs DATABASE_URL_WORKER.
"""

import argparse
import hashlib
import sys
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from .. import db
from . import AIDisabled, ProviderUnavailable, SchemaInvalid, complete
from .gateway import usd

PROMPT = 'Reply with exactly this JSON object and nothing else: {"reply": "pong"}'
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)


class PingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str


def ping(email: str) -> str:
    with db.transaction(role="worker") as conn:  # finds or creates the user, as the sign-in does
        user_id = conn.execute(text("SELECT user_for_verified_email(:e)"), {"e": email}).scalar_one()
    complete(
        "ping", PingResult, [{"role": "user", "content": PROMPT}], principal=user_id, prompt_version=PROMPT_VERSION
    )
    # ponytail: the newest ping row of this user; use an id returned by complete() when there is one
    with db.transaction(role="worker") as conn:
        call_id, cost = conn.execute(
            text(
                "SELECT id, cost_microusd FROM llm_calls WHERE user_id = :u AND task = 'ping' ORDER BY id DESC LIMIT 1"
            ),
            {"u": user_id},
        ).one()
    return f"ping ok: ledger row {call_id}, cost ${usd(cost)} (measured from the provider's token counts)"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.platform.llm")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ping", help="one costed call through the gateway").add_argument("--email", required=True)
    args = parser.parse_args(argv)
    try:
        print(ping(args.email))
    except AIDisabled:
        print("ping: AI is disabled. Set LLM_PROVIDER and its key (or LLM_PROVIDER=fake).", file=sys.stderr)
        return 1
    except ProviderUnavailable:
        print("ping: the provider did not answer; the failed call is in the ledger.", file=sys.stderr)
        return 1
    except SchemaInvalid:
        print("ping: the model's answer did not fit the schema twice; both calls are in the ledger.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
