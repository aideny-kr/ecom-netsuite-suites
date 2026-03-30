# Active Task

> What Claude Code is currently working on. Only ONE task at a time.
> When complete, move to `completed/` and pick next from `queue.md`.

---

## Status: IDLE

No active task. Pick the top item from `queue.md` to begin.

---

## Template (replace above when starting a task)

```markdown
## Status: IN PROGRESS

### Task: [Title from queue.md]
- **Started**: YYYY-MM-DD HH:MM
- **Spec**: path/to/spec.md
- **Wave**: N of M (if multi-wave)
- **Progress notes**:
  - [ ] Step 1
  - [ ] Step 2
  - [ ] Step 3

### Blockers
None / describe blockers

### Last Update
[timestamp] — [what just happened]
```

---

## Completion Protocol

When finishing a task:

1. Run full test suite — record pass/fail count
2. Update CLAUDE.md (Resolved section, migration numbers, current state)
3. Move this task to `completed/YYYY-MM-DD-task-name.md` with results
4. Reset this file to `Status: IDLE`
5. Check `review/` for any pending review feedback to address
6. Pick next item from `queue.md`
