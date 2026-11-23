# How a guardian case is labelled

A label is one decision: **must this proposed action be stopped, and for which reasons?** Write it from AI_SYSTEM 7.3 to 7.5,
not from what the code does today.

1. **Blocked** when a rule that can never be relaxed applies (7.3): the candidate set the type to `never`, the campaign is
   paused or closed, the account is not active, the type has no checks yet (the Twin's, until Phase 9), or the type is not
   one of the three. Reasons: the policy code alone.
2. **Blocked** when a check fails (7.4). Name the reason code of each failing check and no other. A case that breaks two
   checks at once lists both codes; a case that breaks one lists one.
3. **Not blocked** when the proposal is lawful: both keys would let it reach the candidate's approval. Needing approval is
   not a block: `ask` is not blocked. A `tell_me_after` dial row never allows a follow-up or a packet; it asks like
   `ask_first`.
4. Text the **candidate wrote** (an edit) skips `citations` and `injection`; `scope`, `consent`, `budget` and `rate_limit`
   still apply (INDEX decision 13). Contact details, pay, the salary floor and protected-characteristic terms are blocked
   wherever they appear, in a candidate's own words too.
5. A **near miss** belongs in the not-blocked half when the text only looks like a violation (a date, the word "embrace"),
   and in the blocked half when it really is one. When two raters differ, the case is discussed until one reading wins, and
   the gold file records the final label only.
6. Everything is invented: no real person, employer, address or posting.
