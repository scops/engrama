---
name: engrama-onboard
description: Build a personalized Engrama memory graph for any user through a conversational interview. Use this skill whenever someone says "set up Engrama", "create my graph", "onboard", "build my profile", "configure Engrama for me", or asks how to start using Engrama. Also use when the user mentions they have a different role than the current profile (e.g., "I'm a nurse", "I'm a researcher"). This is the first thing any new Engrama user should do.
---

# Engrama Onboard

Build a personalized memory graph schema for any user through conversation.

Engrama's power comes from its graph structure, but that structure must match
how the user actually thinks and works.  A developer tracks Projects, Decisions,
and Technologies.  A nurse tracks Patients, Protocols, and Medications.  A
teacher tracks Students, Curricula, and Assessments.  The onboard skill conducts
a short interview to understand who the user is, then generates a complete
profile that drives the entire system.

## Prerequisites

The only hard dependencies are:
- **Python ≥ 3.11** with PyYAML (installed automatically by `uv sync`)
- An installed Engrama checkout (`git clone` + `uv sync`)
- **Obsidian** vault (optional — only needed for note sync features)

**A database is not a prerequisite.** Since 0.9 Engrama defaults to a
zero-dependency SQLite backend that lives in `~/.engrama/engrama.db`
and is created automatically on first connection. Neo4j is opt-in
(`uv sync --extra neo4j` + Docker) for multi-process production
setups, very large vector indexes, or teams already using Cypher —
see [BACKENDS.md](../../../BACKENDS.md) for the decision guide.

No dependency on any specific AI framework, agent SDK, or MCP runtime.

## What gets generated

From a profile YAML (standalone or composed from base + modules), the codegen
script produces:

1. **`engrama/core/schema.py`** — NodeType enum, RelationType enum, dataclasses,
   `TITLE_KEYED_LABELS` set (used by engine and MCP server for merge-key logic)
2. **`scripts/init-schema.cypher`** — Neo4j-only constraints, fulltext index,
   and range indexes. Applied automatically when the Neo4j backend is selected.
   The SQLite backend ignores this file entirely; its schema lives in
   `engrama/backends/sqlite/schema.sql` and is applied on first connection,
   so `uv run engrama init` is backend-agnostic.

The profile YAML is the **single source of truth**.  Change it, rerun the
script, and the entire schema propagates.  No manual editing needed.

### Composable profiles (the default for onboarding)

Most people have multiple roles.  A nurse who teaches biology and cooks on
weekends.  A sysadmin who does pentesting and nature photography.  Instead of
building one monolithic YAML, Engrama supports **composable profiles**: a
`base.yaml` with universal node types (Project, Concept, Decision, Problem,
Technology, Person) plus domain modules in `profiles/modules/` that add
domain-specific nodes and relations.

```bash
# Composable — base + custom modules:
uv run engrama init --profile base --modules nursing biology cooking

# Standalone (backward-compatible):
uv run engrama init --profile developer
```

**During onboarding, always prefer composable modules.**  For each role or
interest the user mentions, generate a separate module YAML.  The four
included modules (`hacking`, `teaching`, `photography`, `ai`) are examples —
you will typically generate **new** modules tailored to the user.

Modules can safely reference base node types in their relations (e.g. a
teaching module can define `Course COVERS Concept` where `Concept` comes from
`base.yaml`).  Nodes with the same label across modules get their properties
merged (union).

## Bundled resources

This skill is self-contained.  All resources are bundled:

- `scripts/generate_from_profile.py` — the codegen engine
- `references/example-profiles.md` — five complete example profiles for
  different personas (developer, nurse, lawyer, PM, freelancer)

Read `references/example-profiles.md` before proposing a schema — it has
design principles and worked examples for very different user types.

## Interview flow

The interview has three phases.  Keep it conversational — the user shouldn't
feel like they're filling out a form.  Adapt your language to the user's
technical level.

### Phase 1 — Who are you?

Ask about their roles and what they want to track.  People often have multiple
roles (instructor + developer + sysadmin, nurse + mother + researcher).

Example openers:
- "Tell me about what you do — your roles, responsibilities, the things you
  juggle day to day."
- "What kind of things do you find yourself wishing you could connect or
  cross-reference?"

Listen for **nouns** (these become node types) and **verbs/prepositions**
(these become relationships).  Don't ask for these explicitly — extract them
from the user's natural description.

### Phase 2 — Propose a modular schema

Based on Phase 1, read `references/example-profiles.md` for inspiration.

**Group the user's roles/interests into modules.**  For example, if they say
"I'm a nurse who teaches biology and cooks," you'd propose three modules:
`nursing`, `biology`, `cooking`.  Each module adds 3–5 node types and a few
relationships.  The base profile already provides the universal ones (Project,
Concept, Technology, Decision, Problem, Person).

Present the modules in plain language first — **not YAML**.  Show a table per
module: what nodes it adds, what they connect to, and how they link to base
labels.  Example:

> **Module: nursing**
> | Node | Key | Connects to |
> |------|-----|-------------|
> | Patient | name | has → Condition, treated with → Medication |
> | Condition | name | related to → Concept |
> | Medication | name | ... |

Only show the YAML after the user agrees with the plain-language proposal.

For each node type:
- **Label** — PascalCase, singular (Patient not Patients)
- **Merge key** — `name` for most nodes; `title` for nodes identified by a
  sentence (decisions, problems, tasks, protocols, studies, cases)
- **Properties** — the merge key + 3–6 useful properties.  Always include
  `status` if the node has a lifecycle.
- **Description** — one line explaining what this node represents

For relationships:
- **Type** — UPPER_SNAKE_CASE verb (USES, FOLLOWS, TREATS, COVERS)
- **Direction** — from → to, always the natural reading direction
- Relations **can and should cross modules** — that's the whole point of a
  graph (e.g. a Course in the teaching module COVERS a Concept from base)

Ask: "Does this capture how you think about your work?  What's missing?
What doesn't fit?"  Iterate until the user confirms.

### Phase 3 — Generate modules and apply

Once confirmed:

1. **Write each module** as a separate YAML in `profiles/modules/<name>.yaml`.
   Check if an existing module already covers what's needed — reuse it if so.
   If the user has only one role, you can still write a single module.

2. **Run the codegen** with `--dry-run` first:
   ```bash
   uv run engrama init --profile base --modules nursing biology cooking --dry-run
   ```
   Review the output with the user.  Then apply for real:
   ```bash
   uv run engrama init --profile base --modules nursing biology cooking
   ```

3. **(Neo4j backend only)** If the fulltext index already exists with
   different labels, drop it first via `cypher-shell` or Neo4j Browser:
   ```cypher
   DROP INDEX memory_search IF EXISTS;
   ```
   On the SQLite backend this step is unnecessary — `uv run engrama
   init` regenerates the schema and FTS5 index automatically on the
   next connection.

4. Verify:
   ```bash
   uv run pytest tests/ -v
   ```

**Important:** Do not limit the user to the four example modules that ship
with Engrama.  The whole point of the onboard skill is to generate **new**
modules that match the user's actual life.  A cook gets `cooking.yaml`.
A nurse gets `nursing.yaml`.  A fisherman gets `fishing.yaml`.  There is
no closed list.

## YAML templates

### Standalone profile

```yaml
name: <profile_name>
description: <one-line description of who this profile is for>

nodes:
  - label: <PascalCase>
    properties: [<merge_key>, prop2, prop3, status, description]
    required: [<merge_key>]
    description: "<what this node represents>"

relations:
  - {type: <UPPER_SNAKE>, from: <Label>, to: <Label>}
```

### Domain module

Modules follow the same format but can reference node labels defined in
`base.yaml` (Project, Concept, Decision, Problem, Technology, Person)
in their relations without redefining them.

```yaml
name: <module_name>
description: <what this module adds>

nodes:
  - label: <PascalCase>
    properties: [<merge_key>, prop2, status, notes]
    required: [<merge_key>]
    description: "<what this node represents>"

relations:
  - {type: <UPPER_SNAKE>, from: <Label>, to: <Label>}
  # Can reference base labels like Concept, Technology, Project:
  - {type: COVERS, from: Course, to: Concept}
```

### Rules (both formats)

- The merge key (first item in `required`) must be either `name` or `title`
- `title` is for nodes where the identifier is a sentence (Decision, Protocol)
- `name` is for everything else (Project, Technology, Patient)
- `status` is optional but recommended for lifecycle nodes
- `description` on each node helps the LLM understand context
- Every label in `relations` must exist in `nodes` (including base nodes
  when composing)
- Relationship types should be verbs, not nouns (USES not USAGE)
- An `Insight` node type is always added automatically by the codegen —
  don't include it in the profile or module

## After onboarding

Once the profile is applied, suggest these next steps:

1. **Seed the graph** — if the user has existing Obsidian notes, use
   `engrama_sync_vault` to parse and import them
2. **Start remembering** — use `engrama_remember` to add a few nodes
   manually and verify the schema feels right
3. **Run reflect** — once there are enough nodes (10+), run
   `engrama_reflect` to see if the detection queries find anything
4. **Iterate** — if they realize they need new node types or
   relationships, re-run the onboard interview to update the profile
