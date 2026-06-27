# Engrama examples

Runnable examples for the three main ways to use Engrama. All default to the
zero-dependency SQLite backend — no external services required.

| Example | What it shows | Run |
|---|---|---|
| [`python_sdk/quickstart.py`](python_sdk/quickstart.py) | Remember entities, relate them, search and recall via the Python SDK. | `python examples/python_sdk/quickstart.py` |
| [`langchain_agent/example.py`](langchain_agent/example.py) | Wrap Engrama SDK calls as LangChain tools for an agent's long-term memory. | `python examples/langchain_agent/example.py` |
| [`claude_desktop/config.json`](claude_desktop/config.json) | Drop-in MCP server config for Claude Desktop (PyPI install). | Copy into your Claude Desktop config. |
| [`claude_desktop/config.dev.json`](claude_desktop/config.dev.json) | Same, for a local source checkout via `uv run`. | Edit the path, then copy in. |
| [`claude_desktop/system-prompt.md`](claude_desktop/system-prompt.md) | A system prompt that teaches an agent when to call each Engrama tool. | Paste into your agent's system prompt. |

## Prerequisites

```bash
pip install "engrama[mcp]"     # SDK + MCP server
# or, from a source checkout:
uv sync --extra mcp
```

See the [top-level README](../README.md) for the full quick start and the
[security considerations](../README.md#security-considerations) before
exposing Engrama to more than one user.
