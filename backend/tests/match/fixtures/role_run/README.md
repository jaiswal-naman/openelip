Recordings for tests/match/test_role_run.py (plan 06-05). Everything here is invented: the verdicts answer the three
requirements of tests/hiring/factories.py's default role (r1 Python, r2 SQL, r3 Airflow, preferred) for a candidate whose
facts are tests/match/factories.py's resume (f1 role, f2 the Python and SQL achievement, f3 the team lead).

  all_met.json              both required items met by f2, the preferred one unsupported
  required_not_met.json     SQL not met
  weak_fit.json             Python "met" by a fact that shares no word with it (validator rule 6)
  bad_twice.json            cites an alias the request never carried, so the validator refuses it, every time
  invalid_schema_twice.json text that is not an answer at all
