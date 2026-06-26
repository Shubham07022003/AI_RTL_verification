"""
tools/simulation.py

Compile and simulate RTL using Icarus Verilog.
"""

import subprocess
from pathlib import Path

try:
    from langchain.tools import tool
except Exception:
    def tool(func):
        return func

from state import verification_state


# --------------------------------------------------
# Icarus Verilog Paths
# --------------------------------------------------

IVERILOG_PATH = r"C:\iverilog\bin\iverilog.exe"
VVP_PATH = r"C:\iverilog\bin\vvp.exe"


# --------------------------------------------------
# Tool
# --------------------------------------------------

@tool
def run_simulation() -> str:
    """
    Compile and simulate RTL and Testbench
    using Icarus Verilog.
    """

    rtl_file = verification_state["rtl_file"]
    tb_file = verification_state["tb_file"]

    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)

    compile_log = temp_dir / "compile.log"
    sim_log = temp_dir / "simulation.log"

    executable = temp_dir / "sim.out"

    # ------------------------------------------
    # Compile
    # ------------------------------------------

    compile_cmd = [
        IVERILOG_PATH,
        "-g2012",
        "-o",
        str(executable),
        rtl_file,
        tb_file
    ]

    try:

        compile_process = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True
        )

        compile_output = (
            compile_process.stdout +
            compile_process.stderr
        )

        compile_log.write_text(
            compile_output,
            encoding="utf-8"
        )

        compile_status = (
            "PASS"
            if compile_process.returncode == 0
            else "FAIL"
        )

    except Exception as e:

        compile_status = "FAIL"

        compile_output = str(e)

        compile_log.write_text(
            compile_output,
            encoding="utf-8"
        )

        verification_state["simulation_result"] = {
            "compile_status": compile_status,
            "simulation_status": "NOT_RUN",
            "compile_log": str(compile_log),
            "simulation_log": None
        }

        return f"Compilation Failed\n\n{compile_output}"

    # ------------------------------------------
    # Stop if compile failed
    # ------------------------------------------

    if compile_status == "FAIL":

        verification_state["simulation_result"] = {
            "compile_status": compile_status,
            "simulation_status": "NOT_RUN",
            "compile_log": str(compile_log),
            "simulation_log": None
        }

        return (
            "Compilation Failed\n\n"
            f"See log: {compile_log}"
        )

    # ------------------------------------------
    # Simulation
    # ------------------------------------------

    try:

        sim_cmd = [
            VVP_PATH,
            str(executable)
        ]

        sim_process = subprocess.run(
            sim_cmd,
            capture_output=True,
            text=True
        )

        sim_output = (
            sim_process.stdout +
            sim_process.stderr
        )

        sim_log.write_text(
            sim_output,
            encoding="utf-8"
        )

        simulation_status = (
            "PASS"
            if sim_process.returncode == 0
            else "FAIL"
        )

    except Exception as e:

        simulation_status = "FAIL"

        sim_output = str(e)

        sim_log.write_text(
            sim_output,
            encoding="utf-8"
        )

    # ------------------------------------------
    # Save State
    # ------------------------------------------

    verification_state["simulation_result"] = {

        "compile_status": compile_status,

        "simulation_status": simulation_status,

        "compile_log": str(compile_log),

        "simulation_log": str(sim_log),

        "compile_output": compile_output,

        "simulation_output": sim_output
    }

    # ------------------------------------------
    # Return Summary
    # ------------------------------------------

    summary = f"""
Simulation Complete

Compile Status:
{compile_status}

Simulation Status:
{simulation_status}

Compile Log:
{compile_log}

Simulation Log:
{sim_log}
"""

    return summary.strip()