# AI RTL Verification Agent

An AI-powered RTL Verification Agent that analyzes Verilog RTL designs, detects potential functional issues, identifies missing verification scenarios and edge cases, evaluates testbench quality, and generates an automated verification report.

---

## Features

- RTL Parsing
- Verilog Compilation
- RTL Simulation
- Compile & Simulation Log Analysis
- AI-based RTL Design Review
- AI-based Testbench Analysis
- Missing Edge Case Detection
- Markdown Verification Report
- Email Summary (Optional)
- Debug & Auto-Fix Stage (structured root cause, patch approval, rerun, debug report)

---

## Project Structure

```
rtl_verify/
│
├── agent.py
├── rtl_verify.py
├── state.py
├── llm.py
├── patch_utils.py
├── error_fix.py
├── requirements.txt
├── requirements-dev.txt
├── .env
│
├── example/
│   ├── counter.v
│   └── tb_counter.v
│
├── tools/
│   ├── rtl_parser.py
│   ├── simulation.py
│   ├── log_analyzer.py
│   ├── rtl_analysis.py
│   ├── tb_analysis.py
│   ├── tb_checker.py
│   ├── report.py
│   └── mail_sender.py
│
├── tests/
├── temp/
├── reports/
└── README.md
```

---

## Requirements

- Python 3.10+
- Icarus Verilog
- Groq API Key

---

## Install Python Packages

```bash
pip install -r requirements.txt
```

---

## Install Icarus Verilog

- **Windows:** download and install https://bleyer.org/icarus/
- **Linux/macOS** (or any platform where `iverilog` is on your PATH): install via your package manager

The agent resolves `iverilog`/`vvp` in this order:

1. `IVERILOG_PATH` / `VVP_PATH` environment variables (recommended - set them in `.env`)
2. `iverilog` / `vvp` on your PATH (via `shutil.which`)

If the tools are not on your PATH, set the paths in `.env`:

```env
IVERILOG_PATH=C:\iverilog\bin\iverilog.exe
VVP_PATH=C:\iverilog\bin\vvp.exe
```

Verify installation:

```bash
iverilog -V
```

---

## Configure Groq API

Create a `.env` file in the project root.

```env
GROQ_API_KEY=your_groq_api_key
```

---

## Run

```bash
python rtl_verify.py example/counter.v example/tb_counter.v
```

### CLI options

| Option | Description |
| ------ | ----------- |
| `--yes`, `-y` | Auto-approve the proposed RTL patch (no interactive prompt) - use in CI/non-interactive runs |
| `--no-fix` | Skip the debug-fix stage entirely (only run the 7-step verification pipeline) |
| `--no-email` | Do not send the debug report by email |

> The buggy `example/counter.v` shows the two intentional bugs (reset to `8'h01`, increment even when `load` is high) - they are planted for the agent to find, not real defects.

---

## Workflow

```
RTL File
    │
    ▼
RTL Parser
    │
    ▼
Compile (Icarus Verilog)
    │
    ▼
Simulation
    │
    ▼
Log Analysis
    │
    ▼
AI RTL Analysis
    │
    ▼
AI Testbench Analysis
    │
    ▼
Verification Report
    │
    ▼
Debug-Fix Stage (if RTL error found)
    │  1. LLM: structured root-cause analysis (JSON)
    │  2. LLM: minimal code patch (JSON)
    │  3. Human approval (interactive y/N, or --yes)
    │  4. Apply patch -> rerun simulation
    │  5. Save reports/debug_report.md -> email (optional)
    ▼
Debug Report (reports/debug_report.md)
```

---

## Example Output

The generated report contains:

- RTL Information
- Compile Status
- Simulation Status
- Compile Errors & Warnings
- RTL Design Analysis
- Testbench Analysis
- Missing Edge Cases
- Verification Conclusion

If the pipeline finds an error in the RTL, the debug-fix stage additionally produces:

- `reports/debug_report.md`: root-cause analysis, risk level, patch proposal, before/after simulation status, backup path (`reports/backups/`), and the email status

## Debug & Auto-Fix Stage

Run automatically after the verification pipeline when an RTL error is found (compilation failure, simulation failure, log errors, or static RTL findings):

1. The LLM generates a structured root-cause analysis (JSON: root cause, evidence, risk).
2. The LLM generates a minimal code patch (JSON: exact old lines -> new lines).
3. The proposal is printed to the console and the run **pauses for human approval** (`[y/N]`). Use `--yes` to auto-approve, or `--no-fix` to skip the stage entirely.
4. On approval the patch is applied to the RTL file (a timestamped backup is saved under `reports/backups/`), the simulation is re-run, and a full debug report is saved to `reports/debug_report.md`.
5. The debug report is also sent by email when `EMAIL_SENDER` / `EMAIL_PASSWORD` / `EMAIL_RECEIVER` are configured (skip with `--no-email` or by leaving email unset).

The patch is only applied to the RTL file - never to the testbench - and a timestamped backup is always kept.

---

## Tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/
```

The suite covers the debug-fix internals: JSON extraction, patch validation/application, backup creation, error detection and the report builder. It runs without a Groq API key, Icarus Verilog, or LangChain installed.

---

## Future Improvements

- Verilator Support
- UVM Testbench Analysis
- CDC Analysis
- Waveform Analysis
- (done) AI-generated RTL Fixes - see the Debug & Auto-Fix stage

---

