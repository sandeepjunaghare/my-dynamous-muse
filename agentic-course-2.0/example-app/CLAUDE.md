# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: scaffold

This directory is empty apart from this file. The sections below record the *intended* shape of
the example app, not observed fact. **When the first real code lands, re-run `/init` and replace
the Commands and Architecture sections with what actually exists** — verify before relying on any
command here.

Inherited context (already loaded automatically, don't duplicate here):
- `dynamous/CLAUDE.md` — course repo overview, local AI stack, service ports, env var conventions
- `~/.claude/CLAUDE.md` — personal tooling and style preferences

## What this is

The worked example for **agentic-course-2.0**. It exists to be read start-to-finish by course
participants, so it is optimized for legibility over production hardening: prefer the obvious
implementation, keep indirection shallow, and let each file justify its own existence.

Its lineage is `ai-agent-mastery/4_Pydantic_AI_Agent/` in the parent repo — reuse those patterns
rather than inventing new ones, so participants moving between the two see the same structure.

## Stack

Python + Pydantic AI. Supabase (PostgreSQL + pgvector) for storage and retrieval. Streamlit if a
UI is needed. pytest for tests.

## Commands

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

pytest                              # all tests
pytest tests/test_tools.py          # one file
pytest tests/test_tools.py::test_x  # one test
pytest -k "retrieval"               # by name match

streamlit run streamlit_ui.py       # UI, if present
```

## Architecture

Follow the four-file agent split from the parent repo — the boundaries matter more than the
filenames, and crossing them is what makes these examples hard to follow:

- `agent.py` — the Pydantic AI agent: model config, dependency type, tool registration. No tool
  bodies and no I/O.
- `tools.py` — one function per tool, each independently testable without constructing an agent.
- `clients.py` — all outbound I/O (LLM, Supabase, embeddings). The only layer that reads env vars,
  so tests substitute clients here rather than monkeypatching call sites.
- `prompt.py` — system prompt text, kept out of `agent.py` so prompt edits produce clean diffs.

Configuration comes from environment variables throughout — that is what lets the same code run
against local Ollama/Supabase and against cloud providers, which is a point the course makes
explicitly. Never hardcode a provider or model name outside `clients.py`.
