---
name: memory
description: Search past conversations in the agent's history log.
---

# Memory

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` — User profile and preferences. **Managed by Dream.** Do NOT edit.
- `SOUL.md`/`USER.md` 中的 `<!-- persona:managed -->` 与 `<!-- persona:user -->` 标记段由 persona 技能管理，Dream 不覆盖。
- `memory/MEMORY.md` — Long-term facts (project context, important events). **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` — append-only JSONL, not loaded into context. Prefer the
  built-in `grep` tool to search it.

## Search Past Events

Search the exact `History log` path from the system prompt with `grep`; a project-relative
`memory/history.jsonl` may belong to a different workspace. The log is append-only JSONL,
with `cursor`, `timestamp`, and `content` per entry, and is not loaded into context.

Start broad searches with `output_mode="count"`, then narrow by topic or date and request
matching content. Use `fixed_strings=true` for literal timestamps or JSON fragments.
Page long results with `head_limit` / `offset` and use `context_before` / `context_after`
when nearby entries matter.

Example (replace `<history-log-path>` with the path from the system prompt):
`grep(pattern="project-name", path="<history-log-path>", output_mode="content", case_insensitive=true, head_limit=20)`

## Important

- **Do NOT edit SOUL.md, USER.md, or MEMORY.md**, except persona may replace its own managed marker blocks after user confirmation.
- If you notice outdated information, it will be corrected when Dream runs next.
- Users can view Dream's activity with the `/dream-log` command.
