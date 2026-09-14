# skillbench

Cross-harness conformance probe for a single agent skill, built on Omnigent's inner
executors. See [HANDOFF.md](HANDOFF.md) for setup, credentials, how to run it, and
what has and has not been verified.

```bash
uv sync
uv run skillbench --self-test
uv run skillbench --claude-model YOUR_CLAUDE_MODEL --codex-model YOUR_CODEX_MODEL --repeat 3
```
