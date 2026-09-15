"""skillbench — cross-harness conformance probe for a single agent skill.

Runs one bundled Markdown skill (``skills/order-status/SKILL.md``) against two
coding-agent harnesses — Claude (via ``omnigent.inner.claude_sdk_executor``)
and Codex (via ``omnigent.inner.codex_executor``) — through Omnigent's inner
executor interface, with the same logical tool, the same six prompts, and the
same deterministic assertions on both sides.

What this measures: observed conformance to the skill's rules on these six
tasks, per harness. There is no no-skill control here, so nothing in the
output establishes that the skill *improves* anything. It establishes whether
the skill's instructions survive the trip across harnesses.

Everything version-specific about the Omnigent pin lives in ``run_trial`` and
the small helpers directly around it, so the blast radius of a repin is one
screenful.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import platform
import random
import shutil
import signal
import string
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "CASES",
    "EvalCase",
    "RunConfig",
    "grade_trial",
    "lookup_order",
    "main",
    "regrade",
    "run_suite",
    "run_trial",
    "self_test",
]

# --------------------------------------------------------------------------
# Pins and constants
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1
GRADER_VERSION = 1

#: The Omnigent commit this integration was written and verified against.
#: Must match the ``rev`` of all three git sources in ``pyproject.toml``.
OMNIGENT_COMMIT = "322746fd2b00065627a41e4137fd0bdf55e2410a"

HARNESSES = ("claude", "codex")

SKILL_DIR_NAME = "order-status"
#: Plugin/agent name. Claude labels bundled plugin skills ``<agent>:<skill>``.
AGENT_NAME = "skillbench"

LOGICAL_TOOL_NAME = "lookup_order"
TOOL_DESCRIPTION = "Look up the current status of an order by its exact order ID."
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "The exact order ID, as a string. Preserve leading zeros.",
        }
    },
    "required": ["order_id"],
    "additionalProperties": False,
}

#: Exact tool identifiers, per harness, mapped to the logical name.
#:
#: Two separate maps, because on Claude the callback and the event stream do
#: NOT agree on the spelling -- a difference that a single map (or substring
#: matching) would quietly paper over:
#:
#:  * ``_build_mcp_tools`` builds each SDK tool from the Omnigent schema and
#:    its handler calls ``tool_executor(tool_name, args)`` with ``tool_name``
#:    taken from ``schema["name"]`` -- the BARE name.
#:  * The model, however, sees the tool on the in-process MCP server named
#:    ``omnigent``, so ``ToolCallRequest`` / ``ToolCallComplete`` carry
#:    ``mcp__omnigent__lookup_order`` -- the PREFIXED name.
#:
#: Codex registers dynamicTools under their bare names
#: (``_dynamic_tool_specs``) and echoes that same name back on
#: ``item/tool/call``, so both of its maps agree.
#:
#: Exact-match only, both ways: substring matching would fold a
#: harness-internal tool whose name merely contains ``lookup_order`` into the
#: lookup count.
CALLBACK_TOOL_NAMES: dict[str, dict[str, str]] = {
    "claude": {LOGICAL_TOOL_NAME: LOGICAL_TOOL_NAME},
    "codex": {LOGICAL_TOOL_NAME: LOGICAL_TOOL_NAME},
}

EVENT_TOOL_NAMES: dict[str, dict[str, str]] = {
    "claude": {f"mcp__omnigent__{LOGICAL_TOOL_NAME}": LOGICAL_TOOL_NAME},
    "codex": {LOGICAL_TOOL_NAME: LOGICAL_TOOL_NAME},
}

#: Default mode prefix. ``--discovery`` omits *only* this string.
INVOCATION_PREFIX = "Use the order-status skill.\n\n"

#: The fixed order database. Lives only here — never in the skill or prompt.
ORDER_DB: dict[str, str] = {
    "A100": "shipped",
    "B200": "processing",
    "0007": "delivered",
}

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_REPEAT = 3

#: How the final answer is recovered from the event stream, per harness.
#:
#: ``turn_complete``      -- the executor reports an authoritative final
#:                           message; use ``TurnComplete.response``.
#: ``last_text_segment``  -- the executor's ``TurnComplete.response`` is an
#:                           accumulation of every assistant text chunk in the
#:                           turn (intermediate narration included), so the
#:                           final answer is the text emitted after the last
#:                           tool event instead.
#:
#: Verified at the pinned commit:
#:  * codex_executor yields ``TurnComplete(response=final_response)`` where
#:    ``final_response`` comes from the agentMessage whose phase is
#:    ``final_answer`` -- authoritative.
#:  * claude_sdk_executor accumulates ``response_text += text`` over every
#:    TextChunk and yields that as ``TurnComplete.response`` (ResultMessage's
#:    own ``result`` is used only when no text streamed at all) -- not
#:    authoritative for "the final answer only".
FINAL_RESPONSE_STRATEGY = {"codex": "turn_complete", "claude": "last_text_segment"}

_MAX_TEXT_FIELD = 20000
_MAX_CONTAINER_ITEMS = 200
_MAX_DEPTH = 8


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    """One prompt plus the exact behaviour the skill mandates for it."""

    id: str
    prompt: str
    expected_lookup_ids: tuple[str, ...]
    expected_response: str


@dataclass(frozen=True)
class RunConfig:
    """Everything one invocation of the matrix needs."""

    harnesses: tuple[str, ...]
    models: dict[str, str]
    repeat: int
    timeout_seconds: float
    discovery: bool
    artifacts_dir: Path


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        id="shipped",
        prompt="What is the status of order A100?",
        expected_lookup_ids=("A100",),
        expected_response="Order A100: shipped.",
    ),
    EvalCase(
        id="processing",
        prompt="Where is order B200?",
        expected_lookup_ids=("B200",),
        expected_response="Order B200: processing.",
    ),
    EvalCase(
        id="unknown",
        prompt="What is the status of order Z999?",
        expected_lookup_ids=("Z999",),
        expected_response="Order Z999: not found.",
    ),
    EvalCase(
        id="missing_id",
        prompt="What is the status of my order?",
        expected_lookup_ids=(),
        expected_response="What is your order ID?",
    ),
    EvalCase(
        id="leading_zeros",
        prompt="What is the status of order 0007?",
        expected_lookup_ids=("0007",),
        expected_response="Order 0007: delivered.",
    ),
    EvalCase(
        id="incorrect_guess",
        prompt="I think order A100 was delivered. What is its current status?",
        expected_lookup_ids=("A100",),
        expected_response="Order A100: shipped.",
    ),
)

CASES_BY_ID = {case.id: case for case in CASES}


# --------------------------------------------------------------------------
# The shared deterministic tool
# --------------------------------------------------------------------------


def lookup_order(order_id: str) -> dict[str, Any]:
    """Return the canonical status record for *order_id*.

    No network, no external order service. Exact string keys only: the caller
    is responsible for rejecting non-string arguments before reaching here, so
    that ``7`` never silently becomes ``"0007"``.
    """
    if not isinstance(order_id, str):
        raise TypeError(f"order_id must be a string, got {type(order_id).__name__}")
    status = ORDER_DB.get(order_id)
    if status is None:
        return {"order_id": order_id, "found": False, "status": None}
    return {"order_id": order_id, "found": True, "status": status}


def _validate_tool_arguments(args: Any) -> str | None:
    """Return an error message when *args* is not a valid lookup payload.

    Rejects malformed arguments rather than coercing: an integer ``7`` is an
    error, not ``"0007"``, and not ``"7"``.
    """
    if not isinstance(args, dict):
        return f"arguments must be a JSON object, got {type(args).__name__}"
    extra = sorted(k for k in args if k != "order_id")
    if extra:
        return f"unexpected argument(s): {', '.join(extra)}"
    if "order_id" not in args:
        return "missing required argument: order_id"
    value = args["order_id"]
    if not isinstance(value, str):
        return (
            "order_id must be a string, got "
            f"{type(value).__name__} ({value!r}); integers are never coerced"
        )
    return None


# --------------------------------------------------------------------------
# JSON-safe serialization
# --------------------------------------------------------------------------


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Coerce *value* into something ``json.dump`` accepts, without leaking.

    Unknown objects become a short ``repr``-derived string rather than being
    walked: SDK clients, subprocess handles and environment mappings must never
    end up in an artifact.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        # NaN and the infinities are not valid JSON; keep them as text rather
        # than emitting a token no strict parser will read back.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT_FIELD else value[:_MAX_TEXT_FIELD] + "…[truncated]"
    if depth >= _MAX_DEPTH:
        return f"<depth-limited {type(value).__name__}>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _MAX_CONTAINER_ITEMS:
                out["…truncated"] = f"{len(value) - _MAX_CONTAINER_ITEMS} more key(s)"
                break
            out[str(k)] = _json_safe(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        clipped = items[:_MAX_CONTAINER_ITEMS]
        out_list = [_json_safe(v, depth + 1) for v in clipped]
        if len(items) > len(clipped):
            out_list.append(f"…{len(items) - len(clipped)} more item(s)")
        return out_list
    for attr in ("value", "name"):  # enums such as ToolCallStatus
        if hasattr(value, attr) and type(value).__mro__[1] in (str, int):
            return _json_safe(getattr(value, attr), depth + 1)
    if hasattr(value, "value") and hasattr(value, "name") and hasattr(type(value), "__members__"):
        return _json_safe(value.value, depth + 1)
    text = repr(value)
    return text if len(text) <= 500 else text[:500] + "…[truncated]"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_skill_path() -> Path:
    """The bundled skill, resolved relative to the current working directory.

    Running from the repository root is required; there is no wheel-packaged
    copy of the skill assets.
    """
    return Path.cwd() / "skills" / SKILL_DIR_NAME / "SKILL.md"


# --------------------------------------------------------------------------
# Event normalization
# --------------------------------------------------------------------------

_EVENT_KIND_BY_TYPE = {
    "TextChunk": "text",
    "ReasoningChunk": "reasoning",
    "ToolCallRequest": "tool_request",
    "ToolCallComplete": "tool_result",
    "TurnComplete": "turn_complete",
    "ExecutorError": "error",
    "TurnCancelled": "other",
    "CompactionStarted": "other",
    "CompactionComplete": "other",
    "SubAgentStarted": "other",
    "SubAgentCompleted": "other",
    "SubAgentToolCall": "other",
}


def _normalize_event(event: Any, seq: int, t_rel: float, harness: str) -> dict[str, Any]:
    """Turn one Omnigent ``ExecutorEvent`` into a JSON-safe trace record.

    Unknown event types are preserved as ``kind: "other"`` with their public
    attributes, rather than dropped — a repin that adds an event type should
    show up in the artifacts, not vanish.
    """
    type_name = type(event).__name__
    kind = _EVENT_KIND_BY_TYPE.get(type_name, "other")
    payload: dict[str, Any] = {}
    for attr in vars(event) if hasattr(event, "__dict__") else {}:
        if attr.startswith("_"):
            continue
        payload[attr] = _json_safe(getattr(event, attr))
    record: dict[str, Any] = {
        "seq": seq,
        "t_rel_seconds": round(t_rel, 4),
        "source": harness,
        "type": type_name,
        "kind": kind,
        "payload": payload,
    }
    if kind in ("tool_request", "tool_result"):
        raw_name = payload.get("name")
        record["tool_name_raw"] = raw_name
        record["tool_name_logical"] = EVENT_TOOL_NAMES[harness].get(
            raw_name if isinstance(raw_name, str) else "", None
        )
        meta = payload.get("metadata")
        record["call_id"] = meta.get("call_id") if isinstance(meta, dict) else None
    return record


# --------------------------------------------------------------------------
# Deterministic grading
# --------------------------------------------------------------------------


def grade_trial(trace: dict) -> dict:
    """Grade one trial from its recorded evidence alone.

    Reads only fields that are embedded in the trial JSON, so an offline
    regrade of an unchanged trace produces byte-identical grades without
    constructing an executor or making a model call.
    """
    failures: list[str] = []
    assertions: dict[str, bool] = {}

    expected = trace.get("expected") or {}
    expected_ids = list(expected.get("lookup_ids") or [])
    expected_response = expected.get("response")
    status = trace.get("status")
    executions = list(trace.get("tool_executions") or [])

    # 1. Execution completed normally, with no tool callback error.
    completed = status == "completed"
    assertions["execution_completed"] = completed
    if not completed:
        failures.append(f"execution status is {status!r}, not 'completed'")

    errored = [e for e in executions if e.get("error")]
    assertions["no_tool_error"] = not errored
    if errored:
        failures.append(
            "tool callback reported error(s): "
            + "; ".join(str(e.get("error")) for e in errored[:3])
        )

    # 2. Actual lookup_order execution count equals the expected count.
    count_ok = len(executions) == len(expected_ids)
    assertions["lookup_call_count"] = count_ok
    if not count_ok:
        failures.append(
            f"expected {len(expected_ids)} lookup_order execution(s), observed {len(executions)}"
        )

    # 3. Each actual call carries exactly the expected arguments, in order.
    args_ok = True
    for index, want_id in enumerate(expected_ids):
        if index >= len(executions):
            args_ok = False
            failures.append(f"lookup_order call #{index + 1} for {want_id!r} never happened")
            continue
        got_args = executions[index].get("arguments")
        want_args = {"order_id": want_id}
        if got_args != want_args:
            args_ok = False
            failures.append(
                f"lookup_order call #{index + 1} arguments {got_args!r} != {want_args!r}"
            )
    for index in range(len(expected_ids), len(executions)):
        args_ok = False
        failures.append(
            f"unexpected extra lookup_order call #{index + 1} "
            f"with arguments {executions[index].get('arguments')!r}"
        )
    assertions["lookup_arguments"] = args_ok

    # 4. Each tool result matches the deterministic database response.
    results_ok = True
    for index, execution in enumerate(executions):
        got_args = execution.get("arguments")
        order_id = got_args.get("order_id") if isinstance(got_args, dict) else None
        if not isinstance(order_id, str):
            results_ok = False
            failures.append(
                f"lookup_order call #{index + 1} has no usable string order_id to verify"
            )
            continue
        want_result = lookup_order(order_id)
        if execution.get("result") != want_result:
            results_ok = False
            failures.append(
                f"lookup_order call #{index + 1} result {execution.get('result')!r} "
                f"!= {want_result!r}"
            )
    assertions["lookup_results"] = results_ok

    # 5. No lookup request event left unmatched by an actual execution.
    #
    # A request event without a matching callback invocation means the harness
    # (or a permission gate) swallowed the call. The converse -- an execution
    # with no separate request event -- is not a failure: callback evidence is
    # authoritative and some harnesses do not emit a redundant request.
    requests = [
        e
        for e in (trace.get("events") or [])
        if e.get("kind") == "tool_request" and e.get("tool_name_logical") == LOGICAL_TOOL_NAME
    ]
    unmatched = len(requests) - len(executions)
    assertions["no_unmatched_lookup_requests"] = unmatched <= 0
    if unmatched > 0:
        failures.append(
            f"{unmatched} lookup_order request event(s) never reached the tool callback"
        )

    # 6. Exact final answer. Outer whitespace only; everything else counts.
    final_response = trace.get("final_response")
    response_ok = isinstance(final_response, str) and final_response.strip() == expected_response
    assertions["final_response_exact"] = response_ok
    if not response_ok:
        failures.append(f"final response {final_response!r} != expected {expected_response!r}")

    # Reasoning is never graded. Its absence is a valid outcome.
    return {
        "grader_version": GRADER_VERSION,
        "assertions": assertions,
        "passed": all(assertions.values()),
        "failures": failures,
    }


# --------------------------------------------------------------------------
# Trial execution — all pin-specific Omnigent wiring lives below
# --------------------------------------------------------------------------


def _stage_skill_bundle(root: Path) -> tuple[Path, Path]:
    """Copy the repo skill into a throwaway bundle laid out as both harnesses want.

    Claude discovers ``<bundle>/skills/<dir>/SKILL.md`` through the SDK's
    plugin convention (``plugins=[{"type": "local", "path": bundle}]``); Codex
    symlinks the same ``<bundle>/skills/<dir>`` into the per-session
    ``$CODEX_HOME/skills/``. One layout serves both.
    """
    source = _repo_skill_path()
    bundle = root / "bundle"
    target_dir = bundle / "skills" / SKILL_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_dir / "SKILL.md")
    return bundle, target_dir / "SKILL.md"


def _skills_filter_for(harness: str) -> list[str]:
    """The exact skill-listing filter handed to the executor, per harness.

    Claude: the bundle is loaded as a *plugin* named ``AGENT_NAME``, and the
    Agent SDK's ``skills`` list is matched against "the skill's directory name,
    or 'plugin:skill' for plugin-qualified skills"
    (claude_agent_sdk._internal.transport.subprocess_cli._validate_skill_name).
    Both spellings are listed so the filter cannot accidentally hide the only
    skill under test. Names that match nothing are inert.

    Codex: ``select_codex_skill_dirs`` matches directory names and silently
    drops names with no source, so only the bare name is meaningful.
    """
    if harness == "claude":
        return [f"{AGENT_NAME}:{SKILL_DIR_NAME}", SKILL_DIR_NAME]
    return [SKILL_DIR_NAME]


def _harness_notes(harness: str) -> list[str]:
    """Capability differences this PoC cannot paper over. Recorded per trial."""
    if harness == "claude":
        return [
            (
                "The model sees the tool as 'mcp__omnigent__lookup_order' (in-process MCP "
                "server named 'omnigent'), while the skill text says 'lookup_order'. The "
                "callback itself is invoked under the bare name, so the event stream and the "
                "callback disagree on the spelling and are matched against separate maps."
            ),
            (
                "claude_sdk_executor appends its own 'Claude SDK tool naming' note to the "
                "system prompt whenever MCP tools are registered; Codex receives no such note."
            ),
            (
                "Base tool set is ['Skill', 'ToolSearch'] -- no Bash/Read/Edit/Write, because "
                "os_env is not configured."
            ),
            (
                "skills filter is a name list, so the SDK still defaults setting_sources to "
                "['user', 'project']: a user-level ~/.claude/CLAUDE.md is still loaded even "
                "though only the bundled skill is invokable. Not equivalent isolation."
            ),
            (
                "permission_mode='auto': MCP tools are pre-approved, background safety checks "
                "still run inside the CLI."
            ),
        ]
    return [
        "Tool is exposed as an App Server dynamicTool under its bare name 'lookup_order'.",
        "Built-in web_search is disabled (enable_web_search=False).",
        (
            "The native shell tool is left ENABLED: Codex discovers skills as files under "
            "$CODEX_HOME/skills/<name>/SKILL.md and needs a file-reading capability to load "
            "the skill body. Disabling it would test a different, skill-less configuration."
        ),
        (
            "The executor seeds a private $CODEX_HOME that symlinks auth.json plus the user's "
            "AGENTS.md / hooks.json and copies config.toml. Set HARNESS_CODEX_MINIMAL_CONFIG=1 "
            "to drop the instruction files and trim config.toml; auth.json is always linked."
        ),
    ]


def _make_executor(harness: str, model: str, workspace: Path, bundle: Path) -> Any:
    """Construct the pinned executor for *harness*.

    Only keyword arguments that exist in the pinned constructors are passed —
    no compatible-looking invention. Verified against
    ``ClaudeSDKExecutor.__init__`` / ``CodexExecutor.__init__`` at
    ``OMNIGENT_COMMIT``.
    """
    if harness == "claude":
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "model": model,
            "permission_mode": "auto",
            "bundle_dir": bundle,
            "agent_name": AGENT_NAME,
            "skills_filter": _skills_filter_for("claude"),
        }
        # Optional escape hatch for API-key (rather than subscription) auth.
        # The executor strips ANTHROPIC_API_KEY from the CLI's environment at
        # spawn time, so an API key can only reach the CLI through the
        # apiKeyHelper settings command.
        helper = os.environ.get("SKILLBENCH_CLAUDE_API_KEY_HELPER")
        if helper:
            kwargs["api_key_helper"] = helper
        return ClaudeSDKExecutor(**kwargs)

    from omnigent.inner.codex_executor import CodexExecutor

    return CodexExecutor(
        cwd=str(workspace),
        model=model,
        enable_web_search=False,
        disable_native_tools=False,
        bundle_dir=bundle,
        agent_name=AGENT_NAME,
        skills_filter=_skills_filter_for("codex"),
    )


def _versions(harness: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "skillbench": "0.1.0",
    }
    try:
        from omnigent.version import VERSION as _omnigent_version

        info["omnigent"] = _omnigent_version
    except Exception:  # noqa: BLE001 — version reporting is best-effort
        info["omnigent"] = None
    if harness == "claude":
        try:
            from importlib.metadata import version as _pkg_version

            info["claude_agent_sdk"] = _pkg_version("claude-agent-sdk")
        except Exception:  # noqa: BLE001
            info["claude_agent_sdk"] = None
        info["claude_cli_path"] = shutil.which("claude") or os.environ.get("OMNIGENT_CLAUDE_PATH")
    else:
        info["codex_cli_path"] = shutil.which("codex") or os.environ.get("OMNIGENT_CODEX_PATH")
    return info


async def run_trial(
    config: RunConfig,
    case: EvalCase,
    harness: str,
    repetition: int,
) -> dict:
    """Run one case once on one harness and return its complete trial record.

    Always returns a record: execution errors, timeouts and interrupts are
    captured as trial outcomes rather than propagated, so one bad trial cannot
    take the rest of the matrix with it. ``KeyboardInterrupt`` is the single
    exception — it is recorded, then re-raised so the caller can stop.
    """
    run_id = config.artifacts_dir.name
    trial_id = f"{harness}__{case.id}__{repetition}"
    effective_prompt = case.prompt if config.discovery else INVOCATION_PREFIX + case.prompt

    trial: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "trial_id": trial_id,
        "case_id": case.id,
        "repetition": repetition,
        "harness": harness,
        "requested_model": config.models[harness],
        "reported_model": None,
        "omnigent_commit": OMNIGENT_COMMIT,
        "versions": _versions(harness),
        "skill": {},
        "invocation": {
            "mode": "discovery" if config.discovery else "explicit",
            "prefix": "" if config.discovery else INVOCATION_PREFIX,
            "case_prompt": case.prompt,
            "effective_prompt": effective_prompt,
            "system_prompt_requested": "",
            "tool_name_logical": LOGICAL_TOOL_NAME,
            "tool_name_callback": next(iter(CALLBACK_TOOL_NAMES[harness])),
            "tool_name_in_events": next(iter(EVENT_TOOL_NAMES[harness])),
            "timeout_seconds": config.timeout_seconds,
            "harness_notes": _harness_notes(harness),
        },
        "expected": {
            "lookup_ids": list(case.expected_lookup_ids),
            "response": case.expected_response,
        },
        "grader_version": GRADER_VERSION,
        "started_at": _utc_now(),
        "ended_at": None,
        "elapsed_seconds": None,
        "status": "error",
        "error": None,
        "events": [],
        "tool_executions": [],
        "assistant_text": {"chunks": [], "segments": []},
        "final_response": None,
        "final_response_source": None,
        "reasoning": {"availability": "not_observed", "chunks": []},
        "usage": None,
        "grade": None,
    }

    events: list[dict[str, Any]] = trial["events"]
    executions: list[dict[str, Any]] = trial["tool_executions"]
    text_chunks: list[dict[str, Any]] = trial["assistant_text"]["chunks"]
    reasoning_chunks: list[dict[str, Any]] = trial["reasoning"]["chunks"]
    segments: list[list[str]] = [[]]
    turn_complete_response: str | None = None
    saw_turn_complete = False

    root = Path(tempfile.mkdtemp(prefix=f"skillbench-{harness}-"))
    executor: Any = None
    session_key = f"skillbench-{trial_id}-{_rand_suffix(6)}"
    started_mono = time.monotonic()

    def _cut_segment() -> None:
        if segments[-1]:
            segments.append([])

    async def tool_callback(name: str, args: Any) -> dict[str, Any]:
        """The authoritative record of a tool call actually happening.

        Wired onto the executor's ``_tool_executor`` attribute — the same
        private hook Omnigent's own ``ExecutorAdapter`` assigns
        (omnigent/runtime/harnesses/_executor_adapter.py:185). There is no
        public setter at this pin. Exercised end to end by every live trial.
        """
        logical = CALLBACK_TOOL_NAMES[harness].get(name)
        record: dict[str, Any] = {
            "index": len(executions),
            "call_id": None,  # correlated from request events after the turn
            "name_raw": name,
            "name_logical": logical,
            "arguments": _json_safe(args),
            "result": None,
            "error": None,
            "t_rel_seconds": round(time.monotonic() - started_mono, 4),
        }
        if logical != LOGICAL_TOOL_NAME:
            record["error"] = f"unknown tool {name!r}"
            executions.append(record)
            return {"error": record["error"]}
        problem = _validate_tool_arguments(args)
        if problem is not None:
            record["error"] = f"invalid arguments: {problem}"
            executions.append(record)
            return {"error": record["error"]}
        result = lookup_order(args["order_id"])
        record["result"] = result
        executions.append(record)
        return result

    try:
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        bundle, staged_skill = _stage_skill_bundle(root)
        trial["skill"] = {
            "name": SKILL_DIR_NAME,
            "source_path": str(_repo_skill_path()),
            "staged_path": str(staged_skill),
            "sha256": _sha256_file(staged_skill),
            "bundle_root": str(bundle),
            "skills_filter": _skills_filter_for(harness),
            "agent_name": AGENT_NAME,
            "loading_mechanism": (
                "claude-agent-sdk local plugin (plugins=[{'type':'local','path':<bundle>}]), "
                "skills listed by name"
                if harness == "claude"
                else "codex $CODEX_HOME/skills symlink populated from <bundle>/skills, "
                "skills selected by directory name"
            ),
        }

        from omnigent.inner.executor import ExecutorConfig

        executor = _make_executor(harness, config.models[harness], workspace, bundle)
        # Private callback hook; see tool_callback's docstring for provenance.
        executor._tool_executor = tool_callback  # noqa: SLF001

        messages = [
            {"role": "user", "content": effective_prompt, "session_id": session_key},
        ]
        tools = [
            {
                "name": LOGICAL_TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "parameters": TOOL_PARAMETERS,
            }
        ]
        exec_config = ExecutorConfig(model=config.models[harness])

        stream = executor.run_turn(messages, tools, "", exec_config)
        seq = 0
        try:
            async with asyncio.timeout(config.timeout_seconds):
                async for event in stream:
                    record = _normalize_event(event, seq, time.monotonic() - started_mono, harness)
                    events.append(record)
                    seq += 1
                    kind = record["kind"]
                    if kind == "text":
                        text = record["payload"].get("text")
                        if isinstance(text, str) and text:
                            segments[-1].append(text)
                            text_chunks.append({"seq": record["seq"], "text": text})
                    elif kind == "reasoning":
                        reasoning_chunks.append(
                            {
                                "seq": record["seq"],
                                "event_type": record["payload"].get("event_type"),
                                "delta": record["payload"].get("delta"),
                            }
                        )
                    elif kind in ("tool_request", "tool_result"):
                        _cut_segment()
                    elif kind == "turn_complete":
                        saw_turn_complete = True
                        response = record["payload"].get("response")
                        turn_complete_response = response if isinstance(response, str) else None
                        usage = record["payload"].get("usage")
                        if isinstance(usage, dict):
                            trial["usage"] = usage
                            if isinstance(usage.get("model"), str):
                                trial["reported_model"] = usage["model"]
                        # The Session layer would loop on continue_turn; this
                        # PoC runs exactly one turn, so a continuation signal
                        # is recorded and the turn ends here.
                        break
                    elif kind == "error":
                        trial["error"] = {
                            "type": "ExecutorError",
                            "message": record["payload"].get("message"),
                            "retryable": record["payload"].get("retryable"),
                        }
                        usage = record["payload"].get("usage")
                        if isinstance(usage, dict):
                            trial["usage"] = usage
        finally:
            await _aclose_stream(stream)

        if trial["error"] is not None:
            trial["status"] = "error"
        elif not saw_turn_complete:
            trial["status"] = "error"
            trial["error"] = {
                "type": "IntegrationError",
                "message": (
                    "the executor stream ended without a TurnComplete event, so no "
                    "authoritative final text could be identified"
                ),
            }
        else:
            trial["status"] = "completed"

    except TimeoutError:
        trial["status"] = "timeout"
        trial["error"] = {
            "type": "TimeoutError",
            "message": f"turn exceeded {config.timeout_seconds}s",
        }
        await _interrupt_quietly(executor, session_key)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl-C arrives here as a cancellation: ``run_suite`` installs a
        # loop-level SIGINT handler that cancels the in-flight trial task at a
        # safe point, because a KeyboardInterrupt raised from a plain signal
        # handler lands wherever the interpreter happens to be -- usually deep
        # in the event loop, not in this frame -- and would strand the partial
        # trace. KeyboardInterrupt is kept as a backstop for platforms where
        # the loop-level handler could not be installed.
        #
        # ``uncancel`` clears the pending cancellation so the cleanup awaits
        # below (and in the finally block) can actually run instead of
        # re-raising immediately. Recorded and returned rather than re-raised:
        # the partial evidence is the point of catching this at all.
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.uncancel()
        trial["status"] = "interrupted"
        trial["error"] = {"type": "KeyboardInterrupt", "message": "interrupted by operator"}
        trial["interrupt_requested"] = True
        await _interrupt_quietly(executor, session_key)
    except Exception as exc:  # noqa: BLE001 — a bad trial must not kill the matrix
        trial["status"] = "error"
        trial["error"] = {
            "type": type(exc).__name__,
            "message": str(exc) or repr(exc),
            "traceback": traceback.format_exc(limit=12),
        }
    finally:
        _finalize_trial(trial, harness, segments, turn_complete_response, started_mono)
        await _close_quietly(executor, session_key)
        shutil.rmtree(root, ignore_errors=True)

    return trial


def _rand_suffix(length: int) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


async def _aclose_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 — generator teardown is best-effort
        pass


async def _interrupt_quietly(executor: Any, session_key: str) -> None:
    """Ask the executor to abandon an in-flight turn.

    ``interrupt_session`` is the pinned API on both executors; each one also
    drops its live client/thread as part of interrupting, so the subsequent
    ``close`` is a no-op rather than a double free.
    """
    if executor is None:
        return
    try:
        await asyncio.wait_for(executor.interrupt_session(session_key), timeout=10)
    except Exception:  # noqa: BLE001 — interruption is best-effort
        pass


async def _close_quietly(executor: Any, session_key: str) -> None:
    if executor is None:
        return
    for coro in (executor.close_session(session_key), executor.close()):
        try:
            await asyncio.wait_for(coro, timeout=30)
        except Exception:  # noqa: BLE001 — cleanup must never mask the result
            pass


def _finalize_trial(
    trial: dict[str, Any],
    harness: str,
    segments: list[list[str]],
    turn_complete_response: str | None,
    started_mono: float,
) -> None:
    """Fill in derived fields and grade. Safe to call from a ``finally`` block.

    Idempotent: a trial that already carries a grade is left alone, so the
    ``KeyboardInterrupt`` path's early call is not re-done by ``finally``.
    """
    if trial.get("grade") is not None:
        return
    trial["ended_at"] = _utc_now()
    trial["elapsed_seconds"] = round(time.monotonic() - started_mono, 4)
    trial["assistant_text"]["segments"] = ["".join(s) for s in segments if s]

    strategy = FINAL_RESPONSE_STRATEGY[harness]
    last_segment = "".join(segments[-1]) if segments and segments[-1] else ""
    final: str | None = None
    source: str | None = None
    if strategy == "turn_complete":
        if turn_complete_response is not None:
            final, source = turn_complete_response, "turn_complete"
        elif last_segment:
            final, source = last_segment, "last_text_segment_fallback"
    else:
        if last_segment:
            final, source = last_segment, "last_text_segment"
        elif not trial["assistant_text"]["chunks"] and turn_complete_response is not None:
            # No assistant text streamed at all, so TurnComplete.response
            # cannot be an accumulation of narration -- it is the whole answer.
            final, source = turn_complete_response, "turn_complete_no_text_chunks"
    trial["final_response"] = final
    trial["final_response_source"] = source
    trial["turn_complete_response"] = turn_complete_response

    trial["reasoning"]["availability"] = (
        "observed" if trial["reasoning"]["chunks"] else "not_observed"
    )

    # Correlate call ids: the k-th lookup request event belongs to the k-th
    # callback invocation. Both are strictly ordered within one turn.
    request_ids = [
        e.get("call_id")
        for e in trial["events"]
        if e.get("kind") == "tool_request" and e.get("tool_name_logical") == LOGICAL_TOOL_NAME
    ]
    for index, execution in enumerate(trial["tool_executions"]):
        if index < len(request_ids):
            execution["call_id"] = request_ids[index]

    trial["grade"] = grade_trial(trial)


# --------------------------------------------------------------------------
# Suite
# --------------------------------------------------------------------------


def _new_run_dir(artifacts_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = artifacts_dir / f"{stamp}-{_rand_suffix(6)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _summarize(trials: list[dict], config_snapshot: dict) -> dict:
    per_harness: dict[str, dict[str, int]] = {}
    per_case: dict[str, dict[str, int]] = {}
    failures: list[dict[str, Any]] = []
    errors = timeouts = interrupted = 0

    for trial in trials:
        harness = trial["harness"]
        case_id = trial["case_id"]
        grade = trial.get("grade") or {}
        passed = bool(grade.get("passed"))
        for bucket, key in ((per_harness, harness), (per_case, case_id)):
            row = bucket.setdefault(key, {"passed": 0, "attempted": 0})
            row["attempted"] += 1
            row["passed"] += int(passed)
        status = trial.get("status")
        errors += int(status == "error")
        timeouts += int(status == "timeout")
        interrupted += int(status == "interrupted")
        if not passed:
            failures.append(
                {
                    "trial_id": trial["trial_id"],
                    "harness": harness,
                    "case_id": case_id,
                    "repetition": trial["repetition"],
                    "status": status,
                    "reasons": grade.get("failures", []),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "grader_version": GRADER_VERSION,
        "omnigent_commit": OMNIGENT_COMMIT,
        "generated_at": _utc_now(),
        "config": config_snapshot,
        "totals": {
            "attempted": len(trials),
            "passed": sum(1 for t in trials if (t.get("grade") or {}).get("passed")),
            "execution_errors": errors,
            "timeouts": timeouts,
            "interrupted": interrupted,
        },
        "per_harness": per_harness,
        "per_case": per_case,
        "failures": failures,
    }


def _config_snapshot(config: RunConfig) -> dict:
    return {
        "harnesses": list(config.harnesses),
        "models": dict(config.models),
        "repeat": config.repeat,
        "timeout_seconds": config.timeout_seconds,
        # Never pooled with an explicit-invocation run: the two modes ask
        # different questions of the same skill.
        "invocation_mode": "discovery" if config.discovery else "explicit",
        "invocation_prefix": "" if config.discovery else INVOCATION_PREFIX,
        "skill_sha256": _sha256_file(_repo_skill_path()),
    }


async def run_suite(config: RunConfig) -> int:
    """Run the full matrix sequentially and write artifacts. Returns an exit code."""
    run_dir = _new_run_dir(config.artifacts_dir)
    snapshot = _config_snapshot(config)
    total = len(config.harnesses) * len(CASES) * config.repeat
    mode = snapshot["invocation_mode"]
    print(f"skillbench run {run_dir.name}  ({mode} mode, {total} trials)")
    print(f"  artifacts: {run_dir}")

    trials: list[dict] = []
    interrupted = False
    index = 0
    in_flight: list[asyncio.Task[dict]] = []

    def _on_sigint() -> None:
        # Runs as a loop callback, so cancelling here lands inside run_trial's
        # own frame where the partial trace can be saved. A second Ctrl-C hands
        # SIGINT back to the default handler, so an operator who wants out now
        # gets out now.
        nonlocal interrupted
        if interrupted:
            with_default = getattr(signal, "SIG_DFL", None)
            if with_default is not None:
                signal.signal(signal.SIGINT, with_default)
            return
        interrupted = True
        print("\n  interrupt received — finishing the current trial's cleanup…", file=sys.stderr)
        for task in in_flight:
            if not task.done():
                task.cancel()

    loop = asyncio.get_running_loop()
    handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, _on_sigint)
        handler_installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Windows, or not the main thread. run_trial's KeyboardInterrupt
        # backstop is all that is available here.
        pass

    try:
        for harness in config.harnesses:
            for case in CASES:
                for repetition in range(1, config.repeat + 1):
                    if interrupted:
                        raise KeyboardInterrupt
                    index += 1
                    task = asyncio.ensure_future(run_trial(config, case, harness, repetition))
                    in_flight.append(task)
                    try:
                        trial = await task
                    finally:
                        in_flight.remove(task)
                    trials.append(trial)
                    _write_json(run_dir / f"{trial['trial_id']}.json", trial)
                    if trial.get("interrupt_requested"):
                        raise KeyboardInterrupt
                    grade = trial.get("grade") or {}
                    mark = "PASS" if grade.get("passed") else "FAIL"
                    reason = ""
                    if not grade.get("passed"):
                        reasons = grade.get("failures") or []
                        reason = f"  [{reasons[0]}]" if reasons else ""
                    print(
                        f"  [{index}/{total}] {mark} {trial['trial_id']} "
                        f"status={trial['status']} "
                        f"{trial['elapsed_seconds']}s{reason}"
                    )
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        current_task = asyncio.current_task()
        if current_task is not None:
            current_task.uncancel()
        print("  interrupted — no further trials will be launched", file=sys.stderr)
    finally:
        if handler_installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)

    # Let the harnesses' own background close tasks settle before the loop is
    # torn down, so a clean exit does not print "Task was destroyed" noise.
    await _drain_background_tasks()

    summary = _summarize(trials, snapshot)
    summary["interrupted_by_operator"] = interrupted
    _write_json(run_dir / "summary.json", summary)

    _print_summary(summary, run_dir)

    if interrupted:
        return 130
    return _exit_code_from_summary(summary)


async def _drain_background_tasks(timeout: float = 3.0) -> None:
    """Give executor-spawned cleanup tasks a moment to finish."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if not pending:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait(pending, timeout=timeout)


def _exit_code_from_summary(summary: dict) -> int:
    totals = summary["totals"]
    if totals["execution_errors"] or totals["timeouts"]:
        return 2
    if totals["interrupted"]:
        return 130
    return 0 if totals["passed"] == totals["attempted"] else 1


def _print_summary(summary: dict, run_dir: Path) -> None:
    totals = summary["totals"]
    print()
    print(f"  mode: {summary['config']['invocation_mode']}  (never pooled across modes)")
    for harness, row in sorted(summary["per_harness"].items()):
        model = summary["config"]["models"].get(harness, "?")
        print(f"  {harness:<7} {row['passed']}/{row['attempted']} passed   model={model}")
    for case_id, row in summary["per_case"].items():
        print(f"    case {case_id:<16} {row['passed']}/{row['attempted']}")
    print(
        f"  total {totals['passed']}/{totals['attempted']} passed"
        f"   errors={totals['execution_errors']}"
        f" timeouts={totals['timeouts']}"
        f" interrupted={totals['interrupted']}"
    )
    print(f"  artifacts: {run_dir}")


# --------------------------------------------------------------------------
# Offline regrade
# --------------------------------------------------------------------------

_NON_TRIAL_FILES = {"summary.json", "regrade.json"}


def regrade(run_dir: Path) -> int:
    """Re-apply the grader to saved trials. No executors, no model calls."""
    if not run_dir.is_dir():
        print(f"error: not a directory: {run_dir}", file=sys.stderr)
        return 2

    paths = sorted(p for p in run_dir.glob("*.json") if p.name not in _NON_TRIAL_FILES)
    if not paths:
        print(f"error: no trial JSON files in {run_dir}", file=sys.stderr)
        return 2

    trials: list[dict] = []
    changed: list[str] = []
    for path in paths:
        try:
            trace = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read {path.name}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(trace, dict):
            print(f"error: {path.name} is not a trial object", file=sys.stderr)
            return 2
        version = trace.get("schema_version")
        if version != SCHEMA_VERSION:
            print(
                f"error: {path.name} has schema_version {version!r}; "
                f"this build reads {SCHEMA_VERSION}",
                file=sys.stderr,
            )
            return 2
        for required in ("harness", "case_id", "status", "expected", "tool_executions"):
            if required not in trace:
                print(f"error: {path.name} is missing required field {required!r}", file=sys.stderr)
                return 2
        original = trace.get("grade") or {}
        fresh = grade_trial(trace)
        if (
            original.get("passed") != fresh["passed"]
            or original.get("assertions") != fresh["assertions"]
        ):
            changed.append(path.name)
        # Preserve the original record untouched; carry the fresh grade
        # alongside it for the regrade summary only.
        trials.append({**trace, "grade": fresh, "original_grade": original})

    snapshot = (
        json.loads((run_dir / "summary.json").read_text(encoding="utf-8")).get("config", {})
        if (run_dir / "summary.json").is_file()
        else {}
    )
    summary = _summarize(trials, snapshot)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "grader_version": GRADER_VERSION,
        "regraded_at": _utc_now(),
        "run_dir": str(run_dir),
        "trials_regraded": len(trials),
        "grades_changed": changed,
        "grades": [
            {
                "trial_id": t.get("trial_id"),
                "harness": t.get("harness"),
                "case_id": t.get("case_id"),
                "repetition": t.get("repetition"),
                "status": t.get("status"),
                "original_grade": t.get("original_grade"),
                "grade": t.get("grade"),
            }
            for t in trials
        ],
        "summary": summary,
    }
    _write_json(run_dir / "regrade.json", payload)

    print(f"regraded {len(trials)} trial(s) from {run_dir}")
    if changed:
        print(f"  grades CHANGED for: {', '.join(changed)}")
    else:
        print("  all grades identical to the recorded originals")
    _print_summary(summary, run_dir)
    return _exit_code_from_summary(summary)


# --------------------------------------------------------------------------
# Self test — synthetic traces, no model calls, no credentials
# --------------------------------------------------------------------------


def _synthetic_trace(
    *,
    case_id: str,
    harness: str = "claude",
    status: str = "completed",
    executions: list[dict] | None = None,
    final_response: str | None = None,
    request_call_ids: list[str] | None = None,
    reasoning: bool = False,
) -> dict:
    """Build a minimal but schema-shaped trace for grader self-checks."""
    case = CASES_BY_ID[case_id]
    executions = executions if executions is not None else []
    raw_name = next(iter(EVENT_TOOL_NAMES[harness]))
    ids = (
        request_call_ids
        if request_call_ids is not None
        else [f"call_{i}" for i in range(len(executions))]
    )
    events = [
        {
            "seq": i,
            "kind": "tool_request",
            "tool_name_raw": raw_name,
            "tool_name_logical": LOGICAL_TOOL_NAME,
            "call_id": call_id,
        }
        for i, call_id in enumerate(ids)
    ]
    # Harness-internal activity that must never be counted as a lookup.
    events.append(
        {
            "seq": len(events),
            "kind": "tool_request",
            "tool_name_raw": "Skill",
            "tool_name_logical": None,
            "call_id": "call_skill",
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "trial_id": f"{harness}__{case_id}__1",
        "harness": harness,
        "case_id": case_id,
        "repetition": 1,
        "status": status,
        "expected": {
            "lookup_ids": list(case.expected_lookup_ids),
            "response": case.expected_response,
        },
        "events": events,
        "tool_executions": executions,
        "final_response": case.expected_response if final_response is None else final_response,
        "reasoning": {
            "availability": "observed" if reasoning else "not_observed",
            "chunks": [{"seq": 0, "event_type": "reasoning_text", "delta": "…"}]
            if reasoning
            else [],
        },
        "usage": None,
    }


def _execution(order_id: Any, *, result: Any = "auto", error: str | None = None) -> dict:
    if result == "auto":
        result = lookup_order(order_id) if isinstance(order_id, str) else None
    return {
        "index": 0,
        "call_id": None,
        # The callback sees the bare registered name on both harnesses.
        "name_raw": LOGICAL_TOOL_NAME,
        "name_logical": LOGICAL_TOOL_NAME,
        "arguments": {"order_id": order_id},
        "result": result,
        "error": error,
    }


def self_test() -> int:
    """Run deterministic grader checks. No credentials, no model calls."""
    checks: list[tuple[str, dict, bool, str | None]] = [
        (
            "passing lookup",
            _synthetic_trace(case_id="shipped", executions=[_execution("A100")]),
            True,
            None,
        ),
        (
            "passing lookup with reasoning present",
            _synthetic_trace(case_id="shipped", executions=[_execution("A100")], reasoning=True),
            True,
            None,
        ),
        (
            "passing lookup with reasoning absent",
            _synthetic_trace(case_id="shipped", executions=[_execution("A100")], reasoning=False),
            True,
            None,
        ),
        (
            "passing missing-ID case (zero calls expected)",
            _synthetic_trace(case_id="missing_id", executions=[], request_call_ids=[]),
            True,
            None,
        ),
        (
            "passing unknown-order case",
            _synthetic_trace(case_id="unknown", executions=[_execution("Z999")]),
            True,
            None,
        ),
        (
            "passing leading-zero case",
            _synthetic_trace(case_id="leading_zeros", executions=[_execution("0007")]),
            True,
            None,
        ),
        (
            "missing call",
            _synthetic_trace(case_id="shipped", executions=[], request_call_ids=[]),
            False,
            "lookup_call_count",
        ),
        (
            "duplicate call",
            _synthetic_trace(
                case_id="shipped", executions=[_execution("A100"), _execution("A100")]
            ),
            False,
            "lookup_call_count",
        ),
        (
            "unexpected call on the missing-ID case",
            _synthetic_trace(case_id="missing_id", executions=[_execution("A100")]),
            False,
            "lookup_call_count",
        ),
        (
            "wrong-ID call",
            _synthetic_trace(case_id="shipped", executions=[_execution("B200")]),
            False,
            "lookup_arguments",
        ),
        (
            "numeric leading-zero argument (int 7)",
            _synthetic_trace(
                case_id="leading_zeros",
                executions=[
                    _execution(
                        7,
                        result=None,
                        error="invalid arguments: order_id must be a string, got int (7); integers are never coerced",
                    )
                ],
            ),
            False,
            "no_tool_error",
        ),
        (
            "damaged leading-zero argument (string '7')",
            _synthetic_trace(case_id="leading_zeros", executions=[_execution("7")]),
            False,
            "lookup_arguments",
        ),
        (
            "incorrect tool result",
            _synthetic_trace(
                case_id="shipped",
                executions=[
                    _execution(
                        "A100", result={"order_id": "A100", "found": True, "status": "delivered"}
                    )
                ],
            ),
            False,
            "lookup_results",
        ),
        (
            "extra prose around the right answer",
            _synthetic_trace(
                case_id="shipped",
                executions=[_execution("A100")],
                final_response="Sure! **Order A100: shipped.** Let me know if you need more.",
            ),
            False,
            "final_response_exact",
        ),
        (
            "wrong response",
            _synthetic_trace(
                case_id="shipped",
                executions=[_execution("A100")],
                final_response="Order A100: delivered.",
            ),
            False,
            "final_response_exact",
        ),
        (
            "outer whitespace is stripped, inner is not",
            _synthetic_trace(
                case_id="shipped",
                executions=[_execution("A100")],
                final_response="\n  Order A100: shipped.  \n",
            ),
            True,
            None,
        ),
        (
            "inner whitespace difference still fails",
            _synthetic_trace(
                case_id="shipped",
                executions=[_execution("A100")],
                final_response="Order  A100: shipped.",
            ),
            False,
            "final_response_exact",
        ),
        (
            "execution error with otherwise correct text",
            _synthetic_trace(case_id="shipped", executions=[_execution("A100")], status="error"),
            False,
            "execution_completed",
        ),
        (
            "timeout with otherwise correct text",
            _synthetic_trace(case_id="shipped", executions=[_execution("A100")], status="timeout"),
            False,
            "execution_completed",
        ),
        (
            "tool callback error with otherwise correct text",
            _synthetic_trace(
                case_id="shipped",
                executions=[
                    _execution("A100", error="invalid arguments: unexpected argument(s): id")
                ],
            ),
            False,
            "no_tool_error",
        ),
        (
            "request event never reached the callback",
            _synthetic_trace(
                case_id="shipped",
                executions=[_execution("A100")],
                request_call_ids=["call_0", "call_1"],
            ),
            False,
            "no_unmatched_lookup_requests",
        ),
        (
            "callback evidence without a redundant request event still passes",
            _synthetic_trace(
                case_id="shipped", executions=[_execution("A100")], request_call_ids=[]
            ),
            True,
            None,
        ),
        (
            "codex transport names grade identically",
            _synthetic_trace(case_id="shipped", harness="codex", executions=[_execution("A100")]),
            True,
            None,
        ),
    ]

    failed = 0
    for name, trace, want_pass, want_failed_assertion in checks:
        grade = grade_trial(trace)
        ok = grade["passed"] == want_pass
        if ok and want_failed_assertion is not None:
            ok = grade["assertions"].get(want_failed_assertion) is False
        status = "ok  " if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            failed += 1
            print(f"         passed={grade['passed']} (wanted {want_pass})")
            print(f"         assertions={grade['assertions']}")
            print(f"         failures={grade['failures']}")

    # Argument validation is part of the contract; check it directly.
    validation_checks: list[tuple[str, Any, bool]] = [
        ("valid string argument", {"order_id": "0007"}, True),
        ("integer argument rejected", {"order_id": 7}, False),
        ("float argument rejected", {"order_id": 7.0}, False),
        ("null argument rejected", {"order_id": None}, False),
        ("missing argument rejected", {}, False),
        ("extra property rejected", {"order_id": "A100", "hint": "x"}, False),
        ("non-object arguments rejected", ["A100"], False),
    ]
    for name, args, want_ok in validation_checks:
        problem = _validate_tool_arguments(args)
        ok = (problem is None) == want_ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] argument validation: {name}")
        if not ok:
            failed += 1
            print(f"         problem={problem!r}")

    # The database itself.
    db_checks = [
        ("A100", {"order_id": "A100", "found": True, "status": "shipped"}),
        ("B200", {"order_id": "B200", "found": True, "status": "processing"}),
        ("0007", {"order_id": "0007", "found": True, "status": "delivered"}),
        ("Z999", {"order_id": "Z999", "found": False, "status": None}),
        ("7", {"order_id": "7", "found": False, "status": None}),
    ]
    for order_id, want in db_checks:
        got = lookup_order(order_id)
        ok = got == want
        print(f"  [{'ok  ' if ok else 'FAIL'}] lookup_order({order_id!r})")
        if not ok:
            failed += 1
            print(f"         got={got!r} want={want!r}")

    # Reasoning must never influence a grade.
    with_reasoning = grade_trial(
        _synthetic_trace(case_id="shipped", executions=[_execution("A100")], reasoning=True)
    )
    without_reasoning = grade_trial(
        _synthetic_trace(case_id="shipped", executions=[_execution("A100")], reasoning=False)
    )
    ok = with_reasoning["assertions"] == without_reasoning["assertions"]
    print(f"  [{'ok  ' if ok else 'FAIL'}] reasoning presence does not change any assertion")
    if not ok:
        failed += 1

    print()
    if failed:
        print(f"self-test: {failed} check(s) FAILED")
        return 1
    print(
        f"self-test: all {len(checks) + len(validation_checks) + len(db_checks) + 1} checks passed"
    )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillbench",
        description=(
            "Run one bundled agent skill against Claude and Codex through Omnigent's "
            "inner executors and grade the results deterministically."
        ),
    )
    parser.add_argument(
        "--harness",
        choices=("both", "codex", "claude"),
        default="both",
        help="which harness(es) to run (default: both)",
    )
    parser.add_argument("--codex-model", help="exact Codex model id; required when running Codex")
    parser.add_argument(
        "--claude-model", help="exact Claude model id; required when running Claude"
    )
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="repetitions per case")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="per-trial timeout, seconds"
    )
    parser.add_argument(
        "--artifacts-dir", default="artifacts", help="root directory for run artifacts"
    )
    parser.add_argument(
        "--discovery",
        action="store_true",
        help="omit the explicit 'Use the order-status skill.' prefix",
    )
    parser.add_argument("--grade", metavar="RUN_DIR", help="offline regrade of a saved run")
    parser.add_argument(
        "--self-test", action="store_true", help="run synthetic grader checks and exit"
    )
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def _preflight(harnesses: tuple[str, ...]) -> str | None:
    """Return an actionable message when a harness cannot possibly run."""
    if "claude" in harnesses:
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            return (
                "the Claude harness needs the 'claude-agent-sdk' package "
                "(a dependency of the pinned omnigent). Run `uv sync`."
            )
        if not (shutil.which("claude") or os.environ.get("OMNIGENT_CLAUDE_PATH")):
            print(
                "note: no system `claude` CLI on PATH; the Agent SDK will fall back to its "
                "bundled CLI. Authentication still comes from your Claude login "
                "(~/.claude) or SKILLBENCH_CLAUDE_API_KEY_HELPER.",
                file=sys.stderr,
            )
    if "codex" in harnesses:
        if not (shutil.which("codex") or os.environ.get("OMNIGENT_CODEX_PATH")):
            return (
                "the Codex harness needs the `codex` CLI on PATH. Install it "
                "(`npm i -g @openai/codex`) or point OMNIGENT_CODEX_PATH at the binary."
            )
        # The executor bridges auth into its private per-session CODEX_HOME by
        # symlinking <source>/auth.json, where <source> is $CODEX_HOME or
        # ~/.codex (codex_executor._codex_home_config_source_from_env). With no
        # auth.json the app-server accepts the turn and then never answers, so
        # an unauthenticated run manifests as 36 timeouts rather than an error.
        # Checked here so the operator gets the real cause in one line.
        codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        if not (codex_home / "auth.json").is_file():
            return (
                f"the Codex harness found no auth.json under {codex_home}. Run "
                "`codex login` (or set CODEX_HOME to the home that holds your login). "
                "Note that OPENAI_API_KEY is deliberately stripped from the Codex "
                "subprocess by the pinned executor, so an API key in the environment is "
                "not a substitute. Without auth.json every trial hits the timeout "
                "instead of reporting an auth failure."
            )
    return None


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Run, grade and self-test are mutually exclusive modes. Run is the default,
    # so it is identified by any run-only flag being set away from its default.
    if args.grade and args.self_test:
        sys.exit(_fail("--grade and --self-test are mutually exclusive"))
    run_flags = [
        flag
        for flag, is_set in (
            ("--harness", args.harness != "both"),
            ("--codex-model", bool(args.codex_model)),
            ("--claude-model", bool(args.claude_model)),
            ("--repeat", args.repeat != DEFAULT_REPEAT),
            ("--timeout", args.timeout != DEFAULT_TIMEOUT_SECONDS),
            ("--discovery", bool(args.discovery)),
        )
        if is_set
    ]
    other_mode = "--grade" if args.grade else ("--self-test" if args.self_test else None)
    if other_mode is not None and run_flags:
        sys.exit(
            _fail(
                f"{other_mode} is a separate mode and cannot be combined with "
                f"run flags ({', '.join(run_flags)})"
            )
        )

    if args.self_test:
        sys.exit(self_test())

    if args.grade:
        try:
            sys.exit(regrade(Path(args.grade)))
        except KeyboardInterrupt:
            sys.exit(130)

    skill_path = _repo_skill_path()
    if not skill_path.is_file():
        sys.exit(
            _fail(
                f"bundled skill not found at {skill_path}. "
                "skillbench must be run from the repository root."
            )
        )

    harnesses: tuple[str, ...] = HARNESSES if args.harness == "both" else (args.harness,)

    models: dict[str, str] = {}
    for harness, value, flag in (
        ("claude", args.claude_model, "--claude-model"),
        ("codex", args.codex_model, "--codex-model"),
    ):
        if harness not in harnesses:
            continue
        if not value:
            sys.exit(_fail(f"{flag} is required when running the {harness} harness"))
        models[harness] = value

    if args.repeat < 1:
        sys.exit(_fail("--repeat must be a positive integer"))
    if args.timeout <= 0:
        sys.exit(_fail("--timeout must be a positive number"))

    problem = _preflight(harnesses)
    if problem is not None:
        sys.exit(_fail(problem))

    config = RunConfig(
        harnesses=harnesses,
        models=models,
        repeat=args.repeat,
        timeout_seconds=float(args.timeout),
        discovery=bool(args.discovery),
        artifacts_dir=Path(args.artifacts_dir),
    )
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Ctrl-C is handled inside run_suite, which installs a loop-level SIGINT
    # handler so the in-flight trial can be cancelled at a safe point and its
    # partial trace saved. This is the backstop for an interrupt that arrives
    # before or after that window.
    try:
        sys.exit(asyncio.run(run_suite(config)))
    except KeyboardInterrupt:
        sys.exit(130)
