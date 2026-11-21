"""
tools/uvm_backend.py

UVM simulation backend.

UVM testbenches require a UVM-capable simulator (VCS, Xcelium, Questa,
riviera, etc.) - Icarus Verilog and Verilator do not support UVM. This
backend invokes your configured simulator in place of the plain Icarus
path. Configure it via environment variables (see README):

  UVM_SIM_CMD   single command that compiles AND runs the testbench
                (e.g. a VCS-style invocation)
  OR
  UVM_COMPILE_CMD  +  UVM_RUN_CMD   two-step style (e.g. Questa vlog / vsim)

Placeholders supported in the templates: {RTL}, {TB}, {TEMP_DIR}

Both modes return the same result dict as the Icarus backend so the rest
of the pipeline (log analysis, report, debug-fix stage) works unchanged.
"""

import os
import subprocess
from pathlib import Path

SIM_TIMEOUT_SECONDS = int(os.environ.get("SIM_TIMEOUT_SECONDS", "120"))

UVM_SIM_CMD = os.environ.get("UVM_SIM_CMD", "").strip()
UVM_COMPILE_CMD = os.environ.get("UVM_COMPILE_CMD", "").strip()
UVM_RUN_CMD = os.environ.get("UVM_RUN_CMD", "").strip()


def _resolve_uvm_config():
    uvm_sim_cmd = os.environ.get("UVM_SIM_CMD", "").strip()
    compile_cmd = os.environ.get("UVM_COMPILE_CMD", "").strip()
    run_cmd = os.environ.get("UVM_RUN_CMD", "").strip()

    if uvm_sim_cmd:
        return {"mode": "single", "command": uvm_sim_cmd}
    if compile_cmd and run_cmd:
        return {"mode": "two-step", "compile_command": compile_cmd, "run_command": run_cmd}
    if compile_cmd or run_cmd:
        return {"mode": "misconfigured", "command": compile_cmd or run_cmd}
    return None


def build_uvm_command(template, rtl_file, tb_file, temp_dir):
    """Substitute the supported placeholders into a simulator template."""
    return (
        template.replace("{RTL}", str(rtl_file))
                .replace("{TB}", str(tb_file))
                .replace("{TEMP_DIR}", str(temp_dir))
    )


def _run_command(command, timeout):
    """
    Run a simulator command string.

    Returns:
        (exit_code, output) where exit_code is None on timeout/error.
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + (e.stderr or "")
        output += f"\nCommand timed out after {timeout} seconds."
        return None, output
    except Exception as e:
        return None, str(e)


def _default_runner(command, timeout):
    return _run_command(command, timeout)


def run_uvm_simulation(rtl_file, tb_file, temp_dir="temp", timeout=None, _runner=None):
    """
    Compile and run a UVM testbench with the configured UVM simulator.

    Returns the same result dict as the Icarus backend:
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

    runner = _runner or _default_runner

    rtl_file = str(rtl_file)
    tb_file = str(tb_file)

    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    compile_log = temp_dir / "compile.log"
    sim_log = temp_dir / "simulation.log"

    result = {
        "compile_status": "FAIL",
        "simulation_status": "NOT_RUN",
        "compile_log": str(compile_log),
        "simulation_log": None,
        "compile_output": "",
        "simulation_output": None,
    }

    config = _resolve_uvm_config()

    # ------------------------------------------
    # UVM simulator availability check
    # ------------------------------------------

    if config is None:
        compile_output = (
            "ERROR: UVM testbench detected but no UVM simulator is configured. "
            "Set UVM_SIM_CMD (single compile+run command) or "
            "UVM_COMPILE_CMD + UVM_RUN_CMD (two-step), e.g. "
            "UVM_SIM_CMD='vcs -full64 -sverilog -l {TEMP_DIR}/compile.log {RTL} {TB} && simv' "
            "(see README)."
        )
        compile_log.write_text(compile_output, encoding="utf-8")
        result["compile_output"] = compile_output
        return result

    if config["mode"] == "misconfigured":
        compile_output = (
            "ERROR: UVM two-step mode requires BOTH UVM_COMPILE_CMD and UVM_RUN_CMD "
            "(or set UVM_SIM_CMD for a single compile+run command)."
        )
        compile_log.write_text(compile_output, encoding="utf-8")
        result["compile_output"] = compile_output
        return result

    # ------------------------------------------
    # Single-command mode: compile + run in one step
    # ------------------------------------------

    if config["mode"] == "single":
        command = build_uvm_command(config["command"], rtl_file, tb_file, temp_dir)
        exit_code, output = runner(command, timeout)

        compile_log.write_text(output, encoding="utf-8")
        sim_log.write_text(output, encoding="utf-8")

        result["compile_output"] = output
        result["compile_status"] = "PASS" if exit_code == 0 else "FAIL"
        result["simulation_output"] = output
        result["simulation_log"] = str(sim_log)
        result["simulation_status"] = "PASS" if exit_code == 0 else "FAIL"
        return result

    # ------------------------------------------
    # Two-step mode: compile, then run
    # ------------------------------------------

    compile_command = build_uvm_command(config["compile_command"], rtl_file, tb_file, temp_dir)
    compile_exit, compile_output = runner(compile_command, timeout)

    compile_log.write_text(compile_output, encoding="utf-8")
    result["compile_output"] = compile_output
    result["compile_status"] = "PASS" if compile_exit == 0 else "FAIL"

    if compile_exit != 0:
        return result

    run_command = build_uvm_command(config["run_command"], rtl_file, tb_file, temp_dir)
    run_exit, run_output = runner(run_command, timeout)

    sim_log.write_text(run_output, encoding="utf-8")
    result["simulation_output"] = run_output
    result["simulation_log"] = str(sim_log)
    result["simulation_status"] = "PASS" if run_exit == 0 else "FAIL"

    return result
