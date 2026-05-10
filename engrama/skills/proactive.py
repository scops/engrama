"""
engrama/skills/proactive.py

The proactive skill surfaces pending Insights to the agent and manages
the human approval flow.  It also writes approved Insights back to
Obsidian as note sections.

Lifecycle of an Insight:

1. ``reflect`` detects a pattern → writes ``Insight {status: "pending"}``.
2. ``proactive.surface()`` → reads all pending Insights, formats them for
   the agent to present to the human.
3. Human reviews → ``proactive.approve()`` or ``proactive.dismiss()``.
4. If approved, ``proactive.write_to_vault()`` appends the Insight as a
   section in an Obsidian note.

The agent **never** acts on unapproved Insights — it only presents them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engrama.adapters.obsidian import ObsidianAdapter
    from engrama.core.engine import EngramaEngine


@dataclass
class SurfacedInsight:
    """An Insight formatted for agent presentation."""

    title: str
    body: str
    confidence: float
    source_query: str
    created_at: str | None = None


class ProactiveSkill:
    """Surface pending Insights and manage the approval flow."""

    # ------------------------------------------------------------------
    # Surface — read pending Insights from Neo4j
    # ------------------------------------------------------------------

    def surface(
        self,
        engine: EngramaEngine,
        *,
        limit: int = 10,
    ) -> list[SurfacedInsight]:
        """Return all pending Insights, newest first.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            limit: Maximum number of Insights to return.

        Returns:
            A list of :class:`SurfacedInsight` ready for agent presentation.
        """
        records = engine._store.get_pending_insights(limit=limit)

        results: list[SurfacedInsight] = []
        for r in records:
            created = r["created_at"]
            if created is not None:
                created = str(created)
            results.append(
                SurfacedInsight(
                    title=r["title"],
                    body=r["body"],
                    confidence=r["confidence"],
                    source_query=r["source_query"],
                    created_at=created,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Approve / Dismiss
    # ------------------------------------------------------------------

    def approve(self, engine: EngramaEngine, *, title: str) -> dict:
        """Mark an Insight as approved by the human.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            title: Exact title of the Insight to approve.

        Returns:
            A dict with ``title``, ``action``, ``matched`` (bool).
        """
        matched = engine._store.update_insight_status(title, "approved")
        return {
            "title": title,
            "action": "approved",
            "matched": matched,
        }

    def dismiss(self, engine: EngramaEngine, *, title: str) -> dict:
        """Mark an Insight as dismissed by the human.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            title: Exact title of the Insight to dismiss.

        Returns:
            A dict with ``title``, ``action``, ``matched`` (bool).
        """
        matched = engine._store.update_insight_status(title, "dismissed")
        return {
            "title": title,
            "action": "dismissed",
            "matched": matched,
        }

    # ------------------------------------------------------------------
    # Write to Obsidian
    # ------------------------------------------------------------------

    def write_to_vault(
        self,
        engine: EngramaEngine,
        obsidian: ObsidianAdapter,
        *,
        title: str,
        target_note: str,
    ) -> dict:
        """Append an approved Insight to an Obsidian note.

        Only writes Insights with ``status: "approved"``.  Unapproved
        Insights are rejected — the agent must never write them.

        Args:
            engine: An initialised :class:`EngramaEngine`.
            obsidian: An :class:`ObsidianAdapter` with vault access.
            title: Exact title of the Insight.
            target_note: Relative path to the Obsidian note to append to.

        Returns:
            A dict with ``title``, ``target_note``, ``written`` (bool),
            and ``reason`` if not written.
        """
        # Verify the Insight is approved
        insight = engine._store.get_insight_by_title(title)

        if insight is None:
            return {
                "title": title,
                "target_note": target_note,
                "written": False,
                "reason": "Insight not found in graph.",
            }

        if insight["status"] != "approved":
            return {
                "title": title,
                "target_note": target_note,
                "written": False,
                "reason": f"Insight status is '{insight['status']}', not 'approved'. "
                "Only approved Insights can be written to the vault.",
            }

        # Build the markdown section
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        confidence_pct = int(insight["confidence"] * 100)
        section = (
            f"\n## Insight: {title}\n\n"
            f"> **Confidence:** {confidence_pct}% · "
            f"**Source:** {insight['source_query']} · "
            f"**Approved:** {now}\n\n"
            f"{insight['body']}\n"
        )

        # Write to vault
        note = obsidian.read_note(target_note)
        if not note["success"]:
            return {
                "title": title,
                "target_note": target_note,
                "written": False,
                "reason": f"Target note not found: {target_note}",
            }

        target_path = obsidian._resolve(target_note)
        current_content = target_path.read_text(encoding="utf-8")
        target_path.write_text(
            current_content.rstrip("\n") + "\n\n---\n" + section,
            encoding="utf-8",
        )

        # Mark as synced in Neo4j
        engine._store.mark_insight_synced(title, target_note)

        return {
            "title": title,
            "target_note": target_note,
            "written": True,
        }
