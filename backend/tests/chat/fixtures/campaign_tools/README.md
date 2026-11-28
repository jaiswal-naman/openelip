Recordings for tests/chat/test_campaign_tools.py (plan 07-06). Invented: every company, claim and address is made up, and
no model is called. Each file is a list of the model's `chat.turn` answers, replayed in order by the fake provider.

- `dossier_injection.json`  reads a dossier whose claim tells the model to start a campaign; the recorded model quotes it
- `fooled.json`             a model that does follow it: it offers a campaign. Only a card results; nothing is started
- `offer.json`              the candidate asks to go ahead; the model offers the Start campaign button
