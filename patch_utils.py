"""
patch_utils.py

Pure, dependency-free helpers for the debug-fix stage:

  - detect_error_reason()   decide whether an RTL error was found
  - extract_json()          robustly find a JSON object inside LLM output
  - parse_root_cause()      validate the structured root-cause output
  - parse_patch()           validate the structured patch output
  - apply_patch_to_text()   apply a patch with an exact-match guarantee
  - apply_patch_to_file()   apply a patch to a file (with timestamped backup)
  - build_debug_report()    render the full debug report (Markdown)

Everything here is deterministic and testable without Groq/LangChain.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path


# ------------------------------------------------------------------
# Error detection
# ------------------------------------------------------------------

def detect_error_reason(state):
    """
    Decide whether the pipeline found an RTL error.

    Returns:
        (trigger: bool, reason: str)
        trigger is True when compilation failed, simulation failed,
        the log analysis found errors, or RTL static findings exist.
    """
    sim = state.get("simulation_result") or {}
    log = state.get("log_analysis") or {}

    compile_status = sim.get("compile_status")
    simulation_status = sim.get("simulation_status")

    c_errs = log.get("compile_error_count", 0) or 0
    s_errs = log.get("simulation_error_count", 0) or 0

    findings = (state.get("rtl_analysis") or {}).get("findings") or []

    if compile_status and compile_status != "PASS":
        return True, f"Compilation failed (compile_status = {compile_status})"

    if simulation_status and simulation_status != "PASS":
        return True, f"Simulation failed (simulation_status = {simulation_status})"

    if c_errs > 0 or s_errs > 0:
        return True, (
            f"Log analysis found {c_errs} compile error(s) "
            f"and {s_errs} simulation error(s)"
        )

    if findings:
        return True, f"RTL analysis found {len(findings)} issue(s): {'; '.join(str(f) for f in findings)}"

    return False, ""


# ------------------------------------------------------------------
# JSON extraction from LLM output
# ------------------------------------------------------------------

def extract_json(text):
    """
    Extract the first balanced {...} JSON object from arbitrary LLM output.

    Handles markdown code fences, prose before/after the JSON, strings
    containing braces, and escaped characters inside strings.

    Returns the JSON string, or None if no balanced object was found.
    """
    if not text:
        return None

    text = text.strip()

    # Strip a single markdown code fence around the whole payload if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


def _parse_json_object(raw):
    """Parse a JSON string, tolerating a trailing prose tail."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def parse_root_cause(text):
    """
    Parse the LLM root-cause response into a validated dict.

    Returns:
        {"root_cause": str, "evidence": str, "risk": "LOW"|"MEDIUM"|"HIGH"|"UNKNOWN"}

    Raises ValueError if no valid structured object can be extracted.
    """
    raw = extract_json(text)
    obj = _parse_json_object(raw)
    if not isinstance(obj, dict):
        raise ValueError("LLM response did not contain a valid JSON object")

    root_cause = _safe_str(obj.get("root_cause"))
    evidence = _safe_str(obj.get("evidence"))
    risk = (_safe_str(obj.get("risk")) or "UNKNOWN").upper()
    if risk not in ("LOW", "MEDIUM", "HIGH"):
        risk = "UNKNOWN"

    if not root_cause:
        raise ValueError("LLM response is missing 'root_cause'")

    return {
        "root_cause": root_cause,
        "evidence": evidence,
        "risk": risk,
    }


def parse_patch(text):
    """
    Parse the LLM patch response into a validated list of patch entries.

    Returns:
        [{"old_lines": [str, ...], "new_lines": [str, ...]}, ...]

    Raises ValueError if no valid structured object can be extracted or
    the patch is empty/invalid.
    """
    raw = extract_json(text)
    obj = _parse_json_object(raw)
    if not isinstance(obj, dict):
        raise ValueError("LLM response did not contain a valid JSON object")

    patch = obj.get("patch")
    if not isinstance(patch, list) or not patch:
        raise ValueError("LLM response is missing a non-empty 'patch' list")

    valid = []
    for entry in patch:
        if not isinstance(entry, dict):
            continue
        old_lines = entry.get("old_lines", entry.get("old"))
        new_lines = entry.get("new_lines", entry.get("new"))
        if not isinstance(old_lines, list) or not isinstance(new_lines, list):
            continue
        old_lines = [line for line in old_lines if isinstance(line, str)]
        new_lines = [line for line in new_lines if isinstance(line, str)]
        if not old_lines:
            continue
        joined_old = "\n".join(old_lines)
        joined_new = "\n".join(new_lines)
        if not joined_old.strip():
            continue
        if joined_old == joined_new:
            continue
        valid.append({"old_lines": old_lines, "new_lines": new_lines})

    if not valid:
        raise ValueError("LLM response contained no valid patch entries")

    return valid


def _safe_str(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value if v is not None).strip()
    if value is None:
        return ""
    return str(value).strip()


# ------------------------------------------------------------------
# Patch application
# ------------------------------------------------------------------

class PatchError(Exception):
    """Raised when a patch cannot be applied safely."""
    pass


def count_occurrences(text, needle):
    """Count non-overlapping occurrences of needle in text."""
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        count += 1
        start = idx + 1
    return count


def validate_patch(file_text, patch):
    """
    Verify each patch entry: old lines must occur EXACTLY ONCE.

    Raises PatchError with a descriptive message otherwise.
    """
    for index, entry in enumerate(patch):
        old_text = "\n".join(entry["old_lines"])
        if not old_text.strip():
            raise PatchError(f"Patch entry {index}: old_lines is empty")

        occurrences = count_occurrences(file_text, old_text)
        if occurrences == 0:
            raise PatchError(
                f"Patch entry {index}: old_lines were not found in the RTL file. "
                "The file may have changed since the proposal was generated, or the proposed patch is stale.\n"
                f"Old lines:\n{old_text}"
            )
        if occurrences > 1:
            raise PatchError(
                f"Patch entry {index}: old_lines match {occurrences} locations. "
                "Aborting to avoid an ambiguous patch."
            )


def apply_patch_to_text(file_text, patch):
    """
    Apply the patch to file_text. Validates first; raises PatchError.

    Returns the new file content (LF newlines).
    """
    validate_patch(file_text, patch)

    result = file_text
    for entry in patch:
        old_text = "\n".join(entry["old_lines"])
        new_text = "\n".join(entry["new_lines"])
        result = result.replace(old_text, new_text, 1)

    return result


def apply_patch_to_file(rtl_path, patch, reports_dir="reports", timestamp=None):
    """
    Apply patch to a file, creating a timestamped backup first.

    Returns:
        (new_content, backup_path)
    """
    rtl_path = Path(rtl_path)
    text = rtl_path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n")

    new_text = apply_patch_to_text(text, patch)

    ts = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(reports_dir) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{rtl_path.stem}-{ts}.bak"

    backup_path.write_text(text, encoding="utf-8")
    rtl_path.write_text(new_text, encoding="utf-8")

    return new_text, backup_path


# ------------------------------------------------------------------
# Debug report
# ------------------------------------------------------------------

def _format_patch_block(patch):
    lines = []
    for index, entry in enumerate(patch):
        lines.append(f"### Patch entry {index + 1}")
        lines.append("**Before:**")
        lines.append("```verilog")
        lines.extend(entry["old_lines"])
        lines.append("```")
        lines.append("**After:**")
        lines.append("```verilog")
        lines.extend(entry["new_lines"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines).strip()


def build_debug_report(
    *,
    rtl_file,
    tb_file,
    error_reason,
    before=None,
    root_cause=None,
    patch=None,
    approval_status="pending",
    backup_path=None,
    after=None,
    mail_status=None,
    timestamp=None,
):
    """Render the full debug report (Markdown)."""
    before = before or {}
    after = after or {}
    root_cause = root_cause or {}
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    status_line = {
        "applied": "✅ Patch applied",
        "not_approved": "⛔ Patch NOT applied — human declined",
        "llm_error": "❌ Root-cause/patch generation failed",
        "patch_apply_failed": "❌ Patch could not be applied",
        "skipped": "⏭️ No RTL error detected — skipped",
    }.get(approval_status, approval_status)

    report = f"""# RTL Debug & Fix Report

**Status:** {status_line}
**RTL File:** `{rtl_file}`
**Testbench:** `{tb_file}`
**Timestamp (UTC):** `{ts}`

---

## What Failed

{error_reason or "n/a"}

### Before the fix

- Compile status: **{before.get('compile_status', 'UNKNOWN')}**
- Simulation status: **{before.get('simulation_status', 'UNKNOWN')}**
- Compile log: `{before.get('compile_log', 'n/a')}`
- Simulation log: `{before.get('simulation_log', 'n/a')}`

---

## Root-Cause Analysis (LLM)

**Risk:** {root_cause.get('risk', 'UNKNOWN')}

{root_cause.get('root_cause', '(no root-cause analysis available)')}

### Evidence

{root_cause.get('evidence', '(none)')}

---

## Proposed Patch

{_format_patch_block(patch or []) if patch else "(no valid patch was produced)"}

---

## After the Fix

- Compile status: **{after.get('compile_status', 'NOT RUN')}**
- Simulation status: **{after.get('simulation_status', 'NOT RUN')}**
- Compile log: `{after.get('compile_log', 'n/a')}`
- Simulation log: `{after.get('simulation_log', 'n/a')}`

Verification outcome after the fix:
**{after.get('simulation_status', 'UNKNOWN')}**

---

## Backup

{f'`{backup_path}`' if backup_path else '(no backup created)'}

## Email

{mail_status or '(not sent)'}
"""

    return report


def build_debug_email_body(debug_report_text):
    """Plain-text body for the debug-report email.

    The debug report already carries its own header; we just append a footer.
    """
    return (
        debug_report_text.rstrip()
        + "\n\n====================================\n"
        "Generated by RTL Verification Agent's debug-fix stage\n"
    )
