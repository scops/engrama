# Vision

## The problem

AI agents have short-term memory. Every conversation starts from zero. Existing memory systems are either too simple (flat JSON files), too complex (embedding pipelines, vector databases, cloud orchestrators), or compromise privacy by depending on external services.

Karpathy built his second brain in Markdown and wikis — that works for humans because humans perform natural semantic search when reading. Agents don't. Agents need **graphs**: explicit relationships, efficient traversals, precise queries without scanning every document.

## The solution

**Engrama** is a plug-and-play Python framework that gives any AI agent persistent, structured memory backed by a Neo4j knowledge graph. The agent can remember, associate, forget, and reason about its accumulated knowledge — exactly as a human with good memory would.

## What makes it different

| Compared to | Difference |
|---|---|
| MCP Memory (JSON) | Engrama scales. A 10,000-entity JSON file is unmanageable. A 10,000-node graph is navigated in milliseconds. |
| Obsidian / Markdown | Obsidian is for humans. Engrama is for agents. Relationships are first-class citizens, not text links. |
| Mem0 / Zep (cloud) | Engrama is local-first. Your data never leaves your machine. |
| RAG + vector DB | Engrama doesn't need embeddings for structured queries. They're an optional layer, not a requirement. |

## Who it's for

- **Agent developers** who want persistent memory in 5 minutes
- **Researchers** who want a personal knowledge graph
- **Instructors and educators** who want their agent to remember projects, students, and pedagogical decisions
- **Anyone building with LLMs** who is tired of repeating context in every session

## Design philosophy

1. **Local-first** — your graph, on your machine, zero cloud dependencies
2. **Plug-and-play** — `engrama init --profile developer` and ready in 2 minutes
3. **Agent-agnostic** — works with Claude, LangChain, n8n, or anything that speaks MCP or Python
4. **Schema as configuration** — profiles define node types and relationships without touching code
5. **Graphs first, vectors later** — explicit structure always beats brute-force semantic search for efficiency

## The name

*Engrama* (from Greek *engramma*): the physical trace left by a memory in neural tissue. The mark that persists. Exactly what this framework does for agents.
