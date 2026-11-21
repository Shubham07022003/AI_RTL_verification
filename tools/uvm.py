"""
tools/uvm.py

UVM testbench detection and facts extraction.

Icarus Verilog and Verilator do not support UVM testbenches, so this module
is the UVM-aware side of the verification pipeline. It is heuristic by
design - it does NOT attempt to be a full SystemVerilog parser. It extracts
a small, honest "UVM facts" summary that is fed into the testbench-analysis
prompt, so the agent does not misjudge a UVM testbench using plain-Verilog
facts (e.g. "no $monitor / no reset testing / no scoreboard").
"""

import re
from pathlib import Path


def _strip_comments(text):
    """Remove /* ... */ and // comments so extraction ignores comment noise."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


# ------------------------------------------------------------------
# Regexes
# ------------------------------------------------------------------

_UVM_MACRO_RE = re.compile(r"`\s*uvm_[\w]+")
_UVM_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+(uvm_\w+)")
_UVM_MODULE_RE = re.compile(r"\bmodule\s+(\w+)(?:\s+extends\s+(uvm_\w+))?")
_UVM_EXTENDS_RE = re.compile(r"\bextends\s+(uvm_\w+)")
_UVM_TEST_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+uvm_test\b")
_UVM_SCOREBOARD_RE = re.compile(r"\buvm_scoreboard\b", re.IGNORECASE)
_UVM_CONFIG_DB_RE = re.compile(r"\buvm_config_db\b")
_UVM_SEQUENCE_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+uvm_sequence\b")
_UVM_ASSERTION_RE = re.compile(r"\b(?:assert|assertproperty)\b", re.IGNORECASE)
_UVM_PHASE_RE = re.compile(
    r"\b(build_phase|end_of_elaboration|start_of_simulation|pre_monitor|extract_phase|"
    r"post_test|finish_of_elaboration|pre_reset|post_reset|run_phase|"
    r"start_of_source|pre_reset|pre_monitor)\s*\("
)
_DUT_TYPE_ID_RE = re.compile(r"\b([a-zA-Z_]\w*)::type_id::create\(\s*[\"']dut[\"']")
_DUT_INSTANCE_RE = re.compile(r"\b([a-zA-Z_]\w*)\s+(\w*dut\w*)\s*\(")


def is_uvm_tb(text):
    """
    Return True if the given testbench text looks like a UVM testbench.
    """
    if not text:
        return False
    text = _strip_comments(text)
    return bool(
        _UVM_MACRO_RE.search(text)
        or _UVM_CLASS_RE.search(text)
        or re.search(r"\bextends\s+uvm_", text)
        or re.search(r"\buvm_test\b|\buvm_env\b|\buvm_test_top\b", text)
    )


def detect_testbench_type(tb_file):
    """
    Detect whether the testbench file is a UVM testbench.

    Returns:
        "UVM" | "PLAIN" | "UNKNOWN" (missing/unreadable file)
    """
    path = Path(tb_file)
    if not path.exists():
        return "UNKNOWN"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "UNKNOWN"
    return "UVM" if is_uvm_tb(text) else "PLAIN"


def extract_uvm_facts(tb_code, tb_file=None):
    """
    Extract a small, honest 'UVM facts' summary from testbench source.

    Heuristic extraction - never claims more than the code shows.
    Comments are stripped before matching.
    """
    if not tb_code:
        return {"testbench_type": "UNKNOWN"}
    tb_code = _strip_comments(tb_code)

    facts = {
        "testbench_type": "UVM",
        "top_module": None,
        "top_extends": None,
        "top_test_class": None,
        "components_extended": sorted(set(_UVM_EXTENDS_RE.findall(tb_code))),
        "phases_overridden": sorted(set(_UVM_PHASE_RE.findall(tb_code))),
        "sequences_count": len(_UVM_SEQUENCE_RE.findall(tb_code)),
        "uvm_config_db": bool(_UVM_CONFIG_DB_RE.search(tb_code)),
        "scoreboard_present": bool(
            _UVM_SCOREBOARD_RE.search(tb_code)
            or re.search(r"\bscoreboard\b", tb_code, re.IGNORECASE)
        ),
        "monitor_present": bool(re.search(r"\buvm_monitor\b", tb_code)),
        "driver_present": bool(re.search(r"\buvm_driver\b", tb_code)),
        "sequencer_present": bool(re.search(r"\buvm_sequencer\b", tb_code)),
        "assertions_present": bool(_UVM_ASSERTION_RE.search(tb_code)),
        "dut_module": None,
        "dut_instance": None,
    }

    module_match = _UVM_MODULE_RE.search(tb_code)
    if module_match:
        facts["top_module"] = module_match.group(1)
        facts["top_extends"] = module_match.group(2)

    test_class = _UVM_TEST_CLASS_RE.search(tb_code)
    if test_class:
        facts["top_test_class"] = test_class.group(1)

    dut = _DUT_TYPE_ID_RE.search(tb_code)
    if dut:
        facts["dut_module"] = dut.group(1)
        facts["dut_instance"] = "dut"
    else:
        dut = _DUT_INSTANCE_RE.search(tb_code)
        if dut:
            facts["dut_module"] = dut.group(1)
            facts["dut_instance"] = dut.group(2)

    return facts
