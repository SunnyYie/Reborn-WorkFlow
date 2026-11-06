# Agent Entry

Read `Agent.md`, then the Skill for the current task. This file is an
auto-discovery entry for compatible hosts, not a guarantee that every Agent
automatically loads repository instructions.

## Portable Execution

- Resolve paths relative to this checkout, never copy another machine's `/Users/...` paths.
- Run `python reborn.py doctor` first. A nonzero result means prerequisites are
  incomplete, not permission to bypass them. On POSIX use `python3`.
- Missing dependencies: request installation authorization, then run
  `python reborn.py setup --yes` (add `--with-mcp` for an MCP host).
  System-tool installation requires separate scope: `--system-tools`.
- On a new device use the interactive `Start.cmd` / `Start.command` onboarding
  launcher, or `python reborn.py onboard`. Never send secrets through chat,
  tools or JSON inputs. `auth` prompts only in a local terminal.
- Do not silently create a replacement Word template or fake historical RAG.
  Obtain authorized material. `init-library --yes` explicitly creates an empty
  library; report that it contains no history.
- Prefer `python reborn.py run workflow ...` for portable execution.
  Original `workflow/run`, `rag/run`, `mini-workflow/run` remain supported.
- After every successful full-workflow action, run
  `python reborn.py run workflow --state <actual state_path> next`.
  Continue agent-owned steps yourself. Ask only for missing business information,
  real content decisions, actual listening feedback or permissions.
- A failure is not an approval. Re-read `status` / `next` after an interrupted
  action; retain the same session. Never rewrite state or weaken validators.
- Installation authorization is not song, key, spoken or setlist confirmation.
  Native Word/WPS inspection and actual human listening cannot be simulated.
- If the host lacks local execution, use an explicitly connected MCP adapter.
  Ordinary chat-only hosts cannot execute this project by reading files.
  Remote/mobile clients need a trusted execution host and supported tool connection.

## Regression

Keep business changes separate from host adapters. Preserve user files and
existing confirmations. Run `tests_portable`, existing `workflow`, `rag`,
`mini-workflow` and `scripts` test suites after changes. Private-source tests
require the existing authorized materials; do not replace them to obtain green tests.
Never claim Windows, a specific Agent, live PCO, listening or native UI acceptance
based solely on local unit tests or a CI configuration.
