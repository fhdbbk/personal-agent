# Design docs

Architectural views of the personal assistant. Two documents, different altitudes:

- **[HLD.md](HLD.md)** — High-Level Design. Bird's-eye view of the system: vision, components, tech stack, data flow, deployment, and how the architecture evolves through the seven phases. Read this first.
- **[LLD.md](LLD.md)** — Low-Level Design. Module-by-module reference with frame protocols, sequence diagrams, class diagrams, state machines, and `file:line` anchors into the code. Read this when you're about to change something.

All diagrams are [Mermaid](https://mermaid.js.org/) fenced blocks. They render natively on GitHub. In VS Code, install the [Markdown Preview Mermaid Support](https://marketplace.visualstudio.com/items?itemName=bierner.markdown-mermaid) extension (`code --install-extension bierner.markdown-mermaid`) — VS Code's built-in preview doesn't render Mermaid out of the box.

## How this fits with the other docs

| Folder | Question it answers | Mutability |
|---|---|---|
| [decisions/](../decisions/) | *Why* did we make this choice? | Immutable; superseded by new ADRs |
| [design/](.) (this folder) | *What* is the system? | **Living** — kept in sync with the code |
| [learnings/](../learnings/) | *What* did we discover? | Append-only, topic-organised |
| [sessions/](../sessions/) | *When* did things happen? | Append-only, dated |

ADRs and design docs cross-reference each other: design docs explain the **shape**, ADRs explain the **rationale**. If they ever disagree, the ADR wins for *why* and the code wins for *what* — these docs are an interpretation layer that should be updated to match.

## Maintenance

These are living documents. The expectation:

- After a session that **adds or removes a component**, refresh the relevant section of HLD and LLD before closing the session log.
- After a session that **only edits within an existing component**, the docs usually still hold; spot-check the file:line anchors if the edits were structural.
- When a phase completes, flip its **Planned → Done** badge in HLD's "Phased evolution" section.

If a design doc starts disagreeing with the code, treat the doc as the bug, not the code.
