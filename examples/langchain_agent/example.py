"""Use Engrama as the long-term memory of a LangChain agent.

Engrama ships no LangChain-specific classes — the SDK is plain Python, so you
wrap its methods as ``langchain_core`` tools and hand them to any agent.

    pip install "engrama" langchain langchain-openai
    python examples/langchain_agent/example.py

This file is intentionally dependency-light: the agent wiring at the bottom is
guarded behind ``__main__`` and only runs if LangChain is installed. The tool
definitions above work on their own.
"""

from __future__ import annotations

from engrama import Engrama

# A single shared Engrama handle backs every tool (SQLite by default).
_eng = Engrama()


def remember_fact(label: str, name: str, observation: str) -> str:
    """Store a fact in long-term memory. Returns a short confirmation."""
    _eng.remember(label, name, observation)
    return f"Remembered {label}:{name}"


def recall_facts(query: str) -> str:
    """Recall facts related to a query, with their graph neighbourhood."""
    results = _eng.recall(query, hops=1)
    if not results:
        return "No relevant memories."
    lines = []
    for r in results:
        neighbours = ", ".join(n.get("name", "?") for n in r.neighbours) or "(none)"
        lines.append(f"{r.name} (related: {neighbours})")
    return "\n".join(lines)


def _build_tools():
    """Expose the two functions above as LangChain StructuredTools."""
    from langchain_core.tools import StructuredTool

    return [
        StructuredTool.from_function(remember_fact),
        StructuredTool.from_function(recall_facts),
    ]


if __name__ == "__main__":
    # Tools work standalone — no LLM needed to demonstrate the memory layer.
    print(remember_fact("Technology", "FastAPI", "Async Python web framework."))
    print(recall_facts("FastAPI"))

    # To plug these into a real agent (requires an OpenAI key):
    #
    #   from langchain.agents import create_tool_calling_agent, AgentExecutor
    #   from langchain_openai import ChatOpenAI
    #   from langchain_core.prompts import ChatPromptTemplate
    #
    #   llm = ChatOpenAI(model="gpt-4o-mini")
    #   prompt = ChatPromptTemplate.from_messages([
    #       ("system", "You are an assistant with persistent memory. "
    #                  "Use recall_facts before answering and remember_fact "
    #                  "when you learn something new."),
    #       ("human", "{input}"),
    #       ("placeholder", "{agent_scratchpad}"),
    #   ])
    #   agent = create_tool_calling_agent(llm, _build_tools(), prompt)
    #   executor = AgentExecutor(agent=agent, tools=_build_tools())
    #   print(executor.invoke({"input": "What framework does the project use?"}))

    _eng.close()
