"""Plan 07-03, GRD-07 and INDEX decision 24: scam signals are rules that quote the posting; there is no score.
Twenty clean
postings, all invented, raise nothing."""

import pytest

from app.guardian import scam

CLEAN = [
    "We are a small team building route planning software. You will write Python and SQL, review code and own "
    "services.",
    "Requirements: 3 years of experience with PostgreSQL. Nice to have: Kubernetes. We offer 25 days of leave.",
    "Our hiring process has three steps: a call with a recruiter, a technical interview and a conversation with "
    "the team.",
    "Salary range 60,000 to 75,000 EUR. Hybrid, two days in the Lisbon office. Equal opportunity employer.",
    "Apply through our careers page. We reply to every application within ten working days.",
    "You will work on signal processing pipelines for audio data and tune models for noisy input.",
    "Please send your CV and a short cover note through the application form on this page.",
    "After you accept an offer, our people team will ask for your passport details to complete right-to-work checks.",
    "On your first day, bring your passport and proof of address so that payroll can set you up.",
    "We use Slack for team chat and Zoom for interviews. Questions? Write to careers@lumen.example.",
    "The role is fully remote within the EU. Equipment is provided by us, including a laptop.",
    "Benefits include a pension scheme, private health cover and a learning budget.",
    "As part of our background check, which happens only after an offer, we verify your employment history.",
    "Join our data platform team. You will design schemas, tune queries and mentor two junior engineers.",
    "Interview process: phone screen, take-home exercise (two hours, paid), onsite with the team.",
    "We value curiosity. Candidates from all backgrounds are welcome and adjustments are available on request.",
    "Our offices are in Berlin and Pune. We sponsor visas for senior roles after the first six months.",
    "You will support customers by email and chat, and write help articles for our knowledge base.",
    "Experience with payment systems is a plus. You will not handle customer money directly.",
    "Send questions to our recruiter through the contact form on our website and we will respond.",
]


@pytest.mark.parametrize("posting", CLEAN)
def test_a_clean_posting_raises_no_signal(posting):
    assert scam.signals(posting) == []


def test_there_are_twenty_clean_postings_and_none_is_a_score():
    assert len(CLEAN) == 20
    assert not hasattr(scam, "score")


@pytest.mark.parametrize(
    "sentence",
    [
        "A refundable deposit of 150 dollars is required before your first shift.",
        "You must pay a small registration fee to secure your place.",
        "Candidates buy their own equipment and we reimburse you later.",
        "Payment is made in gift cards after your trial week.",
        "Please wire money for the starter kit today.",
    ],
)
def test_a_fee_or_payment_request_is_a_fee_signal_quoting_the_sentence(sentence):
    posting = f"Great role at an exciting company. {sentence} We look forward to hearing from you."
    found = scam.signals(posting)
    assert [(s.kind, s.quote) for s in found] == [("fee_request", sentence)]


@pytest.mark.parametrize(
    "sentence",
    [
        "Send a copy of your passport before the interview.",
        "Please provide your bank details so we can set up your payroll in advance.",
        "Submit your social security number with your application.",
        "Upload a photo of your driver's license to proceed.",
    ],
)
def test_documents_requested_before_an_interview_are_a_documents_signal(sentence):
    found = scam.signals(f"We are hiring now.\n{sentence}\nThanks.")
    assert [(s.kind, s.quote) for s in found] == [("documents_before_interview", sentence)]


@pytest.mark.parametrize(
    "sentence",
    [
        "Contact our hiring manager on WhatsApp to start the process.",
        "Interviews are held on Telegram, message us there.",
        "To apply, send a message via Signal.",
        "Email your CV to hr.lumen.jobs@gmail.com and we will reply.",
    ],
)
def test_an_off_platform_demand_is_an_off_platform_signal(sentence):
    found = scam.signals(f"Open role.\n{sentence}")
    assert [(s.kind, s.quote) for s in found] == [("off_platform", sentence)]


def test_each_matching_sentence_is_one_signal_in_reading_order_and_a_repeat_counts_once():
    posting = (
        "Pay a small registration fee. Send your passport now. Pay a small registration fee. "
        "Chat with us on WhatsApp to begin."
    )
    assert [s.kind for s in scam.signals(posting)] == ["fee_request", "documents_before_interview", "off_platform"]


def test_a_long_sentence_is_quoted_in_part_and_an_empty_posting_has_no_signal():
    long = "Pay a registration fee " + "and wait " * 80 + "."
    quote = scam.signals(long)[0].quote
    assert len(quote) <= scam.MAX_QUOTE and quote.endswith("…")
    assert scam.signals("") == [] and scam.signals("   \n ") == []
