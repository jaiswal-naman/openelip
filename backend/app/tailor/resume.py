"""The base resume: a document that code builds from the candidate's usable facts, and the approved edits that change it
(AI_SYSTEM 4.7, TLR-01 to TLR-03).

Employer, title, dates, degree and institution are written here from fact attributes and no edit touches them. Every
bullet carries the id of the fact it comes from, so an edit can name what it changes and what supports it. A date the
attributes do not hold is left out: nothing is guessed. Documents are frozen and made of tuples; applying edits returns
a new document. No model call and no database access.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

SECTIONS = ("experience", "skills", "education", "certifications", "projects")
BASE_KINDS = frozenset({"role", "achievement", "education", "skill", "certification", "project"})
# record_source of a fact the candidate stated in chat (DATA_MODEL 2.1). It is not on the base resume: it can only
# arrive through an add_from_fact edit the candidate approves (INDEX decision 7).
CHAT_SOURCE = "confirmed_proposal"
OTHER_EXPERIENCE = "Other experience"  # the block for achievements whose role is no longer a usable fact
PRESENT = "Present"
# A plain hyphen, not an en dash: the PDF draws Latin-1 only (decision 27), and every resume would be refused with one.
RANGE = " - "

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ISO_DATE = re.compile(r"(\d{4})(?:-(\d{2}))?")  # attributes.start_date and end_date: YYYY or YYYY-MM
_BULLET_MARK = re.compile(r"^[-*•·▪◦‣]+\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # not valid in a DOCX, and never part of a resume line


@dataclass(frozen=True)
class Fact:
    """A confirmed fact as profile.api.usable_facts returns it, reduced to what tailoring reads. `id` is the fact's id
    as text; `source` is a record_source ('resume', 'candidate_edit' or 'confirmed_proposal')."""

    id: str
    kind: str
    statement: str
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)
    parent_fact_id: str | None = None
    source: str = "resume"


@dataclass(frozen=True)
class Bullet:
    fact_id: str
    text: str


@dataclass(frozen=True)
class RoleBlock:
    """A role heading and its bullets. fact_id is None for the block of achievements whose role is not on the resume."""

    fact_id: str | None
    header: str
    dates: str
    bullets: tuple[Bullet, ...]


@dataclass(frozen=True)
class ResumeDoc:
    name: str
    email: str
    roles: tuple[RoleBlock, ...] = ()
    skills: tuple[Bullet, ...] = ()
    education: tuple[Bullet, ...] = ()
    certifications: tuple[Bullet, ...] = ()
    projects: tuple[Bullet, ...] = ()


class EditLike(Protocol):
    """What apply_edits reads: the stored shape of an edit (a resume_edits row, or a VerifiedEdit). For a reorder,
    supporting_fact_ids lists the facts in their new order; for an add_from_fact it holds the one fact added."""

    @property
    def target_fact_id(self) -> str | None: ...
    @property
    def before_text(self) -> str: ...
    @property
    def after_text(self) -> str: ...
    @property
    def supporting_fact_ids(self) -> Sequence[str]: ...
    @property
    def section(self) -> str: ...


# ---- one fact as one line of text ----------------------------------------


def line_text(statement: str) -> str:
    """A statement as one line of a resume: control characters dropped, white space collapsed, a leading bullet mark
    removed."""
    one_line = " ".join(_CONTROL.sub(" ", statement).split())
    return _BULLET_MARK.sub("", one_line, count=1)


def _attribute(attributes: Mapping[str, Any], key: str) -> str:
    value = attributes.get(key)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _date_text(value: Any) -> str | None:
    """'2021-08' as 'Aug 2021', '2021' as '2021'. Anything else is not a date and is not printed."""
    found = _ISO_DATE.fullmatch(value) if isinstance(value, str) else None
    if found is None:
        return None
    year, month = found.groups()
    if month is None:
        return year
    return f"{_MONTHS[int(month) - 1]} {year}" if 1 <= int(month) <= len(_MONTHS) else None


def _date_range(attributes: Mapping[str, Any]) -> str:
    """The dates a fact states, and only those. Present is printed only for a role the attributes call current."""
    start = _date_text(attributes.get("start_date"))
    end = _date_text(attributes.get("end_date"))
    if end is None and attributes.get("is_current") is True:
        end = PRESENT
    return RANGE.join(part for part in (start, end) if part)


def _start_key(fact: Fact) -> tuple[int, int, int]:
    found = _ISO_DATE.fullmatch(str(fact.attributes.get("start_date")))
    if found is None or _date_text(found.group()) is None:
        return (0, 0, 0)  # no usable start date: after every dated role
    return (1, int(found.group(1)), int(found.group(2) or 0))


def role_header(fact: Fact) -> str:
    """Title, employer: from the attributes. A fact with neither (one the candidate corrected has no attributes) shows
    the candidate's own words."""
    parts = [_attribute(fact.attributes, key) for key in ("title", "employer")]
    return ", ".join(part for part in parts if part) or line_text(fact.statement)


def role_line(fact: Fact) -> str:
    """A role as one line: its header, and its dates in brackets when it has any."""
    dates = _date_range(fact.attributes)
    return f"{role_header(fact)} ({dates})" if dates else role_header(fact)


def item_text(fact: Fact) -> str:
    """The text a fact has on the resume: a skill's name, an education entry written from its attributes, otherwise
    the statement as one line. An add_from_fact edit must carry exactly this text."""
    if fact.kind == "skill":
        return _attribute(fact.attributes, "name") or line_text(fact.statement)
    if fact.kind == "education":
        named = ", ".join(p for p in (_attribute(fact.attributes, k) for k in ("degree", "institution")) if p)
        dates = _date_range(fact.attributes)
        return (f"{named}, {dates}" if dates else named) if named else line_text(fact.statement)
    return line_text(fact.statement)


def is_on_base(fact: Fact) -> bool:
    """Whether the base resume shows this fact. A fact the candidate stated in chat is not shown until an approved edit
    adds it; one they added or corrected by hand in the record is their own record and is shown (INDEX deviation: the
    plan said resume-sourced only, which would drop a corrected bullet from the resume without a word)."""
    return fact.kind in BASE_KINDS and fact.source != CHAT_SOURCE


# ---- the base document ----------------------------------------


def _bullets(facts: Sequence[Fact]) -> tuple[Bullet, ...]:
    return tuple(Bullet(fact.id, item_text(fact)) for fact in facts)


def build_base(facts: Sequence[Fact], *, name: str, email: str) -> ResumeDoc:
    """The base resume. Roles newest first, a role with no start date last; achievements under their role in the order
    given (profile.api.usable_facts order: document order); the other kinds in their own sections."""
    shown = [fact for fact in facts if is_on_base(fact)]
    roles = sorted((f for f in shown if f.kind == "role"), key=_start_key, reverse=True)  # stable: ties keep order
    role_ids = {role.id for role in roles}
    blocks = [
        RoleBlock(
            role.id,
            role_header(role),
            _date_range(role.attributes),
            _bullets([f for f in shown if f.kind == "achievement" and f.parent_fact_id == role.id]),
        )
        for role in roles
    ]
    orphans = [f for f in shown if f.kind == "achievement" and f.parent_fact_id not in role_ids]
    if orphans:
        blocks.append(RoleBlock(None, OTHER_EXPERIENCE, "", _bullets(orphans)))
    return ResumeDoc(
        name=name,
        email=email,
        roles=tuple(blocks),
        skills=_bullets([f for f in shown if f.kind == "skill"]),
        education=_bullets([f for f in shown if f.kind == "education"]),
        certifications=_bullets([f for f in shown if f.kind == "certification"]),
        projects=_bullets([f for f in shown if f.kind == "project"]),
    )


def fact_ids(doc: ResumeDoc) -> set[str]:
    """Every fact the document shows: roles, bullets, skills and the rest."""
    ids = {block.fact_id for block in doc.roles if block.fact_id is not None}
    for bullets in (
        *(block.bullets for block in doc.roles),
        doc.skills,
        doc.education,
        doc.certifications,
        doc.projects,
    ):
        ids.update(bullet.fact_id for bullet in bullets)
    return ids


def reorder_groups(doc: ResumeDoc) -> list[tuple[Bullet, ...]]:
    """The groups whose order an edit may change: each role's bullets, and the skills line."""
    return [*(block.bullets for block in doc.roles), doc.skills]


# ---- applying edits ----------------------------------------


def derive_op(target_fact_id: str | None, before_text: str, after_text: str) -> str:
    """The op a stored edit stands for (INDEX shared contracts: a resume_edits row has no op column)."""
    if before_text == "":
        return "add_from_fact"
    if after_text == "":
        return "remove"
    if target_fact_id is None and sorted(before_text.splitlines()) == sorted(after_text.splitlines()):
        return "reorder"
    return "rephrase"


def _each_bullet_tuple(doc: ResumeDoc, change: Callable[[tuple[Bullet, ...]], tuple[Bullet, ...]]) -> ResumeDoc:
    return replace(
        doc,
        roles=tuple(replace(block, bullets=change(block.bullets)) for block in doc.roles),
        skills=change(doc.skills),
        education=change(doc.education),
        certifications=change(doc.certifications),
        projects=change(doc.projects),
    )


def _permuted(bullets: tuple[Bullet, ...], order: tuple[str, ...]) -> tuple[Bullet, ...] | None:
    """These bullets in this order, or None when the order is not a permutation of exactly these bullets."""
    by_id = {bullet.fact_id: bullet for bullet in bullets}
    if len(by_id) != len(bullets) or len(order) != len(by_id) or set(order) != set(by_id):
        return None
    return tuple(by_id[fact_id] for fact_id in order)


def _reorder(doc: ResumeDoc, order: tuple[str, ...]) -> ResumeDoc:
    roles = []
    for block in doc.roles:
        permuted = _permuted(block.bullets, order)
        roles.append(block if permuted is None else replace(block, bullets=permuted))
    skills = _permuted(doc.skills, order)
    return replace(doc, roles=tuple(roles), skills=doc.skills if skills is None else skills)


def _add(doc: ResumeDoc, edit: EditLike) -> ResumeDoc:
    fact_id = edit.supporting_fact_ids[0] if edit.supporting_fact_ids else None
    if fact_id is None or fact_id in fact_ids(doc):
        return doc  # nothing to add, or already shown: a fact is never shown twice
    bullet = Bullet(fact_id, edit.after_text)
    if edit.section == "skills":
        return replace(doc, skills=(*doc.skills, bullet))
    if edit.section == "certifications":
        return replace(doc, certifications=(*doc.certifications, bullet))
    if edit.section == "projects":
        return replace(doc, projects=(*doc.projects, bullet))
    return doc  # a section this document cannot append to (experience goes under a role the edit does not name)


def _apply_one(doc: ResumeDoc, edit: EditLike) -> ResumeDoc:
    op = derive_op(edit.target_fact_id, edit.before_text, edit.after_text)
    if op == "rephrase":
        return _each_bullet_tuple(
            doc,
            lambda bullets: tuple(
                replace(b, text=edit.after_text) if b.fact_id == edit.target_fact_id else b for b in bullets
            ),
        )
    if op == "remove":
        return _each_bullet_tuple(doc, lambda bullets: tuple(b for b in bullets if b.fact_id != edit.target_fact_id))
    if op == "add_from_fact":
        return _add(doc, edit)
    return _reorder(doc, tuple(edit.supporting_fact_ids))


def apply_edits(base: ResumeDoc, edits: Sequence[EditLike]) -> ResumeDoc:
    """The document after these edits, in the order given. The input is not changed. An edit that names a fact the
    document no longer shows (it was corrected or retired since the edit was proposed) changes nothing, so an export
    drops it."""
    doc = base
    for edit in edits:
        doc = _apply_one(doc, edit)
    return doc
