"""
Obsidian MCP Server (FastMCP 3.x)

MCP server that manages an Obsidian vault. Exposes tools for CRUD
operations on Markdown notes, resources for vault structure and
persistent memory, and prompts for repeatable workflows.

Demonstrates all 4 MCP primitives:
  1. TOOLS       - Actions the model can execute (create, read, move notes)
  2. RESOURCES   - Read-only data the model can inspect (vault structure, memory)
  3. PROMPTS     - Reusable instruction templates (inbox processing, HTB writeups)
  4. ELICITATION - Interactive confirmation before destructive operations
"""

# ──────────────────────────────────────────────────────────────────
# IMPORTS
# ──────────────────────────────────────────────────────────────────
import os
import sys
import json
import re
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations


# ──────────────────────────────────────────────────────────────────
# ELICITATION SCHEMAS (Pydantic models required by FastMCP 3.x)
# ──────────────────────────────────────────────────────────────────

class ConfirmSchema(BaseModel):
    confirm: bool = Field(default=False, title="Confirm")

class MoveConfirmSchema(BaseModel):
    confirm: bool = Field(default=True, title="Confirm move")
    alternative_destination: str = Field(
        default="",
        title="Alternative destination (optional)",
        description="Enter a different path if preferred. Leave empty to use the proposed one.",
    )


# ──────────────────────────────────────────────────────────────────
# GLOBAL CONFIGURATION
# ──────────────────────────────────────────────────────────────────

# VAULT_PATH is injected as an environment variable from the MCP client config.
VAULT_PATH = Path(os.environ.get("VAULT_PATH", str(Path.home() / "Documents/vault")))
if not VAULT_PATH.exists():
    print(f"ERROR: VAULT_PATH no existe: {VAULT_PATH}", file=sys.stderr)
    sys.exit(1)
# Special file used as persistent memory across sessions.
MEMORY_FILE = "_memoria_claude.md"

# Purpose map for each PARA folder. Used by tools and prompts to
# decide where notes belong.
VAULT_STRUCTURE = {
    "00-inbox":                   "Unsorted new notes. Entry point for everything.",
    "10-projects":                "Active projects with a deadline or deliverable.",
    "20-areas":                   "Ongoing responsibilities (courses, business, personal).",
    "30-resources/hacking/htb":   "HackTheBox writeups and notes.",
    "30-resources/hacking/tools": "Tool cheatsheets (nmap, john, etc.).",
    "30-resources/dev":           "Development snippets and references.",
    "30-resources/futuribles":    "Future ideas and potential projects.",
    "40-archive":                 "Closed projects and inactive notes.",
    "50-cursos":                  "Teaching resources and guides related to courses.",
}

mcp = FastMCP("obsidian_mcp")


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _resolve(relative_path: str) -> Path:
    """Resolve a vault-relative path to an absolute path, blocking path traversal."""
    target = (VAULT_PATH / relative_path).resolve()
    if not str(target).startswith(str(VAULT_PATH.resolve())):
        raise ValueError(f"Path traversal blocked: '{relative_path}'")
    return target


def _note_path(folder: str, filename: str) -> Path:
    """Build the full path for a note, ensuring .md extension."""
    if not filename.endswith(".md"):
        filename += ".md"
    return _resolve(f"{folder}/{filename}")


def _frontmatter(tags: List[str], extra: dict) -> str:
    """Generate a YAML frontmatter block for an Obsidian note."""
    lines = ["---"]
    lines.append(f"date: {datetime.now().strftime('%Y-%m-%d')}")
    if tags:
        lines.append(f"tags: [{', '.join(tags)}]")
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _list_md_files(folder: Path, recursive: bool = False) -> List[dict]:
    """List .md files in a folder with basic metadata."""
    pattern = "**/*.md" if recursive else "*.md"
    files = []
    for f in sorted(folder.glob(pattern)):
        files.append({
            "path": str(f.relative_to(VAULT_PATH)),
            "name": f.stem,
            "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return files


def _folder_tree(base: Path, indent: int = 0) -> str:
    """Generate a directory tree string for the vault structure resource."""
    lines = []
    try:
        for item in sorted(base.iterdir()):
            if item.name.startswith("."):
                continue
            prefix = "  " * indent + ("dir  " if item.is_dir() else "file ")
            lines.append(f"{prefix}{item.name}")
            if item.is_dir():
                lines.append(_folder_tree(item, indent + 1))
    except PermissionError:
        lines.append("  " * indent + "[no permissions]")
    return "\n".join(filter(None, lines))


# ══════════════════════════════════════════════════════════════════
# RESOURCES
# ══════════════════════════════════════════════════════════════════

@mcp.resource(
    uri="vault://structure",
    name="Vault folder structure",
    description=(
        "Returns the full directory tree of the Obsidian vault along with "
        "the purpose of each PARA folder. Read this before deciding where "
        "to create or move notes."
    ),
    mime_type="text/plain",
)
async def resource_vault_structure() -> str:
    """Return the vault folder tree and PARA folder purposes."""
    if not VAULT_PATH.exists():
        return f"Vault not found at: {VAULT_PATH}"

    tree = _folder_tree(VAULT_PATH)
    guide = "\n".join(f"  {k}: {v}" for k, v in VAULT_STRUCTURE.items())

    return f"""# Vault Structure
Path: {VAULT_PATH}

## File tree
{tree}

## Folder purposes (PARA system)
{guide}
"""


@mcp.resource(
    uri="vault://memory",
    name="Persistent memory file",
    description=(
        "Returns the contents of _memoria_claude.md, a persistent memory file "
        "containing active projects, user preferences, and vault conventions. "
        "Read this at the start of each session for context continuity."
    ),
    mime_type="text/markdown",
)
async def resource_memory() -> str:
    """Return the persistent memory file contents."""
    memory_path = VAULT_PATH / MEMORY_FILE

    if not memory_path.exists():
        return (
            f"# No memory file\n\n"
            f"`{MEMORY_FILE}` does not exist in the vault.\n\n"
            f"Create one with `vault_update_memory` to provide persistent "
            f"context across sessions.\n"
        )

    content = memory_path.read_text(encoding="utf-8")
    modified = datetime.fromtimestamp(memory_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"<!-- Last updated: {modified} -->\n\n{content}"


# ══════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════

@mcp.prompt(
    name="process_inbox",
    description=(
        "Start the weekly PARA inbox review: read each note in 00-inbox, "
        "classify it into the appropriate folder, and confirm with the user "
        "before moving."
    ),
)
async def prompt_process_inbox() -> str:
    """Workflow prompt for processing the inbox."""
    return f"""Let's process the vault inbox.

## Steps
1. `vault_list_notes` with folder="00-inbox" to see pending notes
2. For each note:
   a. `vault_read_note` to read its content
   b. Decide the destination folder based on the PARA system:
      {json.dumps(VAULT_STRUCTURE, ensure_ascii=False, indent=6)}
   c. `vault_move_note` to move it (will ask for user confirmation)
3. Final summary: how many notes were processed and where each ended up

## Rules
- Drafts or very generic notes: leave them in inbox and let me know
- Only move to 10-projects if there is a clear objective
- When in doubt, ask before moving

Let's start!"""


@mcp.prompt(
    name="new_htb_writeup",
    description=(
        "Generate a HackTheBox writeup with standard structure (CVSS v3.1 + "
        "MITRE ATT&CK) and save it to the vault. Parameters: machine name, "
        "difficulty, OS."
    ),
)
async def prompt_new_htb_writeup(
    machine_name: str,
    difficulty: str = "medium",
    target_os: str = "Linux",
) -> str:
    """Parameterized prompt for creating HTB writeups."""
    return f"""Create a complete writeup for the HTB machine **{machine_name}**.

## Details
- Difficulty: {difficulty} | OS: {target_os} | Date: {datetime.now().strftime('%Y-%m-%d')}

## Writeup structure
```markdown
# {machine_name} - HTB Writeup

## Info
| Field | Value |
|-------|-------|
| OS | {target_os} |
| Difficulty | {difficulty} |
| Date | {datetime.now().strftime('%Y-%m-%d')} |

## CVSS v3.1
Vector: CVSS:3.1/AV:.../...
Score: X.X (Critical/High/Medium)

## MITRE ATT&CK
| Technique | ID | Phase |
|-----------|----|-------|

## Reconnaissance
## Exploitation
## Privilege escalation
## Flags and lessons learned
## Tools used
```

## Final instructions
Save it with `vault_create_note`:
- folder: "30-resources/hacking/htb/writeups"
- filename: "{machine_name.lower()}-writeup"
- tags: "htb,writeup,{target_os.lower()},{difficulty}"
"""


# ══════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════

@mcp.tool(
    name="vault_create_note",
    annotations=ToolAnnotations(
        title="Create a new note in the Obsidian vault",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def vault_create_note(
    folder: str,
    filename: str,
    content: str,
    ctx: Context,
    tags: str = "",
    overwrite: bool = False,
) -> str:
    """Create a new Markdown note in the Obsidian vault with auto-generated YAML frontmatter.

    Creates parent directories if they don't exist. Returns an error if the
    note already exists unless overwrite is True.

    Use this tool when the user asks to create, write, or save a new note,
    document, or markdown file in the vault. If unsure which folder to use,
    read the vault://structure resource first or default to '00-inbox'.

    Args:
        folder: Target folder relative to vault root. Common folders:
                '00-inbox' (default/unsorted), '10-projects' (active projects),
                '20-areas' (ongoing responsibilities), '30-resources/dev' (dev references),
                '30-resources/hacking/htb' (HTB writeups), '40-archive' (inactive).
                E.g. '00-inbox', '30-resources/dev'
        filename: Note name without .md extension. E.g. 'jarvis-writeup'
        content: Full Markdown content for the note body
        tags: Comma-separated tags for frontmatter. E.g. 'htb,writeup,linux'
        overwrite: If True, overwrite an existing note. Default False.
    """
    try:
        target = _note_path(folder, filename)

        if target.exists() and not overwrite:
            return json.dumps({
                "success": False,
                "error": f"Note already exists: {target.relative_to(VAULT_PATH)}. Use overwrite=True.",
            })

        target.parent.mkdir(parents=True, exist_ok=True)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        fm = _frontmatter(tag_list, {})
        full_content = fm + content
        target.write_text(full_content, encoding="utf-8")

        return json.dumps({
            "success": True,
            "path": str(target.relative_to(VAULT_PATH)),
            "bytes": len(full_content.encode("utf-8")),
            "message": f"Note created: {target.name}",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_read_note",
    annotations=ToolAnnotations(
        title="Read a note from the Obsidian vault",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_read_note(path: str) -> str:
    """Read the full content of a note from the vault, including YAML frontmatter.

    Use this when the user asks to read, open, show, or view a note. If the
    user refers to a note by name or topic rather than by path, use
    vault_search_notes or vault_list_notes first to find the exact path.

    Args:
        path: Relative path to the note with .md extension.
              E.g. '30-resources/hacking/htb/writeups/jarvis.md'
    """
    try:
        target = _resolve(path)

        if not target.exists():
            return json.dumps({"success": False, "error": f"Note not found: {path}"})

        content = target.read_text(encoding="utf-8")
        stat = target.stat()

        return json.dumps({
            "success": True,
            "path": path,
            "content": content,
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_append_note",
    annotations=ToolAnnotations(
        title="Append content to an existing note",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def vault_append_note(path: str, content: str, add_separator: bool = True) -> str:
    """Append Markdown content to the end of an existing note.

    Opens the file in append mode (does not overwrite). Inserts a '---'
    separator before the new content by default for visual clarity in Obsidian.

    Use this when the user asks to add, append, or extend content to an
    existing note. For replacing or editing specific sections within a note,
    use vault_edit_note instead.

    Args:
        path: Relative path to the note to modify.
        content: Markdown content to append.
        add_separator: If True (default), prepend a '---' horizontal rule before the content.
    """
    try:
        target = _resolve(path)

        if not target.exists():
            return json.dumps({"success": False, "error": f"Note not found: {path}"})

        separator = "\n\n---\n\n" if add_separator else "\n\n"
        addition = separator + content

        with target.open("a", encoding="utf-8") as f:
            f.write(addition)

        return json.dumps({
            "success": True,
            "path": path,
            "bytes_added": len(addition.encode("utf-8")),
            "message": "Content appended.",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_list_notes",
    annotations=ToolAnnotations(
        title="List notes in a vault folder",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_list_notes(folder: str = "", recursive: bool = False) -> str:
    """List Markdown notes in a vault folder with path, name, and modification date.

    Args:
        folder: Folder to list, relative to vault root. Empty string for the root.
                E.g. '10-projects', '30-resources/hacking'
        recursive: If True, include notes in subfolders.
    """
    try:
        base = _resolve(folder) if folder else VAULT_PATH

        if not base.exists():
            return json.dumps({"success": False, "error": f"Folder not found: {folder}"})

        files = _list_md_files(base, recursive)

        return json.dumps({
            "success": True,
            "folder": folder or "/",
            "count": len(files),
            "notes": files,
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_search_notes",
    annotations=ToolAnnotations(
        title="Full-text search across vault notes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_search_notes(
    query: str,
    folder: str = "",
    case_sensitive: bool = False,
) -> str:
    """Search for text in note contents and filenames across the vault.

    Returns matching notes with a context excerpt (60 chars before and after
    the first match) so the model can evaluate relevance without reading the
    full file.

    Args:
        query: Text to search for. E.g. 'SQLi', 'Kerberoasting', 'Atalaya'
        folder: Restrict search to this folder. Empty string searches the entire vault.
        case_sensitive: If True, match case exactly. Default False.
    """
    try:
        base = _resolve(folder) if folder else VAULT_PATH
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(re.escape(query), flags)

        results = []
        for f in sorted(base.glob("**/*.md")):
            content = f.read_text(encoding="utf-8", errors="ignore")
            matches = list(pattern.finditer(content))
            name_match = pattern.search(f.stem)

            if matches or name_match:
                excerpt = ""
                if matches:
                    start = max(0, matches[0].start() - 60)
                    end = min(len(content), matches[0].end() + 60)
                    excerpt = "..." + content[start:end].replace("\n", " ").strip() + "..."

                results.append({
                    "path": str(f.relative_to(VAULT_PATH)),
                    "name": f.stem,
                    "matches_in_content": len(matches),
                    "match_in_name": bool(name_match),
                    "excerpt": excerpt,
                })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(results),
            "results": results,
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_move_note",
    annotations=ToolAnnotations(
        title="Move or rename a note within the vault",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_move_note(source: str, destination: str, ctx: Context) -> str:
    """Move or rename a note within the vault.

    Use this when the user asks to move a note to a different folder, or to
    rename a note (set destination to the same folder with a different filename).
    When moving from 00-inbox, uses elicitation to confirm with the user.

    Args:
        source: Current path. E.g. '00-inbox/idea.md'
        destination: New path. E.g. '30-resources/futuribles/idea.md'
                     For renaming: same folder, different filename.
                     E.g. '10-projects/old-name.md' -> '10-projects/new-name.md'
    """
    try:
        src = _resolve(source)
        dst = _resolve(destination)

        if not src.exists():
            return json.dumps({"success": False, "error": f"Note not found: {source}"})

        if dst.exists():
            return json.dumps({"success": False, "error": f"A note already exists at: {destination}"})

        if "00-inbox" in source and "00-inbox" not in destination:
            dest_folder = str(Path(destination).parent)
            dest_desc = VAULT_STRUCTURE.get(dest_folder, "custom folder")

            response = await ctx.elicit(
                message=(
                    f"Move '{Path(source).name}' from inbox to:\n"
                    f"  {destination}\n({dest_desc})\n\nConfirm?"
                ),
                schema=MoveConfirmSchema,
            )

            if not response.data or not response.data.confirm:
                return json.dumps({"success": False, "error": "Move cancelled by user."})

            alt = (response.data.alternative_destination or "").strip()
            if alt:
                dst = _resolve(alt)
                destination = alt

        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

        return json.dumps({
            "success": True,
            "from": source,
            "to": str(dst.relative_to(VAULT_PATH)),
            "message": "Note moved.",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_update_memory",
    annotations=ToolAnnotations(
        title="Update the persistent memory file",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_update_memory(content: str, ctx: Context) -> str:
    """Overwrite the persistent memory file (_memoria_claude.md) in the vault.

    This file is read at the start of each session via the vault://memory
    resource, providing context continuity across conversations. Uses
    elicitation to confirm before overwriting existing memory.

    Args:
        content: Full Markdown content for the memory file.
    """
    try:
        memory_path = VAULT_PATH / MEMORY_FILE

        if memory_path.exists():
            size = memory_path.stat().st_size
            modified = datetime.fromtimestamp(
                memory_path.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M")

            response = await ctx.elicit(
                message=f"Memory file already exists ({size} bytes, last modified {modified}). Overwrite?",
                schema=ConfirmSchema,
            )

            if not response.data or not response.data.confirm:
                return json.dumps({"success": False, "error": "Update cancelled by user."})

        header = f"<!-- Memory | Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n\n"
        memory_path.write_text(header + content, encoding="utf-8")

        return json.dumps({
            "success": True,
            "path": MEMORY_FILE,
            "bytes": len(content.encode("utf-8")),
            "message": "Memory updated. It will be read at the start of the next session via vault://memory.",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_edit_note",
    annotations=ToolAnnotations(
        title="Edit a note by replacing text within it",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_edit_note(path: str, old_text: str, new_text: str) -> str:
    """Replace a specific text fragment inside an existing note.

    Performs an exact string replacement within the note content. Only the
    first occurrence is replaced. Use this when the user asks to edit, update,
    fix, correct, or change part of an existing note. For replacing all
    content, use vault_create_note with overwrite=True instead.

    Read the note with vault_read_note first to find the exact text to replace.

    Args:
        path: Relative path to the note. E.g. '10-projects/my-project.md'
        old_text: Exact text to find and replace (must match content in the file).
        new_text: Replacement text.
    """
    try:
        target = _resolve(path)

        if not target.exists():
            return json.dumps({"success": False, "error": f"Note not found: {path}"})

        content = target.read_text(encoding="utf-8")

        if old_text not in content:
            return json.dumps({
                "success": False,
                "error": "old_text not found in the note. Read the note first with vault_read_note to get the exact text.",
            })

        updated = content.replace(old_text, new_text, 1)
        target.write_text(updated, encoding="utf-8")

        return json.dumps({
            "success": True,
            "path": path,
            "bytes": len(updated.encode("utf-8")),
            "message": "Note edited.",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool(
    name="vault_delete_note",
    annotations=ToolAnnotations(
        title="Delete a note from the vault",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def vault_delete_note(path: str, ctx: Context) -> str:
    """Permanently delete a note from the vault.

    Use this when the user asks to delete, remove, or trash a note.
    Uses elicitation to confirm with the user before deleting, since
    this action is irreversible.

    Args:
        path: Relative path to the note to delete. E.g. '00-inbox/old-note.md'
    """
    try:
        target = _resolve(path)

        if not target.exists():
            return json.dumps({"success": False, "error": f"Note not found: {path}"})

        size = target.stat().st_size
        response = await ctx.elicit(
            message=f"Permanently delete '{path}' ({size} bytes)? This cannot be undone.",
            schema=ConfirmSchema,
        )

        if not response.data or not response.data.confirm:
            return json.dumps({"success": False, "error": "Deletion cancelled by user."})

        target.unlink()

        return json.dumps({
            "success": True,
            "path": path,
            "message": "Note deleted.",
        })

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ══════════════════════════════════════════════════════════════════
# MARKMAP TOOLS
# ══════════════════════════════════════════════════════════════════

@mcp.tool(
    name="listar_markmaps",
    annotations=ToolAnnotations(
        title="Listar notas Markmap del vault",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def listar_markmaps() -> str:
    """
    Busca y devuelve todas las notas del vault que contienen
    un bloque ```markmap```. Útil para descubrir qué mapas
    mentales hay disponibles antes de visualizarlos.

    Returns:
        JSON con lista de dicts con 'path', 'title' y 'modified'.
    """
    results = []
    for md_file in VAULT_PATH.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            if "```markmap" in text:
                results.append({
                    "path": str(md_file.relative_to(VAULT_PATH)),
                    "title": md_file.stem.replace("-", " ").title(),
                    "modified": md_file.stat().st_mtime,
                })
        except Exception:
            continue
    return json.dumps(sorted(results, key=lambda x: x["modified"], reverse=True))


@mcp.tool(
    name="visualizar_markmap",
    annotations=ToolAnnotations(
        title="Visualizar Markmap en el navegador",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def visualizar_markmap(ruta_nota: str) -> str:
    """
    Extrae el bloque ```markmap``` de una nota de Obsidian,
    genera un HTML interactivo con markmap.js y lo abre
    automáticamente en el navegador por defecto.

    Args:
        ruta_nota: Ruta relativa al vault raíz.
                   Ej: '50-cursos/mcp-mar-26/mcp-mapa-mental.md'

    Returns:
        JSON con 'status', 'title' y 'html_path' del archivo temporal.
    """
    nota = VAULT_PATH / ruta_nota
    if not nota.exists():
        raise FileNotFoundError(
            f"Nota no encontrada: {ruta_nota}\n"
            f"Usa listar_markmaps() para ver las disponibles."
        )

    content = nota.read_text(encoding="utf-8")
    markmap_block = _extraer_bloque_markmap(content)
    if not markmap_block:
        raise ValueError(
            f"La nota '{ruta_nota}' no contiene un bloque ```markmap```.\n"
            f"Añade un bloque así:\n```markmap\n# Tu mapa\n## Rama\n```"
        )

    title = nota.stem.replace("-", " ").title()
    html  = _generar_html(title, markmap_block)

    # Guardar en archivo temporal y abrir en navegador
    tmp = tempfile.NamedTemporaryFile(
        suffix=".html",
        delete=False,
        mode="w",
        encoding="utf-8",
        prefix=f"markmap_{nota.stem}_"
    )
    tmp.write(html)
    tmp.close()

    # webbrowser puede imprimir a stdout, lo cual corrompe el
    # protocolo JSON-RPC de MCP (stdio). Redirigimos stdout/stderr
    # temporalmente a /dev/null durante la apertura.
    devnull = open(os.devnull, "w")
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = devnull, devnull
        webbrowser.open(f"file://{tmp.name}")
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        devnull.close()

    return json.dumps({
        "status":    "ok",
        "title":     title,
        "html_path": tmp.name,
        "message":   f"Mapa mental '{title}' abierto en el navegador.",
    })


# ── Markmap helpers privados ─────────────────────────────────

def _extraer_bloque_markmap(content: str) -> str | None:
    """Extrae el contenido entre ```markmap y ``` ."""
    match = re.search(r"```markmap\n(.*?)```", content, re.DOTALL)
    if not match:
        return None
    # Eliminar el frontmatter YAML del bloque si lo tiene (--- ... ---)
    block = match.group(1).strip()
    block = re.sub(r"^---.*?---\s*", "", block, flags=re.DOTALL).strip()
    return block


def _generar_html(title: str, markmap_md: str) -> str:
    """Genera HTML standalone con markmap-autoloader desde CDN."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ height: 100%; overflow: hidden; }}
    body {{
      background: #fff;
      font-family: 'Courier New', monospace;
      display: flex;
      flex-direction: column;
    }}
    header {{
      flex-shrink: 0;
      padding: 10px 20px;
      background: #1e293b;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    header h1 {{
      color: #0f172a;
      font-size: 14px;
      font-weight: bold;
      letter-spacing: 0.08em;
      background: #22d3ee;
      padding: 2px 10px;
      border-radius: 3px;
    }}
    header .badge {{
      font-size: 9px;
      color: #64748b;
      padding: 2px 8px;
      border: 1px solid #cbd5e1;
      border-radius: 3px;
      background: #f1f5f9;
    }}
    header .path {{
      margin-left: auto;
      font-size: 9px;
      color: #94a3b8;
    }}
    /* El div.markmap debe tener altura explícita o el SVG queda 0×0 */
    .markmap {{
      flex: 1;
      width: 100%;
      height: calc(100vh - 41px);
    }}
    .markmap svg {{ background: #ffffff; }}
    .markmap-node text  {{ fill: #1e293b !important; }}
    .markmap-link       {{ stroke-opacity: 0.6 !important; }}
    .markmap-node circle {{ stroke-width: 1.5px; }}
  </style>
</head>
<body>
  <header>
    <h1>⬡ {title}</h1>
    <span class="badge">obsidian-mcp · markmap</span>
    <span class="path">Scroll = zoom · Drag = mover · Click nodo = colapsar</span>
  </header>

  <!-- El autoloader detecta class="markmap" y renderiza el markdown inline -->
  <div class="markmap">
{markmap_md}
  </div>

  <script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.17"></script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main():
    """Entry point. Starts the MCP server with stdio transport."""
    print("Obsidian MCP Server (FastMCP 3.x)", file=sys.stderr)
    print(f"Vault: {VAULT_PATH}", file=sys.stderr)
    print("10 tools | 2 resources | 2 prompts", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
