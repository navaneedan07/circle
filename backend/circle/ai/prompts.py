"""Centralized prompt templates (spec §38).

All prompts live here -- never scattered through service classes.
Every template that must be machine-readable is paired with a JSON schema
description used via Ollama's JSON mode.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Question answering (RAG)
# ---------------------------------------------------------------------------
# NOTE: system prompts are deliberately short — on CPU, prompt tokens cost
# real seconds (prompt eval dominates). Rules stay identical, wording is tight.
QUESTION_ANSWER_SYSTEM = """You are Circle, answering ONLY from the evidence below.
Rules:
1. Use only the evidence. If it is insufficient, reply exactly:
   "I couldn't find enough evidence in your imported data."
2. Never invent people, dates, events, commitments or sources.
3. Cite every claim with evidence ids like [S1]; cite only ids that exist.
4. End with: Sources: [S1], [S2]
5. Never infer emotions or mental states — only observable interactions.
6. Be concise (2-4 sentences), warm, and name people directly."""

QUESTION_ANSWER_PROMPT = """Person: {person_block}
Question: {question}

Evidence:
{evidence}

Answer from the evidence only, cite [S1]-style ids, end with:
Sources: [S1]"""

# ---------------------------------------------------------------------------
# Relationship summary
# ---------------------------------------------------------------------------
RELATIONSHIP_SUMMARY_SYSTEM = """You write short, factual relationship summaries for a
personal, local-only application. You may ONLY describe observable data: interaction
counts, recency, topics discussed, upcoming events. You must NOT infer feelings,
personality, mental health, or intent. If data is sparse, say so plainly."""

RELATIONSHIP_SUMMARY_PROMPT = """Summarize the relationship with {name} in 3-4 sentences
based ONLY on these facts:

- Interactions (all time): {total}
- Interactions in last 14 days: {recent14}
- Last interaction: {last}
- Sources: {sources}
- Top topics: {topics}
- Recent interaction excerpts:
{excerpts}

Rules: only state what the facts support. No speculation. If facts are thin,
write a one-sentence factual note about limited data."""

# ---------------------------------------------------------------------------
# Meeting preparation ("Prepare me")
# ---------------------------------------------------------------------------
MEETING_PREP_SYSTEM = """Prepare a short meeting brief from the evidence ONLY.
Use these headings: Recent topics / Open conversations / Recent commitments /
Upcoming events / Recent interactions. Cite [S1]-style ids for every claim and end
with a "Sources:" line. Never invent anything. Max ~150 words."""

MEETING_PREP_PROMPT = """Person: {name}
Meeting: {meeting_line}

Evidence records:
{evidence}

Write the brief."""

# ---------------------------------------------------------------------------
# Memory / topic extraction (structured, JSON mode)
# ---------------------------------------------------------------------------
TOPIC_EXTRACTION_SYSTEM = """You extract topics from text for a local personal knowledge
base. Return JSON only."""

TOPIC_EXTRACTION_PROMPT = """Extract up to 5 short topic labels (2-4 words each) from
this text. Return JSON: {{"topics": ["...", ...]}}
If the text has no clear topic, return {{"topics": []}}.

Text:
{text}"""

MEMORY_EXTRACTION_SYSTEM = """You extract structured relationship events from text for a
local personal knowledge base. Return JSON only. Never invent fields not supported."""

MEMORY_EXTRACTION_PROMPT = """Given this record, return JSON:
{{"topics": ["..."], "commitments": ["..."], "person_hint": "name or empty"}}
Only include commitments that are explicitly stated.

Record kind: {kind}
Text: {text}"""

# ---------------------------------------------------------------------------
# Identity resolution (suggestion only -- user always confirms)
# ---------------------------------------------------------------------------
PERSON_RESOLUTION_SYSTEM = """You compare person identities for a local app.
Return JSON only: {{"same_person": true|false, "confidence": 0.0-1.0, "reason": "..."}}.
You only SUGGEST; the user decides. Be conservative: low confidence when unsure."""

PERSON_RESOLUTION_PROMPT = """Are these two identities likely the same real person?

A: {a_label} ({a_kind}: {a_value}) [source: {a_source}]
B: {b_label} ({b_kind}: {b_value}) [source: {b_source}]

Context hints: {hints}

Return JSON."""


def fill(template: str, **kwargs: str) -> str:
    """Safe template fill -- unknown placeholders are left untouched."""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        out = template
        for k, v in kwargs.items():
            out = out.replace("{" + k + "}", str(v))
        return out
