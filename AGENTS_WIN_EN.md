# AGENTS.md — General Agent Operating Conventions (Windows / English)

This file is the **master ruleset**: every run must follow it.
You may freely edit it to append domain-specific conventions.
At startup, use the tools to obtain the time and environment, so you understand the current time and where you are.
**Important:** If the user's opening message has no clear request, you MUST call the `ask_user` tool to reply and ask for a detailed requirement.

**Important:** If the user only greets you or has no concrete question, use `ask_user` to politely respond and invite a specific task.

## Run directory and working directory

### This run's directory (`$RUN_DIR`)
- The dedicated directory for this run is the environment variable `RUN_DIR` (format: `runs/YYYYMMDD-HHMMSS`).
- **Temporary files, fetched web pages, intermediate artifacts, debug output** — all go into `$RUN_DIR/artifacts/`.
- Run logs, scratchpad, final_answer, etc. are written into `RUN_DIR` automatically; no manual handling required.

### Long-lived workspace
Some tasks produce outputs that need to persist across multiple runs — e.g. developing a code project, maintaining a long-lived doc, building a data pipeline. Such tasks should NOT write into `runs/`; use a fixed workspace directory and manage versions via git.

**Decision rule:**

```
Does the task output need to be accessed or modified in future runs?
├── Clearly yes (long-running project / iterative development)
│   └── Ask the user whether they already have a workspace dir, or have them specify a path
│       → Write outputs there; git commit after each meaningful stage
├── Clearly no (one-off analysis / temporary output)
│   └── Write to $RUN_DIR/artifacts/ as usual
└── Unclear (cannot tell from the goal text)
    └── Use ask_user: "Does this task's output need long-term maintenance?
        If yes, tell me the workspace path; if no, I'll write to this run's dir."
```

**Conventions for long-lived workspaces:**
- Write files into the user-specified workspace path (absolute or user-provided relative); not constrained to `runs/`.
- After each meaningful stage, run `git add` + `git commit` with a message describing what was done.
- `RUN_DIR` still hosts this run's execution log and scratchpad — the two responsibilities don't overlap.

#### Work log (WORKLOG.md) — required for long-lived projects

Long-lived projects MUST keep a work-log file `WORKLOG.md` in the **root of the workspace**, recording each
meaningful stage of work, so the next run (which may have lost all context) can quickly recall "where things
were left off and why it was done that way."

**Format:**
- Every entry has three parts: **date**, **item title**, **detailed content**.
- New entries are always **appended to the end**; never edit or delete existing history (this is an append-only journal, not an editable doc).
- Each entry uses this format (one title line + a content paragraph):

  ```
  ## YYYY-MM-DD | <item title>

  <details: what was done this time, why, problems hit and conclusions, open items / next steps…>
  ```

**Conventions:**
- On first entering a workspace, if `WORKLOG.md` doesn't exist, create it (an opening `# Work Log` line is fine), then append the first entry.
- After each meaningful stage: **append a log entry first, then `git add` + `git commit`** (log and code go in the same commit).
- Use the real current date — already confirmed via the time/environment tool at startup; use it directly, don't guess.
- Write substantively: capture key decisions and pitfalls, not just "finished X." This log is written for a future self who has forgotten everything.

### File-write conventions
- When using `write_file(path, content)`:
  - **Temp/intermediate artifacts**: `path` MUST start with `runs/` or use `$RUN_DIR`.
  - **Long-lived workspace outputs**: use the user-specified workspace path, which may be anywhere outside `runs/`.
  - Large files (HTML/JSON/XML) go into `artifacts/` with semantic filenames (e.g. `search_results.html`).

---

## Operating system: Windows (CMD + PowerShell environment)

**This environment is Windows, NOT Linux/Mac.** Before running commands, make sure you use Windows commands.

### Common Unix → Windows alternatives

| Unix command | Windows / PowerShell alternative |
|--------------|----------------------------------|
| `head -N file` | `powershell -Command "Get-Content 'file' -TotalCount N"` |
| `tail -N file` | `powershell -Command "Get-Content 'file' \| Select-Object -Last N"` |
| `grep pattern file` | `findstr "pattern" file` or `powershell -Command "Select-String ..."` |
| `cat file` | `type file` (cmd) or the `read_file` tool (preferred) |
| `ls` | `dir /b` |
| `curl url` | `powershell -Command "Invoke-WebRequest -Uri 'url'"` |
| `export VAR=val` | `set VAR=val` (cmd) or `$env:VAR='val'` (PowerShell) |
| `which cmd` | `where cmd` |

---

## `run_python` tool usage

The `run_python` tool uses the **current framework runtime's Python interpreter** (auto-detected); use it directly, no need to worry about locating an interpreter.

If you need to invoke Python manually via shell:
```
shell(command='python -c "print(1+1)"')
```
Do NOT use `python3 -c` (it may not be available on Windows).

---

## CLI commands first

### Core principle
**If a single command can be run directly via CLI, prefer CLI — don't wrap it into a tool.**

### Decision tree

```
Need to run a system command?
├── Simple (few args, one-off use)
│   └── Use the shell tool directly (remember to use Windows commands)
├── Complex (multi-step, needs validation, used often)
│   └── Wrap into a tool
└── Needs deep agent integration (arg validation, result parsing)
    └── Wrap into a tool
```

---

## Large-file / full-disk search conventions

### No root-wide recursive scans

```
# The following scans the whole drive and will time out — forbidden!
dir /s /b C:\*progname*
```

### Correct search strategy (in priority order)

1. **Check PATH first**: `where <prog>`
2. **Check standard install dirs**: `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%LocalAppData%`
3. **Check the registry**: `reg query "HKLM\SOFTWARE" /s /f "<prog>"`
4. **Bounded recursive search**: `where /R "C:\Program Files" <prog>.exe` (limited to known dirs)

---

## Before installing software

1. Avoid duplicate installs: check first whether it is already installed.
2. When possible, ask the user and obtain consent before installing.
3. When installing software from a Git repo, you MUST read that repo's README.md first as the primary install reference.

---

## Tool usage conventions (important)

### 1. Prefer `register_tool` for new tools; never modify framework source

When extending capabilities, you MUST register new tools at runtime via `register_tool`, NOT by editing `agent/tools/standard.py` or other framework source files.

```
Need a new tool?
├── Can be composed from existing tools  →  use those; don't make a new one
├── Genuinely new capability             →  call register_tool (stored in the user tool set)
└── Forbidden                            →  editing agent/tools/standard.py, agent/core/*.py, etc.
```

**Why:** Editing source pollutes git history, and the change persists for all future instances, making the impact hard to trace. Tools registered via `register_tool` live in a separate JSON file — reviewable and revertible.

### 2. `register_tool` scope

- Tool code may `import` any installed third-party library; the `exec()` environment is the same as normal Python.
- If a library isn't installed, install it via the `shell` tool first, THEN register the tool — don't bake library code into source files.
- Registered tools take effect immediately in this run's `state.tools`; next startup, `load_tools` restores them from JSON.

### 3. Files that must NOT be modified

The following files are framework core; **must not be modified during task execution** unless the user explicitly asks:

- `agent/tools/standard.py`
- `agent/core/llm.py`
- `agent/core/compression.py`
- `agent/core/types.py`
- `run_goal.py`
- `AGENTS.md` (and all `AGENTS_*.md` variants)

If a change to the above is genuinely needed, first explain via `ask_user` and obtain explicit user consent.

---

## Loop-detection rules (important)

### Warning signs
- Same tool + nearly identical args called 3+ times in a row
- Identical error message but you keep retrying

### Mandatory handling

```
If a tool/command fails 3 times in a row (same tool + similar args):
  → MUST stop and switch to a completely different strategy

If 5+ distinct approaches have all failed:
  → choose ask_user (report the blocker and ask for guidance) or done (report current state)
```

---

## Long-running operations and legitimate polling (important)

### Loop vs. polling — the essential difference

| | Dead loop | Legitimate polling |
|---|---|---|
| Per-call result | Identical, no progress | Changing (progress moving) |
| Strategy meaningful? | More retries gain no info | Waiting is necessary to finish |
| Typical scenario | Command keeps erroring; search yields nothing | Download progress, build status, service-startup check |

**The framework already exempts calls whose `thought` contains keywords like "wait / poll / progress" from loop warnings.**
Spell out polling intent in `thought` (e.g. "waiting for download to finish", "checking progress") so it isn't misclassified as a dead loop.

---

### Recommended workflows for long-running operations

#### 1. Event-driven (recommended, no polling)

At the start of each iteration the framework auto-checks background jobs; once complete, it injects the result into context.
**The agent doesn't need to poll**, just:

```
step 1: shell_bg("curl -L <url> -o output.zip", timeout=600) → job_id
step 2: [do other work if any; otherwise call wait_for_job]
step 3: framework auto-notifies: "[system] background job_xxx finished, exit 0, output: ..."
step 4: handle result
```

**When you have other work to do (best):**
```
shell_bg("curl -L <url> -o output.zip") → job_abc
↓ continue with other work (read docs, write code, etc.)
↓ framework injects completion notice
↓ agent sees it and proceeds
```

**When there's nothing else to do (pure wait):**
```
shell_bg("curl -L <url> -o output.zip") → job_abc
wait_for_job(job_id="job_abc", check_interval=30)
↓ framework waits silently — no LLM calls, no iteration budget consumed
↓ on completion, auto-resume and inject result
```

#### 2. Don't poll via repeated `job_wait`

`job_wait` is for a **one-shot status query**, not for loops.
Repeatedly calling `job_wait` on the same `job_id` is a polling loop and trips loop detection.

```
✅ Correct: shell_bg → other work → framework notifies
✅ Correct: shell_bg → wait_for_job (pure-wait scenario)
✅ Correct: shell_bg → (iter N) job_wait once for status → (do not repeat)
❌ Wrong:   repeated job_wait(same_job_id) polling
```

#### 3. When progress freezes, diagnose actively

If 3 `job_wait` calls in a row return **identical output** (progress frozen):

```
→ Stop polling
→ Investigate: is the process still alive? Network broken? Disk full?
→ Find the root cause, THEN decide: retry / change approach / ask_user
```

#### 4. Update the scratchpad after each meaningful progress change

```
scratchpad_append: started download job_abc, ~2GB, ETA ~10 min
```

Even if context compaction triggers, key progress info isn't lost.

#### 5. Extra tips for large downloads

- Prefer tools that support resume (`curl -C -`, `wget -c`)
- Check free space on the target disk first
- For files >500MB, verify checksum after download (MD5/SHA256)

---

## Scratchpad
- The scratchpad is for "intermediate notes and analysis during execution" — NOT the final answer.
- Multi-step tasks must maintain the scratchpad:
  - Before starting: `scratchpad_set` with the plan / breakdown
  - After each significant tool result: `scratchpad_append` with key findings / next step

---

## Risk control
- Any `args` likely to produce huge output / long strings (especially `run_python.code`) should be split into steps to avoid truncated JSON and parse failures.

---

## JSON output conventions (important!)

### 1. The `action` field must be `tool_call` or `done`

**Wrong:**
```json
{action: submit_completion_report, ...}
```

**Right:**
```json
{action: tool_call, tool: submit_completion_report, args: {...}}
```

All tool invocations must use `action: tool_call`; put the tool name in the `tool` field.

### 2. No unescaped newlines inside strings

**Wrong:**
```json
{final_answer: line 1
line 2}
```

**Right:**
```json
{final_answer: line 1\nline 2}
```

All multi-line text must use `\n`, never a raw newline.

### 3. Write very long content to a file

If `args.command` or `args.content` contains very long content (e.g. base64, code scripts),
do NOT wrap mid-string — write the content to a temp file via `write_file` first,
then reference that path in the command (e.g. `python /tmp/script.py`), which fully avoids these issues.
