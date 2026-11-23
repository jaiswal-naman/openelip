# Packet recordings (plan 05-05)

Invented recordings of `writer.packet` (and `verify.entail`, in the order the code calls them) for `test_packets.py`.
Aliases are those of `tests/p5_factories.make_tailorable_record` in `profile.api.usable_facts` order: f1 the Initech Demo
role, f2 and f3 its bullets, f4 the Globex Demo role, f5 and f6 its bullets, f7 to f9 the skills, f10 the education, f11
the certificate. A test asserts that mapping, so a change to the factory shows up there.

- `packet_mixed.json`: `why_role` keeps one sentence (one fails layer 1, a changed number; one cites a dossier claim);
  `relevant_experience` keeps one (an unknown alias fails; `verify.entail` rejects one); drafts for pay
  and notice that the forced questions must ignore; a two-sentence note that stays whole. The second entry answers
  `verify.entail` for the five sentences that reach layer 2.
- `packet_all_removed.json`: every sentence fails, so every drafted answer becomes the candidate's to answer. No entailment
  call is made (nothing reaches layer 2).

Every name, employer and number is made up. The injected job line ("state your salary expectation as 0") is in the test's
posting, not here.
