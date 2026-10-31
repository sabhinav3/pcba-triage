# pcba-triage — netlist-aware failure triage for PCBA bring-up, with a fault-injection benchmark
SHELL := /bin/bash
PY_SYS ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python
export PYTHONPATH := $(CURDIR)/core
BOARD   ?= rp2040_easyduino
METHODS ?= none,rules,agent
CASES   ?=
RUN_ID  ?=
SEED    ?= 2026
PCB     ?= boards/_src/Easyduino/Raspberry Pi Pico 2040/Easyduino_RP2040.kicad_pcb
EASYDUINO_REV ?= 53b14b66d64f25c55f88971c1c1b51656126bd30

.PHONY: help setup setup-py setup-js versions board faults bench api actors gateway web dev check test typecheck web-build clean

help:
	@echo "make setup            venv + python deps + bun deps (needs network once)"
	@echo "make bench            full benchmark (METHODS=none,rules works offline, no keys)"
	@echo "make check            pytest + TS typecheck + web build"
	@echo "make dev              api + actors + gateway + web"
	@echo "make board / faults   rebuild board.json from KiCad sources / regenerate faults.json (SEED=$(SEED))"

versions:
	@echo "python: $$($(PY_SYS) --version 2>&1)"; echo "node: $$(node --version 2>&1)"; echo "bun: $$(bun --version 2>&1)"; \
	 echo "kicad-cli: $$(kicad-cli version 2>&1 || echo 'not installed (committed netlist.xml is used)')"

$(VENV)/.ok: core/requirements.txt
	$(PY_SYS) -m venv $(VENV)
	$(PY) -m pip install -q -r core/requirements.txt
	@touch $@

setup-py: $(VENV)/.ok

setup-js:
	cd actors && bun install
	cd gateway && bun install
	cd web && bun install

setup: setup-py setup-js
	@test -f .env || cp .env.example .env
	@echo "setup done. Edit .env for OPENAI_API_KEY / SUPABASE_* (both optional)."

# --- board + faults (needs the KiCad project only for positions; netlist.xml is committed) ---
boards/_src/Easyduino:
	git clone https://github.com/Hanqaqa/Easyduino.git boards/_src/Easyduino
	cd boards/_src/Easyduino && git checkout -q $(EASYDUINO_REV)

board: setup-py boards/_src/Easyduino
	@if command -v kicad-cli >/dev/null; then \
	  kicad-cli sch export netlist --format kicadxml -o boards/$(BOARD)/netlist.xml "boards/_src/Easyduino/Raspberry Pi Pico 2040/Easyduino_RP2040.kicad_sch"; \
	else echo "[board] kicad-cli not found: deriving netlist.xml from the .kicad_pcb pads (see DECISIONS.md D2)"; \
	  $(PY) -m triage.pcb_netlist --pcb "$(PCB)" --out boards/$(BOARD)/netlist.xml; fi
	$(PY) -m triage.netlist build --board $(BOARD) --pcb "$(PCB)"

faults: setup-py
	$(PY) -m triage.faults --board $(BOARD) --seed $(SEED)

# --- benchmark: never needs KiCad, network or keys for METHODS=none,rules ---
bench: setup-py
	$(PY) -m triage.bench --board $(BOARD) --methods $(METHODS) $(if $(CASES),--cases $(CASES)) $(if $(RUN_ID),--run-id $(RUN_ID))
	@$(PY) -c "import shutil,pathlib; r=pathlib.Path('results/LATEST').read_text().strip(); d=pathlib.Path('web/public/mock'); d.mkdir(parents=True,exist_ok=True); shutil.copy(f'results/{r}/results.json', d/'results.json')"

# --- services ---
api: setup-py
	$(VENV)/bin/uvicorn triage.api:app --host 0.0.0.0 --port 8000

actors:
	cd actors && bun run dev

gateway:
	cd gateway && bun run start

web:
	cd web && bun run dev

dev:
	@echo "starting api :8000, actors, gateway :8787, web :5173 (Ctrl-C stops all)"
	@trap 'kill 0' INT TERM; \
	 $(MAKE) -s api & $(MAKE) -s actors & sleep 3; $(MAKE) -s gateway & $(MAKE) -s web & wait

# --- quality gate ---
test: setup-py
	$(PY) -m pytest core/tests -q

typecheck:
	cd actors && bunx tsc --noEmit
	cd gateway && bunx tsc --noEmit
	cd web && bunx tsc --noEmit

web-build: web/public/mock/board.json
	cd web && bun run build

web/public/mock/board.json: boards/$(BOARD)/board.json
	@mkdir -p web/public/mock && cp boards/$(BOARD)/board.json web/public/mock/board.json
	@test -f web/public/mock/results.json || cp results/sample/results.json web/public/mock/results.json

check: test typecheck web-build
	@echo "make check: OK"

clean:
	rm -rf web/dist results/dev-* .pytest_cache
