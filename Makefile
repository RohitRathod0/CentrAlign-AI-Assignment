# Targets from PRD section 1/5. On Windows without `make`, run the commands
# under each target directly (see README "Running on Windows").
PY ?= python
TASK ?= Find the latest invoice from Acme Corp in the inbox and enter it into LedgerOps.

.PHONY: setup seed up demo test eval

setup:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m playwright install chromium

seed:
	$(PY) -m mock_env.maildesk.seed
	$(PY) -m mock_env.ledgerops.seed

up:
	honcho start

# Needs `make up` running in another terminal. Resets the mock apps so chaos faults fire again.
demo:
	curl -s -X POST http://localhost:8001/__admin/reset
	curl -s -X POST http://localhost:8002/__admin/reset
	$(PY) -m agent.cli --chaos "$(TASK)"

test:
	$(PY) -m pytest -q

eval:
	$(PY) -m eval.run_eval
