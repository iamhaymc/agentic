# Agent Instructions

## Core Rules

- Ask clarifying questions.
- Avoid excessive token burn.
- Never commit secrets or local config (e.g. `config.secret.yml`, `.env`).
- Prefer adding to existing files before creating new ones.

## Communication

- Use direct and concise language and common terminology.
- Easy to digest by a human, technical enough for an agent.

## Name Conventions

- **Isometric / aligned naming**
  - improves scannability (column alignment)
- **Semantic symmetry / parallel pairs**
  - If you know the opposite verb, you can guess the pair
- **Rhythmic variation**
  - Words in a phrase differ in length to avoid monotony and improve rhythm

## Development Process

- Break feature work into ~3 phases
  - Phases: "Minimum Scope", "Complete Scope", "Extended Scope"
  - Write phase list to documents for tracking
  - Update documentation after each step.
- Break feature phases into tasks:
  - Write task list to documents for tracking
  - Update documentation after each step.
- Use test driven development
  - Unit tests MUST be fast (<= few seconds) and should be ran frequently
  - Other tests may be slow (>= few minutes) and should be ran infrequently
- Log diagnostics often for debugging and optimization
- Update documentation with every change
- Treat compiler warnings, test failures, leaks, out-of-bounds access and
  unchecked allocation or I/O as defects; handle failure paths explicitly.

## System Design

- Minimal dependencies
- Modular for ease of composition and testing
- Core modules decoupled from infrastructure
- Avoid overfitting to a use case to preserve versitility
- Keep behavior deterministic for identical seeds, inputs and time steps
- Avoid hidden global state

## User Interface Design

- Use WebSockets often for async messaging
- Modulate hero icon in a variety of ways to indicate status

## User Interface Style

- Sleak and minimal with balance of empty space.
- High quality and legible typography.
- Prefer icons over text labels.
- Responsive to change in size and orientation.
- Mobile first

## Documentation Style

- Dense and detailed.
- Use lists over tables
- Use mermaid charts over ASCII charts
- Try to avoid line breaks mid sentence
- Use check list boxes (open: `- [ ]`, closed: `- [x]`)

## Environment Tools

- Use `uv` (for Python, `.venv`, and other applications)
- Use `clang` (for C11)
- Use `gh` (read-only unless explicitly told otherwise)
- Use `aws` (read-only unless explicitly told otherwise)

## Workspace Content

### File Descriptions

#### Documentation files

- _`AGENTS.md`_ Agent instructions
- _`LICENSE.md`_ Content license
- _`README.md`_ Summary and quickstart
- _`OPTIONS.md`_ Configuration properties
- _`DESIGN.md`_ Complete overview and reference
- _`EXAMPLE.ipynb`_ Minimal demonstration
- _`CHANGES.md`_ Closed tasks list sorted by version
- _`TODO.md`_ Open tasks list sorted by impact

#### Project CI/CD files

- _setup.ps1_
  - Environment setup (Windows)
- _setup.sh_
  - Environment setup (Mac/Linux)
- _config.yml_
  - Default configuration

#### Project Python files

- _app/\*main.py_
  - Python CLI/TUI
- _app/\*core.py_
  - Python API library
- _test/\*test_bdd.py_ or _app/\*test_bdd.py_:
  - Python unit tests
- _test/\*test_e2e.py_ or _app/\*test_e2e.py_:
  - Python UI captures (images in `assets/`)
- _test/\*eval.py_:
  - Python benchmark tests
- _pyproject.toml_
  - package metadata and dependencies

#### Project Web UI files

- _app/\*ui.html_
  - Web UI markup
- _app/\*ui.css_
  - Web UI styles
- _app/\*ui.js_:
  - Web UI logic
