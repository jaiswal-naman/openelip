"""Requirement extraction from a posting's text, with no database and no queue (AI_SYSTEM 4.3, IDX-08).

segment   splits a posting into numbered lines, each with its offsets
schema    the shape of the model's answer: segment numbers, an optional quote, two enums
validate  turns that answer into rows whose text and span are computed here, from the posting
rules     a model-free heading baseline that returns rows of the same shape, for the gold set to score beside the model
task      renders the request and runs it through the model gateway as the task job.requirements.extract

The model never supplies stored text: a requirement is the posting's own words at a span that code found.
"""
