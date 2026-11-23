"""The morning brief: composed once a day by email and in the app, with its frequency and one-click unsubscribe
(BRF-01 to BRF-03).

Other modules import only app.brief.api (ARCHITECTURE 3.3); everything else here belongs to this module. This is
layer 7: it reads matches through app.match.api and hands the email to the platform's outbox.
"""
