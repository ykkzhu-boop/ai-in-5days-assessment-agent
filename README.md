# Ambient Expense Agent Prototype (ADK 2.0)

A local prototype for an ambient expense agent that streamlines employee expense reporting by instantly approving standard claims (< $100) while flagging larger expenses (>= $100) for review via a human-in-the-loop pause.

## Project Structure

- `agent.py`: Defines the `ExpenseState` and graph workflow nodes/routing.
- `main.py`: Entrypoint that runs simulations demonstrating the workflow.
- `pyproject.toml`: Package dependencies.

## Setup Instructions

### 1. Authorize Python/uv environment
Because Google macOS workstations enforce **Santa Lockdown**, you may encounter `SIGKILL` (exit code 137) when running unsigned binaries. To run the environment:
1. Run `uvx google-agents-cli setup` or `python -m venv .venv` in your local terminal.
2. If blocked by Santa, visit [Airlock (http://airlock/)](http://airlock/) to temporarily authorize the execution of the `uv` and `python` binaries.

### 2. Install dependencies
Once authorized, initialize and install the project packages:
```bash
# Using uv (recommended)
uv sync

# Or using standard pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Run the Prototype
Run the execution script to simulate expense reports:
```bash
python main.py
```
This runs two sample claims:
1. **$45.00 Lunch Claim**: Instantly routes to `auto_approve` and logs completion.
2. **$250.00 Monitor Claim**: Routes to `review_agent`, pauses for human approval via standard input, and updates the state based on input.
