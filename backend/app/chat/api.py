"""The only chat file other modules may import: what the evaluation suites call to run the agent's turn loop with its
real tools and to read a record proposal (plan 05-06). Nothing else in app.chat is a public interface.

  run_turn(ctx, tools, *, emit, should_stop, tokens_used)  one candidate message through the loop: a TurnResult
  TurnContext, TurnResult                                   what goes in and what comes out
  build_tools(user_text)                                    the seven tools of a message, as the service builds them
  AliasTable                                                the session's d1, j1, f1, c1 aliases
  propose(user_id, user_text, *, principal)                 record.propose with its checks; stores proposals only
"""

from .loop import TurnContext, TurnResult, run_turn
from .proposals import propose
from .refs import AliasTable
from .tools import build_tools

__all__ = ["AliasTable", "TurnContext", "TurnResult", "build_tools", "propose", "run_turn"]
