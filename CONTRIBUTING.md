# Contributing to StoryBored

This guide is written for **humans and coding agents alike**. If you are an
agent: read this file, then [ARCHITECTURE.md](ARCHITECTURE.md), then
[docs/CONTRACT.md](docs/CONTRACT.md) — in that order — before editing anything.

## Ground rules (the short version)

1. **docs/CONTRACT.md is binding.** It specifies the v1 API, data model, env
   vars, and behavior. Code conforms to the contract, not the other way
   around. If a change requires a contract change, propose the contract edit
   in the same PR and call it out loudly in the description.
2. **Respect the invariants** in [ARCHITECTURE.md](ARCHITECTURE.md#invariants--do-not-break-these).
   The non-negotiables: single GPU lane; engines only via workflow packs; no
   machine-specific facts anywhere in the repo; adapters degrade gracefully;
   tests never touch the network.
3. **Never commit machine-specific facts.** No hostnames, IPs, usernames,
   personal LoRA names, private URLs, or absolute paths from your machine — in
   code, docs, tests, example graphs, screenshots, or commit messages.
   Machine config goes in `.env` (untracked); document new vars in
   `.env.example` with a neutral placeholder.
4. **No fabricated numbers.** Don't invent benchmarks, speeds, or VRAM figures
   in docs. State only what you measured (and say on what class of hardware)
   or keep it qualitative.
5. **User-facing copy is jargon-free.** Primary UI text never says "ComfyUI",
   "LoRA", "workflow", "node", or "safetensors" — say **engine**,
   **character**, **style**, **engine pack**. Jargon is fine in docs and code.

## Dev setup

```bash
git clone https://github.com/storybored-app/storybored.git
cd storybored
cp .env.example .env               # defaults are fine for backend dev; no GPU needed

# Backend (Python 3.11+)
python3 -m venv .venv
.venv/bin/pip install -e 'backend[dev]'

# Frontend (Node 20+)
npm --prefix frontend i

# Run both with hot reload (backend :8600, vite dev server proxies /api)
scripts/dev.sh
```

You do **not** need ComfyUI, an LLM, or a GPU to develop: the app degrades
gracefully (health shows components as down) and the entire test suite runs
against local fakes.

## Quality bar — run these before every PR

```bash
# Backend: lint + tests (in-memory sqlite; fake ComfyUI + fake LLM servers)
.venv/bin/ruff check backend
.venv/bin/pytest backend/tests -q

# Frontend: types + build must be clean (that's the v1 bar; no component tests)
(cd frontend && npx tsc --noEmit)
npm --prefix frontend run build
```

CI (`.github/workflows/ci.yml`) runs exactly these on every push/PR — green CI
is required to merge.

### Testing conventions

- New backend behavior gets a pytest in `backend/tests/`.
- Anything that would call ComfyUI or an LLM must go through the existing fake
  servers in `backend/tests/conftest.py` (extend the fakes if you need new
  endpoints). **No network calls in tests, ever.**
- Keep tests hermetic: in-memory sqlite, `tmp_path` for files.

## Making changes: where things go

| You want to… | Do this | Guide |
| --- | --- | --- |
| Add a generation engine | New folder in `workflows/` — no code | [docs/WORKFLOWS.md](docs/WORKFLOWS.md) |
| Add an API endpoint | Router in `backend/storybored/api/` + schemas + test | [ARCHITECTURE.md](ARCHITECTURE.md#add-an-api-endpoint) |
| Touch generation plumbing | `backend/storybored/engine/` (keep `graph.py` pure/testable) | contract §ComfyUI client |
| Add a job type | Handler + lane decision in `backend/storybored/jobs/` | invariant #1 first |
| Trainer / LLM work | `backend/storybored/training/` / `backend/storybored/llm/` | [ARCHITECTURE.md](ARCHITECTURE.md#how-to-extend) |
| UI work | `frontend/` — match the existing dark cinematic look, no new jargon | contract §Frontend |
| New dependency | Justify it in the PR; backend deps stay minimal (contract lists them) | — |

## PR checklist

- [ ] Lint, tests, tsc, and build all green locally.
- [ ] Conforms to docs/CONTRACT.md (or the PR also updates the contract and
      says so).
- [ ] No machine-specific facts anywhere in the diff (grep your diff for your
      hostname/username before pushing).
- [ ] New env vars documented in `.env.example`; unset ⇒ graceful degradation.
- [ ] Mutations publish SSE events; UI copy stays jargon-free.
- [ ] Docs updated if behavior or extension points changed.

## Notes for coding agents specifically

- **Read order:** CONTRIBUTING.md → ARCHITECTURE.md → docs/CONTRACT.md → the
  files you touch. The file map in ARCHITECTURE.md is accurate; don't invent
  new top-level directories.
- **Smallest correct diff wins.** Don't reformat unrelated code, don't rename
  for taste, don't add speculative abstractions.
- **When the contract is ambiguous**, pick the simplest reading, implement it,
  and record the interpretation in your PR description — do not silently
  extend the API surface.
- **Never weaken a test to make it pass**, and never mock away path-traversal
  or graceful-degradation checks.
- **Verify your scrub:** before finishing, grep your changes for anything that
  looks like a hostname, IP, username, or personal model name.

## License

MIT. By contributing you agree your contributions are MIT-licensed.
