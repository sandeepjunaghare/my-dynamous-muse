# my-dynamous-muse

Personal working repo from the Dynamous [AI Agent Mastery](https://github.com/dynamous-community/ai-agent-mastery) course — memory
experiments, a context-engineering (PRP) workflow, and course scaffolding.

This is a learning repo, not a library. The code here is kept legible rather than
production-hardened, and several folders are deliberately at different stages of completeness.

## Layout

| Folder | What it is | State |
|---|---|---|
| `mem0-agent/` | Chat agents with long-term memory via [Mem0](https://mem0.ai), progressing from in-memory to Supabase-backed storage | Runnable |
| `my-ai-prp-project/` | Context engineering with PRPs (Product Requirement Prompts) for Pydantic AI agents | Runnable |
| `agentic-course-2.0/` | Scaffold for the course's worked example app | Scaffold only |

## mem0-agent

Three progressively richer takes on the same idea — an OpenAI chat loop that recalls what you told
it earlier.

- `v1-basic-mem0.py` — `Memory.from_config()` with default local storage. Start here.
- `v2-supabase-mem0.py` — same loop, but with Supabase (PostgreSQL + pgvector) as the vector store.
- `mem0helloworld.py` — minimal `MemoryClient` call against Mem0's hosted API.
- `test_supabaseconn.py` — quick psycopg2 connection check before debugging anything else.

```bash
cd mem0-agent
python -m venv mem0-venv && source mem0-venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # see Environment setup below
python v1-basic-mem0.py
```

V2 expects PostgreSQL with pgvector on `127.0.0.1:5432`. The
[`local-ai-packaged`](https://github.com/coleam00/local-ai-packaged) stack provides one.

## my-ai-prp-project

The PRP workflow: describe an agent in `PRPs/INITIAL.md`, generate a detailed implementation prompt
from it, then execute that prompt.

```
INITIAL.md  →  /generate-pydantic-ai-prp INITIAL.md  →  /execute-pydantic-ai-prp PRPs/<file>.md
```

- `.claude/` — the commands and subagents that drive the workflow (dependency manager, prompt
  engineer, tool integrator, validator)
- `PRPs/examples/` — reference agents to build from: basic chat, tool-enabled, structured output,
  a RAG pipeline, and testing patterns
- `PromptProgression/` — eight prompts for one task, from vague "vibe coding" to a full PRP, with
  `comparison.md` explaining what each step adds
- `FullExample/` — a complete agentic RAG system (Pydantic AI + pgvector + Neo4j/Graphiti +
  FastAPI). Has its own README and setup.

## Environment setup

Each runnable subproject reads its own local `.env`, copied from the `.env.example` sitting beside
it. Nothing is read from a repo-wide file.

| Copy this | To | Used by |
|---|---|---|
| `mem0-agent/.env.example` | `mem0-agent/.env` | all `mem0-agent/` scripts |
| `my-ai-prp-project/FullExample/.env.example` | `FullExample/.env` | the agentic RAG system |
| `my-ai-prp-project/PRPs/examples/main_agent_reference/.env.example` | alongside it | the reference agent |

### mem0-agent

```bash
cd mem0-agent
cp .env.example .env
```

Then fill in `.env`. What each script actually needs:

| Script | Requires | Optional |
|---|---|---|
| `v1-basic-mem0.py` | `OPENAI_API_KEY` | — |
| `v2-supabase-mem0.py` | `OPENAI_API_KEY`, `DATABASE_URL` | `MODEL_CHOICE` |
| `mem0helloworld.py` | `MEM0_API_KEY` | — |
| `test_supabaseconn.py` | nothing — it hardcodes `localhost:5432` | — |

- **`OPENAI_API_KEY`** — from <https://platform.openai.com/api-keys>.
- **`MEM0_API_KEY`** — from <https://app.mem0.ai>, for the hosted Mem0 API only.
- **`MODEL_CHOICE`** — sets both the chat model and Mem0's memory-extraction model in v2.
  Defaults to `gpt-4o-mini`.
- **`DATABASE_URL`** — Postgres with pgvector. The comments in `.env.example` give the local and
  hosted forms; `test_supabaseconn.py` is the fastest way to confirm it before debugging anything
  else.

`v2-supabase-mem0.py` guards `DATABASE_URL` and `mem0helloworld.py` guards `MEM0_API_KEY`, each
raising with the variable name at startup rather than failing partway through a run. A missing
`OPENAI_API_KEY` is not guarded — it surfaces later, as an auth error from the OpenAI client.

### FullExample

`FullExample/` is a larger system with roughly 30 variables — LLM and embedding providers, Neo4j
credentials, chunking and rate-limit tuning. Follow its own README rather than this section.

## Secrets

`.env` files are gitignored; only `.env.example` files are tracked. Nothing in this repo should
ever contain a real key — if you add code that needs one, read it with `os.getenv()` and fail
loudly when it is missing.
