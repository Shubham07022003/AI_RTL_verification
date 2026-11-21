"""
tools/simulation.py

Compile and simulate RTL using Icarus Verilog.

Icarus Verilog is resolved in this order:
  1. IVERILOG_PATH / VVP_PATH environment variables
  2. iverilog / vvp on PATH (via shutil.which)

Both compile and simulation steps run with a timeout so a hung
testbench cannot block the agent indefinitely.
"""

import os
import shutil
import subprocess
from pathlib import Path

try:
    from langchain.tools import tool
except Exception:
    def tool(func):
        return func

from state import verification_state


# --------------------------------------------------
# Icarus Verilog resolution
# --------------------------------------------------

IVERILOG_PATH = os.environ.get("IVERILOG_PATH") or shutil.which("iverilog")
VVP_PATH = os.environ.get("VVP_PATH") or shutil.which("vvp")

SIM_TIMEOUT_SECONDS = int(os.environ.get("SIM_TIMEOUT_SECONDS", "120"))


# --------------------------------------------------
# Core logic (plain function - reused by the debug-fix stage)
# --------------------------------------------------

def compile_and_simulate(rtl_file, tb_file, temp_dir="temp", timeout=None):
    """
    Compile rtl_file + tb_file with Icarus Verilog and (if compilation
    succeeds) simulate the result.

    Returns a plain dict:
        {
            "compile_status": "PASS" | "FAIL",
            "simulation_status": "PASS" | "FAIL" | "NOT_RUN",
            "compile_log": str,
            "simulation_log": str | None,
            "compile_output": str,
            "simulation_output": str | None,
        }
    """
    if timeout is None:
        timeout = SIM_TIMEOUT_SECONDS

    rtl_file = str(rtl_file)
    tb_file = str(tb_file)

    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    compile_log = temp_dir / "compile.log"
    sim_log = temp_dir / "simulation.log"
    executable = temp_dir / "sim.out"

    result = {
        "compile_status": "FAIL",
        "simulation_status": "NOT_RUN",
        "compile_log": str(compile_log),
        "simulation_log": None,
        "compile_output": "",
        "simulation_output": None,
    }

    # ------------------------------------------
    # Icarus Verilog availability check
    # ------------------------------------------

    if not IVERILOG_PATH or not VVP_PATH:
        missing = "iverilog" if not IVERILOG_PATH else "vvp"
        compile_output = (
            f"ERROR: {missing} not found. Set the IVERILOG_PATH and VVP_PATH "
            "environment variables (see README) or make sure iverilog and vvp "
            "are on your PATH."
        )
        compile_log.write_text(compile_output, encoding="utf-8")
        result["compile_output"] = compile_output
        return result

    # ------------------------------------------
    # Compile
    # ------------------------------------------

    compile_cmd = [
        IVERILOG_PATH,
        "-g2012",
        "-o",
        str(executable),
        rtl_file,
        tb_file,
    ]

    try:
        compile_process = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        compile_output = (
            compile_process.stdout + compile_process.stderr
        )

        compile_log.write_text(compile_output, encoding="utf-8")

        result["compile_status"] = (
            "PASS" if compile_process.returncode == 0 else "FAIL"
        )

    except subprocess.TimeoutExpired:
        compile_output = f"Compilation timed out after {timeout} seconds."
        compile_log.write_text(compile_output, encoding="utf-8")
        result["compile_status"] = "FAIL"
        result["compile_output"] = compile_output
        return result

    except Exception as e:
        compile_output = str(e)
        compile_log.write_text(compile_output, encoding="utf-8")
        result["compile_status"] = "FAIL"
        result["compile_output"] = compile_output
        return result

    result["compile_output"] = compile_output

    # ------------------------------------------
    # Stop if compile failed
    # ------------------------------------------

    if result["compile_status"] == "FAIL":
        return result

    # ------------------------------------------
    # Simulation
    # ------------------------------------------

    try:
        sim_cmd = [VVP_PATH, str(executable)]

        sim_process = subprocess.run(
            sim_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        sim_output = (
            sim_process.stdout + sim_process.stderr
        )

        sim_log.write_text(sim_output, encoding="utf-8")

        result["simulation_status"] = (
            "PASS" if sim_process.returncode == 0 else "FAIL"
        )

    except subprocess.TimeoutExpired:
        sim_output = f"Simulation timed out after {timeout} seconds."
        sim_log.write_text(sim_output, encoding="utf-8")
        result["simulation_status"] = "FAIL"

    except Exception as e:
        sim_output = str(e)
        sim_log.write_text(sim_output, encoding="utf-8")
        result["simulation_status"] = "FAIL"

    result["simulation_output"] = sim_output
    result["simulation_log"] = str(sim_log)

    return result


# --------------------------------------------------
# Tool (used by the agent)
# --------------------------------------------------

@tool
def run_simulation() -> str:
    """
    Compile and simulate RTL and Testbench
    using Icarus Verilog.
    """

    rtl_file = verification_state.get("rtl_file")
    tb_file = verification_state.get("tb_file")

    if not rtl_file or not tb_file:
        return "ERROR: RTL/Testbench file paths not found in state."

    result = compile_and_simulate(rtl_file, tb_file)

    verification_state["simulation_result"] = result

    # ------------------------------------------
    # Tool summary
    # ------------------------------------------

    if result["compile_status"] == "FAIL":
        return (
            "Compilation Failed\n\n"
            f"See log: {result['compile_log']}"
        )

    summary = f"""
Simulation Complete

Compile Status:
{result['compile_status']}

Simulation Status:
{result['simulation_status']}

Compile Log:
{result['compile_log']}

Simulation Log:
{result['simulation_log']}
"""

    return summary.strip()
