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
- **Python ≥ 3.11** with PyYAML (`pip install pyyaml`)
- **Neo4j** running (Docker or native)
- **Obsidian** vault (optional — only needed for note sync features)

No dependency on any specific AI framework, agent SDK, or MCP runtime.

## What gets generated

From a single `profiles/<name>.yaml` file, the bundled codegen script produces:

1. **`engrama/core/schema.py`** — NodeType enum, RelationType enum, dataclasses,
   `TITLE_KEYED_LABELS` set (used by engine and MCP server for merge-key logic)
2. **`scripts/init-schema.cypher`** — Neo4j constraints, fulltext index, range indexes

The profile YAML is the **single source of truth**.  Change it, rerun the
script, and the entire schema propagates.  No manual editing needed.

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

### Phase 2 — Propose a schema

Based on Phase 1, read `references/example-profiles.md` for inspiration,
then propose 5–8 node types and 6–12 relationships.

Present them in plain language first — a summary table, not YAML.  For each
node type explain in one sentence what it is and what it connects to.

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

Ask: "Does this capture how you think about your work?  What's missing?
What doesn't fit?"  Iterate until the user confirms.

### Phase 3 — Generate and apply

Once confirmed:

1. Write the profile YAML to the Engrama project's `profiles/<name>.yaml`
2. Run the codegen script (bundled in this skill):
   ```bash
   python scripts/generate_from_profile.py profiles/<name>.yaml --project-root <engrama_root>
   ```
   If the Engrama project is at the default location, `--project-root` can be
   omitted.  Use `--dry-run` first to preview without writing files.
3. Show the user a summary of what was generated (not full files)
4. Tell them to apply the Neo4j schema:
   ```bash
   docker exec -i engrama-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD < scripts/init-schema.cypher
   ```
   If the fulltext index already exists with different labels, drop it first:
   ```cypher
   DROP INDEX memory_search IF EXISTS;
   ```
5. Verify:
   ```bash
   uv run pytest tests/ -v
   ```

## YAML template

Use this skeleton when building the profile:

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

Rules:
- The merge key (first item in `required`) must be either `name` or `title`
- `title` is for nodes where the identifier is a sentence (Decision, Protocol)
- `name` is for everything else (Project, Technology, Patient)
- `status` is optional but recommended for lifecycle nodes
- `description` on each node helps the LLM understand context
- Every label in `relations` must exist in `nodes`
- Relationship types should be verbs, not nouns (USES not USAGE)
- An `Insight` node type is always added automatically by the codegen —
  don't include it in the profile

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
