"""The hiring desk: an employer describes a role and gets a shortlist of only the candidates who said yes, and must
answer every introduction on a visible clock (HIR-01 to HIR-08).

Other modules import only app.hiring.api (ARCHITECTURE 3.3); everything else here belongs to this module. This is
layer 5: it reads the job index, the Career Record, consent and matching through their api modules, and publishes a
role as an unlisted job only through app.ingest.api. The database guards the rules that matter most
(db/sql/p6_0601_*.sql).
"""
