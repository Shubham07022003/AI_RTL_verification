import argparse
import itertools
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import collections
import multiprocessing
import textwrap
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait
sys.setrecursionlimit(3000)
_shutdown_requested = False

# Absolute path of this script itself
try:
    _SELF_PATH = os.path.abspath(__file__)
except NameError:
    _SELF_PATH = None

def _handle_shutdown_signal(signum, frame):
    global _shutdown_requested
    if not _shutdown_requested:
        _shutdown_requested = True
        print("\n\n[!] Ctrl+C received - terminating all workers immediately. "
              "Files a worker was actively writing may be left partially written.", flush=True)
    else:
        print("\n[!] Ctrl+C received again - exiting immediately.", flush=True)
        os._exit(1)

def _worker_init():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

MACRO_SENS_RE = re.compile(rb'^\s*`\w+\b')
STRING_RE = re.compile(rb'"(?:\\.|[^"\\])*"')
BLOCK_COMMENT_RE = re.compile(rb'/\*.*?\*/', re.DOTALL)
LINE_COMMENT_RE = re.compile(rb'//[^\n]*')
DEFINE_RE = re.compile(rb'`define\b(?:[^\n]*\\\s*\n)*[^\n]*')

COMBINED_MASK_RE = re.compile(
    rb'"(?:\\.|[^"\\])*"'                                 
    rb'|/\*[^*]*\*+(?:[^/*][^*]*\*+)*/'                   
    rb'|//[^\n]*'                                         
    rb'|`define\b(?:[^\n]*\\\s*\n)*[^\n]*'                
    rb'|\(\*[^*]*\*+(?:[^)*][^*]*\*+)*\)'                 
    rb'|\\[^\s]+'                                     
    rb'|\b(?:unique0|unique|priority)\b',                 
    re.DOTALL
)

_WHITESPACE_BYTES = frozenset(b' \t\n\r\x0b\x0c')
_LP, _RP, _LB, _RB, _LC, _RC, _SEMI, _AT, _STAR = (ord(c) for c in '()[]{};@*')

_KW_IF_RE = re.compile(rb'\bif\b')
_KW_FOR_RE = re.compile(rb'\b(for|foreach)\b')
_KW_WHILE_RE = re.compile(rb'\bwhile\b')
_KW_REPEAT_RE = re.compile(rb'\brepeat\b')
_KW_FOREVER_RE = re.compile(rb'\bforever\b')
_KW_ELSE_RE = re.compile(rb'\belse\b')
_KW_DO_RE = re.compile(rb'\bdo\b')
_KW_ASSERT_RE = re.compile(rb'\b(assert|assume|cover|restrict|wait_order|expect)\b')

_MACRO_OPERATOR_KW_RE = re.compile(rb'\b(inside|matches|dist|with)\b')

STATEMENT_LABEL_RE = re.compile(rb'(?!(?:begin|end|fork|join|join_any|join_none)\b)(?:[`a-zA-Z_][`a-zA-Z0-9_]*|\\\S+)\s*:')
END_BLOCK_LABEL_RE = re.compile(rb'\s*:\s*(?:[`a-zA-Z_][`a-zA-Z0-9_]*|\\\S+)')
OPTIONAL_BLOCK_LABEL_RE = re.compile(rb'\s*:\s*(?:[`a-zA-Z_][`a-zA-Z0-9_]*|\\\S+)?')
SEMI_RE = re.compile(rb';')

KEYWORDS = {
    b'module', b'endmodule', b'initial', b'always', b'always_ff', b'always_comb',
    b'always_latch', b'assign', b'begin', b'end', b'if', b'else', b'for', b'while',
    b'do', b'case', b'endcase', b'generate', b'endgenerate', b'parameter',
    b'localparam', b'logic', b'wire', b'reg', b'input', b'output', b'inout',
    b'type', b'bit', b'byte', b'shortint', b'int', b'longint', b'integer', b'time',
    b'struct', b'enum', b'typedef', b'function', b'endfunction', b'task', b'endtask',
    b'class', b'endclass', b'property', b'endproperty', b'sequence', b'endsequence',
    b'disable', b'iff', b'assert', b'assume', b'cover', b'expect', b'restrict',
    b'wait', b'wait_order', b'posedge', b'negedge', b'edge', b'return', b'break', 
    b'continue', b'default', b'import', b'export', b'pure', b'virtual', b'fork', 
    b'join', b'join_any', b'join_none', b'string', b'event', b'chandle', b'void', 
    b'real', b'shortreal', b'super', b'this', b'null', b'rand', b'randc', b'constraint', 
    b'with', b'inside', b'dist', b'clocking', b'endclocking', b'program', b'endprogram', 
    b'interface', b'endinterface', b'package', b'endpackage', b'config', b'endconfig', 
    b'checker', b'endchecker', b'primitive', b'endprimitive', b'macromodule', b'foreach',
    b'unique', b'unique0', b'priority', b'let', b'matches', b'cross', b'bins'
}

IDENT_RE = re.compile(rb'\b([a-zA-Z_]\w*)\b')
IDENT_AT_POS_RE = re.compile(rb'([a-zA-Z_]\w*)\b')

AST_MACRO_RE = re.compile(rb'`\w+\b')
AST_MACRO_GROUP_RE = re.compile(rb'`([a-zA-Z0-9_]+)\b')
AST_BARE_RE = re.compile(rb'[a-zA-Z_][a-zA-Z0-9_.]*')
AST_WAIT_RE = re.compile(rb'wait\b')
AST_UNIT_RE = re.compile(rb'(s|ms|us|ns|ps|fs|step)\b')

AST_PP_RE = re.compile(rb'`(elsif|else|endif|[a-zA-Z0-9_]*ELSIF|[a-zA-Z0-9_]*ELSE|[a-zA-Z0-9_]*ENDIF)\b', re.IGNORECASE)
AST_PP_SEARCH_RE = re.compile(
    rb'`(ifdef|ifndef|endif|[a-zA-Z0-9_]*IFDEF|[a-zA-Z0-9_]*IFNDEF|[a-zA-Z0-9_]*ENDIF)\b', 
    re.IGNORECASE
)

class ParserContext:
    def __init__(self, code: bytes, global_ctx=None):
        self.assign_macros = set(global_ctx.assign_macros) if global_ctx else set()
        self.always_macros = set(global_ctx.always_macros) if global_ctx else set()
        self.initial_macros = set(global_ctx.initial_macros) if global_ctx else set()
        self.final_macros = set(global_ctx.final_macros) if global_ctx else set()
        
        self.standalone_assign_macros = set(global_ctx.standalone_assign_macros) if global_ctx else set()
        self.standalone_always_macros = set(global_ctx.standalone_always_macros) if global_ctx else set()
        self.standalone_initial_macros = set(global_ctx.standalone_initial_macros) if global_ctx else set()
        self.standalone_final_macros = set(global_ctx.standalone_final_macros) if global_ctx else set()
        
        self.begin_macros = set(global_ctx.begin_macros) if global_ctx else set()
        self.end_macros = set(global_ctx.end_macros) if global_ctx else set()
        self.fork_macros = set(global_ctx.fork_macros) if global_ctx else set()
        self.join_macros = set(global_ctx.join_macros) if global_ctx else set()
        self.case_macros = set(global_ctx.case_macros) if global_ctx else set()
        self.endcase_macros = set(global_ctx.endcase_macros) if global_ctx else set()
        
        if code:
            for m in DEFINE_RE.finditer(code):
                macro_def = m.group(0)
                name_m = re.match(rb'`define\s+([a-zA-Z0-9_]+)', macro_def)
                if not name_m: continue
                name = name_m.group(1)
                
                payload = macro_def[name_m.end():]
                payload = LINE_COMMENT_RE.sub(b'', payload)
                payload = BLOCK_COMMENT_RE.sub(b'', payload)
                payload = STRING_RE.sub(b'', payload)
                
                has_initial = re.search(rb'\binitial\b', payload)
                has_always = re.search(rb'\b(always|always_ff|always_comb|always_latch)\b', payload)
                has_final = re.search(rb'\bfinal\b', payload)
                has_assign = re.search(rb'\bassign\b', payload)
                
                has_begin = re.search(rb'\bbegin\b', payload)
                has_end = re.search(rb'\bend\b', payload)
                has_semi = re.search(rb';', payload)
                
                if has_initial:
                    self.initial_macros.add(name)
                    if (has_begin and has_end) or (has_semi and not has_begin):
                        self.standalone_initial_macros.add(name)
                        
                if has_always:
                    self.always_macros.add(name)
                    if (has_begin and has_end) or (has_semi and not has_begin):
                        self.standalone_always_macros.add(name)
                        
                if has_final:
                    self.final_macros.add(name)
                    if (has_begin and has_end) or (has_semi and not has_begin):
                        self.standalone_final_macros.add(name)
                        
                if has_assign:
                    self.assign_macros.add(name)
                    if has_semi:
                        self.standalone_assign_macros.add(name)
                        
                if has_begin and not has_end:
                    self.begin_macros.add(name)
                elif has_end and not has_begin:
                    self.end_macros.add(name)
                    
                has_fork = re.search(rb'\bfork\b', payload)
                has_join = re.search(rb'\b(join|join_any|join_none)\b', payload)
                if has_fork and not has_join:
                    self.fork_macros.add(name)
                elif has_join and not has_fork:
                    self.join_macros.add(name)
                    
                has_case = re.search(rb'\b(case[xz]?|randcase)\b', payload)
                has_endcase = re.search(rb'\bendcase\b', payload)
                if has_case and not has_endcase:
                    self.case_macros.add(name)
                elif has_endcase and not has_case:
                    self.endcase_macros.add(name)

        def build_re(base, macros, flags=0):
            if not macros: return re.compile(base, flags=flags)
            sorted_macros = sorted(list(macros), key=len, reverse=True)
            alt = b'|'.join(rb'`' + m + rb'\b' for m in sorted_macros)
            return re.compile(base + b'|' + alt, flags=flags)

        self.re_assign = build_re(rb'\bassign\b', self.assign_macros)
        self.re_always = build_re(rb'\b(always|always_ff|always_comb|always_latch)\b', self.always_macros)
        self.re_initial = build_re(rb'\binitial\b', self.initial_macros)
        self.re_final = build_re(rb'\bfinal\b', self.final_macros)

        sorted_begins = sorted(list(self.begin_macros), key=len, reverse=True)
        self.re_kw_begin = build_re(rb'\bbegin\b|`[a-zA-Z0-9_]*BEGIN\b', sorted_begins, re.IGNORECASE)
        
        sorted_forks = sorted(list(self.fork_macros), key=len, reverse=True)
        self.re_kw_fork = build_re(rb'\bfork\b|`[a-zA-Z0-9_]*FORK\b', sorted_forks, re.IGNORECASE)
        
        sorted_cases = sorted(list(self.case_macros), key=len, reverse=True)
        self.re_kw_case = build_re(rb'\b(case[xz]?|randcase)\b|`[a-zA-Z0-9_]*(?<!END)CASE\b', sorted_cases, re.IGNORECASE)

        all_be_macros = sorted(list(self.begin_macros | self.end_macros), key=len, reverse=True)
        if all_be_macros:
            be_alt = b'|'.join(all_be_macros)
            self.re_begin_end_pp = re.compile(
                rb'\b(begin|end|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*BEGIN|[a-zA-Z0-9_]*END|' + be_alt + rb')\b',
                re.IGNORECASE
            )
        else:
            self.re_begin_end_pp = re.compile(
                rb'\b(begin|end|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*BEGIN|[a-zA-Z0-9_]*END)\b',
                re.IGNORECASE
            )

        all_fork_macros = sorted(list(self.fork_macros | self.join_macros), key=len, reverse=True)
        if all_fork_macros:
            fork_alt = b'|'.join(all_fork_macros)
            self.re_fork_pp = re.compile(
                rb'\b(disable\s+fork|wait\s+fork|fork|join|join_any|join_none|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*FORK|[a-zA-Z0-9_]*JOIN(?:_[A-Z0-9_]*)?|' + fork_alt + rb')\b',
                re.IGNORECASE
            )
        else:
            self.re_fork_pp = re.compile(
                rb'\b(disable\s+fork|wait\s+fork|fork|join|join_any|join_none|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*FORK|[a-zA-Z0-9_]*JOIN(?:_[A-Z0-9_]*)?)\b',
                re.IGNORECASE
            )

        all_case_macros = sorted(list(self.case_macros | self.endcase_macros), key=len, reverse=True)
        if all_case_macros:
            case_alt = b'|'.join(all_case_macros)
            self.re_case_pp = re.compile(
                rb'\b(case[xz]?|randcase|endcase|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*CASE|' + case_alt + rb')\b',
                re.IGNORECASE
            )
        else:
            self.re_case_pp = re.compile(
                rb'\b(case[xz]?|randcase|endcase|always|always_ff|always_comb|always_latch|initial|final|module|endmodule)\b'
                rb'|`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*CASE)\b',
                re.IGNORECASE
            )

        self.set_begins = set(m.lower() for m in self.begin_macros)
        self.set_ends = set(m.lower() for m in self.end_macros)
        self.set_forks = set(m.lower() for m in self.fork_macros)
        self.set_joins = set(m.lower() for m in self.join_macros)
        self.set_cases = set(m.lower() for m in self.case_macros)
        self.set_endcases = set(m.lower() for m in self.endcase_macros)

def mask_unwanted_structures(text: bytes) -> bytes:
    def _repl(m):
        val = m.group(0)
        if val.startswith(b'\\'):
            return b'X' * len(val)
        return b' ' * len(val)
    
    masked_text = COMBINED_MASK_RE.sub(_repl, text)

    text_mut = bytearray(masked_text)
    for m in re.finditer(rb'`\w+\s*\(', text_mut):
        p_start = m.end() - 1 
        p_end = _skip_balanced_parens(text_mut, p_start) 
        if p_end > p_start + 1:
            text_mut[p_start+1 : p_end-1] = b' ' * (p_end - p_start - 2)
            
    return bytes(text_mut)

WS_RE = re.compile(rb'\s+')
def _skip_ws_idx(masked: bytes, pos: int) -> int:
    m = WS_RE.match(masked, pos)
    return m.end() if m else pos

def _skip_balanced_parens(text: bytes, start_idx: int) -> int:
    start_idx = _skip_ws_idx(text, start_idx)
    if start_idx >= len(text) or text[start_idx] != _LP:
        return start_idx
    
    depth = 0
    i = start_idx
    n = len(text)
    open_b = b'('
    close_b = b')'
    
    next_open = text.find(open_b, i)
    next_close = text.find(close_b, i)
    
    while i < n:
        if next_close == -1:
            return n
            
        if next_open != -1 and next_open < i:
            next_open = text.find(open_b, i)
        if next_close != -1 and next_close < i:
            next_close = text.find(close_b, i)
            
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + 1
        else:
            depth -= 1
            if depth == 0:
                return next_close + 1
            i = next_close + 1
    return n

def _skip_sensitivity_list(masked: bytes, pos: int) -> int:
    n = len(masked)
    while True:
        pos = _skip_ws_idx(masked, pos)
        if pos >= n: break
            
        if masked[pos] == _AT:
            pos += 1
            pos = _skip_ws_idx(masked, pos)
            if pos < n and masked[pos] == _LP:
                pos = _skip_balanced_parens(masked, pos)
            elif pos < n and masked[pos] == _STAR:
                pos += 1
            else:
                m_macro = AST_MACRO_RE.match(masked, pos)
                if m_macro: 
                    pos = m_macro.end()
                else:
                    m_bare = AST_BARE_RE.match(masked, pos)
                    if m_bare: pos = m_bare.end()
            continue 

        if masked[pos] == ord('#'):
            pos += 1
            pos = _skip_ws_idx(masked, pos)
            if pos < n and masked[pos] == _LP:
                pos = _skip_balanced_parens(masked, pos)
            else:
                m_macro = AST_MACRO_RE.match(masked, pos)
                if m_macro:
                    pos = m_macro.end()
                else:
                    m_ident = IDENT_AT_POS_RE.match(masked, pos)
                    if m_ident:
                        pos = m_ident.end()
                    else:
                        while pos < n and masked[pos] in b'0123456789.': pos += 1
                        m_unit = AST_UNIT_RE.match(masked, pos)
                        if m_unit: pos = m_unit.end()
            continue
            
        wait_match = AST_WAIT_RE.match(masked, pos)
        if wait_match:
            pos = wait_match.end()
            pos = _skip_ws_idx(masked, pos)
            if pos < n and masked[pos] == _LP:
                pos = _skip_balanced_parens(masked, pos)
            continue
            
        break 
        
    return pos

def _skip_ws_and_macros(masked: bytes, pos: int) -> int:
    n = len(masked)
    while True:
        pos = _skip_ws_idx(masked, pos)
        if pos >= n: break
        
        macro_match = AST_MACRO_RE.match(masked, pos)
        if macro_match:
            kw = macro_match.group(0)
            p = macro_match.end()
            
            if kw in (b'`ifdef', b'`ifndef', b'`elsif'):
                p = _skip_ws_idx(masked, p)
                ident_m = IDENT_AT_POS_RE.match(masked, p)
                if ident_m:
                    p = ident_m.end()
            
            p = _skip_ws_idx(masked, p)
            if p < n and masked[p] == _LP:
                p = _skip_balanced_parens(masked, p)
                
            pos = p
            continue
            
        break
    return pos

_PP_BRANCH_RE = re.compile(
    rb'`(ifdef|ifndef|elsif|else|endif|[a-zA-Z0-9_]*IFDEF|[a-zA-Z0-9_]*IFNDEF|[a-zA-Z0-9_]*ELSIF|[a-zA-Z0-9_]*ELSE|[a-zA-Z0-9_]*ENDIF)\b', 
    re.IGNORECASE
)

def _skip_preprocessor_branch(masked: bytes, pos: int) -> int:
    depth = 0
    for m in _PP_BRANCH_RE.finditer(masked, pos):
        kw = m.group(1).lower()
        if kw not in (b'ifdef', b'ifndef', b'elsif', b'else', b'endif'):
            line_start = masked.rfind(b'\n', 0, m.start()) + 1
            if masked[line_start:m.start()].strip(b' \t\r') != b'':
                continue 
            
        if kw.endswith(b'ifdef') or kw.endswith(b'ifndef'):
            depth += 1
        elif kw.endswith(b'endif'):
            if depth == 0:
                return m.end()
            depth -= 1
    return len(masked)

def _consume_begin_block(masked: bytes, begin_pos: int, ctx: ParserContext) -> int:
    level = 0
    pos = begin_pos
    n = len(masked)
    while pos < n:
        m = ctx.re_begin_end_pp.search(masked, pos)
        if not m:
            break
        kw = m.group(1)
        if kw is not None:
            kw_lower = kw.lower()
            if kw_lower == b'begin':
                level += 1
                pos = m.end()
            elif kw_lower == b'end':
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            else:
                if level > 0 and kw_lower in (b'module', b'endmodule'):
                    return m.start()
                pos = m.end()
        else:
            pp_kw = m.group(2)
            pp_kw_lower = pp_kw.lower()
            if pp_kw_lower.endswith(b'ifdef') or pp_kw_lower.endswith(b'ifndef'):
                pos = m.end()
            elif pp_kw_lower.endswith(b'elsif') or pp_kw_lower.endswith(b'else'):
                pos = _skip_preprocessor_branch(masked, m.end())
            elif pp_kw_lower.endswith(b'endif'):  
                pos = m.end()
            elif pp_kw_lower.endswith(b'begin') or pp_kw_lower in ctx.set_begins:
                level += 1
                pos = m.end()
            elif pp_kw_lower.endswith(b'end') or pp_kw_lower in ctx.set_ends:
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            else:
                pos = m.end()
    return n

def _consume_fork_block(masked: bytes, pos: int, ctx: ParserContext) -> int:
    level = 0
    n = len(masked)
    while pos < n:
        m = ctx.re_fork_pp.search(masked, pos)
        if not m: return n 
        kw = m.group(1)
        if kw is not None:
            kw_norm = re.sub(rb'\s+', b' ', kw.lower())
            if kw_norm in (b'disable fork', b'wait fork'):
                pos = m.end()
                continue 
            elif kw_norm == b'fork':
                level += 1
                pos = m.end()
            elif kw_norm in (b'join', b'join_any', b'join_none'):
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            else:
                if level > 0 and kw_norm in (b'module', b'endmodule'): 
                    return m.start()
                pos = m.end()
        else:
            pp_kw = m.group(2)
            pp_kw_lower = pp_kw.lower()
            if pp_kw_lower.endswith(b'ifdef') or pp_kw_lower.endswith(b'ifndef'):
                pos = m.end()
            elif pp_kw_lower.endswith(b'elsif') or pp_kw_lower.endswith(b'else'):
                pos = _skip_preprocessor_branch(masked, m.end())
            elif pp_kw_lower.endswith(b'endif'):  
                pos = m.end()
            elif pp_kw_lower.endswith(b'fork') or pp_kw_lower in ctx.set_forks:
                level += 1
                pos = m.end()
            elif pp_kw_lower.endswith(b'join') or pp_kw_lower.endswith(b'join_any') or pp_kw_lower.endswith(b'join_none') or pp_kw_lower in ctx.set_joins:
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            else:
                pos = m.end()
    return n

def _consume_case_block(masked: bytes, case_pos: int, ctx: ParserContext) -> int:
    level = 0
    pos = case_pos
    n = len(masked)
    while pos < n:
        m = ctx.re_case_pp.search(masked, pos)
        if not m:
            break
        kw = m.group(1)
        if kw is not None:
            kw_lower = kw.lower()
            if kw_lower in (b'case', b'casex', b'casez', b'randcase'):
                level += 1
                pos = m.end()
            elif kw_lower == b'endcase':
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            else:
                if level > 0 and kw_lower in (b'module', b'endmodule'): 
                    return m.start()
                pos = m.end()
        else:
            pp_kw = m.group(2)
            pp_kw_lower = pp_kw.lower()
            if pp_kw_lower.endswith(b'ifdef') or pp_kw_lower.endswith(b'ifndef'):
                pos = m.end()
            elif pp_kw_lower.endswith(b'elsif') or pp_kw_lower.endswith(b'else'):
                pos = _skip_preprocessor_branch(masked, m.end())
            elif pp_kw_lower.endswith(b'endif'):  
                pos = m.end()
            elif pp_kw_lower.endswith(b'endcase') or pp_kw_lower in ctx.set_endcases:
                level -= 1
                if level == 0:
                    p = m.end()
                    m_label = OPTIONAL_BLOCK_LABEL_RE.match(masked, p)
                    return m_label.end() if m_label else p
                pos = m.end()
            elif pp_kw_lower.endswith(b'case') or pp_kw_lower in ctx.set_cases:
                level += 1
                pos = m.end()
            else:
                pos = m.end()
    return n

def _consume_statement(masked: bytes, pos: int, _depth: int, ctx: ParserContext) -> int:
    n = len(masked)
    started_with_pp = False
    
    while True:
        prev_pos = pos
        pos = _skip_ws_idx(masked, pos)
        
        label_match = STATEMENT_LABEL_RE.match(masked, pos)
        if label_match:
            pos = label_match.end()
            continue
            
        opt_label_match = OPTIONAL_BLOCK_LABEL_RE.match(masked, pos)
        if opt_label_match:
            pos = opt_label_match.end()
            continue
            
        pos = _skip_sensitivity_list(masked, pos)
        
        m_macro = AST_MACRO_RE.match(masked, pos)
        if m_macro:
            kw = m_macro.group(0)
            kw_no_tick = kw[1:].lower()
            
            if kw_no_tick in ctx.set_begins or kw_no_tick in ctx.set_ends or \
               kw_no_tick in ctx.set_forks or kw_no_tick in ctx.set_joins or \
               kw_no_tick in ctx.set_cases or kw_no_tick in ctx.set_endcases or \
               re.match(rb'`[a-zA-Z0-9_]*BEGIN\b', kw, re.IGNORECASE) or \
               re.match(rb'`[a-zA-Z0-9_]*END\b', kw, re.IGNORECASE) or \
               re.match(rb'`[a-zA-Z0-9_]*FORK\b', kw, re.IGNORECASE) or \
               re.match(rb'`[a-zA-Z0-9_]*JOIN(?:_[A-Z0-9_]*)?\b', kw, re.IGNORECASE) or \
               re.match(rb'`[a-zA-Z0-9_]*CASE\b', kw, re.IGNORECASE):
                break

            p_end = m_macro.end()
            
            if kw_no_tick.endswith(b'ifdef') or kw_no_tick.endswith(b'ifndef') or \
               kw_no_tick.endswith(b'elsif') or kw_no_tick.endswith(b'else') or \
               kw_no_tick.endswith(b'endif'):
                if kw_no_tick.endswith(b'ifdef') or kw_no_tick.endswith(b'ifndef') or kw_no_tick.endswith(b'elsif'):
                    p_end = _skip_ws_idx(masked, p_end)
                    ident_m = IDENT_AT_POS_RE.match(masked, p_end)
                    if ident_m: p_end = ident_m.end()
                pos = p_end
                started_with_pp = True
                continue

            p_args = _skip_ws_idx(masked, p_end)
            if p_args < n and masked[p_args] == _LP:
                p_args = _skip_balanced_parens(masked, p_args)
            
            p_look = _skip_ws_and_macros(masked, p_args)
            
            STRUCT_KW = re.compile(rb'\b(begin|if|do|for|foreach|while|repeat|forever|case[xz]?|randcase|assert|assume|cover|restrict|expect|wait_order|fork)\b')
            
            is_dyn_struct = False
            m_next = AST_MACRO_GROUP_RE.match(masked, p_look)
            if m_next:
                n_kw = m_next.group(1).lower()
                if n_kw in ctx.set_begins or n_kw in ctx.set_forks or n_kw in ctx.set_cases:
                    is_dyn_struct = True

            if STRUCT_KW.match(masked, p_look) or is_dyn_struct or \
               STATEMENT_LABEL_RE.match(masked, p_look) or \
               OPTIONAL_BLOCK_LABEL_RE.match(masked, p_look) or \
               (p_look < n and masked[p_look] == _AT) or \
               (p_look < n and masked[p_look] == ord('#')) or \
               AST_WAIT_RE.match(masked, p_look):
                pos = p_args
                continue
            break
            
        if pos == prev_pos:
            break
            
    if pos >= n or _depth > 2000:
        return pos

    p = _consume_statement_core(masked, pos, _depth, ctx)
    
    if started_with_pp:
        while True:
            p_ws = _skip_ws_idx(masked, p)
            m_pp = AST_PP_RE.match(masked, p_ws)
            if m_pp:
                kw_pp = m_pp.group(1).lower()
                if kw_pp.endswith(b'endif'):
                    p = m_pp.end()
                    continue
                elif kw_pp.endswith(b'elsif') or kw_pp.endswith(b'else'):
                    p_next = m_pp.end()
                    if kw_pp.endswith(b'elsif'):
                        p_ident = _skip_ws_idx(masked, p_next)
                        ident_m = IDENT_AT_POS_RE.match(masked, p_ident)
                        if ident_m: p_next = ident_m.end()
                    p = _consume_statement(masked, p_next, _depth + 1, ctx)
                    continue
            break

    return p

def _consume_statement_core(masked: bytes, pos: int, _depth: int, ctx: ParserContext) -> int:
    n = len(masked)
    
    if ctx.re_kw_begin.match(masked, pos): return _consume_begin_block(masked, pos, ctx)
    if ctx.re_kw_fork.match(masked, pos):  return _consume_fork_block(masked, pos, ctx)
    if ctx.re_kw_case.match(masked, pos):  return _consume_case_block(masked, pos, ctx)

    m = _KW_IF_RE.match(masked, pos)
    if m:
        p = _skip_balanced_parens(masked, _skip_ws_idx(masked, m.end()))
        p = _consume_statement(masked, p, _depth + 1, ctx)
        p_ws = _skip_ws_and_macros(masked, p)  
        m_else = _KW_ELSE_RE.match(masked, p_ws)
        if m_else:
            return _consume_statement(masked, m_else.end(), _depth + 1, ctx)
            
        p_next = _skip_ws_idx(masked, p)
        m_macro = AST_MACRO_RE.match(masked, p_next)
        if m_macro:
            p_end = m_macro.end()
            p_args = _skip_ws_idx(masked, p_end)
            if p_args < n and masked[p_args] == _LP:
                p_end = _skip_balanced_parens(masked, p_args)
            p_semi = _skip_ws_idx(masked, p_end)
            return p_semi + 1 if (p_semi < n and masked[p_semi] == _SEMI) else p_end
        return p

    for kw_re in (_KW_FOR_RE, _KW_WHILE_RE, _KW_REPEAT_RE):
        m = kw_re.match(masked, pos)
        if m:
            p = _skip_balanced_parens(masked, _skip_ws_idx(masked, m.end()))
            return _consume_statement(masked, p, _depth + 1, ctx)

    m = _KW_FOREVER_RE.match(masked, pos)
    if m:
        return _consume_statement(masked, m.end(), _depth + 1, ctx)

    m = _KW_DO_RE.match(masked, pos)
    if m:
        p = _consume_statement(masked, m.end(), _depth + 1, ctx)
        p_ws = _skip_ws_and_macros(masked, p)
        m_while = _KW_WHILE_RE.match(masked, p_ws)
        if m_while:
            p = _skip_balanced_parens(masked, _skip_ws_idx(masked, m_while.end()))
            p_ws = _skip_ws_idx(masked, p)
            if p_ws < n and masked[p_ws] == _SEMI:
                return p_ws + 1
            m_macro = AST_MACRO_RE.match(masked, p_ws)
            if m_macro:
                return m_macro.end()
            return p
            
        p_next = _skip_ws_idx(masked, p)
        m_macro = AST_MACRO_RE.match(masked, p_next)
        if m_macro:
            p_end = m_macro.end()
            p_args = _skip_ws_idx(masked, p_end)
            if p_args < n and masked[p_args] == _LP:
                p_end = _skip_balanced_parens(masked, p_args)
            p_semi = _skip_ws_idx(masked, p_end)
            return p_semi + 1 if (p_semi < n and masked[p_semi] == _SEMI) else p_end
        return p

    m = _KW_ASSERT_RE.match(masked, pos)
    if m:
        p = m.end()
        while p < n and masked[p] != _LP and masked[p] != _SEMI:
            p += 1
        if p < n and masked[p] == _LP:
            p = _skip_balanced_parens(masked, p)
        p = _consume_statement(masked, p, _depth + 1, ctx)
        p_ws = _skip_ws_and_macros(masked, p)
        m_else = _KW_ELSE_RE.match(masked, p_ws)
        if m_else:
            return _consume_statement(masked, m_else.end(), _depth + 1, ctx)
            
        p_next = _skip_ws_idx(masked, p)
        m_macro = AST_MACRO_RE.match(masked, p_next)
        if m_macro:
            p_end = m_macro.end()
            p_args = _skip_ws_idx(masked, p_end)
            if p_args < n and masked[p_args] == _LP:
                p_end = _skip_balanced_parens(masked, p_args)
            p_semi = _skip_ws_idx(masked, p_end)
            return p_semi + 1 if (p_semi < n and masked[p_semi] == _SEMI) else p_end
        return p

    depth = 0
    i = pos
    while i < n:
        c = masked[i]
        
        if c == ord('`'):
            m_macro = AST_MACRO_RE.match(masked, i)
            if m_macro:
                kw = m_macro.group(0)
                kw_lower = kw.lower()

                if kw_lower.endswith(b'ifdef') or kw_lower.endswith(b'ifndef'):
                    p_end = m_macro.end()
                    p_ident = _skip_ws_idx(masked, p_end)
                    ident_m = IDENT_AT_POS_RE.match(masked, p_ident)
                    if ident_m:
                        p_end = ident_m.end()
                    i = p_end
                    continue
                elif kw_lower.endswith(b'elsif') or kw_lower.endswith(b'else'):
                    p_end = m_macro.end()
                    i = _skip_preprocessor_branch(masked, p_end)
                    continue
                elif kw_lower.endswith(b'endif'):
                    i = m_macro.end()
                    continue

                if depth == 0:
                    p_args = _skip_ws_idx(masked, m_macro.end())

                    if p_args < n and masked[p_args] == _LP:
                        p_paren_end = _skip_balanced_parens(masked, p_args)

                        for ptr in range(p_args, p_paren_end):
                            if masked[ptr] == _SEMI:
                                return ptr + 1

                        p_next_token = _skip_ws_idx(masked, p_paren_end)
                    else:
                        p_paren_end = m_macro.end()
                        p_next_token = _skip_ws_idx(masked, p_paren_end)

                    p_look = p_next_token
                    while True:
                        m_mac = AST_MACRO_RE.match(masked, p_look)
                        if m_mac:
                            p_look = m_mac.end()
                            p_look = _skip_ws_idx(masked, p_look)
                            if p_look < n and masked[p_look] == _LP:
                                p_look = _skip_balanced_parens(masked, p_look)
                                p_look = _skip_ws_idx(masked, p_look)
                        else:
                            break

                    if p_look >= n:
                        return p_paren_end
                    else:
                        i = p_paren_end - 1
                else:
                    i = m_macro.end() - 1

        if c == _LP or c == _LB or c == _LC:
            depth += 1
        elif c == _RP or c == _RB or c == _RC:
            if depth > 0:
                depth -= 1
        elif c == _SEMI and depth == 0:
            return i + 1
            
        i += 1
    return n

def _balance_preprocessor_directives(masked: bytes, start: int, end: int):
    def _is_valid_directive(match_obj):
        kw = match_obj.group(1).lower()
        if kw in (b'ifdef', b'ifndef', b'elsif', b'else', b'endif'):
            return True
            
        l_start = masked.rfind(b'\n', 0, match_obj.start()) + 1
        return masked[l_start:match_obj.start()].strip(b' \t\r') == b''

    depth = 0
    for m in AST_PP_SEARCH_RE.finditer(masked, start, end):
        if not _is_valid_directive(m): continue
        kw = m.group(1).lower()
        if kw.endswith(b'ifdef') or kw.endswith(b'ifndef'):
            depth += 1
        elif kw.endswith(b'endif'):
            depth -= 1
            
    while depth > 0 and end < len(masked):
        m = AST_PP_SEARCH_RE.search(masked, end)
        if not m: break
        end = m.end()
        if not _is_valid_directive(m): continue
        kw = m.group(1).lower()
        if kw.endswith(b'ifdef') or kw.endswith(b'ifndef'):
            depth += 1
        elif kw.endswith(b'endif'):
            depth -= 1
        
    while depth < 0 and start > 0:
        matches = [m for m in AST_PP_SEARCH_RE.finditer(masked, 0, start) if _is_valid_directive(m)]
        if not matches: break
        last_m = matches[-1]
        kw = last_m.group(1).lower()
        if kw.endswith(b'ifdef') or kw.endswith(b'ifndef'):
            depth += 1
        elif kw.endswith(b'endif'):
            depth -= 1
        start = last_m.start()
        
    return start, end

# ==============================================================================
# AST-AWARE RANGE FINDERS
# ==============================================================================

def _vacuum_whitespace_and_attributes(code: bytes, p: int) -> int:
    while p >= 0:
        if code[p] in b' \t\n\r\x0b\x0c':
            p -= 1
            continue
        if p >= 1 and code[p] == ord(')') and code[p-1] == ord('*'):
            p -= 2
            depth = 1
            while p >= 1 and depth > 0:
                if code[p] == ord('*') and code[p-1] == ord('('):
                    depth -= 1
                    p -= 2
                elif code[p] == ord(')') and code[p-1] == ord('*'):
                    depth += 1
                    p -= 2
                else:
                    p -= 1
            continue
        break
    return p

def _get_construct_start(code: bytes, kw_start: int) -> int:
    p_back = kw_start - 1
    while p_back >= 0 and code[p_back] in b' \t\n\r\x0b\x0c':
        p_back -= 1
        
    label_start = kw_start
    if p_back >= 0 and code[p_back] == ord(':'):
        temp_p = p_back - 1
        while temp_p >= 0 and code[temp_p] in b' \t\n\r\x0b\x0c':
            temp_p -= 1
        ident_end = temp_p + 1
        while temp_p >= 0 and (code[temp_p] in b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_\\'):
            temp_p -= 1
        if temp_p + 1 < ident_end:
            label_start = temp_p + 1

    temp_attr = _vacuum_whitespace_and_attributes(code, label_start - 1)
    start_idx = temp_attr + 1
    while start_idx < kw_start and code[start_idx] in b' \t\n\r\x0b\x0c':
        start_idx += 1
        
    return start_idx

def _find_assign_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    pos = 0
    n = len(masked)
    pattern = ctx.re_assign

    while pos < n:
        match = pattern.search(masked, pos)
        if not match: break
        kw_start = match.start()
        kw_end = match.end()
        kw = match.group(0)

        start_idx = _get_construct_start(code, kw_start)

        if kw.startswith(b'`'):
            name = kw[1:]
            if name in ctx.standalone_assign_macros:
                p_args = _skip_ws_idx(masked, kw_end)
                if p_args < n and masked[p_args] == _LP:
                    kw_end = _skip_balanced_parens(masked, p_args)
                ranges.append((start_idx, kw_end))
                pos = kw_end
                continue
            if name in ctx.begin_macros:
                end_idx = _consume_begin_block(masked, kw_start, ctx)
            elif name in ctx.fork_macros:
                end_idx = _consume_fork_block(masked, kw_start, ctx)
            elif name in ctx.case_macros:
                end_idx = _consume_case_block(masked, kw_start, ctx)
            else:
                end_idx = _consume_statement(masked, kw_end, 0, ctx)
        else:
            end_idx = _consume_statement(masked, kw_end, 0, ctx)

        if end_idx > start_idx:
            start_idx, end_idx = _balance_preprocessor_directives(masked, start_idx, end_idx)
            ranges.append((start_idx, end_idx))
            pos = end_idx
        else:
            pos = kw_start + 1
            
    return ranges

def _find_always_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    pos = 0
    n = len(masked)
    pattern = ctx.re_always

    while pos < n:
        match = pattern.search(masked, pos)
        if not match: break
        kw_start = match.start()
        kw_end = match.end()
        kw = match.group(0)

        start_idx = _get_construct_start(code, kw_start)

        if kw.startswith(b'`'):
            name = kw[1:]
            if name in ctx.standalone_always_macros:
                p_args = _skip_ws_idx(masked, kw_end)
                if p_args < n and masked[p_args] == _LP:
                    kw_end = _skip_balanced_parens(masked, p_args)
                ranges.append((start_idx, kw_end))
                pos = kw_end
                continue
            body_start = _skip_sensitivity_list(masked, kw_end)
            if name in ctx.begin_macros:
                end_idx = _consume_begin_block(masked, kw_start, ctx)
            elif name in ctx.fork_macros:
                end_idx = _consume_fork_block(masked, kw_start, ctx)
            elif name in ctx.case_macros:
                end_idx = _consume_case_block(masked, kw_start, ctx)
            else:
                end_idx = _consume_statement(masked, body_start, 0, ctx)
        else:
            body_start = _skip_sensitivity_list(masked, kw_end)
            end_idx = _consume_statement(masked, body_start, 0, ctx)

        if end_idx > start_idx:
            start_idx, end_idx = _balance_preprocessor_directives(masked, start_idx, end_idx)
            ranges.append((start_idx, end_idx))
            pos = end_idx
        else:
            pos = kw_start + 1
            
    return ranges

def _find_initial_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    pos = 0
    n = len(masked)
    pattern = ctx.re_initial

    while pos < n:
        match = pattern.search(masked, pos)
        if not match: break
        kw_start = match.start()
        kw_end = match.end()
        kw = match.group(0)

        start_idx = _get_construct_start(code, kw_start)

        if kw.startswith(b'`'):
            name = kw[1:]
            if name in ctx.standalone_initial_macros:
                p_args = _skip_ws_idx(masked, kw_end)
                if p_args < n and masked[p_args] == _LP:
                    kw_end = _skip_balanced_parens(masked, p_args)
                ranges.append((start_idx, kw_end))
                pos = kw_end
                continue
            body_start = _skip_sensitivity_list(masked, kw_end)
            if name in ctx.begin_macros:
                end_idx = _consume_begin_block(masked, kw_start, ctx)
            elif name in ctx.fork_macros:
                end_idx = _consume_fork_block(masked, kw_start, ctx)
            elif name in ctx.case_macros:
                end_idx = _consume_case_block(masked, kw_start, ctx)
            else:
                end_idx = _consume_statement(masked, body_start, 0, ctx)
        else:
            body_start = _skip_sensitivity_list(masked, kw_end)
            end_idx = _consume_statement(masked, body_start, 0, ctx)

        if end_idx > start_idx:
            start_idx, end_idx = _balance_preprocessor_directives(masked, start_idx, end_idx)
            ranges.append((start_idx, end_idx))
            pos = end_idx
        else:
            pos = kw_start + 1
            
    return ranges

def _find_final_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    pos = 0
    n = len(masked)
    pattern = ctx.re_final

    while pos < n:
        match = pattern.search(masked, pos)
        if not match: break
        kw_start = match.start()
        kw_end = match.end()
        kw = match.group(0)

        start_idx = _get_construct_start(code, kw_start)

        if kw.startswith(b'`'):
            name = kw[1:]
            if name in ctx.standalone_final_macros:
                p_args = _skip_ws_idx(masked, kw_end)
                if p_args < n and masked[p_args] == _LP:
                    kw_end = _skip_balanced_parens(masked, p_args)
                ranges.append((start_idx, kw_end))
                pos = kw_end
                continue
            body_start = _skip_sensitivity_list(masked, kw_end)
            if name in ctx.begin_macros:
                end_idx = _consume_begin_block(masked, kw_start, ctx)
            elif name in ctx.fork_macros:
                end_idx = _consume_fork_block(masked, kw_start, ctx)
            elif name in ctx.case_macros:
                end_idx = _consume_case_block(masked, kw_start, ctx)
            else:
                end_idx = _consume_statement(masked, body_start, 0, ctx)
        else:
            body_start = _skip_sensitivity_list(masked, kw_end)
            end_idx = _consume_statement(masked, body_start, 0, ctx)

        if end_idx > start_idx:
            start_idx, end_idx = _balance_preprocessor_directives(masked, start_idx, end_idx)
            ranges.append((start_idx, end_idx))
            pos = end_idx
        else:
            pos = kw_start + 1
            
    return ranges

# ==============================================================================
# BLOCK BODY REMOVERS (BRACKET-AWARE)
# ==============================================================================

def _find_signature_end(masked: bytes, start_idx: int) -> int:
    """Walks forward from start_idx, balancing {}, (), and [] brackets.
       Returns the index of the first semicolon found at depth 0."""
    depth = 0
    n = len(masked)
    i = start_idx
    
    LBRACE, RBRACE = ord('{'), ord('}')
    LPAREN, RPAREN = ord('('), ord(')')
    LBRACK, RBRACK = ord('['), ord(']')
    SEMI = ord(';')
    
    while i < n:
        c = masked[i]
        if c == LBRACE or c == LPAREN or c == LBRACK:
            depth += 1
        elif c == RBRACE or c == RPAREN or c == RBRACK:
            if depth > 0:
                depth -= 1
        elif c == SEMI and depth == 0:
            return i
        i += 1
    return -1

def _find_task_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    kw_re = re.compile(rb'\btask\b')
    end_kw_re = re.compile(rb'\bendtask\b')
    pos = 0
    n = len(masked)
    while pos < n:
        m = kw_re.search(masked, pos)
        if not m:
            break
        
        p_back_str = masked[max(0, m.start()-150):m.start()].lower()
        last_semi = p_back_str.rfind(b';')
        search_area = p_back_str[last_semi+1:] if last_semi != -1 else p_back_str
        is_extern = bool(re.search(rb'\b(extern|pure|import|export)\b', search_area))
        
        if is_extern:
            pos = m.end()
            continue

        p_semi = _find_signature_end(masked, m.end())
        m_end = end_kw_re.search(masked, m.end())
        
        if m_end and p_semi != -1 and p_semi < m_end.start():
            body_start = p_semi + 1
            body_end = m_end.start()
            
            start_idx, end_idx = _balance_preprocessor_directives(masked, body_start, body_end)
            ranges.append((start_idx, end_idx))
            pos = m_end.end()
        elif m_end:
            pos = m_end.end()
        else:
            pos = m.end()
            
    return ranges

def _find_function_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    kw_re = re.compile(rb'\bfunction\b')
    end_kw_re = re.compile(rb'\bendfunction\b')
    pos = 0
    n = len(masked)
    while pos < n:
        m = kw_re.search(masked, pos)
        if not m:
            break
        
        p_back_str = masked[max(0, m.start()-150):m.start()].lower()
        last_semi = p_back_str.rfind(b';')
        search_area = p_back_str[last_semi+1:] if last_semi != -1 else p_back_str
        is_extern = bool(re.search(rb'\b(extern|pure|import|export)\b', search_area))
        
        if is_extern:
            pos = m.end()
            continue

        p_semi = _find_signature_end(masked, m.end())
        m_end = end_kw_re.search(masked, m.end())
        
        # =====================================================================
        # [!] CONSTRUCTOR PROTECTOR
        # =====================================================================
        if p_semi != -1:
            # Find where the arguments start
            p_paren = masked.find(b'(', m.end(), p_semi)
            decl_end = p_paren if p_paren != -1 else p_semi
            
            # Extract the string between 'function' and '('
            func_decl = masked[m.end():decl_end]
            
            # Check if the word "new" is used as the function name
            # This perfectly catches both "new" and "bus_tx::new"
            if re.search(rb'\bnew\b', func_decl):
                if m_end:
                    pos = m_end.end()
                else:
                    pos = p_semi
                continue # Safely skip hollowing out this constructor!
        # =====================================================================
        
        if m_end and p_semi != -1 and p_semi < m_end.start():
            body_start = p_semi + 1
            body_end = m_end.start()
            
            start_idx, end_idx = _balance_preprocessor_directives(masked, body_start, body_end)
            ranges.append((start_idx, end_idx))
            pos = m_end.end()
        elif m_end:
            pos = m_end.end()
        else:
            pos = m.end()
            
    return ranges

def _find_virtual_class_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    ranges = []
    # 1. Match 'virtual ' (including the space) so we can delete just the keyword
    kw_re = re.compile(rb'\b(virtual\s+)class\b')
    end_kw_re = re.compile(rb'\bendclass\b')
    
    pos = 0
    n = len(masked)

    while pos < n:
        m = kw_re.search(masked, pos)
        if not m:
            break

        # SAFETY: Protect against forward declarations
        p_back_str = masked[max(0, m.start()-150):m.start()].lower()
        last_semi = p_back_str.rfind(b';')
        search_area = p_back_str[last_semi+1:] if last_semi != -1 else p_back_str

        if b'typedef' in search_area:
            pos = m.end()
            continue

        p_semi = _find_signature_end(masked, m.end())
        m_end = end_kw_re.search(masked, m.end())

        if m_end and p_semi != -1 and p_semi < m_end.start():
            # =================================================================
            # REQUIREMENT 1: Remove 'virtual' keyword from the class name.
            # =================================================================
            ranges.append((m.start(1), m.end(1), b""))
            
            class_body_start = p_semi + 1
            class_body_end = m_end.start()

            # =================================================================
            # SWEEP 1: Tasks and Functions 
            # =================================================================
            tf_re = re.compile(rb'\b(task|function)\b')
            tf_pos = class_body_start
            while tf_pos < class_body_end:
                tf_m = tf_re.search(masked, tf_pos)
                
                if not tf_m or tf_m.start() >= class_body_end:
                    break
                    
                is_task = (tf_m.group(1) == b'task')
                end_tf_re = re.compile(rb'\bendtask\b') if is_task else re.compile(rb'\bendfunction\b')
                
                tf_back_start = max(class_body_start, tf_m.start() - 150)
                tf_back_str = masked[tf_back_start:tf_m.start()].lower()
                last_tf_semi = tf_back_str.rfind(b';')
                
                if last_tf_semi != -1:
                    search_base_idx = tf_back_start + last_tf_semi + 1
                else:
                    search_base_idx = tf_back_start
                    
                tf_search_area = masked[search_base_idx:tf_m.start()].lower()
                
                # =================================================================
                # REQUIREMENT 2: Morph Pure Virtuals (Strip keywords, inject endfunction)
                # =================================================================
                is_pure = bool(re.search(rb'\bpure\b', tf_search_area))
                is_extern_or_dpi = bool(re.search(rb'\b(extern|import|export)\b', tf_search_area))
                
                if is_extern_or_dpi:
                    tf_pos = tf_m.end() 
                    continue

                if is_pure:
                    # 1. Strip 'pure'
                    pure_m = re.search(rb'\bpure\s+', tf_search_area)
                    if pure_m:
                        p_start = search_base_idx + pure_m.start()
                        p_end = search_base_idx + pure_m.end()
                        ranges.append((p_start, p_end, b""))
                    
                    # 2. Strip 'virtual'
                    virt_m = re.search(rb'\bvirtual\s+', tf_search_area)
                    if virt_m:
                        v_start = search_base_idx + virt_m.start()
                        v_end = search_base_idx + virt_m.end()
                        ranges.append((v_start, v_end, b""))

                    # 3. Inject 'endtask' or 'endfunction' after the semicolon
                    tf_semi = _find_signature_end(masked, tf_m.end())
                    if tf_semi != -1:
                        injection = b"\nendtask\n" if is_task else b"\nendfunction\n"
                        ranges.append((tf_semi + 1, tf_semi + 1, injection))
                        tf_pos = tf_semi + 1
                    else:
                        tf_pos = tf_m.end()
                        
                    continue 
                
                # =================================================================
                # REQUIREMENT 3A: Remove 'virtual' keyword from standard methods
                # =================================================================
                virt_m = re.search(rb'\b(virtual\s+)', tf_search_area)
                if virt_m:
                    v_start = search_base_idx + virt_m.start(1)
                    v_end = search_base_idx + virt_m.end(1)
                    ranges.append((v_start, v_end, b""))
                
                # REQUIREMENT 3B: Hollow out standard method bodies
                tf_semi = _find_signature_end(masked, tf_m.end())
                tf_end_m = end_tf_re.search(masked, tf_m.end())
                
                # =================================================================
                # [!] CONSTRUCTOR PROTECTOR (Fixing the crash inside Virtual Classes)
                # =================================================================
                if tf_semi != -1:
                    p_paren = masked.find(b'(', tf_m.end(), tf_semi)
                    decl_end = p_paren if p_paren != -1 else tf_semi
                    func_decl = masked[tf_m.end():decl_end]
                    
                    if re.search(rb'\bnew\b', func_decl):
                        # It's a constructor! Skip hollowing out its body.
                        # But we still safely removed the 'virtual' keyword above.
                        if tf_end_m:
                            tf_pos = tf_end_m.end()
                        else:
                            tf_pos = tf_semi
                        continue
                # =================================================================

                if tf_end_m and tf_semi != -1 and tf_semi < tf_end_m.start():
                    body_start = tf_semi + 1
                    body_end = tf_end_m.start()
                    b_start, b_end = _balance_preprocessor_directives(masked, body_start, body_end)
                    ranges.append((b_start, b_end, b""))
                    tf_pos = tf_end_m.end()
                elif tf_end_m:
                    tf_pos = tf_end_m.end()
                else:
                    tf_pos = tf_m.end()

            # =================================================================
            # SWEEP 2: Constraints (Hollow to {})
            # =================================================================
            c_re = re.compile(rb'\bconstraint\b')
            c_pos = class_body_start
            while c_pos < class_body_end:
                c_m = c_re.search(masked, c_pos)
                if not c_m or c_m.start() >= class_body_end:
                    break

                brace_pos = masked.find(b'{', c_m.end(), class_body_end)
                semi_pos = masked.find(b';', c_m.end(), class_body_end)

                if brace_pos == -1:
                    c_pos = c_m.end()
                    continue
                if semi_pos != -1 and semi_pos < brace_pos:
                    c_pos = semi_pos + 1
                    continue

                open_braces = 0
                match_pos = -1
                for i in range(brace_pos, class_body_end):
                    if masked[i:i+1] == b'{':
                        open_braces += 1
                    elif masked[i:i+1] == b'}':
                        open_braces -= 1
                        if open_braces == 0:
                            match_pos = i
                            break

                if match_pos != -1:
                    body_start = brace_pos + 1
                    body_end = match_pos
                    c_start, c_end = _balance_preprocessor_directives(masked, body_start, body_end)
                    ranges.append((c_start, c_end, b""))
                    c_pos = match_pos + 1
                else:
                    c_pos = brace_pos + 1

            # =================================================================
            # SWEEP 3: Covergroups (Hollow to endgroup)
            # =================================================================
            cg_re = re.compile(rb'\bcovergroup\b')
            end_cg_re = re.compile(rb'\bendgroup\b')
            cg_pos = class_body_start
            while cg_pos < class_body_end:
                cg_m = cg_re.search(masked, cg_pos)
                if not cg_m or cg_m.start() >= class_body_end:
                    break
                    
                cg_semi = _find_signature_end(masked, cg_m.end())
                cg_end_m = end_cg_re.search(masked, cg_m.end())
                
                if cg_end_m and cg_end_m.start() < class_body_end and cg_semi != -1 and cg_semi < cg_end_m.start():
                    body_start = cg_semi + 1
                    body_end = cg_end_m.start()
                    cg_start, cg_end = _balance_preprocessor_directives(masked, body_start, body_end)
                    ranges.append((cg_start, cg_end, b""))
                    cg_pos = cg_end_m.end()
                elif cg_end_m:
                    cg_pos = cg_end_m.end()
                else:
                    cg_pos = cg_m.end()

            # Advance the main parser
            pos = m_end.end()
        elif m_end:
            pos = m_end.end()
        else:
            pos = m.end()

    return ranges


def _find_procedural_ranges(code: bytes, masked: bytes, ctx: ParserContext):
    """Finds all ranges belonging to procedural or non-module scopes where 
    module instantiations cannot legally exist."""
    ranges = []
    ranges.extend(_find_always_ranges(code, masked, ctx))
    ranges.extend(_find_initial_ranges(code, masked, ctx))
    ranges.extend(_find_final_ranges(code, masked, ctx))

    block_pairs = [
        (re.compile(rb'\btask\b'), re.compile(rb'\bendtask\b')),
        (re.compile(rb'\bfunction\b'), re.compile(rb'\bendfunction\b')),
        (re.compile(rb'\bclass\b'), re.compile(rb'\bendclass\b')),
        (re.compile(rb'\bproperty\b'), re.compile(rb'\bendproperty\b')),
        (re.compile(rb'\bsequence\b'), re.compile(rb'\bendsequence\b')),
        (re.compile(rb'\bcovergroup\b'), re.compile(rb'\bendgroup\b')),
    ]

    n = len(masked)
    for kw_re, end_kw_re in block_pairs:
        pos = 0
        while pos < n:
            m = kw_re.search(masked, pos)
            if not m:
                break
            kw_start = _get_construct_start(code, m.start())
            
            p_back_str = masked[max(0, m.start()-150):m.start()].lower()
            last_semi = p_back_str.rfind(b';')
            search_area = p_back_str[last_semi+1:] if last_semi != -1 else p_back_str
            is_extern = bool(re.search(rb'\b(extern|pure|import|export)\b', search_area))
            
            if is_extern:
                p_semi = _find_signature_end(masked, m.end())
                if p_semi != -1:
                    ranges.append((kw_start, p_semi + 1))
                    pos = p_semi + 1
                else:
                    pos = m.end()
                continue

            m_end = end_kw_re.search(masked, m.end())
            if m_end:
                ranges.append((kw_start, m_end.end()))
                pos = m_end.end()
            else:
                pos = m.end()

    if not ranges:
        return []

    ranges.sort()
    merged = []
    for r_start, r_end in ranges:
        if not merged:
            merged.append([r_start, r_end])
        else:
            if r_start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], r_end)
            else:
                merged.append([r_start, r_end])
    return [(r[0], r[1]) for r in merged]

def _snap_range_to_whole_lines(code: bytes, masked: bytes, start: int, end: int):
    line_start = code.rfind(b'\n', 0, start) + 1  
    prefix = masked[line_start:start]
    new_start = line_start if prefix.strip(b' \t\r') == b'' else start

    next_nl = code.find(b'\n', end)
    if next_nl == -1:
        tail = masked[end:]
        new_end = len(code) if tail.strip(b' \t\r') == b'' else end
    else:
        tail = masked[end:next_nl]
        new_end = next_nl + 1 if tail.strip(b' \t\r') == b'' else end

    return new_start, new_end

def _apply_ranges(code: bytes, masked: bytes, ranges, snap_to_lines: bool = False) -> bytes:
    if not ranges:
        return code
        
    # 1. Normalize all tuples to (start, end, replacement_text)
    norm_ranges = []
    for r in ranges:
        if len(r) == 2:
            start, end = r
            repl = b""
        else:
            start, end, repl = r
            
        if snap_to_lines and start != end:
            start, end = _snap_range_to_whole_lines(code, masked, start, end)
            
        norm_ranges.append((start, end, repl))
        
    # 2. Sort ranges safely by start index.
    norm_ranges.sort(key=lambda x: (x[0], -x[1]))
    
    # 3. Merge overlapping ranges (combining replacements if needed)
    merged = []
    for start, end, repl in norm_ranges:
        if not merged:
            merged.append([start, end, repl])
        else:
            prev_start, prev_end, prev_repl = merged[-1]
            if start <= prev_end:
                # Overlapping ranges! Extend the end and concatenate the replacement text.
                merged[-1][1] = max(prev_end, end)
                merged[-1][2] = prev_repl + repl
            else:
                merged.append([start, end, repl])
                
    # 4. Reconstruct the byte array and apply all deletions/injections
    out = []
    last = 0
    for start, end, repl in merged:
        out.append(code[last:start])
        out.append(repl)
        last = end
    out.append(code[last:])
    return b''.join(out)



#//==============================================below portremove with put expicit wire==================================
import re

def remove_top_n_port_connections(code: bytes, target_count: int, ctx: ParserContext = None) -> bytes:
    if target_count <= 0:
        return code, False

    if not re.search(rb'\b(?:module|interface|class|package|program|bind|macromodule|config|property)\b', code):
        return code, False

    if ctx is None:
        ctx = ParserContext(code)

    masked = mask_unwanted_structures(code)

    # =========================================================================
    # [!] INTERFACE WRAPPER SHIELD
    # If the file defines a module that acts as an interface wrapper (e.g., ends 
    # with _intf), instantly skip the entire file to preserve internal routing!
    # [BUGFIX] Tested against MASKED text, not raw source: a commented-out or
    # string-literal `module foo_intf;` must NOT trigger the whole-file skip
    # (previously one commented decoy line silently disabled the whole tool).
    # =========================================================================
    if re.search(rb'\bmodule\s+[a-zA-Z_]\w*_intf\b', masked, re.IGNORECASE):
        return code, False
    protected_ranges = list(_find_procedural_ranges(code, masked, ctx))

    # =========================================================================
    # CONSTANTS AND HELPER FUNCTIONS
    # =========================================================================
    STATEMENT_ABORT_RE = re.compile(
        rb'(?:^|_)(?:if|mp|io|tb|vip|env|drv|mon|seq|sco|sb|cfg|uvc|pkg)(?:_|$)|'
        rb'vif|intf|interface|probe|test|agent|driver|monitor|checker|scoreboard',
        re.IGNORECASE
    )

    # =========================================================================
    # [!] UPDATED SHIELD: PULP / CVA6 / AXI Library Submodule Protection
    # Added `counter` to the list of protected open-source IP library submodules.
    # =========================================================================
    STD_CELL_RE = re.compile(
        rb'^(?:'
        rb'(?:DFF|SDFF|TLAT|LATCH|INV|BUF|NAND|NOR|XOR|XNOR|MUX|AOI|OAI|TIE|FILL|ANTENNA|CK|CLK|ICG|RC_CG|SEQGEN|USR_ACCESS)\w*'
        rb'|(?:AND|OR)[0-9]\w*'
        rb'|\\[*]+SEQGEN[*]+'
        rb'|[a-zA-Z0-9_]+(?:_X\d+|X\d+|_XL|XL|_D\d+|D\d+)'
        rb'|(?:counter|rr_arb_tree|axi_err_slv|axi_lite_demux|axi_demux|axi_mux|axi_cut|axi_join|axi_isolate|spill_register|fall_through_register|lzc|popcount|lfsr_[a-zA-Z0-9_]*|fifo_[a-zA-Z0-9_]*|stream_[a-zA-Z0-9_]*)'
        rb')$',
        re.IGNORECASE
    )
    
    PP_COND_RE = re.compile(rb'`(?:ifdef|ifndef|elsif|else|endif)\b')

    PORT_IF_RE = re.compile(
        rb'(?:^|_)(?:if|mp|port|bus|io|probe|bfm|master|slave|mst|slv|pm|ps)(?:_|$)|vif|intf|interface|ifc', 
        re.IGNORECASE
    )

    if_kws = [
        b'_if', b'if_', b'vif', b'_vif', b'vif_', 
        b'_ifc', b'ifc_', b'_intf', b'intf_', b'_interface', b'interface_', 
        b'_io', b'io_', b'_bus', b'bus_', 
        b'_probe', b'probe_', b'_port', b'port_', b'_bfm', b'bfm_',
        b'_pm', b'pm_', b'_ps', b'ps_'
    ]

    SV_RESERVED_KWS = frozenset([
        b'module', b'macromodule', b'primitive', b'program', b'interface', 
        b'package', b'class', b'covergroup', b'sequence', b'property', 
        b'clocking', b'function', b'task', b'always', b'always_ff', 
        b'always_comb', b'always_latch', b'initial', b'final', b'assign',
        b'localparam', b'parameter', b'typedef', b'import', b'export',
        b'extends', b'implements', b'virtual', b'pure', b'logic',
        b'reg', b'wire', b'bit', b'byte', b'int', b'integer', b'longint',
        b'shortint', b'time', b'real', b'realtime', b'string', b'event',
        b'chandle', b'type', b'void', b'enum', b'struct', b'union', b'rand',
        b'randc', b'const', b'static', b'automatic', b'local', b'protected',
        b'public', b'randsequence', b'randcase', b'fork', b'join', b'join_any',
        b'join_none', b'begin', b'end', b'endmodule', b'endinterface',
        b'endclass', b'endtask', b'endfunction', b'endprogram', b'endpackage',
        b'endsequence', b'endproperty', b'endclocking', b'endgroup', b'generate',
        b'endgenerate', b'specify', b'endspecify', b'table', b'endtable',
        b'config', b'endconfig', b'defparam', b'checker', b'endchecker',
        b'default', b'assert', b'assume', b'cover', b'expect', b'if', b'else',
        b'while', b'for', b'foreach', b'do', b'case', b'casex', b'casez', b'break',
        b'continue', b'return', b'super', b'this', b'null', b'let', b'cross',
        b'bins', b'illegal_bins', b'ignore_bins', b'intersect', b'first_match',
        b'throughout', b'within', b'with', b'inside', b'matches', b'tagged',
        b'wait', b'wait_order', b'iff', b'dist', b'unique', b'unique0', b'priority',
        b'genvar', b'design', b'instance', b'cell', b'liblist', b'use', b'var', 
        b'new', b'super',
        b'rgba', b'rgb', b'hsl', b'hsla', b'var', b'calc'
    ])

    HEADER_KWS_WITH_SEMI = frozenset([
        b'module', b'macromodule', b'interface', b'class', b'program', 
        b'package', b'covergroup', b'property', b'sequence', 
        b'function', b'task', b'import', b'export', b'typedef', b'parameter', b'localparam',
        b'input', b'output', b'inout', b'wire', b'tri', b'tri0', b'tri1', b'triand', b'trior', b'trireg', 
        b'supply0', b'supply1', b'wand', b'wor', b'reg', b'logic', b'bit', b'byte', b'int', b'integer', b'time',
        b'genvar', b'event', b'string', b'chandle', b'type', b'void', b'enum', b'struct', b'union',
        b'and', b'nand', b'or', b'nor', b'xor', b'xnor', b'buf', b'not', b'bufif0', b'bufif1', b'notif0', b'notif1',
        b'cmos', b'rcmos', b'rnmos', b'rpmos', b'rtran', b'rtranif0', b'rtranif1', b'tran', b'tranif0', b'tranif1',
        b'pullup', b'pulldown', b'assign', b'defparam', b'assert', b'assume', b'cover', b'expect'
    ])
    
    PROCEDURAL_CONTEXT_KWS = frozenset([
        b'function', b'task', b'extern', b'import', b'export', b'pure', b'virtual',
        b'always', b'always_ff', b'always_comb', b'always_latch', b'initial', b'final',
        b'if', b'else', b'while', b'for', b'foreach', b'case', b'casex', b'casez',
        b'repeat', b'return', b'assign', b'generate'
    ])

    _WS = frozenset(b' \t\n\r\x0b\x0c')
    HASH, LPAREN, RPAREN, LBRACKET, RBRACKET, SEMI, COMMA = (
        ord('#'), ord('('), ord(')'), ord('['), ord(']'), ord(';'), ord(',')
    )
    
    PORT_WS_RE = re.compile(rb'\s+')
    def _port_skip_ws(text: bytes, pos: int) -> int:
        m = PORT_WS_RE.match(text, pos)
        return m.end() if m else pos

    def fast_rskip_ws(text: bytes, pos: int) -> int:
        _WS_BYTES = b' \t\n\r\x0b\x0c'
        while pos >= 0 and text[pos] in _WS_BYTES:
            chunk_size = 131072
            start = max(0, pos - chunk_size + 1)
            chunk = text[start:pos+1]
            stripped_len = len(chunk.rstrip(_WS_BYTES))
            if stripped_len == 0:
                pos = start - 1
            else:
                return start + stripped_len - 1
        return pos

    def _port_skip_balanced(text: bytes, start_idx: int, open_char=LPAREN, close_char=RPAREN) -> int:
        depth = 0
        i = start_idx
        n = len(text)
        open_b = bytes([open_char])
        close_b = bytes([close_char])
        
        next_open = text.find(open_b, i)
        next_close = text.find(close_b, i)
        
        while i < n:
            if next_close == -1: return -1
            if next_open != -1 and next_open < i: next_open = text.find(open_b, i)
            if next_close != -1 and next_close < i: next_close = text.find(close_b, i)
                
            if next_open != -1 and next_open < next_close:
                depth += 1
                i = next_open + 1
            else:
                depth -= 1
                if depth == 0: return next_close
                i = next_close + 1
        return -1

    def _safe_skip_to_semi(text: bytes, start_idx: int, max_n: int) -> int:
        curr_idx = start_idx
        while curr_idx < max_n:
            if text[curr_idx] == SEMI:
                return curr_idx + 1
            elif text[curr_idx] == LPAREN:
                ep = _port_skip_balanced(text, curr_idx, LPAREN, RPAREN)
                curr_idx = ep + 1 if ep != -1 else curr_idx + 1
            elif text[curr_idx] == LBRACKET:
                ep = _port_skip_balanced(text, curr_idx, LBRACKET, RBRACKET)
                curr_idx = ep + 1 if ep != -1 else curr_idx + 1
            else:
                curr_idx += 1
        return max_n

    STATEMENT_IDENT_RE = re.compile(rb'(\\[^\s()]+|`?\b[a-zA-Z_]\w*\b)')
    INSTANCE_NAME_RE = re.compile(rb'(\\[^\s()]+|`?\b[a-zA-Z_]\w*\b)')
    VALUE_LIT_RE = re.compile(rb'[0-9.a-zA-Z_]+')
    PP_DIRECTIVE_RE = re.compile(rb'`(ifdef|ifndef|else|elsif|endif)\b')
    IDENT_AT_POS_RE = re.compile(rb'[a-zA-Z_]\w*')
    SUBROUTINE_KW_RE = re.compile(rb'\b(input|output|inout|ref|logic|int|bit|byte|shortint|longint|integer|void|real|shortreal|time)\b')

    # INDEPENDENT LOOP & BLOCK SKIPPERS
    gen_depth = 0
    gen_start = -1
    for m_gen in re.finditer(rb'\b(generate|endgenerate)\b', masked):
        w = m_gen.group(1)
        if w == b'generate':
            if gen_depth == 0: gen_start = m_gen.start()
            gen_depth += 1
        else:
            gen_depth -= 1
            if gen_depth <= 0 and gen_start != -1:
                protected_ranges.append((gen_start, m_gen.end()))
                gen_start = -1
                gen_depth = 0

    scope_depth = 0
    scope_start = -1
    for m_scope in re.finditer(rb'\b(begin|end)\b', masked):
        w = m_scope.group(1)
        if w == b'begin':
            if scope_depth == 0: scope_start = m_scope.start()
            scope_depth += 1
        else:
            scope_depth -= 1
            if scope_depth <= 0 and scope_start != -1:
                protected_ranges.append((scope_start, m_scope.end()))
                scope_start = -1
                scope_depth = 0
                
    for m_loop in re.finditer(rb'\b(for|foreach|while)\b', masked):
        start_idx = m_loop.start()
        curr = _port_skip_ws(masked, m_loop.end())
        if curr < len(masked) and masked[curr] == ord('('):
            end_paren = _port_skip_balanced(masked, curr, ord('('), ord(')'))
            if end_paren != -1:
                next_semi = masked.find(b';', end_paren)
                if next_semi != -1:
                    chunk = masked[end_paren:next_semi]
                    if b'begin' not in chunk:
                        protected_ranges.append((start_idx, next_semi + 1))

    n = len(masked)

    # =========================================================================
    # [BUGFIX] MODULE-SCOPED STANDARD_VARS
    # Track module boundaries so the standard-datatype shield only applies
    # WITHIN the same module it was declared in. Prevents a `logic vif;`
    # declared in one module from wrongly poisoning interface detection
    # for an unrelated `.vif(vif)` instantiation in a different module
    # elsewhere in the same file.
    # =========================================================================
    module_scopes = []
    _mod_depth = 0
    _mod_start = -1
    # [BUGFIX] `macromodule` is a legal (legacy) scope opener: it must be counted,
    # otherwise its orphan `endmodule` drives the depth negative and EVERY later
    # module scope is lost (which silently disables the scope-aware shield below).
    # The depth is also self-healing, so any stray endmodule cannot poison it.
    for m_mod_scope in re.finditer(rb'\b(macromodule|module|endmodule)\b', masked):
        w = m_mod_scope.group(1)
        if w == b'endmodule':
            _mod_depth -= 1
            if _mod_depth <= 0 and _mod_start != -1:
                module_scopes.append([_mod_start, m_mod_scope.end(), set(), set()])
                _mod_start = -1
                _mod_depth = 0
        else:
            if _mod_depth <= 0:            # self-heal (stray endmodule / unbalanced file)
                _mod_start = m_mod_scope.start()
                _mod_depth = 0
            _mod_depth += 1

    def _get_scope_vars(pos: int):
        for m_s, m_e, vset, _iv in module_scopes:
            if m_s <= pos < m_e:
                return vset
        return None

    def _get_scope_iface_vars(pos: int):
        """names of the INTERFACE-typed ports declared in the enclosing module
        header (e.g. `AXI4.Master s_axi`). Used as positive interface evidence -
        `.s_axi(s_axi)` must never be deleted."""
        for m_s, m_e, _sv, ivset in module_scopes:
            if m_s <= pos < m_e:
                return ivset
        return None

    # =========================================================================
    # [BUGFIX] MACRO-AWARE CONNECTION CLASSIFICATION
    # `mask_unwanted_structures()` blanks the CONTENTS of every macro invocation
    # (`` `MACRO(...) `` -> "`MACRO(   )"). That erases the only evidence of what a
    # macro-based connection binds to, so `` `MEM_CONN(memory_bus[2].master) ``
    # looked like an empty/data connection and was DELETED - disconnecting an
    # interface port (xrun: *E,CUIMBC "An interface port declaration must be
    # connected"). Restore the raw text of macro-call bodies for CLASSIFICATION
    # only; structural scanning still uses `masked` unchanged.
    # =========================================================================
    classify_text = bytearray(masked)
    for _mm in re.finditer(rb'`[A-Za-z_]\w*\s*\(', masked):
        _p0 = _mm.end() - 1
        _p1 = _port_skip_balanced(masked, _p0, LPAREN, RPAREN)
        if _p1 != -1 and _p1 > _p0 + 1:
            classify_text[_p0 + 1:_p1] = code[_p0 + 1:_p1]
    classify_text = bytes(classify_text)   # slicing must yield bytes, not bytearray

    # =========================================================================
    # `define table of THIS file: function-like AND object-like, with `\`
    # line-continuations. Scanned in MASKED text so a commented-out `define is
    # not picked up, but read from the raw text (masking preserves offsets).
    # =========================================================================
    _macro_defs = {}
    for _md in re.finditer(rb'`define[ \t]+([A-Za-z_]\w*)', masked):
        _p = _md.end()
        _params = None
        if code[_p:_p+1] == b'(':
            _close = code.find(b')', _p)
            if _close != -1:
                _params = [x.strip() for x in code[_p+1:_close].split(b',') if x.strip()]
                _p = _close + 1
        _body = bytearray()
        while True:
            _nl = code.find(b'\n', _p)
            if _nl == -1: _nl = len(code)
            _line = code[_p:_nl]
            if _line.endswith(b'\\'):            # continued definition
                _body += _line[:-1] + b' '
                _p = _nl + 1
                continue
            _body += _line
            break
        _body = re.sub(rb'//[^\n]*', b' ', bytes(_body)).strip()
        _macro_defs[_md.group(1)] = (_params, _body)

    def _split_top_args(txt: bytes):
        out_args, depth, cur = [], 0, bytearray()
        for ch in txt:
            if ch in b'([{': depth += 1
            elif ch in b')]}': depth -= 1
            if ch == COMMA and depth == 0:
                out_args.append(bytes(cur)); cur = bytearray(); continue
            cur.append(ch)
        out_args.append(bytes(cur))
        return out_args

    def _has_top_level_comma(txt: bytes) -> bool:
        d = 0
        for ch in txt:
            if ch in b'([{': d += 1
            elif ch in b')]}': d -= 1
            elif ch == COMMA and d == 0:
                return True
        return False

    def _expand_macro(txt: bytes, depth: int = 0) -> bytes:
        """textually expand macros defined in THIS file (function-like and
        object-like, bounded depth)."""
        if depth > 3 or b'`' not in txt:
            return txt
        out_b = bytearray()
        i = 0
        while i < len(txt):
            if txt[i:i+1] == b'`':
                m = re.match(rb'`([A-Za-z_]\w*)', txt[i:])
                if m and m.group(1) in _macro_defs:
                    _name = m.group(1)
                    _pars, _bdy = _macro_defs[_name]
                    if _pars is None:                      # object-like
                        out_b += _expand_macro(_bdy, depth + 1)
                        i += m.end()
                        continue
                    _k = i + m.end()
                    while _k < len(txt) and txt[_k:_k+1] in b' \t':
                        _k += 1
                    if txt[_k:_k+1] == b'(':
                        _rp, _d = -1, 0
                        for _q in range(_k, len(txt)):
                            if txt[_q:_q+1] == b'(': _d += 1
                            elif txt[_q:_q+1] == b')':
                                _d -= 1
                                if _d == 0: _rp = _q; break
                        if _rp != -1:
                            _args = _split_top_args(txt[_k+1:_rp])
                            _rep = _bdy
                            for _pa, _ar in zip(_pars, _args):
                                _rep = re.sub(rb'(?<![A-Za-z0-9_])' + re.escape(_pa) + rb'(?![A-Za-z0-9_])',
                                              lambda _m, _v=_ar: _v, _rep)
                            out_b += _expand_macro(_rep, depth + 1)
                            i = _rp + 1
                            continue
                out_b += txt[i:i+1]
                i += 1
                continue
            out_b += txt[i:i+1]
            i += 1
        return bytes(out_b)

    # =========================================================================
    # PHASE 0: HIGH-RELIABILITY INTERFACE HARVESTER
    # =========================================================================
    inst_to_type = set()
    harvest_pattern = rb'\b([a-zA-Z_]\w*(?:_if|if_|vif|_ifc|ifc_|_intf|intf_|interface|_io|io_|_bus|bus_|_probe|probe_|_port|port_|_bfm|bfm_|_pm|pm_|_ps|ps_)\w*)\b'
    words_iter = re.finditer(harvest_pattern, masked, re.IGNORECASE)
    
    for w1_m in words_iter:
        w1 = w1_m.group(1)
        w1_lower = w1.lower()
        if w1_lower in SV_RESERVED_KWS: continue
        
        p_pre = fast_rskip_ws(masked, w1_m.start() - 1)
        if p_pre >= 0 and masked[p_pre] in b'.=<>+-*/&|^?:([{)},!~%`':
            continue
            
        curr = w1_m.end()
        curr = _port_skip_ws(masked, curr)
        
        if curr < n and masked[curr] == ord('#'):
            curr += 1
            curr = _port_skip_ws(masked, curr)
            if curr < n and masked[curr] == ord('('):
                p_end = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                if p_end != -1: curr = _port_skip_ws(masked, p_end + 1)
        
        while True:
            curr = _port_skip_ws(masked, curr)
            m_inst = INSTANCE_NAME_RE.match(masked, curr)
            if not m_inst: break
            
            inst_name = m_inst.group(1)
            inst_lower = inst_name.lower()
            
            if inst_lower in SV_RESERVED_KWS: break
                
            is_if = any(kw in w1_lower for kw in if_kws) or any(kw in inst_lower for kw in if_kws)
            if not is_if: break 
                
            curr = m_inst.end()
            
            array_skip_failed = False
            while True:
                curr = _port_skip_ws(masked, curr)
                if curr < n and masked[curr] == ord('['):
                    b_end = _port_skip_balanced(masked, curr, ord('['), ord(']'))
                    if b_end != -1: curr = _port_skip_ws(masked, b_end + 1)
                    else: array_skip_failed = True; break
                else: break
            if array_skip_failed: break
            
            curr = _port_skip_ws(masked, curr)
            if curr >= n or masked[curr] != ord('('):
                break
                
            p_end = _port_skip_balanced(masked, curr, ord('('), ord(')'))
            if p_end == -1: break
            curr = _port_skip_ws(masked, p_end + 1)
                    
            inst_to_type.add(w1)         
            inst_to_type.add(inst_name)  
            
            if curr < n and masked[curr] == ord(','): 
                curr = _port_skip_ws(masked, curr + 1)
            else: 
                break

    # =========================================================================
    # [BUGFIX] INTERFACE TYPES WHOSE NAME HAS NO MARKER TOKEN
    # `AXI4 s_axi ();` is an interface instance, but "AXI4" contains none of the
    # harvester's marker tokens, so `s_axi` was never recognised and
    # `.s_axi(s_axi)` got deleted -> unconnected interface port -> xrun *E,CUIMBC.
    # Collect the types declared as `interface <name>` plus every type used with a
    # modport (`AXI4.Master s_axi`), then harvest instances of those types.
    # =========================================================================
    iface_types = set()
    for _im in re.finditer(rb'\binterface\s+([A-Za-z_]\w*)', masked):
        iface_types.add(_im.group(1))
    for _im in re.finditer(rb'\b([A-Za-z_]\w*)\s*\.\s*[A-Za-z_]\w*\s+[A-Za-z_]\w*', masked):
        iface_types.add(_im.group(1))
    for _it in iface_types:
        for _im in re.finditer(rb'\b' + re.escape(_it) + rb'\b', masked):
            _j = _port_skip_ws(masked, _im.end())
            if _j < n and masked[_j] == ord('#'):
                _j = _port_skip_ws(masked, _j + 1)
                if _j < n and masked[_j] == ord('('):
                    _e = _port_skip_balanced(masked, _j, LPAREN, RPAREN)
                    if _e == -1: continue
                    _j = _port_skip_ws(masked, _e + 1)
            _mnm = INSTANCE_NAME_RE.match(masked, _j)
            if not _mnm: continue
            _k = _port_skip_ws(masked, _mnm.end())
            while _k < n and masked[_k] == ord('['):
                _e = _port_skip_balanced(masked, _k, LBRACKET, RBRACKET)
                if _e == -1: break
                _k = _port_skip_ws(masked, _e + 1)
            if _k < n and masked[_k] == ord('('):
                inst_to_type.add(_mnm.group(1))
                inst_to_type.add(_im.group(0))

    # =========================================================================
    # [!] PHASE 0.5: STANDARD DATATYPE SHIELD
    # =========================================================================
    tmp_clean = bytearray(masked)
    idx_tmp = 0
    while idx_tmp < n:
        if tmp_clean[idx_tmp] == ord('('):
            end_idx = _port_skip_balanced(tmp_clean, idx_tmp, ord('('), ord(')'))
            if end_idx != -1:
                tmp_clean[idx_tmp:end_idx+1] = b' ' * (end_idx - idx_tmp + 1)
                idx_tmp = end_idx + 1
            else: idx_tmp += 1
        elif tmp_clean[idx_tmp] == ord('['):
            end_idx = _port_skip_balanced(tmp_clean, idx_tmp, ord('['), ord(']'))
            if end_idx != -1:
                tmp_clean[idx_tmp:end_idx+1] = b' ' * (end_idx - idx_tmp + 1)
                idx_tmp = end_idx + 1
            else: idx_tmp += 1
        else:
            idx_tmp += 1

    std_decl_pattern = rb'\b(?:input|output|inout|wire|reg|logic|bit|int|integer|byte|time|real|shortreal|string|event|genvar|parameter|localparam)\b([^;]*?);'
    for block in re.finditer(std_decl_pattern, tmp_clean):
        decl_text = block.group(1)
        clean_decl = re.sub(rb'=.*?($|,)', rb'\1', decl_text, flags=re.DOTALL)
        scope_vars = _get_scope_vars(block.start())
        if scope_vars is None:
            continue
        for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', clean_decl):
            scope_vars.add(ident.group(0))

    mod_pattern = re.compile(rb'\bmodule\b\s+[a-zA-Z_]\w*')
    for m_mod in mod_pattern.finditer(masked):
        scope_vars = _get_scope_vars(m_mod.start())
        if scope_vars is None:
            scope_vars = set()   # orphan header safeguard; never used elsewhere
        scope_iface_vars = _get_scope_iface_vars(m_mod.start())
        if scope_iface_vars is None:
            scope_iface_vars = set()
        curr = m_mod.end()
        while curr < n:
            if masked[curr] in b' \t\n\r\x0b\x0c':
                curr += 1
            elif masked.startswith(b'import', curr) and (curr + 6 < n and masked[curr+6] in b' \t\n\r\x0b\x0c'):
                semi_idx = masked.find(b';', curr)
                if semi_idx != -1: curr = semi_idx + 1
                else: break
            elif masked[curr] == ord('#'):
                curr += 1
                curr = _port_skip_ws(masked, curr)
                if curr < n and masked[curr] == ord('('):
                    ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                    if ep != -1:
                        params_text = masked[curr+1:ep]
                        for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', params_text):
                            scope_vars.add(ident.group(0))
                        curr = ep + 1
                    else: break
            elif masked[curr] == ord('('):
                ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                if ep != -1:
                    ports_text = masked[curr+1:ep]
                    # [BUGFIX] interface-typed header ports (e.g. `AXI4.Master s_axi`)
                    # must NOT enter the standard-datatype shield (that shield would
                    # otherwise veto `.s_axi(s_axi)`), and they must be remembered as
                    # positive interface evidence for this module's instances.
                    _iface_port_names = set()
                    for _tm in re.finditer(rb'\b([A-Za-z_]\w*)\s*\.\s*[A-Za-z_]\w*\s+([A-Za-z_]\w*)', ports_text):
                        _iface_port_names.add(_tm.group(2))
                    for _tm in re.finditer(rb'\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)', ports_text):
                        if _tm.group(1) in iface_types:
                            _iface_port_names.add(_tm.group(2))
                    for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', ports_text):
                        if ident.group(0) not in _iface_port_names:
                            scope_vars.add(ident.group(0))
                    scope_iface_vars.update(_iface_port_names)
                    curr = ep + 1
                else: break 
            elif masked[curr] == ord(';'):
                break 
            else:
                curr += 1

    # =========================================================================

    pos = 0
    modifications = []
    harvested_signals = set()
    instance_count = 0
    target_reached = False

    while pos < n:
        if instance_count >= target_count: break

        in_protected = False
        for p_start, p_end in protected_ranges:
            if p_start <= pos < p_end:
                pos = p_end
                in_protected = True
                break
        if in_protected:
            continue

        failsafe_start_pos = pos
        match = STATEMENT_IDENT_RE.search(masked, pos)
        if not match: break

        match_start = match.start()
        
        in_protected = False
        for p_start, p_end in protected_ranges:
            if p_start <= match_start < p_end:
                pos = p_end
                in_protected = True
                break
        if in_protected: continue

        word1 = match.group(1)
        word1_end = match.end()

        if word1.startswith(b'\\'):
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if word1.startswith(b'`'):
            pos = word1_end
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        clean_word1 = word1.lstrip(b'`')

        if not re.match(rb'^[a-zA-Z_]\w*$', clean_word1):
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if STD_CELL_RE.search(clean_word1):
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if clean_word1 == b'bind':
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if clean_word1 in HEADER_KWS_WITH_SEMI:
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        # [BUGFIX] an interface INSTANTIATION is not a DUT: `AXI4_LITE u_axi (clk);`
        # binds the interface's own clock/parameters. Emptying it silently kills the
        # interface (its clock floats) - so never touch an instance whose TYPE is an
        # interface type.
        if iface_types and clean_word1 in iface_types:
            pos = _safe_skip_to_semi(masked, word1_end, n)
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if clean_word1 in SV_RESERVED_KWS or clean_word1.endswith(b'_t') or clean_word1.endswith(b'_if'):
            pos = word1_end
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        p_before = fast_rskip_ws(masked, match.start() - 1)
        if p_before >= 0 and masked[p_before] in b'.=<>+-*/&|^?:([{)},!~%':
            pos = word1_end
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue
            
        la_curr = _port_skip_ws(masked, word1_end)
        if la_curr >= n: break

        next_char = masked[la_curr]
        is_valid_next = (next_char == HASH or ord('a') <= next_char <= ord('z') or ord('A') <= next_char <= ord('Z') or next_char == ord('_') or next_char == ord('`') or next_char == ord('\\'))

        if not is_valid_next:
            pos = word1_end
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        p_back = match.start() - 1
        is_function_context = False
        prev_words = []
        p_curr = p_back
        
        for _ in range(6): 
            p_curr = fast_rskip_ws(masked, p_curr)
            if p_curr < 0: break
            p_start = p_curr
            
            while p_start >= 0 and (masked[p_start] in b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_\\`'): 
                p_start -= 1
                
            if p_start == p_curr: break
            prev_words.append(masked[p_start+1:p_curr+1])
            p_curr = p_start

        for w in prev_words:
            if w in PROCEDURAL_CONTEXT_KWS:
                is_function_context = True; break
            elif w not in (b'automatic', b'static', b'const', b'begin', b'end'): break 

        if is_function_context:
            pos = word1_end
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        curr = word1_end
        
        statement_modifications = []
        statement_instance_count = 0
        abort_statement = False
        pos_next = curr
        found_any_ports = False
        
        while True:
            curr = _port_skip_ws(masked, curr)
            if curr >= n: break
            m_macro = PP_DIRECTIVE_RE.match(masked, curr)
            if m_macro:
                kw = m_macro.group(1); curr = m_macro.end()
                if kw in (b'ifdef', b'ifndef', b'elsif'):
                    curr = _port_skip_ws(masked, curr)
                    m_ident = IDENT_AT_POS_RE.match(masked, curr)
                    if m_ident: curr = m_ident.end()
                continue
                
            if masked[curr] == HASH:
                curr_look = _port_skip_ws(masked, curr + 1)
                if curr_look < n:
                    if masked[curr_look] == LPAREN:
                        end_hash_paren = _port_skip_balanced(masked, curr_look, LPAREN, RPAREN)
                        if end_hash_paren == -1: 
                            abort_statement = True
                            break
                        curr = end_hash_paren + 1
                    else:
                        m_val = VALUE_LIT_RE.match(masked, curr_look)
                        if m_val: curr = m_val.end()
                        else: curr = curr_look
                curr = _port_skip_ws(masked, curr)
                continue 
            
            match2 = INSTANCE_NAME_RE.match(masked, curr)
            if not match2: 
                abort_statement = True
                break
                
            word2 = match2.group(1)
            curr = match2.end()
            
            if word2.startswith(b'`') or word2.lstrip(b'`') in SV_RESERVED_KWS: 
                abort_statement = True
                break

            w1_lower = word1.lower()
            w2_lower = word2.lower()
            
            if STATEMENT_ABORT_RE.search(w1_lower) or STATEMENT_ABORT_RE.search(w2_lower):
                abort_statement = True
                break 

            array_skip_failed = False
            while True:
                curr = _port_skip_ws(masked, curr)
                if curr >= n: break
                m_macro = PP_DIRECTIVE_RE.match(masked, curr)
                if m_macro:
                    kw = m_macro.group(1); curr = m_macro.end()
                    if kw in (b'ifdef', b'ifndef', b'elsif'):
                        curr = _port_skip_ws(masked, curr)
                        m_ident = IDENT_AT_POS_RE.match(masked, curr)
                        if m_ident: curr = m_ident.end()
                    continue

                if masked[curr] == LBRACKET:
                    end_bracket = _port_skip_balanced(masked, curr, LBRACKET, RBRACKET)
                    if end_bracket == -1: array_skip_failed = True; break
                    curr = end_bracket + 1
                else: break
                    
            if array_skip_failed: 
                abort_statement = True
                break
            
            curr = _port_skip_ws(masked, curr)
            if curr < n:
                c_check = masked[curr]
                if (ord('a') <= c_check <= ord('z')) or (ord('A') <= c_check <= ord('Z')) or (ord('0') <= c_check <= ord('9')) or c_check == ord('_') or c_check == ord('\\'):
                    abort_statement = True
                    break 
                
            port_modifications = []
            is_valid_terminator = False
            is_comma = False
            abort_instance = False
            
            while curr < n:
                curr = _port_skip_ws(masked, curr)
                if curr >= n: break
                
                m_macro = PP_DIRECTIVE_RE.match(masked, curr)
                if m_macro:
                    kw = m_macro.group(1); curr = m_macro.end()
                    if kw in (b'ifdef', b'ifndef', b'elsif'):
                        curr = _port_skip_ws(masked, curr)
                        m_ident = IDENT_AT_POS_RE.match(masked, curr)
                        if m_ident: curr = m_ident.end()
                    continue
                    
                if masked[curr] == LPAREN:
                    port_start = curr
                    arg_start = _port_skip_ws(masked, port_start + 1)
                    
                    m_subroutine = SUBROUTINE_KW_RE.match(masked, arg_start)
                    is_function_prototype = False
                    if m_subroutine: is_function_prototype = True
                    else:
                        arg_content = masked[arg_start:port_start+50] 
                        if re.match(rb'[a-zA-Z_]\w*\s+[a-zA-Z_]\w*\s*[,)]', arg_content): is_function_prototype = True
                    if is_function_prototype:
                        p_after = _port_skip_ws(masked, m_subroutine.end() if m_subroutine else arg_start)
                        if p_after < n and masked[p_after] != ord('\''):
                            abort_instance = True; break 
                            
                    end_paren = _port_skip_balanced(masked, port_start, LPAREN, RPAREN)
                    if end_paren == -1: abort_instance = True; break

                    # [BUGFIX] Conditional compilation inside a port-list makes the flat
                    # argument/comma model ambiguous: a comma that is trailing in one branch
                    # is an interior comma in the other. Deleting across an `else/`endif
                    # boundary can therefore emit a trailing comma before ')'. Refuse to
                    # touch such an instance instead of emitting possibly-illegal code.
                    if PP_COND_RE.search(masked[port_start:end_paren]):
                        abort_instance = True; break

                    arg_i = port_start + 1
                    ports = []
                    
                    while arg_i < end_paren:
                        arg_i = _port_skip_ws(masked, arg_i)
                        if arg_i >= end_paren: break
                        
                        m_macro = PP_DIRECTIVE_RE.match(masked, arg_i)
                        if m_macro:
                            kw = m_macro.group(1); arg_i = m_macro.end()
                            if kw in (b'ifdef', b'ifndef', b'elsif'):
                                arg_i = _port_skip_ws(masked, arg_i)
                                m_ident = IDENT_AT_POS_RE.match(masked, arg_i)
                                if m_ident: arg_i = m_ident.end()
                            continue
                            
                        arg_start_idx = arg_i
                        arg_end_idx = arg_i
                        depth = 0
                        while arg_end_idx < end_paren:
                            c = masked[arg_end_idx]
                            if c in (LPAREN, LBRACKET, ord('{')): depth += 1
                            elif c in (RPAREN, RBRACKET, ord('}')): depth -= 1
                            elif c == COMMA and depth == 0: break
                            arg_end_idx += 1
                            
                        arg_bytes = classify_text[arg_start_idx:arg_end_idx]
                        
                        is_named = False
                        port_name = b""
                        conn_val = b""
                        
                        arg_check = arg_bytes.strip()
                        if arg_check.startswith(b'(*'):
                            attr_end = arg_check.find(b'*)')
                            if attr_end != -1: arg_check = arg_check[attr_end+2:].strip()

                        # [BUGFIX] look through macros defined in this file before
                        # deciding: `` `MEM_CONN(x) .mem(x) `` must classify as the
                        # named interface connection `.mem(...)`, not as data.
                        _raw_arg = arg_check
                        _wildcard_arg = (_raw_arg == b'.*')          # `.*` binds every port by name
                        arg_check = _expand_macro(arg_check)
                        _unresolved_macro = (arg_check == _raw_arg
                                             and re.match(rb'`[A-Za-z_]\w*', _raw_arg) is not None)
                        # a macro that expands to a WHOLE PORT LIST (object-like
                        # `DUT_PORTS, or `DUT_BUNDLE(...) with a top-level comma) cannot
                        # be split: deleting it would drop every connection, interfaces
                        # included. Leave such an argument alone.
                        _macro_bundle = _has_top_level_comma(arg_check)

                        # [BUGFIX] ordered (positional) vs named connection.
                        # `.*` and `.port` / `.port(...)` are named; a bare expression
                        # is an ordered connection whose meaning depends on its position.
                        is_positional = not arg_check.startswith(b'.')

                        m_cast = re.match(rb'^[a-zA-Z0-9_:]+\s*\'\(\s*(.*)\s*\)$', arg_check)
                        if m_cast:
                            arg_check = m_cast.group(1).strip()
                                
                        m_named = re.match(rb'^\.\s*([a-zA-Z_]\w*)', arg_check)
                        if m_named:
                            is_named = True
                            port_name = m_named.group(1)
                            open_p = arg_check.find(b'(')
                            close_p = arg_check.rfind(b')')
                            if open_p != -1 and close_p != -1 and close_p > open_p:
                                conn_val = arg_check[open_p+1:close_p].strip()
                            else:
                                conn_val = port_name 
                        else:
                            conn_val = arg_check 
                            
                        if conn_val.startswith(b'(*'):
                            attr_end = conn_val.find(b'*)')
                            if attr_end != -1: conn_val = conn_val[attr_end+2:].strip()
                                
                        last_dot = conn_val.rfind(b'.')
                        if last_dot != -1:
                            terminal_val = conn_val[last_dot+1:].strip()
                            base_val = conn_val[:last_dot].strip()
                        else:
                            terminal_val = conn_val
                            base_val = conn_val
                            
                        base_clean = re.sub(rb'\[.*?\]', b'', base_val).strip()
                        terminal_clean = re.sub(rb'\[.*?\]', b'', terminal_val).strip()
                        
                        terminal_lower = terminal_val.lower()
                        port_name_lower = port_name.lower()
                        
                        is_interface = False
                        if arg_check != b'.*':
                            if is_named and PORT_IF_RE.search(port_name_lower):
                                is_interface = True
                            elif last_dot == -1:
                                if base_clean in inst_to_type or terminal_clean in inst_to_type:
                                    is_interface = True
                                elif PORT_IF_RE.search(terminal_lower):
                                    if not any(op in terminal_clean for op in b'+-*/&|^~?!'):
                                        is_interface = True
                            else:
                                if PORT_IF_RE.search(terminal_lower):
                                    if base_clean in inst_to_type:
                                        is_interface = True
                                    else:
                                        # [BUGFIX] hierarchical / dotted value such as
                                        # `top_env.agent_h.vif_h`: the terminal name looks like
                                        # an interface, but the prefix is a hierarchy path, not a
                                        # known local interface instance. Being wrong here DISCONNECTS
                                        # a live interface (illegal), so stay conservative and keep it.
                                        is_interface = True
                                        
                        _scope_vars = _get_scope_vars(arg_start_idx)
                        if _scope_vars and (base_clean in _scope_vars or terminal_clean in _scope_vars):
                            is_interface = False

                        # [BUGFIX] a macro invocation whose definition is not in this file
                        # cannot be resolved (no preprocessor run): keep the connection
                        # rather than risk disconnecting an interface port (illegal).
                        if _unresolved_macro or _wildcard_arg or _macro_bundle:
                            is_interface = True

                        # [BUGFIX] a value that IS an interface-typed port of this very
                        # module (`AXI4.Master s_axi` + `.s_axi(s_axi)`) is an interface.
                        # This must win over the standard-datatype shield above.
                        _scope_iface = _get_scope_iface_vars(arg_start_idx)
                        if _scope_iface and (base_clean in _scope_iface or terminal_clean in _scope_iface):
                            is_interface = True
                                    
                        curr_look = arg_end_idx
                        trail_comma_idx = -1
                        while curr_look < end_paren:
                            if masked[curr_look] in b' \t\n\r\x0b\x0c':
                                curr_look += 1
                            elif masked[curr_look] == COMMA:
                                trail_comma_idx = curr_look
                                break
                            else:
                                break
                                
                        ports.append({
                            'start': arg_start_idx,
                            'end': arg_end_idx,
                            'is_deleted': not is_interface,
                            'trail_comma': trail_comma_idx,
                            'conn_val': conn_val,
                            'is_positional': is_positional
                        })
                        
                        if trail_comma_idx != -1:
                            arg_i = trail_comma_idx + 1
                        else:
                            arg_i = arg_end_idx + 1

                    # =====================================================
                    # [!] POSITIONAL CONNECTION SAFETY
                    # In an ordered port list a connection is bound by its POSITION,
                    # so deleting connection k re-binds every later connection to the
                    # previous port (e.g. an interface value would silently land on a
                    # scalar net port -> elaboration error). Only a contiguous run AT
                    # THE END of the ordered region can be removed without moving
                    # anything, so any ordered connection that precedes a kept ordered
                    # connection must stay untouched.
                    # =====================================================
                    _kept_ord_to_the_right = False
                    for _i in range(len(ports) - 1, -1, -1):
                        _p = ports[_i]
                        if not _p.get('is_positional'):
                            continue                      # named: binds by name, never shifts
                        if _p['is_deleted'] and not _kept_ord_to_the_right:
                            continue                      # still inside the removable tail run
                        _kept_ord_to_the_right = True     # a survivor blocks all earlier ordered args
                        if _p['is_deleted']:
                            _p['is_deleted'] = False

                    port_modifications = []
                    
                    for p in ports:
                        if p['is_deleted'] and p['conn_val']:
                            if re.match(rb'^[a-zA-Z_]\w*$', p['conn_val']):
                                if not p['conn_val'].isupper():
                                    harvested_signals.add(p['conn_val'])
                                
                    i = 0
                    while i < len(ports):
                        if ports[i]['is_deleted']:
                            j = i
                            while j + 1 < len(ports) and ports[j + 1]['is_deleted']:
                                j += 1
                                
                            if j < len(ports) - 1:
                                wipe_end = ports[j]['trail_comma'] + 1
                                while wipe_end < end_paren and masked[wipe_end] in b' \t':
                                    wipe_end += 1
                                if wipe_end < end_paren and masked[wipe_end] in b'\n\r':
                                    if masked[wipe_end] == ord('\r'): wipe_end += 1
                                    if wipe_end < end_paren and masked[wipe_end] == ord('\n'): wipe_end += 1
                                    
                                wipe_start = ports[i]['start']
                                while wipe_start > port_start + 1 and masked[wipe_start - 1] in b' \t\n\r\x0b\x0c':
                                    wipe_start -= 1
                                    
                                port_modifications.append((wipe_start, wipe_end, b""))
                            else:
                                # [!] PRECISION LAST-PORT STRIPPER 
                                # Protects `endif` and other macros by explicitly deleting the exact port
                                # sequence and ONLY the preceding comma character, preserving whitespace/macros.
                                wipe_start = ports[i]['start']
                                wipe_end = ports[j]['end']
                                if ports[j]['trail_comma'] != -1:
                                    wipe_end = ports[j]['trail_comma'] + 1
                                    
                                while wipe_end < end_paren and masked[wipe_end] in b' \t':
                                    wipe_end += 1
                                if wipe_end < end_paren and masked[wipe_end] in b'\n\r':
                                    if masked[wipe_end] == ord('\r'): wipe_end += 1
                                    if wipe_end < end_paren and masked[wipe_end] == ord('\n'): wipe_end += 1
                                    
                                port_modifications.append((wipe_start, wipe_end, b""))
                                
                                if i != 0:
                                    prec_comma_idx = ports[i-1]['trail_comma']
                                    if prec_comma_idx != -1:
                                        port_modifications.append((prec_comma_idx, prec_comma_idx + 1, b""))
                            i = j + 1
                        else:
                            i += 1

                    found_any_ports = True
                    curr = end_paren + 1
                    continue
                    
                if masked[curr] == SEMI:
                    is_valid_terminator = True; pos_next = curr + 1; break

                elif masked[curr] == COMMA:
                    is_valid_terminator = True
                    is_comma = True
                    pos_next = curr + 1
                    break
                    
                abort_instance = True
                break
                
            if abort_instance or not is_valid_terminator:
                abort_statement = True
                break
                
            if is_valid_terminator:
                statement_modifications.extend(port_modifications)
                
                if found_any_ports:
                    statement_instance_count += 1
                
                if is_comma:
                    curr = pos_next
                    continue
                else:
                    break 

        if abort_statement:
            pos = word1_end 
            if pos <= failsafe_start_pos: pos = failsafe_start_pos + 1
            continue

        if found_any_ports and instance_count < target_count:
            modifications.extend(statement_modifications)
            instance_count += statement_instance_count
            if instance_count >= target_count: target_reached = True

        pos = pos_next
        if target_reached: break
            
    # =========================================================================
    # [DISABLED] DUAL-DATABASE WIRE INJECTION (UNIVERSAL SCOPE EXTRACTOR)
    # Disabled by default: In professional ASIC and FPGA development, relying 
    # on implicit wire generation is highly discouraged and often blocked by 
    # `default_nettype none` and linting tools.
    # Uncomment the multi-line string (r''') below to re-enable if needed.
    # =========================================================================
    r'''
    if harvested_signals and modifications:
        
        mod_pattern = re.compile(rb'\bmodule\b\s+[a-zA-Z_]\w*')
        existing_decls = set()
        
        for m_mod in mod_pattern.finditer(masked):
            if m_mod.start() > modifications[0][0]:
                break
            
            curr = m_mod.end()
            while curr < n:
                if masked[curr] in b' \t\n\r\x0b\x0c':
                    curr += 1
                elif masked.startswith(b'import', curr) and (curr + 6 < n and masked[curr+6] in b' \t\n\r\x0b\x0c'):
                    semi_idx = masked.find(b';', curr)
                    if semi_idx != -1:
                        curr = semi_idx + 1
                    else:
                        break
                elif masked[curr] == ord('#'):
                    curr += 1
                    curr = _port_skip_ws(masked, curr)
                    if curr < n and masked[curr] == ord('('):
                        ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                        if ep != -1:
                            params_text = masked[curr+1:ep]
                            for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', params_text):
                                existing_decls.add(ident.group(0))
                            curr = ep + 1
                        else:
                            break
                elif masked[curr] == ord('('):
                    ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                    if ep != -1:
                        ports_text = masked[curr+1:ep]
                        for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', ports_text):
                            existing_decls.add(ident.group(0))
                        curr = ep + 1
                    else:
                        break 
                elif masked[curr] == ord(';'):
                    curr += 1
                    if curr <= modifications[0][0]:
                        insert_pos = curr
                    break 
                else:
                    curr += 1
        
        clean_code = bytearray(masked)
        idx = 0
        n_clean = len(clean_code)
        
        while idx < n_clean:
            if clean_code[idx] == ord('('):
                end_idx = _port_skip_balanced(clean_code, idx, ord('('), ord(')'))
                if end_idx != -1:
                    clean_code[idx:end_idx+1] = b' ' * (end_idx - idx + 1)
                    idx = end_idx + 1
                else:
                    idx += 1
            elif clean_code[idx] == ord('['):
                end_idx = _port_skip_balanced(clean_code, idx, ord('['), ord(']'))
                if end_idx != -1:
                    clean_code[idx:end_idx+1] = b' ' * (end_idx - idx + 1)
                    idx = end_idx + 1
                else:
                    idx += 1
            else:
                idx += 1
        
        for ident in re.finditer(rb'\b[a-zA-Z_]\w*\b', clean_code):
            existing_decls.add(ident.group(0))
                
        safe_signals_to_inject = harvested_signals - existing_decls - inst_to_type
        
        if safe_signals_to_inject:
            wire_code = b"\n\n    // ==========================================\n"
            wire_code += b"    // AUTO-INJECTED WIRES (Duplicate-Proof)\n"
            wire_code += b"    // ==========================================\n"
            for sig in sorted(safe_signals_to_inject):
                wire_code += b"    wire " + sig + b";\n"
                
            first_mod_pos = modifications[0][0]
            insert_pos = 0
            
            for m in re.finditer(rb'\bmodule\b.*?;', masked, re.DOTALL):
                if m.start() > first_mod_pos:
                    break
                
                curr = m.end()
                while curr < n:
                    if masked[curr] in b' \t\n\r\x0b\x0c':
                        curr += 1
                    elif masked.startswith(b'import', curr) and (curr + 6 < n and masked[curr+6] in b' \t\n\r\x0b\x0c'):
                        semi_idx = masked.find(b';', curr)
                        if semi_idx != -1:
                            curr = semi_idx + 1
                        else:
                            break
                    elif masked[curr] == ord('#'):
                        curr += 1
                        curr = _port_skip_ws(masked, curr)
                        if curr < n and masked[curr] == ord('('):
                            ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                            curr = ep + 1 if ep != -1 else curr + 1
                    elif masked[curr] == ord('('):
                        ep = _port_skip_balanced(masked, curr, ord('('), ord(')'))
                        curr = ep + 1 if ep != -1 else curr + 1
                    elif masked[curr] == ord(';'):
                        curr += 1
                        if curr <= first_mod_pos:
                            insert_pos = curr
                        break
                    else:
                        curr += 1
                        
            if insert_pos > 0:
                modifications.append((insert_pos, insert_pos, wire_code))
    '''

    code = _apply_ranges(code, masked, modifications, snap_to_lines=False)
    return code, bool(modifications)

























# /////////////////comment out virtual interface connection ///////////////////////////////////








# def auto_remove_interface_ports(code: bytes, masked: bytes, ctx: ParserContext = None) -> bytes:
#     """Automatically hunts down and modifies interface port connections and safely comments out UVM virtual interfaces."""
#     if ctx is None:
#         ctx = ParserContext(code)
        
#     protected_ranges = _find_procedural_ranges(code, masked, ctx)
#     modifications = []
    
#     # =========================================================================
#     # 1. THE UVM_CONFIG_DB SEVER
#     # Wraps uvm_config_db#(virtual ...)::set(...) in a block comment
#     # so it can be easily uncommented later.
#     # =========================================================================
#     uvm_re = re.compile(rb'\buvm_config_db\s*#\s*\(\s*virtual\b[^\)]*\)\s*::\s*set\s*\(')
#     pos = 0
#     n = len(masked)
#     while pos < n:
#         m = uvm_re.search(masked, pos)
#         if not m:
#             break
#         # Find the closing semicolon for the set() statement
#         semi_idx = masked.find(b';', m.end())
#         if semi_idx != -1:
#             # Extract the original code so we don't lose it, and wrap it in comments
#             original_statement = code[m.start() : semi_idx + 1]
#             commented_statement = b"/* " + original_statement + b" */"
            
#             modifications.append((m.start(), semi_idx + 1, commented_statement))
#             pos = semi_idx + 1
#         else:
#             pos = m.end()

#     # =========================================================================
#     # 2. THE TARGETED INTERFACE PORT REMOVER
#     # =========================================================================
#     # STRICT REGEX: Only matches a dot if it immediately follows a comma or '('
#     # This prevents accidentally matching object.method() calls!
#     port_re = re.compile(rb'([,\(]\s*)\.\s*([a-zA-Z_]\w*)\b')
#     if_re = re.compile(rb'(_if|vif|_vif|_intf|_interface)\b', re.IGNORECASE)
    
#     pos = 0
#     while pos < n:
#         m = port_re.search(masked, pos)
#         if not m:
#             break
            
#         port_name = m.group(2)
#         abs_end = m.end()
        
#         # [!] SAFETY GUARD: Ensure we are outside procedural blocks / classes
#         in_protected = False
#         for p_start, p_end in protected_ranges:
#             if p_start <= abs_end < p_end:
#                 pos = p_end
#                 in_protected = True
#                 break
                
#         if in_protected:
#             continue
            
#         # Walk forward to find the opening parenthesis '(' for the port mapping
#         curr = abs_end
#         while curr < n and masked[curr] in b' \t\n\r\x0b\x0c':
#             curr += 1
            
#         if curr < n and masked[curr] == ord('('):
#             end_paren = _skip_balanced_parens(masked, curr)
#             if end_paren > curr + 1:
#                 signal_content = code[curr+1 : end_paren-1]
                
#                 # Hierarchical dot sniper (analyze only the final signal name)
#                 mapped_val = signal_content.strip()
#                 if mapped_val.startswith(b'(*'):
#                     attr_end = mapped_val.find(b'*)')
#                     if attr_end != -1:
#                         mapped_val = mapped_val[attr_end+2:].strip()
                        
#                 last_dot = mapped_val.rfind(b'.')
#                 terminal_val = mapped_val[last_dot+1:].strip() if last_dot != -1 else mapped_val
                
#                 # SCENARIO 1: The PORT NAME is an interface
#                 if if_re.search(port_name):
#                     modifications.append((curr + 1, end_paren - 1, b"dummy_plug"))
                
#                 # SCENARIO 2: The terminal SIGNAL is an interface
#                 elif if_re.search(terminal_val):
#                     # Make sure it's not a math expression/concatenation
#                     if not any(op in terminal_val for op in b'{}+-*/&|^~?!'):
#                         modifications.append((curr + 1, end_paren - 1, b"dummy_plug"))
                    
#             pos = end_paren
#             continue
            
#         pos = abs_end
        
#     return _apply_ranges(code, masked, modifications)




#////////////////////////////////////////////////////////////////////////////////////////////////


import re

def auto_remove_interface_ports(code: bytes, masked: bytes, ctx: ParserContext = None) -> bytes:
    """Automatically hunts down interface ports, completely isolates DUT/UVM, and strictly relies on the Instance Database."""
    if ctx is None:
        ctx = ParserContext(code)
        
    protected_ranges = _find_procedural_ranges(code, masked, ctx)
    modifications = []
    
    # Harvester Databases
    inst_to_type = {}
    inst_to_params = {}
    inst_to_ports = {}
    inst_to_array = {}
    inst_to_pos = {}  # Tracks the exact byte position of the interface in the file
    
    uvm_insts_needed = set()
    dut_insts_needed = set()
    
    # Expanded Lexicons
    if_kws = [b'_if', b'if_', b'vif', b'_vif', b'vif_', b'_ifc', b'ifc_',b'_intf', b'intf_', b'_interface', b'interface_', b'_io', b'io_', b'_bus', b'bus_']
    mp_kws = [b'_mp', b'mp_', b'master', b'slave', b'mst', b'slv', b'modport']

    SV_RESERVED_KWS = frozenset([
        b'module', b'macromodule', b'primitive', b'program', b'interface', 
        b'package', b'class', b'covergroup', b'sequence', b'property', 
        b'clocking', b'function', b'task', b'always', b'always_ff', 
        b'always_comb', b'always_latch', b'initial', b'final', b'assign',
        b'localparam', b'parameter', b'typedef', b'import', b'export',
        b'bind', b'extends', b'implements', b'virtual', b'pure', b'logic',
        b'reg', b'wire', b'bit', b'byte', b'int', b'integer', b'longint',
        b'shortint', b'time', b'real', b'realtime', b'string', b'event',
        b'chandle', b'type', b'void', b'enum', b'struct', b'union', b'rand',
        b'randc', b'const', b'static', b'automatic', b'local', b'protected',
        b'public', b'randsequence', b'randcase', b'fork', b'join', b'join_any',
        b'join_none', b'begin', b'end', b'endmodule', b'endinterface',
        b'endclass', b'endtask', b'endfunction', b'endprogram', b'endpackage',
        b'endsequence', b'endproperty', b'endclocking', b'endgroup', b'generate',
        b'endgenerate', b'specify', b'endspecify', b'table', b'endtable',
        b'config', b'endconfig', b'defparam', b'checker', b'endchecker',
        b'default', b'assert', b'assume', b'cover', b'expect', b'if', b'else',
        b'while', b'for', b'foreach', b'do', b'case', b'casex', b'casez', b'break',
        b'continue', b'return', b'super', b'this', b'null', b'let', b'cross',
        b'bins', b'illegal_bins', b'ignore_bins', b'intersect', b'first_match',
        b'throughout', b'within', b'with', b'inside', b'matches', b'tagged',
        b'wait', b'wait_order', b'iff', b'dist', b'unique', b'unique0', b'priority',
        b'genvar', b'design', b'instance', b'cell', b'liblist', b'use', b'input', b'output', b'inout',
        
        # Web Blocklist (JS / CSS)
        b'var', b'const', b'let', b'new', b'typeof', b'instanceof', b'switch', 
        b'catch', b'try', b'finally', b'delete', b'yield', b'await', b'async', b'throw',
        b'rgba', b'rgb', b'hsl', b'hsla', b'url', b'calc', b'console', b'document', b'window',
        b'Math', b'Date', b'JSON', b'Object', b'Array', b'String', b'Number', b'Boolean', 
        b'RegExp', b'Error', b'Symbol', b'Promise', b'prototype', b'constructor', b'length',
        b'call', b'apply', b'undefined', b'NaN', b'Infinity', b'module', b'exports', b'require', 
        b'define', b'arguments', b'setTimeout', b'setInterval', b'clearTimeout', b'clearInterval',
        b'infinite', b'steps', b'format', b'rotate', b'scale', b'translate', b'matrix', b'linear', 
        b'ease', b'normal', b'bold', b'italic', b'solid', b'dashed', b'dotted', b'none', b'block', 
        b'inline', b'flex', b'grid', b'absolute', b'relative', b'fixed', b'transparent', b'inherit', 
        b'initial', b'unset', b'font', b'src', b'content', b'margin', b'padding', b'display', b'position',
        b'border', b'color', b'background', b'width', b'height', b'top', b'left', b'right', b'bottom',
        b'opacity', b'overflow', b'cursor', b'transform', b'transition', b'animation', b'webkit', 
        b'moz', b'ms', b'o', b'keyframes', b'media', b'charset', b'supports', b'true', b'false', 
        b'null', b'debugger', b'eval', b'super'
    ])

    # --- Helper Function ---
    def _skip_balanced_parens(text: bytes, start_idx: int, open_char=ord('('), close_char=ord(')')) -> int:
        depth = 0
        i = start_idx
        n = len(text)
        while i < n:
            if text[i] == open_char: depth += 1
            elif text[i] == close_char:
                depth -= 1
                if depth == 0: return i
            i += 1
        return -1

    PORT_WS_RE = re.compile(rb'\s+')
    def _port_skip_ws(text: bytes, pos: int) -> int:
        m = PORT_WS_RE.match(text, pos)
        return m.end() if m else pos

    INSTANCE_NAME_RE = re.compile(rb'(\\[^\s()]+|`?[a-zA-Z_]\w*\b)')

    n = len(masked)

    # =========================================================================
    # 0. THE ULTIMATE INTERFACE HARVESTER (10x OPTIMIZED)
    # =========================================================================
    harvest_pattern = rb'\b([a-zA-Z_]\w*(?:_if|if_|vif|_ifc|ifc_|_intf|intf_|interface|_io|io_|_bus|bus_)\w*)\b'
    words_iter = re.finditer(harvest_pattern, masked, re.IGNORECASE)
    
    for w1_m in words_iter:
        w1 = w1_m.group(1)
        w1_lower = w1.lower()
        if w1_lower in SV_RESERVED_KWS:
            continue
            
        if any(kw in w1_lower for kw in if_kws):
            curr = w1_m.end()
            curr = _port_skip_ws(masked, curr)
            
            params_str = b""
            if curr < n and masked[curr] == ord('#'):
                hash_pos = curr
                curr += 1
                curr = _port_skip_ws(masked, curr)
                if curr < n and masked[curr] == ord('('):
                    p_end = _skip_balanced_parens(masked, curr)
                    if p_end != -1:
                        params_str = code[hash_pos : p_end+1]
                        curr = _port_skip_ws(masked, p_end + 1)
            
            while True:
                curr = _port_skip_ws(masked, curr)
                m_inst = INSTANCE_NAME_RE.match(masked, curr)
                if not m_inst: break
                
                inst_name = m_inst.group(1)
                
                if inst_name.lower() in SV_RESERVED_KWS:
                    break
                    
                curr = m_inst.end()
                
                arr_dim = b""
                array_skip_failed = False
                while True:
                    curr = _port_skip_ws(masked, curr)
                    if curr < n and masked[curr] == ord('['):
                        b_end = _skip_balanced_parens(masked, curr, ord('['), ord(']'))
                        if b_end != -1: 
                            arr_dim += code[curr : b_end+1]
                            curr = _port_skip_ws(masked, b_end + 1)
                        else: array_skip_failed = True; break
                    else: break
                if array_skip_failed: break
                
                ports_str = b""
                if curr < n and masked[curr] == ord('('):
                    p_end = _skip_balanced_parens(masked, curr)
                    if p_end != -1:
                        ports_str = code[curr : p_end+1]
                        curr = _port_skip_ws(masked, p_end + 1)
                        
                # =====================================================================
                # [!] THE ANSI PORT FIX: 
                # If there are no parentheses, it's an ANSI port or virtual interface. 
                # We strictly exclude it from the database so it remains untouched!
                # =====================================================================
                if ports_str != b"":
                    inst_to_type[inst_name] = w1
                    inst_to_params[inst_name] = params_str
                    inst_to_ports[inst_name] = ports_str
                    inst_to_array[inst_name] = arr_dim
                    inst_to_pos[inst_name] = curr
                
                if curr < n and masked[curr] == ord(','):
                    curr = _port_skip_ws(masked, curr + 1)
                else:
                    break

    # =========================================================================
    # 1. THE UVM_CONFIG_DB REDIRECTOR (UVM ISOLATION)
    # =========================================================================
    uvm_re = re.compile(
        rb'(\buvm_config_db\s*#\s*\(\s*virtual\s+)'   
        rb'([a-zA-Z_][a-zA-Z0-9_\.]*)'                
        rb'(\s*\)\s*::\s*set\s*)'                     
    )
    
    pos = 0
    while pos < n:
        m = uvm_re.search(masked, pos)
        if not m: break
            
        open_paren = masked.find(b'(', m.end())
        if open_paren != -1:
            close_paren = _skip_balanced_parens(masked, open_paren)
            if close_paren != -1:
                arg_start = open_paren + 1
                arg_end = close_paren
                
                depth = 0
                in_string = False
                last_comma_idx = -1
                
                i = arg_start
                while i < arg_end:
                    c = masked[i]
                    if c == ord('"') and (i == 0 or masked[i-1] != ord('\\')):
                        in_string = not in_string
                    elif not in_string:
                        if c in (ord('('), ord('['), ord('{')): depth += 1
                        elif c in (ord(')'), ord(']'), ord('}')): depth -= 1
                        elif c == ord(',') and depth == 0: last_comma_idx = i
                    i += 1
                
                if last_comma_idx != -1:
                    if_inst = code[last_comma_idx + 1 : arg_end].strip()
                    if_inst_clean = re.sub(rb'\[.*?\]', b'', if_inst).strip()
                    
                    # =====================================================================
                    # [!] THE DATABASE GATEKEEPER:
                    # We only redirect the UVM connection if the interface is in the database!
                    # =====================================================================
                    if if_inst_clean in inst_to_type:
                        uvm_insts_needed.add(if_inst_clean)
                        dummy_name = if_inst.replace(if_inst_clean, b"dummy_uvm_plug_" + if_inst_clean, 1)
                        
                        prefix = code[m.start() : last_comma_idx + 1]
                        redirected_statement = prefix + b" " + dummy_name + b" /* " + if_inst + b" */)"
                        modifications.append((m.start(), close_paren + 1, redirected_statement))
                    
            pos = close_paren + 1 if close_paren != -1 else m.end()
        else:
            pos = m.end()

    # =========================================================================
    # 2. THE TARGETED INTERFACE PORT REMOVER (DUT ISOLATION)
    # =========================================================================
    port_re = re.compile(rb'([,\(]\s*)\.\s*([a-zA-Z_]\w*)\b')
    IF_KW_RE = re.compile(rb'(?:^|_)(?:if|mp|port|bus|io)(?:_|$)|vif|intf|interface', re.IGNORECASE)
    
    pos = 0
    while pos < n:
        m = port_re.search(masked, pos)
        if not m: break
            
        port_name = m.group(2)
        abs_end = m.end()
        match_start = m.start()
        
        in_protected = False
        for p_start, p_end in protected_ranges:
            if p_start <= abs_end < p_end:
                pos = p_end
                in_protected = True
                break
                
        if in_protected: continue
            
        depth = 0
        i = match_start
        if masked[i] == ord('('):
            depth = 1
            i -= 1
            
        while i >= 0:
            c = masked[i]
            if c == ord(')'): depth += 1
            elif c == ord('('):
                if depth == 0: break
                depth -= 1
            i -= 1
            
        is_interface_inst = False
        if i >= 0:
            prefix_chunk = masked[max(0, i-100):i]
            words = re.findall(rb'[a-zA-Z_]\w*', prefix_chunk)
            for w in words[-3:]:
                if any(kw in w.lower() for kw in if_kws):
                    is_interface_inst = True
                    break
                    
        if is_interface_inst:
            pos = abs_end
            continue
            
        curr = abs_end
        while curr < n and masked[curr] in b' \t\n\r\x0b\x0c':
            curr += 1
            
        if curr < n and masked[curr] == ord('('):
            end_paren = _skip_balanced_parens(masked, curr)
            if end_paren != -1:
                signal_content = code[curr+1 : end_paren]
                mapped_val = signal_content.strip()
                
                if mapped_val.startswith(b'(*'):
                    attr_end = mapped_val.find(b'*)')
                    if attr_end != -1: mapped_val = mapped_val[attr_end+2:].strip()
                        
                last_dot = mapped_val.rfind(b'.')
                base_val = mapped_val[:last_dot].strip() if last_dot != -1 else mapped_val
                terminal_val = mapped_val[last_dot+1:].strip() if last_dot != -1 else mapped_val
                base_clean = re.sub(rb'\[.*?\]', b'', base_val).strip()

                dummy_name = None
                
                # =====================================================================
                # STRICT DATABASE VERIFICATION
                # =====================================================================
                if base_clean in inst_to_type:
                    if last_dot != -1:
                        terminal_lower = terminal_val.lower()
                        port_lower = port_name.lower()
                        is_modport_or_if = IF_KW_RE.search(terminal_lower) or IF_KW_RE.search(port_lower)
                        
                        if is_modport_or_if:
                            dut_insts_needed.add(base_clean)
                            dummy_name = base_val.replace(base_clean, b"dummy_dut_plug_" + base_clean, 1) + b"." + terminal_val
                    else:
                        dut_insts_needed.add(base_clean)
                        dummy_name = base_val.replace(base_clean, b"dummy_dut_plug_" + base_clean, 1)
                    
                if dummy_name:
                    if not any(op in terminal_val for op in b'{}+-*/&|^~?!'):
                        modifications.append((curr + 1, end_paren, dummy_name))
                    
            pos = end_paren + 1 if end_paren != -1 else abs_end
            continue
            
        pos = abs_end

    # =========================================================================
    # 3. AUTO-INJECT THE TEST CUTTER DEFINITIONS (INSTANCE CLONING)
    # =========================================================================
    all_insts = uvm_insts_needed.union(dut_insts_needed)
    
    if all_insts:
        injection = b"\n  // --- AUTO-INJECTED TEST-CUTTER INTERFACES ---\n"
        
        for inst in all_insts:
            t = inst_to_type.get(inst, b"")
            prm = inst_to_params.get(inst, b"")
            prt = inst_to_ports.get(inst, b"")
            arr = inst_to_array.get(inst, b"")
            
            if prm: prm = b" " + prm
            if arr: arr = b" " + arr
            if prt: prt = b" " + prt
            
            if inst in uvm_insts_needed:
                injection += b"  " + t + prm + b" dummy_uvm_plug_" + inst + arr + prt + b";\n"
            if inst in dut_insts_needed:
                injection += b"  " + t + prm + b" dummy_dut_plug_" + inst + arr + prt + b";\n"
                
        injection += b"  // ---------------------------------------------\n\n  "
        
        max_pos = 0
        for inst in all_insts:
            pos_val = inst_to_pos.get(inst, 0)
            if pos_val > max_pos:
                max_pos = pos_val
                
        next_semi = masked.find(b';', max_pos)
        
        if next_semi != -1:
            insert_loc = next_semi + 1
        else:
            initial_m = re.search(rb'\binitial\b', masked)
            if initial_m: 
                insert_loc = initial_m.start()
            else:
                endmodule_m = re.search(rb'\bendmodule\b', masked)
                insert_loc = endmodule_m.start() if endmodule_m else len(masked)
                
        modifications.append((insert_loc, insert_loc, injection))
        
    return _apply_ranges(code, masked, modifications)





















def process_code(code: bytes, args):
    changed = False
    needs_construct_removal = (
        args.remove_assign_statement or args.remove_always_block or args.remove_initial_block or 
        args.remove_final_block or args.remove_task or args.remove_function or args.remove_virtual_class
    )

    ctx = ParserContext(code, getattr(args, 'global_ctx', None))

    if needs_construct_removal:
        masked = mask_unwanted_structures(code)
        all_ranges = []
        if args.remove_assign_statement:
            all_ranges.extend(_find_assign_ranges(code, masked, ctx))
        if args.remove_always_block:
            all_ranges.extend(_find_always_ranges(code, masked, ctx))
        if args.remove_initial_block:
            all_ranges.extend(_find_initial_ranges(code, masked, ctx))
        if args.remove_final_block:
            all_ranges.extend(_find_final_ranges(code, masked, ctx))
        if args.remove_task:
            all_ranges.extend(_find_task_ranges(code, masked, ctx))
        if args.remove_function:
            all_ranges.extend(_find_function_ranges(code, masked, ctx))
        if args.remove_virtual_class:
            all_ranges.extend(_find_virtual_class_ranges(code, masked, ctx))
            
        if all_ranges:
            changed = True
            code = _apply_ranges(code, masked, all_ranges, snap_to_lines=True)
        del masked

    if args.remove_port_connection > 0:
        code, port_changed = remove_top_n_port_connections(code, args.remove_port_connection, ctx)
        changed = changed or port_changed


    if getattr(args, 'remove_interfaces_port_connection', False):
        # Generate a fresh mask in case previous steps altered the code
        fresh_mask = mask_unwanted_structures(code)
        new_code = auto_remove_interface_ports(code, fresh_mask)
        
        if len(new_code) != len(code):
            code = new_code
            changed = True
    return code, changed

# ==============================================================================
# OS-LEVEL TIMEOUT HANDLERS
# ==============================================================================
class TimeoutException(Exception):
    pass

def _timeout_handler(signum, frame):
    raise TimeoutException("Processing timed out after 60 seconds (Infinite Loop Aborted)")

# ==============================================================================
# WORKER FILE PROCESSOR
# ==============================================================================
def worker_process_file(file_path, args):
    try:
        if not os.path.isfile(file_path) or os.path.islink(file_path):
            return ('skipped', file_path, 'Skipped non-regular file or symlink', None, 0)
            
        skip_extensions = (
            '.py', '.pl', '.sh', '.tcl', '.sdc', '.f', '.makefile', '.mk','.json', '.xml', '.yaml', '.yml','.dat', '.cfg', '.ini',
            '.sim', '.sym', '.summary', '.exe', '.out', '.bin', '.elf', '.so', '.o', '.d','.do','.a', '.lib',
            '.vpd', '.fsdb', '.vcd', '.shm', '.vdb', '.wlf', '.tr0', '.tr1', '.daidir','.config','.sdf', '.sif', '.sifc', '.sifh', '.sifd', '.sifo', '.sifp', '.sifm', '.sifx',
            '.vqm', '.edif', '.edf','.css','.cc','.h','.hpp','.c','.cpp','.java','.js','.ts','.jsx','.tsx',
            '.log', '.rpt', '.err', '.warn', '.drc','.tpl', '.tcl','.tclsh', '.bat', '.cmd', '.ps1', '.psm1', '.psd1',
            '.vp', '.svp',
            '.tar', '.gz', '.zip', '.tgz', '.bak', '.swp', '.tmp', '.temp', '.lock','.md','.png','.pdf'
        )
        if file_path.endswith(skip_extensions):
            return ('skipped', file_path, 'Skipped non-RTL extension', None, 0)

        try:
            file_size = os.path.getsize(file_path)
            
            rtl_extensions = (
                '.v', '.sv', '.vh', '.svh', '.sva', '.vl', '.vmd',
                '.vhd', '.vhdl', '.vho', '.vhi',
                '.inc', '.def'
            )
            is_hardware_file = file_path.lower().endswith(rtl_extensions)
            
            if not is_hardware_file and file_size > 5 * 1024 * 1024:
                with open(file_path, 'rb') as f:
                    peek_chunk = f.read(65536)
                
                if b'\x00' in peek_chunk:
                    return ('skipped', file_path, 'Skipped massive binary file', None, 0)
                    
                hardware_dna_re = re.compile(rb'\b(module|input|output|wire|assign|always|logic|reg)\b')
                dna_matches = len(hardware_dna_re.findall(peek_chunk))
                
                if dna_matches >= 3:
                    pass
                else:
                    return ('skipped', file_path, 'Skipped: Massive >20MB file with no RTL signature', None, 0)
                    
        except OSError:
            return ('error', file_path, 'OS Error accessing file stats', None, 0)

        with open(file_path, 'rb') as f:
            original_code = f.read()
            
        if b'\x00' in original_code[:8192]:
            del original_code
            return ('skipped', file_path, 'Skipped binary file content', None, 0)
            
        if sys.platform != 'win32':
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(60)
            
        try:
            modified_code, changed = process_code(original_code, args)
        finally:
            if sys.platform != 'win32':
                signal.alarm(0)

        line_info = None
        bytes_removed = 0
        
        if changed:
            lines_before = original_code.count(b'\n') + 1
            lines_after = modified_code.count(b'\n') + 1
            line_info = (lines_before, lines_after)
            bytes_removed = len(original_code) - len(modified_code)
            
        del original_code
        
        if not args.dry_run and changed:
            try:
                with open(file_path, 'wb') as f:
                    f.write(modified_code)
            except PermissionError:
                return ('skipped', file_path, 'No write access', None, 0)
                
        del modified_code
        return ('success', file_path, None, line_info, bytes_removed)
        
    except TimeoutException as e:
        return ('error', file_path, str(e), None, 0)
    except Exception as e:
        return ('error', file_path, str(e), None, 0)

def worker_process_chunk(file_paths, args):
    return [worker_process_file(fp, args) for fp in file_paths]

def _scandir_walk(root_path):
    stack = [root_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                            
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith('.') or entry.name in ('sys', 'proc', 'dev', 'run', 'etc'):
                                continue
                            stack.append(entry.path)
                        else:
                            yield current, entry
                    except OSError:
                        continue
        except OSError:
            continue

def _is_self(path: str) -> bool:
    if _SELF_PATH is None:
        return False
    try:
        return os.path.abspath(path) == _SELF_PATH
    except OSError:
        return False

def iter_verilog_files(root_path, target_exts):
    if os.path.isfile(root_path):
        if not _is_self(root_path):
            yield root_path
        return

    for _dir_path, entry in _scandir_walk(root_path):
        if target_exts is None and _is_self(entry.path):
            continue
        if target_exts is None or entry.name.endswith(target_exts):
            yield entry.path

def clear_line():
    term_width = shutil.get_terminal_size().columns
    print("\r" + " " * (term_width - 1) + "\r", end="", flush=True)

def _print_scan_progress(scanned: int, matched: int, current_dir: str, current_file: str, size_bytes: int, spinner_index: int) -> None:
    spinner = "|/-\\"[spinner_index % 4]
    dir_label = os.path.basename(os.path.normpath(current_dir)) or current_dir
    size_label = f"{size_bytes / 1024**2:.0f}MB" if size_bytes else "0MB"

    term_width = shutil.get_terminal_size().columns
    prefix = f"Analyzing {spinner} Dir: {dir_label[:15]:<15} File: "
    suffix = f" ({size_label}) Scanned: {scanned} | Matched: {matched}"

    space_for_file = term_width - len(prefix) - len(suffix) - 1

    if space_for_file >= 5:
        display_file = current_file[:space_for_file].ljust(space_for_file)
        line = f"{prefix}{display_file}{suffix}"
    else:
        line = f"Analyzing {spinner} Scanned: {scanned} | Matched: {matched}".ljust(term_width - 1)[:term_width - 1]

    print(f"\r{line}", end="", flush=True)

def scan_and_collect_files(root_path, target_exts):
    if os.path.isfile(root_path):
        if _is_self(root_path):
            return [], 0, 0, 0, set()
        size = 0
        try:
            size = os.path.getsize(root_path)
        except OSError:
            pass
        ext = os.path.splitext(root_path)[1]
        return [(root_path, size)], 1, size, size, {ext} if ext else set()

    matched_entries = []
    total_scanned = 0
    total_bytes = 0
    max_file_bytes = 0
    spinner_index = 0
    last_update = 0.0
    extensions_found = set()

    _print_scan_progress(0, 0, root_path, "starting", 0, spinner_index)

    for dir_path, entry in _scandir_walk(root_path):
        if _shutdown_requested:
            print()
            return None, None, None, None, set()

        if target_exts is None and _is_self(entry.path):
            continue

        total_scanned += 1

        if target_exts is not None and not entry.name.endswith(target_exts):
            now = time.time()
            if now - last_update >= 0.10:
                spinner_index += 1
                _print_scan_progress(total_scanned, len(matched_entries), dir_path, entry.name, 0, spinner_index)
                last_update = now
            continue

        ext = os.path.splitext(entry.name)[1]
        if ext:
            extensions_found.add(ext)
        fsize = 0
        try:
            fsize = entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        matched_entries.append((entry.path, fsize))
        total_bytes += fsize
        if fsize > max_file_bytes:
            max_file_bytes = fsize

        now = time.time()
        if now - last_update >= 0.10:
            spinner_index += 1
            _print_scan_progress(total_scanned, len(matched_entries), dir_path, entry.name, fsize, spinner_index)
            last_update = now

    clear_line()
    orange = "\033[1;38;5;208m"
    reset = "\033[0m"
    extensions = sorted(extensions_found) if extensions_found else ["none"]
    extension_text = ", ".join(extensions)
    extension_prefix = "  File extensions in directory : "
    terminal_width = shutil.get_terminal_size().columns
    extension_width = max(10, terminal_width - len(extension_prefix))
    extension_lines = textwrap.wrap(
        extension_text,
        width=extension_width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or ["none"]

    print(f"{orange}Scan complete{reset}")
    print(f"  Total files scanned          : {total_scanned:,}")
    print(f"  Working files (matched)      : {len(matched_entries):,}")
    print(f"{extension_prefix}{extension_lines[0]}")
    continuation_prefix = " " * len(extension_prefix)
    for extension_line in extension_lines[1:]:
        print(f"{continuation_prefix}{extension_line}")

    return matched_entries, total_scanned, total_bytes, max_file_bytes, extensions_found

def _default_worker_count():
    if hasattr(os, 'sched_getaffinity'):
        try:
            return len(os.sched_getaffinity(0))
        except Exception:
            pass
    return multiprocessing.cpu_count()

def _macos_core_breakdown():
    if sys.platform != 'darwin':
        return None, None
    try:
        perf = subprocess.run(['sysctl', '-n', 'hw.perflevel0.physicalcpu'], capture_output=True, text=True, timeout=2)
        eff = subprocess.run(['sysctl', '-n', 'hw.perflevel1.physicalcpu'], capture_output=True, text=True, timeout=2)
        if perf.returncode == 0 and eff.returncode == 0:
            return int(perf.stdout.strip()), int(eff.stdout.strip())
    except Exception:
        pass
    return None, None

def _detect_total_memory_bytes():
    try:
        if sys.platform == 'darwin':
            result = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return int(result.stdout.strip())
        elif sys.platform.startswith('linux'):
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        kb = int(line.split()[1])
                        return kb * 1024
    except Exception:
        pass
    return None

def parse_size_str(value):
    s = str(value).strip().upper()
    if s in ('0', ''):
        return 0
    multipliers = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3}
    if s[-1] in multipliers:
        return int(float(s[:-1]) * multipliers[s[-1]])
    return int(s)

def _prompt_yes_no(message: str) -> bool:
    while True:
        try:
            answer = input(message).strip().lower()
        except EOFError:
            print("\nNo input available (non-interactive environment?) - defaulting to 'no'. "
                  "Use --yes to skip this prompt for automated runs.")
            return False
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print("Please enter y or n.")

def _print_backup_progress(processed: int, skipped: int, current_path: str, spinner_index: int) -> None:
    spinner = "|/-\\"[spinner_index % 4]
    current_label = os.path.basename(current_path) if current_path else "starting"

    term_width = shutil.get_terminal_size().columns
    prefix = f"Creating TAR backup {spinner} files={processed} skipped={skipped} curr="
    space_for_file = term_width - len(prefix) - 1

    if space_for_file >= 5:
        display_file = current_label[:space_for_file].ljust(space_for_file)
        line = f"{prefix}{display_file}"
    else:
        line = f"TAR backup {spinner} f={processed} skip={skipped}".ljust(term_width - 1)[:term_width - 1]

    print(f"\r{line}", end="", flush=True)

def create_tar_backup(input_path: str) -> str:
    base_name = os.path.basename(os.path.normpath(input_path)) or "backup"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{base_name}_{timestamp}.tar.gz"
    backup_path = os.path.abspath(backup_name)

    tar_bin = shutil.which('tar')
    if tar_bin and os.path.isdir(input_path):
        parent = os.path.dirname(os.path.abspath(input_path)) or '.'
        base = os.path.basename(os.path.normpath(input_path))
        try:
            print(f"Creating backup via system tar ({tar_bin})...")
            proc = subprocess.Popen(
                [tar_bin, "-czf", backup_path, "-C", parent, base],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            spinner = "|/-\\"
            i = 0
            start = time.time()
            while proc.poll() is None:
                elapsed = int(time.time() - start)
                h = elapsed // 3600
                m = (elapsed % 3600) // 60
                s = elapsed % 60
                print(f"\rCreating TAR backup {spinner[i % 4]}  Elapsed {h:02}:{m:02}:{s:02}", end="", flush=True)
                time.sleep(0.2)
                i += 1
            print()
            if proc.returncode == 0:
                print("Complete: System tar backup complete.")
                return backup_path
            print(f"System tar failed (exit {proc.returncode}) - falling back to Python tarfile.")
        except Exception as exc:
            print(f"System tar unavailable/failed ({exc}) - falling back to Python tarfile.")

    processed = 0
    skipped = 0
    spinner_index = 0
    last_update = 0.0

    def maybe_update(current_path: str, force: bool = False) -> None:
        nonlocal spinner_index, last_update
        now = time.time()
        if force or now - last_update >= 0.5:
            spinner_index += 1
            _print_backup_progress(processed, skipped, current_path, spinner_index)
            last_update = now

    with tarfile.open(backup_path, "w:gz") as tar:
        maybe_update(input_path, force=True)
        if os.path.isfile(input_path):
            arcname = os.path.basename(input_path)
            try:
                tar.add(input_path, arcname=arcname)
                processed += 1
            except (OSError, PermissionError) as exc:
                skipped += 1
                print(f"[Backup skip] {input_path}: {exc}")
            maybe_update(input_path, force=True)
        else:
            for root, _dirs, files in os.walk(input_path, onerror=lambda exc: print(f"[Backup skip] {exc}")):
                rel_root = os.path.relpath(root, input_path)
                arc_root = base_name if rel_root == '.' else os.path.join(base_name, rel_root)
                try:
                    tar.add(root, arcname=arc_root, recursive=False)
                except (OSError, PermissionError) as exc:
                    skipped += 1
                    print(f"[Backup skip] {root}: {exc}")
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    arcname = os.path.join(arc_root, file_name)
                    try:
                        tar.add(file_path, arcname=arcname)
                        processed += 1
                    except (OSError, PermissionError) as exc:
                        skipped += 1
                        print(f"[Backup skip] {file_path}: {exc}")
                    maybe_update(file_path)

    clear_line()
    print(f"Creating TAR backup done. files={processed} skipped={skipped}")
    return backup_path

def create_error_report_path(input_path: str) -> str:
    base_name = os.path.basename(os.path.normpath(input_path)) or "input"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.abspath(f"{base_name}_{timestamp}_error_report.txt")

def write_error_report(report_path: str, entries) -> None:
    with open(report_path, "w", encoding="utf-8") as report_file:
        report_file.write("Status\tPath\tMessage\n")
        for status, file_path, message in entries:
            report_file.write(f"{status}\t{file_path}\t{message or ''}\n")

def create_line_report_path(input_path: str) -> str:
    base_name = os.path.basename(os.path.normpath(input_path)) or "input"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.abspath(f"{base_name}_{timestamp}_line_report.txt")

def write_line_report(report_path: str, entries) -> None:
    max_path_len = max((len(path) for path, _, _ in entries), default=4)
    col_width = min(max(max_path_len + 2, 6), 90)
    separator = "-" * (col_width + 34)

    total_before = 0
    total_after = 0

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"{'File':<{col_width}}{'Prev Lines':>12}{'New Lines':>12}{'Removed':>10}\n")
        f.write(separator + "\n")

        for file_path, before, after in entries:
            total_before += before
            total_after += after
            removed = before - after
            display_path = "..." + file_path[-(col_width - 5):] if len(file_path) > col_width - 2 else file_path

            f.write(f"{display_path:<{col_width}}{before:>12}{after:>12}{removed:>10}\n")

        f.write(separator + "\n")
        f.write(f"Files changed:       {len(entries)}\n")
        f.write(f"Total lines before:  {total_before}\n")
        f.write(f"Total lines after:   {total_after}\n")
        f.write(f"Total lines removed: {total_before - total_after}\n")
def main():
    neon_green = "\033[1;92m"
    reset = "\033[0m"
    
    big_title = r"""
  _______ ______  _____ _______    _____ _    _ _______ _______ ______ _____  
 |__   __|  ____|/ ____|__   __|  / ____| |  | |__   __|__   __|  ____|  __ \ 
    | |  | |__  | (___    | |    | |    | |  | |  | |     | |  | |__  | |__) |
    | |  |  __|  \___ \   | |    | |    | |  | |  | |     | |  |  __| |  _  / 
    | |  | |____ ____) |  | |    | |____| |__| |  | |     | |  | |____| | \ \ 
    |_|  |______|_____/   |_|     \_____|\____/   |_|     |_|  |______|_|  \_\
    """
    
    print(f"{neon_green}{big_title}{reset}")

    class CustomParser(argparse.ArgumentParser):
            def error(self, message):
                print(f"Error: {message}\n")
                print("Select at least one of the following flags:")
                print("  --remove_assign_statement           Remove assign statements")
                print("  --remove_always_block               Remove always/always_ff/always_comb/always_latch blocks")
                print("  --remove_initial_block              Remove initial blocks")
                print("  --remove_final_block                Remove final blocks")
                print("  --remove_task                       Remove task blocks")
                print("  --remove_function                   Remove function blocks")
                print("  --remove_virtual_class              Remove the body of virtual classes")
                print("  --remove_port_connection N          Clear port connection for the first N module instantiations")
                print("  --remove_interface_port_connection  Remove interface port connections")
                print("\nExample: --remove_assign_statement --remove_interface_port_connection")
                sys.exit(1)

    parser = CustomParser(description="Massive-Scale Verilog/SystemVerilog Modifier (AST-Aware)", allow_abbrev=False)

    parser.add_argument("input_path", nargs="?", default=".", help="Path to file or directory (default: current directory)")
    parser.add_argument("--remove_assign_statement", action="store_true", help="Remove assign statements")
    parser.add_argument("--remove_always_block", action="store_true", help="Remove always blocks")
    parser.add_argument("--remove_initial_block", action="store_true", help="Remove initial blocks")
    parser.add_argument("--remove_final_block", action="store_true", help="Remove final blocks")
    parser.add_argument("--remove_task", action="store_true", help="Remove task blocks")
    parser.add_argument("--remove_function", action="store_true", help="Remove function blocks")
    parser.add_argument("--remove_virtual_class", action="store_true", help="Remove virtual class bodies")
    parser.add_argument("--remove_port_connection", type=int, default=0, help="Clear port connection for the first N module instantiations")
    parser.add_argument("--remove_interface_port_connection", action="store_true", help="Remove interface port connections")
    parser.add_argument("--dry-run", action="store_true", help="Process files without modifying them")
    parser.add_argument("--workers", type=int, default=0, help="Number of worker processes (default: available CPU cores)")
    parser.add_argument("--chunk-bytes", type=str, default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--max-inflight-bytes", type=str, default=None)
    parser.add_argument("--window", type=int, default=0)
    parser.add_argument("--progress-interval", type=float, default=1.0)
    parser.add_argument("--no-count", action="store_true")
    parser.add_argument("--error_report", action="store_true")
    parser.add_argument("--line_report", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--no-fork", action="store_true")
    parser.add_argument("--macro-file", action="append", default=[], help="Path to header file (.vh/.svh) containing global macro definitions")

    args, unknown_args = parser.parse_known_args()

    if unknown_args:
        print(f"Error: unrecognized argument provided: {' '.join(unknown_args)}")
        print("Select at least one of the following flags:")
        print("  --remove_assign_statement           Remove assign statements")
        print("  --remove_always_block               Remove always/always_ff/always_comb/always_latch blocks")
        print("  --remove_initial_block              Remove initial blocks")
        print("  --remove_final_block                Remove final blocks")
        print("  --remove_task                       Remove task blocks")
        print("  --remove_function                   Remove function blocks")
        print("  --remove_virtual_class              Remove the body of virtual classes")
        print("  --remove_port_connection N          Clear port connection for the first N module instantiations")
        print("  --remove_interface_port_connection  Remove interface port connections")
        print("\nExample: --remove_assign_statement --remove_interface_port_connection")
        sys.exit(1)

    if not os.path.exists(args.input_path):
        print(f"Error: Path '{args.input_path}' not found.")
        print("Please provide a valid file/directory path or Select at least one of the following flags:")
        print("  --remove_assign_statement           Remove assign statements")
        print("  --remove_always_block               Remove always/always_ff/always_comb/always_latch blocks")
        print("  --remove_initial_block              Remove initial blocks")
        print("  --remove_final_block                Remove final blocks")
        print("  --remove_task                       Remove task blocks")
        print("  --remove_function                   Remove function blocks")
        print("  --remove_virtual_class              Remove the body of virtual classes")
        print("  --remove_port_connection N          Clear port connection for the first N module instantiations")
        print("  --remove_interface_port_connection  Remove interface port connections")
        print("\nExample: --remove_assign_statement --remove_interface_port_connection")
        sys.exit(1)

    if not (args.remove_assign_statement or args.remove_always_block
                or args.remove_initial_block or args.remove_port_connection > 0 
                or args.remove_final_block or args.remove_task 
                or args.remove_function or args.remove_virtual_class 
                or args.remove_interface_port_connection):
            print("Error: no processing option selected .")
            print("Select at least one of the following flags:")
            print("  --remove_assign_statement           Remove assign statements")
            print("  --remove_always_block               Remove always/always_ff/always_comb/always_latch blocks")
            print("  --remove_initial_block              Remove initial blocks")
            print("  --remove_final_block                Remove final blocks")
            print("  --remove_task                       Remove task blocks")
            print("  --remove_function                   Remove function blocks")
            print("  --remove_virtual_class              Remove the body of virtual classes")
            print("  --remove_port_connection N          Clear port connection for the first N module instantiations")
            print("  --remove_interface_port_connection  Remove interface port connections")
            print("\nExample: --remove_assign_statement --remove_interface_port_connection")
            sys.exit(1)

    # =========================================================================
    # [!] UI UPGRADE: Cleaner Action Names and Progress Bar Labels
    # =========================================================================
    action_names = []
    progress_actions = []

    if args.remove_assign_statement:
        action_names.append("Assign Statements")
        progress_actions.append("Removing Assign Statements")
    if args.remove_always_block:
        action_names.append("Always Blocks")
        progress_actions.append("Removing Always Blocks")
    if args.remove_initial_block:
        action_names.append("Initial Blocks")
        progress_actions.append("Removing Initial Blocks")
    if args.remove_final_block:
        action_names.append("Final Blocks")
        progress_actions.append("Removing Final Blocks")
    if args.remove_task:
        action_names.append("Task Blocks")
        progress_actions.append("Removing Task Blocks")
    if args.remove_function:
        action_names.append("Function Blocks")
        progress_actions.append("Removing Function Blocks")
    if args.remove_virtual_class:
        action_names.append("Virtual Class Bodies")
        progress_actions.append("Removing Virtual Class Bodies")
    if args.remove_port_connection > 0:
        action_names.append(f"Port Connection Top {args.remove_port_connection}")
        progress_actions.append(f"Removing Port Connection Top {args.remove_port_connection}")
    if args.remove_interface_port_connection:
        action_names.append("Interface Port Connections")
        progress_actions.append("Removing Interface Port Connections")

    action_str = ", ".join(action_names)
    progress_action_label = " + ".join(progress_actions)
    # =========================================================================

    target_exts = None
    if not args.yes:
        def prompt_single_extension(prompt_text: str) -> str:
            while True:
                try:
                    ext = input(prompt_text).strip()
                except EOFError:
                    return ""
                if not ext: return ""
                if ',' in ext:
                    print("Please provide one file extension at a time. Example: .v or .sv")
                    continue
                if not ext.startswith('.'):
                    ext = '.' + ext
                return ext

        while True:
            try:
                ans = input(f"This will remove \033[1m{action_str}\033[0m from all file extension(.s,.sv,.txt,etc), Do you agree ? (y/n) : ").strip().lower()
            except EOFError:
                ans = 'y'

            if ans in ('y', 'yes'):
                target_exts = None
                break
            elif ans in ('n', 'no'):
                exts_set = set()
                ext = prompt_single_extension("Provide file extention example(.v,.sv) : ")
                if ext: exts_set.add(ext)

                while True:
                    try:
                        add_more = input("You want add more file ? :(y/n) : ").strip().lower()
                    except EOFError:
                        add_more = 'n'

                    if add_more in ('y', 'yes'):
                        ext = prompt_single_extension(f"Add more file extention where you want to remove {action_str}: ")
                        if ext: exts_set.add(ext)
                    elif add_more in ('n', 'no'):
                        break
                    else:
                        print("Please enter y or n.")

                target_exts = tuple(exts_set)
                if not target_exts: target_exts = None
                break
            else:
                print("Please enter y or n.")
    else:
        target_exts = None

    if args.dry_run:
        print("\nDry-run mode: no files will be modified.\n")
    else:
        print("\n\033[1;31mWARNING:\033[0m ", end="")
        print("This script permanently modifies your files.\n")

        if args.backup:
            try:
                backup_path = create_tar_backup(args.input_path)
                print(f"Backup created: {backup_path}\n")
            except Exception as exc:
                print(f"Warning: Backup completed with issues: {exc}\n")
        elif not args.yes:
            if _prompt_yes_no("Would you like to create a TAR backup before continuing? (y/n): "):
                try:
                    backup_path = create_tar_backup(args.input_path)
                    print(f"Backup created: {backup_path}\n")
                except Exception as exc:
                    print(f"Warning: Backup completed with issues: {exc}\n")

        if not args.yes:
            if not _prompt_yes_no("Continue and permanently modify files now? (y/n): "):
                print("Aborted.")
                sys.exit(0)
        else:
            print("--yes supplied: skipping confirmation prompt.\n")

    global_ctx = ParserContext(b'')
    if args.macro_file:
        for fpath in args.macro_file:
            try:
                with open(fpath, 'rb') as f:
                    mcode = f.read()
                    global_ctx = ParserContext(mcode, global_ctx)
            except Exception as e:
                print(f"Warning: could not read global macro file {fpath}: {e}")
    args.global_ctx = global_ctx

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    max_workers = args.workers if args.workers > 0 else _default_worker_count()
    window_chunks = args.window if args.window > 0 else max_workers * 4

    chunk_size_auto_detected = args.chunk_size is None
    if chunk_size_auto_detected:
        chunk_size_cap = int(min(5000, max(200, max_workers * 150)))
    else:
        chunk_size_cap = max(1, args.chunk_size)

    total_ram_bytes = _detect_total_memory_bytes()
    ram_auto_detected = False
    if args.max_inflight_bytes is None:
        if total_ram_bytes:
            max_inflight_bytes = int(total_ram_bytes * 0.5 / 3)
            ram_auto_detected = True
        else:
            max_inflight_bytes = 0
    else:
        max_inflight_bytes = parse_size_str(args.max_inflight_bytes)

    chunk_bytes_auto_detected = args.chunk_bytes is None
    if chunk_bytes_auto_detected:
        if max_inflight_bytes > 0 and window_chunks > 0:
            auto_chunk_bytes = max_inflight_bytes // window_chunks
            chunk_bytes = max(1 * 1024 * 1024, min(256 * 1024 * 1024, auto_chunk_bytes))
        else:
            chunk_bytes = 4 * 1024 * 1024
    else:
        chunk_bytes = parse_size_str(args.chunk_bytes)

    mp_context = None
    if sys.platform != 'win32' and not args.no_fork:
        try:
            mp_context = multiprocessing.get_context('fork')
        except ValueError:
            mp_context = None

    chunk_bytes_label = f"~{chunk_bytes / 1024**2:.0f}M (auto)" if chunk_bytes_auto_detected else f"{args.chunk_bytes}"
    chunk_size_label = f"{chunk_size_cap} (auto)" if chunk_size_auto_detected else f"{chunk_size_cap}"

    if chunk_bytes > 0:
        print(f"\nStarting processing using {max_workers} CPU cores, chunk target "
              f"{chunk_bytes_label} per task (cap: {chunk_size_label} files/task), "
              f"in-flight window: {window_chunks} chunks...")
    else:
        print(f"\nStarting processing using {max_workers} CPU cores, fixed chunk size "
              f"{chunk_size_label} files/task, in-flight window: {window_chunks} chunks "
              f"(~{window_chunks * chunk_size_cap} files)...")

    if sys.platform == 'darwin':
        perf_cores, eff_cores = _macos_core_breakdown()
        if perf_cores is not None:
            print(f"Apple Silicon detected: {perf_cores} performance cores, {eff_cores} efficiency "
                  f"cores ({max_workers} workers in use).")

    if max_inflight_bytes > 0:
        ram_note = (f" (auto: 50% of {total_ram_bytes / 1024**3:.0f}GB detected system RAM, / 3x overhead)"
                    if ram_auto_detected else "")
        print(f"Max in-flight file data: ~{max_inflight_bytes / 1024**2:.0f}MB{ram_note} "
              f"(actual peak RAM use will run roughly 3x this due to per-file processing overhead).")
    print("Press Ctrl+C to terminate immediately (all workers will be stopped right away).\n")
    
    total_files = None
    dir_total_scanned = None
    extensions_found = set()
    matched_entries = None
    total_dataset_bytes = 0 
    
    if not args.no_count:
        matched_entries, dir_total_scanned, total_dataset_bytes, max_file_bytes, extensions_found = \
            scan_and_collect_files(args.input_path, target_exts)

        if matched_entries is None:
            print("Shutdown requested during scan - exiting.")
            sys.exit(0)

        total_files = len(matched_entries)

        if max_inflight_bytes > 0 and max_file_bytes and max_file_bytes >= max_inflight_bytes:
            print(f"\n[!] NOTE: your largest file (~{max_file_bytes/1024**2:.0f}MB) is at or above your "
                  f"in-flight memory budget (~{max_inflight_bytes/1024**2:.0f}MB). Files this large will "
                  f"effectively process one at a time regardless of --workers, since the memory cap won't "
                  f"allow multiple such files in flight simultaneously - this protects against OOM, but "
                  f"means CPU parallelism won't help much for these specific files. If you have more RAM "
                  f"available than the auto-detected budget assumes, raise --max-inflight-bytes explicitly. "
                  f"Total dataset size: ~{total_dataset_bytes/1024**3:.2f}GB across {total_files} matched files.\n")

    if matched_entries is not None:
        file_iter = iter(matched_entries)
    else:
        file_iter = ((fp, None) for fp in iter_verilog_files(args.input_path, target_exts))

    files_dispatched = 0

    def chunk_iter():
        nonlocal files_dispatched
        while True:
            if total_files:
                remaining = max(0, total_files - files_dispatched)
                guided_cap = max(1, remaining // max(1, max_workers * 2))
                effective_cap = min(chunk_size_cap, guided_cap)
            else:
                effective_cap = chunk_size_cap

            chunk = []
            total_bytes = 0
            while len(chunk) < effective_cap:
                item = next(file_iter, None)
                if item is None:
                    break
                fp, fsize = item
                if fsize is None:
                    try:
                        fsize = os.path.getsize(fp)
                    except OSError:
                        fsize = 0
                chunk.append(fp)
                total_bytes += fsize
                if chunk_bytes > 0 and total_bytes >= chunk_bytes:
                    break
            if not chunk:
                return
            files_dispatched += len(chunk)
            yield chunk, total_bytes

    chunks = chunk_iter()

    processed = 0
    files_changed = 0
    skipped = 0
    errors = 0
    total_lines_removed = 0
    total_bytes_removed = 0
    report_entries = []
    line_report_entries = []
    
    start_time = time.time()
    last_print_time = start_time
    spinner_index = 0
    progress_line_started = False

    _rate_window_seconds = 5.0
    _rate_samples = collections.deque()

    def _current_rate(now, completed):
        _rate_samples.append((now, completed))
        while _rate_samples and now - _rate_samples[0][0] > _rate_window_seconds:
            _rate_samples.popleft()
        if len(_rate_samples) >= 2:
            oldest_t, oldest_c = _rate_samples[0]
            dt = now - oldest_t
            dc = completed - oldest_c
            if dt > 0:
                return dc / dt
        elapsed = now - start_time
        return completed / elapsed if elapsed > 0 else 0.0

    def print_progress(force=False):
        nonlocal last_print_time, spinner_index, progress_line_started
        now = time.time()
        if not force and now - last_print_time < args.progress_interval:
            return
        spinner = "|/-\\"[spinner_index % 4]
        spinner_index += 1
        completed = processed + skipped + errors
        rate = _current_rate(now, completed)

        if total_files:
            pct = (completed / total_files) * 100 if total_files else 0
            progress_ratio = min(1.0, max(0.0, completed / total_files))
            bar_width = 20
            filled_width = round(progress_ratio * bar_width)
            progress_bar = "#" * filled_width + "." * (bar_width - filled_width)
            summary = (f"[{spinner}] Progress: {progress_action_label} | {pct:5.1f}% "
                       f"[{progress_bar}] {completed:,}/{total_files:,} | OK: {processed:,} | Skip: {skipped:,} "
                       f"| Err: {errors:,} | {rate:.0f} files/s")
        else:
            summary = (f"[{spinner}] Progress: {progress_action_label} | {completed:,} done "
                       f"| OK: {processed:,} | Skip: {skipped:,} | Err: {errors:,} | {rate:.0f} files/s")

        term_width = shutil.get_terminal_size().columns
        line = summary[:max(1, term_width - 1)].ljust(max(1, term_width - 1))
        colored_line = line.replace("Progress", f"\033[1;32mProgress\033[0m", 1)

        if not progress_line_started:
            print("\n\n", end="")
            progress_line_started = True
        print(f"\r{colored_line}", end="", flush=True)
        last_print_time = now

    def handle_chunk_result(status_list):
        nonlocal processed, files_changed, skipped, errors, total_lines_removed, total_bytes_removed
        for status, file_path, error_msg, line_info, bytes_removed in status_list:
            if status == 'success':
                processed += 1
                total_bytes_removed += bytes_removed
                if line_info is not None:
                    files_changed += 1
                    total_lines_removed += (line_info[0] - line_info[1])
                    if args.line_report:
                        line_report_entries.append((file_path, line_info[0], line_info[1]))
            elif status == 'skipped':
                skipped += 1
                if args.error_report:
                    report_entries.append((status, file_path, error_msg))
            elif status == 'error':
                errors += 1
                if args.error_report:
                    report_entries.append((status, file_path, error_msg))
                print(f"\n[Error] Failed to process {progress_action_label} : {file_path}: {error_msg}")

    executor_kwargs = {'max_workers': max_workers, 'initializer': _worker_init}
    if mp_context is not None:
        executor_kwargs['mp_context'] = mp_context

    inflight_bytes = 0
    pending_chunk = None
    pending_bytes = 0

    executor = ProcessPoolExecutor(**executor_kwargs)
    try:
        futures = {}

        def try_submit_next():
            nonlocal pending_chunk, pending_bytes, inflight_bytes
            while len(futures) < window_chunks and not _shutdown_requested:
                if pending_chunk is None:
                    item = next(chunks, None)
                    if item is None:
                        return
                    pending_chunk, pending_bytes = item
                if (max_inflight_bytes > 0 and futures and
                        inflight_bytes + pending_bytes > max_inflight_bytes):
                    return
                futures[executor.submit(worker_process_chunk, pending_chunk, args)] = (pending_chunk, pending_bytes)
                inflight_bytes += pending_bytes
                pending_chunk, pending_bytes = None, 0

        try_submit_next()

        while futures and not _shutdown_requested:
            done, _pending = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)

            for fut in done:
                chunk, chunk_bytes_used = futures.pop(fut)
                inflight_bytes -= chunk_bytes_used
                try:
                    handle_chunk_result(fut.result())
                except Exception as e:
                    for fp in chunk:
                        errors += 1
                        if args.error_report:
                            report_entries.append(('error', fp, str(e)))

            if not _shutdown_requested:
                try_submit_next()

            print_progress()

        if _shutdown_requested:
            print("\nTerminating all workers immediately - any file a worker was actively "
                  "writing may be left partially written...")
            for fut in list(futures.keys()):
                fut.cancel()
            for p in getattr(executor, '_processes', {}).values():
                try:
                    p.terminate()
                except Exception:
                    pass
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                pass
        else:
            executor.shutdown(wait=True)
    except KeyboardInterrupt:
        print("\nInterrupted - shutting down workers...")
        executor.shutdown(wait=False, cancel_futures=True)
        for p in getattr(executor, '_processes', {}).values():
            p.terminate()

    print_progress(force=True)

    total = processed + skipped + errors
    elapsed = time.time() - start_time

    if args.error_report:
        report_path = create_error_report_path(args.input_path)
        write_error_report(report_path, report_entries)
        print(f"\nError report written to: {report_path}")

    if args.line_report:
        line_report_path = create_line_report_path(args.input_path)
        write_line_report(line_report_path, line_report_entries)
        print(f"\nLine report written to: {line_report_path}")

    def format_size(size_bytes):
        if size_bytes == 0: return "0 B"
        unit_names = ("B", "KB", "MB", "GB", "TB", "PB")
        i = 0
        while size_bytes >= 1024 and i < len(unit_names) - 1:
            size_bytes /= 1024.0
            i += 1
        return f"{size_bytes:.2f} {unit_names[i]}"

    before_size_str = format_size(total_dataset_bytes) if not args.no_count else "Unknown (--no-count)"
    after_size_str = format_size(total_dataset_bytes - total_bytes_removed) if not args.no_count else "Unknown (--no-count)"
    
    # =========================================================================
    # [!] UI UPGRADE: Prevents Table Layout Breakage
    # =========================================================================
    if len(action_names) <= 2:
        table_action_label = action_str.title()
    else:
        table_action_label = f"{len(action_names)} Selected Target Types"
        
    after_label = f"Data After Removing {table_action_label}" if action_str else "Total Data After Processing"
    # =========================================================================

    extension_values = extensions_found or set(target_exts or ())
    ext_label = ", ".join(
        sorted({ext if ext.startswith('.') else f".{ext}" for ext in extension_values})
    ) or "none"

    table_rows = [
        ("Extensions", ext_label),
        ("Total Files In Directory", str(dir_total_scanned if dir_total_scanned is not None else "Unknown (--no-count)")),
        ("Total Matching Extension Files", str(total_files if total_files is not None else "Unknown")),
        ("Total Files Processed", str(total)),
        ("Successfully Processed", str(processed)),
        ("Skipped (No write access)", str(skipped)),
        ("Errors", str(errors)),
        ("Total Files Changed", str(files_changed)),
        ("Total Lines Removed", str(total_lines_removed)),
        ("Total Data Before Processing", before_size_str),
        (after_label, after_size_str),
        ("Elapsed", f"{elapsed:.1f}s ({(total/elapsed if elapsed else 0):.0f} files/sec)"),
    ]

    label_width = max(len(label) for label, _ in table_rows)
    
    MAX_VAL_WIDTH = 45
    max_actual_val_len = max(len(str(val)) for _, val in table_rows)
    value_width = min(MAX_VAL_WIDTH, max(max_actual_val_len, 10))

    total_width = label_width + value_width + 7
    inner_width = total_width - 2

    RESET = "\033[0m"
    BORDER_COLOR = "\033[38;5;244m"     
    HEADER_TEXT = "\033[1;36m"          
    HEADER_ALERT = "\033[1;31m"         
    LABEL_COLOR = "\033[33m"            
    VALUE_COLOR = "\033[32m"            

    full_title_base = f"Process Completed: Remove {table_action_label}"
    if _shutdown_requested:
        full_title_base += " (interrupted)"

    title_lines = textwrap.wrap(full_title_base, width=inner_width - 2)
    if not title_lines:
        title_lines = [full_title_base]

    print("\n\n" + BORDER_COLOR + "+" + "-" * inner_width + "+" + RESET)

    for t_line in title_lines:
        pad_total = inner_width - len(t_line)
        left_p = max(0, pad_total // 2)
        right_p = max(0, pad_total - left_p)

        if _shutdown_requested and "(interrupted)" in t_line:
            base_msg = t_line.replace(" (interrupted)", "")
            colored_content = f"{HEADER_TEXT}{base_msg}{RESET}{HEADER_ALERT} (interrupted){RESET}"
        else:
            colored_content = f"{HEADER_TEXT}{t_line}{RESET}"

        line_str = f"{' ' * left_p}{colored_content}{' ' * right_p}"
        print(BORDER_COLOR + "|" + RESET + line_str + BORDER_COLOR + "|" + RESET)

    print(BORDER_COLOR + "+" + "-" * inner_width + "+" + RESET)
    print(BORDER_COLOR + "+" + "-" * (label_width + 2) + "+" + "-" * (value_width + 2) + "+" + RESET)

    for label, value in table_rows:
        val_str = str(value)
        wrapped_lines = textwrap.wrap(val_str, width=value_width) if val_str else [""]
        if not wrapped_lines:
            wrapped_lines = [""]

        for i, line in enumerate(wrapped_lines):
            current_label = label if i == 0 else ""
            padded_label = current_label.ljust(label_width)
            padded_value = line.ljust(value_width)

            print(
                f"{BORDER_COLOR}|{RESET} "
                f"{LABEL_COLOR}{padded_label}{RESET} "
                f"{BORDER_COLOR}|{RESET} "
                f"{VALUE_COLOR}{padded_value}{RESET} "
                f"{BORDER_COLOR}|{RESET}"
            )

    print(BORDER_COLOR + "+" + "-" * (label_width + 2) + "+" + "-" * (value_width + 2) + "+" + RESET)
    
if __name__ == "__main__":
    main()