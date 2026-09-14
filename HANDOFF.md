# skillbench — implementation handoff

A runnable proof of concept for the talk *Stop Trusting Markdown: Testing Agent
Skills Across Harnesses with Omnigent*.

One bundled Markdown skill runs against two coding-agent harnesses — Claude and
Codex — through Omnigent's inner executors, with the same logical tool, the same
six prompts, and the same deterministic assertions on both sides. Every trial
writes a self-contained JSON trace that can be regraded offline.

**What the results mean.** They show observed conformance to the skill's rules
on these six tasks, per harness. There is no no-skill control, so nothing here
establishes that the skill *improves* anything — only whether its instructions
survive the trip across harnesses.

---

## 1. Authored files

| File | What it is |
| --- | --- |
| `pyproject.toml` | uv project, git-pinned Omnigent sources, `skillbench` entrypoint |
| `src/skillbench/__init__.py` | the whole runner: cases, tool, executor wiring, grader, CLI |
| `skills/order-status/SKILL.md` | the skill under test |
| `uv.lock` | generated, committed |

`artifacts/` is generated output and is gitignored.

---

## 2. Setup on your laptop

### 2.1 Prerequisites

```bash
# uv (if you don't have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# The two vendor CLIs. Both are required: Omnigent's executors drive the real
# binaries, they do not talk to the provider APIs directly.
npm install -g @anthropic-ai/claude-code   # `claude`
npm install -g @openai/codex               # `codex`

claude --version && codex --version
```

Python 3.12 or 3.13 is required (Omnigent needs `>=3.12`; `pyproject.toml` caps
at `<3.14`). uv will fetch a suitable interpreter for you.

### 2.2 Install

```bash
git clone <this repo> && cd skillbench
uv sync
```

This resolves Omnigent at the pinned commit
`322746fd2b00065627a41e4137fd0bdf55e2410a`. Note that `pyproject.toml` declares
`omnigent-client` and `omnigent-ui-sdk` as direct dependencies with their own
git-subdirectory sources. That is deliberate: Omnigent version-locks them to
`0.14.0.dev0`, which is not on PyPI, and uv only applies `[tool.uv.sources]` to
a project's *own* dependencies. Without those two lines `uv sync` fails with
`omnigent-client==0.14.0.dev0 not found in registry`.

### 2.3 Authentication

The two harnesses authenticate very differently. Both paths were read out of the
pinned executor source; neither could be exercised end to end from the build
environment for Codex (see §5).

**Claude** — `ClaudeSDKExecutor` spawns the `claude` CLI through the Claude Agent
SDK and, at spawn time, **strips `ANTHROPIC_API_KEY` from the child
environment** so the CLI uses subscription auth rather than a developer key that
would bill separately (`claude_sdk_executor.py`, `_unset_env_var`). So:

```bash
claude login          # Pro/Max/Team subscription or Console OAuth — the normal path
claude /status        # confirm it reports logged in
```

If you must use an API key instead, hand it to the CLI through the settings
`apiKeyHelper` command, which is the only channel the executor leaves open:

```bash
export SKILLBENCH_CLAUDE_API_KEY_HELPER='printf %s "$ANTHROPIC_API_KEY"'
export ANTHROPIC_API_KEY=sk-ant-...
```

skillbench forwards that string to `ClaudeSDKExecutor(api_key_helper=...)`. The
helper command is never written into an artifact.

**Codex** — `CodexExecutor` runs `codex app-server` in a **private per-session
`CODEX_HOME`** and bridges your real one into it by symlinking `auth.json` (and
copying `config.toml`). It also explicitly strips `OPENAI_API_KEY` from the
subprocess environment (`_CODEX_ENV_DENY_EXACT`), for the same billing reason.
So an API key in your shell is **not** a substitute:

```bash
codex login           # ChatGPT subscription login — writes ~/.codex/auth.json
ls ~/.codex/auth.json # must exist
```

skillbench preflights this and refuses to start with an actionable message,
because an unauthenticated Codex app-server accepts the turn and then simply
never answers — you would otherwise see 36 timeouts instead of an auth error.

### 2.4 Pick your models

There are no default model names — you choose what your account actually serves.

```bash
claude --help | grep -i model     # or check https://docs.claude.com
codex --help | grep -i model
```

Use the exact ids, e.g. `--claude-model claude-sonnet-4-5`,
`--codex-model gpt-5.1-codex`.

---

## 3. Running it

```bash
# always from the repository root — the bundled skill is resolved relative to cwd
cd skillbench

# 1. No credentials needed. Deterministic grader checks.
uv run skillbench --self-test

# 2. One case per harness, to prove the wiring before spending 36 turns.
uv run skillbench --harness claude --claude-model YOUR_CLAUDE_MODEL --repeat 1
uv run skillbench --harness codex  --codex-model  YOUR_CODEX_MODEL  --repeat 1

# 3. The full default matrix: 2 harnesses x 6 cases x 3 repetitions = 36 trials.
uv run skillbench \
  --claude-model YOUR_CLAUDE_MODEL \
  --codex-model  YOUR_CODEX_MODEL \
  --repeat 3

# 4. The same matrix without the "Use the order-status skill." prefix.
#    Scored separately — never pool the two.
uv run skillbench \
  --claude-model YOUR_CLAUDE_MODEL \
  --codex-model  YOUR_CODEX_MODEL \
  --repeat 3 --discovery

# 5. Regrade a saved run offline. No model calls, no credentials.
uv run skillbench --grade artifacts/<run-dir>
```

Trials run sequentially with a fresh executor, a fresh session and a fresh
temporary workspace each time; no conversational state is ever reused. Budget
roughly 10–20 s per Claude trial, so ~10 min for a 36-trial matrix, more if
Codex is slower on your account.

### Flags

| Flag | Behaviour |
| --- | --- |
| `--harness {both,codex,claude}` | default `both` |
| `--codex-model NAME` | required when running Codex |
| `--claude-model NAME` | required when running Claude |
| `--repeat N` | positive integer, default 3 |
| `--timeout SECONDS` | positive, default 120 |
| `--artifacts-dir PATH` | default `artifacts` |
| `--discovery` | omit the explicit invocation prefix, change nothing else |
| `--grade RUN_DIR` | offline regrade, no model calls |
| `--self-test` | synthetic grader checks, no model calls |

Run, grade and self-test are mutually exclusive. Grade and self-test need no
model flags.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | every attempted trial passed |
| 1 | eval failures (the model got something wrong — a valid result) |
| 2 | invalid configuration, or an integration/execution error or timeout |
| 130 | Ctrl-C |

Errors take precedence over eval failures. Ctrl-C saves the in-flight trial with
`status: "interrupted"` and its partial evidence, writes the summary, and stops
launching further trials.

### Useful environment variables

| Variable | Effect |
| --- | --- |
| `SKILLBENCH_CLAUDE_API_KEY_HELPER` | `apiKeyHelper` command for API-key Claude auth (§2.3) |
| `OMNIGENT_CLAUDE_PATH` / `OMNIGENT_CODEX_PATH` | point at a CLI not on `PATH` (nvm, etc.) |
| `CODEX_HOME` | the home whose `auth.json` / `config.toml` Codex bridges in |
| `HARNESS_CODEX_MINIMAL_CONFIG=1` | drop your `AGENTS.md` / `hooks.json` from the private Codex home and trim `config.toml` to provider tables — tighter isolation, read by Omnigent directly |

---

## 4. What the artifacts contain

One run directory per invocation, `artifacts/<UTC-stamp>-<suffix>/`, holding
`<harness>__<case>__<rep>.json` per trial plus `summary.json` (and `regrade.json`
after `--grade`).

Each trial JSON carries the run/trial ids, exact requested model and the model
the harness reported, the Omnigent commit and package versions, the skill path
and SHA-256, the skills filter and loading mechanism, the effective prompt, the
expected lookup ids and response (so regrading is self-contained), UTC
start/end plus monotonic elapsed seconds, the status, the ordered normalized
event stream, the authoritative tool executions, the assistant text split into
segments, the final response and how it was derived, reasoning availability,
usage as reported (never zero-filled), and the grade.

Credentials, process environments and SDK objects are never serialized;
unknown objects are reduced to a short `repr`, and strings are truncated.

### The grader

A trial passes only when all of these hold:

1. execution completed normally, with no tool-callback error;
2. the `lookup_order` execution count equals the expected count, including zero;
3. each call carries exactly `{"order_id": "..."}` with the expected value, in
   order, types and leading zeros preserved;
4. each tool result matches the deterministic database response;
5. no lookup request event went unexecuted;
6. `final_response.strip()` equals the expected string exactly — case,
   punctuation, inner whitespace, extra prose and Markdown all count.

No LLM judge, no fuzzy matching. Missing reasoning never fails a trial. Error,
timeout and interrupted trials cannot pass and stay in the denominator.

---

## 5. What was verified, and what was not

### Installation checks — done

* `uv sync` resolves the pinned Omnigent commit and its lockstep SDK siblings.
* `skillbench` entrypoint runs; Python 3.12.3, omnigent `0.14.0.dev0`,
  claude-agent-sdk `0.2.152`.
* Every keyword argument skillbench passes to `ClaudeSDKExecutor.__init__` and
  `CodexExecutor.__init__` was bound against the real signatures at the pin.
  None were invented.

### Deterministic checks — done

* `--self-test`: 36 checks pass — a passing lookup, the missing-ID case,
  missing / duplicate / wrong-ID calls, an integer `order_id` and a damaged
  `"7"`, an incorrect tool result, extra prose, a wrong response, execution
  error and timeout with otherwise-correct text, an unmatched request event,
  callback evidence without a redundant request event, and both harnesses'
  transport names. Reasoning presence provably changes no assertion.
* A 43-check wiring suite drove `run_trial` against stub executors replaying the
  exact event shapes each real executor emits: skill staging, tool schema and
  callback registration, event normalization, per-harness final-response
  extraction, call-id correlation, grading, artifact writing, timeout cleanup,
  and regrade determinism (including that a tampered `final_response` flips the
  grade).
* Codex skill mounting was exercised offline against the real
  `populate_codex_skills_from_bundle`: the filter exposes exactly
  `order-status`, at `$CODEX_HOME/skills/order-status/SKILL.md`, byte-identical
  to the repo skill; a filter naming nothing exposes nothing.
* Offline regrade of an unchanged live run reproduced every grade exactly.

### Live checks — Claude done, Codex **not** done

**Claude: fully exercised, live.** A 6-case matrix passed 6/6 in explicit mode
and 6/6 in discovery mode against `claude-sonnet-4-5`. The traces show the real
chain end to end: the model invoked the bundled skill as
`skillbench:order-status`, used `ToolSearch` to resolve
`mcp__omnigent__lookup_order`, called it with `{"order_id": "A100"}`, our
callback executed and returned `{"order_id":"A100","found":true,"status":"shipped"}`,
and the model's final line was exactly `Order A100: shipped.` The actual CLI
invocation Omnigent built was:

```
claude --tools Skill,ToolSearch \
  --allowedTools mcp__omnigent__lookup_order,Skill(skillbench:order-status),Skill(order-status) \
  --model claude-sonnet-4-5 --permission-mode auto \
  --mcp-config {"mcpServers":{"omnigent":{"type":"sdk","name":"omnigent"}}} \
  --setting-sources=user,project --plugin-dir /tmp/skillbench-claude-*/bundle \
  --no-session-persistence
```

A real Ctrl-C mid-matrix was also tested: the in-flight trial was saved with
`status: "interrupted"` and 9 events of partial evidence, the summary counted it
in the denominator, and no further trials launched.

**Codex: NOT verified live.** The build environment's egress proxy refuses the
WebSocket the Codex CLI needs (`HTTP CONNECT failed with status 403, url:
wss://api.openai.com/v1/responses`), and no Codex credentials were available.
What *was* confirmed: the `codex app-server` subprocess spawns, the private
`CODEX_HOME` is created inside the trial workspace with the skill correctly
symlinked, the JSON-RPC session is established, and the timeout path interrupts
and closes cleanly while preserving the partial trace. What remains unverified
on your laptop:

1. **the one live Codex lookup trial** — that the dynamic tool is registered,
   the callback fires, the model receives the result, and the final text is
   extracted;
2. **the full 36-trial matrix** with both harnesses;
3. **whether `TurnComplete.response` is in fact the clean final answer on
   Codex.** The code reads that way at the pin (the `final_answer` phase of the
   completed `agentMessage`), and skillbench trusts it, but it has not been
   observed. If Codex trials fail with a `final_response` that contains
   narration, check `final_response_source` in the trace — that is the field
   that would need revisiting.

Run step 2 of §3 for each harness first; if the Codex probe passes, the matrix
is trustworthy.

Note also that a Codex network or auth problem shows up as a **timeout**, not an
error: the CLI retries the WebSocket indefinitely and emits no executor error.
If Codex trials all time out, look at the network before the eval.

---

## 6. Harness differences a common interface does not erase

These are recorded in every trial JSON under `invocation.harness_notes`, and
they are the honest material for the talk's "what the meta-harness does not
solve" section.

**Skill loading is not the same mechanism.** Claude loads the bundle as a local
*plugin*, so the skill is labelled `skillbench:order-status` and is selected via
`--allowedTools Skill(<name>)`. Codex symlinks the skill directory into a private
`$CODEX_HOME/skills/` and selects by directory name. skillbench passes Claude
both spellings, because the SDK matches "the skill's directory name, or
`plugin:skill` for plugin-qualified skills".

**The tool has two names on Claude, and they disagree.** Omnigent registers it on
an in-process MCP server, so the *model* sees `mcp__omnigent__lookup_order` while
the *callback* is invoked under the bare `lookup_order`. skillbench keeps two
separate exact-match maps for this; a single map — or substring matching — would
silently miscount. Codex uses the bare name on both sides.

**Claude gets an extra system-prompt note.** Whenever MCP tools are registered,
`claude_sdk_executor` appends its own "Claude SDK tool naming" paragraph telling
the model to use the prefixed form. Codex receives nothing equivalent. Both
harnesses are given an empty system prompt by skillbench; the note is Omnigent's.

**"Final response" means different things.** On Codex,
`TurnComplete.response` is the `final_answer` agent message. On Claude it is the
*concatenation of every assistant text chunk in the turn*. In the live run that
came back as `"I'll look up the status of order A100 for you using the
order-status skill.Order A100: shipped."` — using it would have failed the exact
match even though the model behaved perfectly. skillbench therefore takes
Claude's final answer as the text emitted after the last tool event and records
which rule it used in `final_response_source`. This is the single most
instructive difference in the whole PoC.

**Tool isolation is not equivalent.** Claude runs with a base tool set of just
`Skill` and `ToolSearch` — no Bash, Read, Edit or Write, since `os_env` is not
configured. Codex keeps its native shell tool enabled, because it discovers
skills as files on disk and needs a file-reading capability to load the skill
body at all; its built-in `web_search` is disabled. Do not describe these as the
same sandbox.

**Host configuration still leaks, asymmetrically.** Claude's skills filter is a
name list, which makes the SDK default `setting_sources` to `["user",
"project"]` — so a user-level `~/.claude/CLAUDE.md` is still loaded even though
only the bundled skill is invokable. Codex's private home symlinks your
`AGENTS.md` and `hooks.json` unless `HARNESS_CODEX_MINIMAL_CONFIG=1` is set. For
a clean comparison, run from a machine without personal `CLAUDE.md` / `AGENTS.md`
files, or set that variable and note it.

**Reasoning visibility differs.** Codex streams reasoning deltas; Claude emits
thinking blocks only when the model produces them. `reasoning.availability` is
recorded as `observed` or `not_observed`, and never affects a grade. Nothing here
requests hidden chain-of-thought.

---

## 7. Deliberately out of scope

No no-skill baseline, no significance testing, no harness-vs-model causal
attribution, no cost accounting, no external eval platform, no second skill, no
concurrency, no retries, no dashboard, no deployment. The first thing to add is
the no-skill control — without it these numbers describe conformance, not value.
