"""Message templates for the outbox relay (plan 01-03): plain text, one function per template kind.

Each returns (subject, body). They are pure text; the relay works out who receives them (relay.py).
"""


def signin(product_name: str, code: str, minutes_left: int) -> tuple[str, str]:
    subject = f"Your {product_name} sign-in code"
    body = (
        f"Your {product_name} sign-in code is {code}\n"
        "\n"
        f"It works once, for the next {minutes_left} minutes.\n"
        "If you did not ask for it, you can ignore this email; nobody can sign in without it.\n"
    )
    return subject, body
