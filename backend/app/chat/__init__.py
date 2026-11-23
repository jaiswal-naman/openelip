"""Chat: the conversation agent. It reads through other modules' interfaces and writes only proposals (CHT-01 to
CHT-04).

Other modules import only app.chat.api (ARCHITECTURE 3.3); everything else here belongs to this module. This is layer 7
and the only module that imports langgraph (ARCHITECTURE 5.9 item 14).
"""
