# AI RTL Verification Agent

An AI-powered RTL Verification framework that analyzes Verilog RTL designs and testbenches using **LangChain**, **llama-3.3-70b-versatile**, and **Icarus Verilog**.

The agent automatically compiles, simulates, analyzes RTL functionality, evaluates testbench quality, and generates a verification report.

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

---

## Project Structure

```
rtl_verify/
│
├── agent.py
├── rtl_verify.py
├── state.py
├── llm.py
├── requirements.txt
├── .env
│
├── examples/
│   ├── fifo.v
│   └── tb_fifo.v
│
├── tools/
│   ├── rtl_parser.py
│   ├── simulation.py
│   ├── log_analyzer.py
│   ├── rtl_analysis.py
│   ├── tb_analysis.py
│   ├── tb_checker.py
│   ├── report.py
│   └── mail.py
│
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

## Install Icarus Verilog (Windows)

Download and install:

https://bleyer.org/icarus/

After installation, update the paths in:

```python
tools/simulation.py
```

```python
IVERILOG_PATH = r"C:\iverilog\bin\iverilog.exe"
VVP_PATH = r"C:\iverilog\bin\vvp.exe"
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
python rtl_verify.py examples/fifo.v examples/tb_fifo.v
```

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

---

## Future Improvements

- Verilator Support
- UVM Testbench Analysis
- CDC Analysis
- Waveform Analysis
- AI-generated RTL Fixes
- AI-generated Testbenches
- Coverage Analysis
- Multi-Agent Verification

---

