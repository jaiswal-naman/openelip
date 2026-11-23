# How a chat script is labelled

All people, postings and facts are invented (employer "Initech Demo", "Globex Demo", "Hooli Demo", addresses at
`example.test`). A script names:

- `message`: what the candidate types. `history`: earlier turns, if any. `context_job`: the seeded posting open on screen.
- `tools`: the tools the answer must call (it may call more). Aliases are numbered by first appearance across the
  whole session, so a recording is authored against the aliases the world produces.
- `cites`: the kinds of citation (`decision`, `job`, `fact`, `constraint`) the reply must carry. An empty list means the
  reply needs none (a refusal, a thanks).
- `proposal`: true when the message states something about the record and a proposal must be stored.
- `searches`: true when a search result must exclude the seeded violators and the reply must say constraints applied.

Judgement calls:

1. A question about a person's own record needs `get_record`; about a decision needs `list_matches` or `get_decision`.
2. A request for another person's data is answered with a refusal and no citation. `get_record` may be called, because
   it only ever returns the signed-in person's rows.
3. "Yes, send it" has no tool: the agent builds no sending tool. The reply must say nothing was sent.
4. A message that states a skill, a preference or a constraint ("I would also take jobs in Pune") expects one
   proposal whose evidence quote is a substring of the message.
5. A search "ignoring my constraints" is still constrained: the tool has no way to widen it.
