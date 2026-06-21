# AGENTS.md — General Agent Operating Conventions (Linux / English)

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

### File-write conventions
- When using `write_file(path, content)`:
  - **Temp/intermediate artifacts**: `path` MUST start with `runs/` or use `$RUN_DIR`.
  - **Long-lived workspace outputs**: use the user-specified workspace path, which may be anywhere outside `runs/`.
  - Large files (HTML/JSON/XML) go into `artifacts/` with semantic filenames (e.g. `search_results.html`).

---

## Operating system: Linux / Unix (bash environment)

**This environment is Linux/Unix; POSIX shell applies.** Before running commands, verify the target system has the required tools (most distros ship coreutils, grep, curl, etc.).

### Common command cheat sheet

| Purpose | Command |
|---------|---------|
| Show first N lines | `head -n N file` |
| Show last N lines | `tail -n N file` |
| Follow file output | `tail -f file` |
| Text search | `grep -n 'pattern' file` or `rg 'pattern'` |
| Whole file content | `cat file` or use the `read_file` tool (preferred) |
| List directory | `ls -la` |
| Download URL | `curl -L -o output 'url'` or `wget 'url'` |
| Set env var inline | `VAR=val command` or `export VAR=val` |
| Locate executable | `which cmd` or `command -v cmd` |
| Find files | `find /path -name 'pattern' -maxdepth N` |
| List processes | `ps aux \| grep keyword` |
| Check port usage | `ss -tlnp` or `lsof -i :PORT` |

### Path and shell notes
- Path separator is `/`; never mix in `\`.
- Quote paths containing spaces: `"path with space/file"`.
- Command chaining: `&&` (run next only if previous succeeded), `||` (fallback), `;` (unconditional sequence).

---

## `run_python` tool usage

The `run_python` tool uses the **current framework runtime's Python interpreter** (auto-detected); use it directly, no need to worry about locating an interpreter.

If you need to invoke Python manually via shell:
```
shell(command='python3 -c "print(1+1)"')
```
Most Linux distros expose `python3`, not `python`.

---

## CLI commands first

### Core principle
**If a single command can be run directly via CLI, prefer CLI — don't wrap it into a tool.**

### Decision tree

```
Need to run a system command?
├── Simple (few args, one-off use)
│   └── Use the shell tool directly
├── Complex (multi-step, needs validation, used often)
│   └── Wrap into a tool
└── Needs deep agent integration (arg validation, result parsing)
    └── Wrap into a tool
```

---

## Large-file / full-disk search conventions

### No root-wide recursive scans

```
# These scan the entire root and can take a very long time — forbidden!
find / -name 'keyword'
grep -r 'keyword' /
```

### Correct search strategy (in priority order)

1. **Check PATH first**: `which <prog>` or `command -v <prog>`
2. **Check standard install dirs**: `/usr/bin`, `/usr/local/bin`, `/opt`, `$HOME/.local/bin`
3. **Use package manager**: `dpkg -L <pkg>` (Debian/Ubuntu) / `rpm -ql <pkg>` (RHEL/Fedora) / `pacman -Ql <pkg>` (Arch)
4. **Bounded recursive find**: `find /opt /usr/local -name '<prog>' -maxdepth 4` (limited to known dirs)

---

## Before installing software

1. Avoid duplicate installs: check first via `which`, `apt list --installed`, `dpkg -l` / `rpm -qa`.
2. When in doubt, ask the user before installing.
3. System-wide installs usually require `sudo`; prefer user-level options (pipx, `--user`, conda env, Docker) to avoid polluting the system.
4. When installing software from a Git repo, you MUST read that repo's README.md first as the primary install reference.

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
step 1: shell_bg("curl -L <url> -o output.tar.gz", timeout=600) → job_id
step 2: [do other work if any; otherwise call wait_for_job]
step 3: framework auto-notifies: "[system] background job_xxx finished, exit 0, output: ..."
step 4: handle result
```

**When you have other work to do (best):**
```
shell_bg("curl -L <url> -o output.tar.gz") → job_abc
↓ continue with other work (read docs, write code, etc.)
↓ framework injects completion notice
↓ agent sees it and proceeds
```

**When there's nothing else to do (pure wait):**
```
shell_bg("curl -L <url> -o output.tar.gz") → job_abc
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
→ Investigate: is the process still alive (ps -p <pid>)? Network broken? Disk full (df -h)?
→ Find the root cause, THEN decide: retry / change approach / ask_user
```

#### 4. Update the scratchpad after each meaningful progress change

```
scratchpad_append: started download job_abc, ~2GB, ETA ~10 min
```

Even if context compaction triggers, key progress info isn't lost.

#### 5. Extra tips for large downloads

- Prefer tools that support resume (`curl -C -`, `wget -c`, `aria2c`)
- Check free space first: `df -h <target dir>`
- For files >500MB, verify checksum after download (`sha256sum file` or `md5sum file`)

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
then reference that path in the command (e.g. `python3 /tmp/script.py`), which fully avoids these issues.
