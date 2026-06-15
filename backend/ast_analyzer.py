"""Tree-sitter AST analysis. Returns objective metrics only.

Works with tree-sitter 0.25+ (all-method node API) via tree-sitter-language-pack.
Source is passed as str to the parser; byte offsets refer to UTF-8 bytes.
"""

from pathlib import Path
import re

try:
    from tree_sitter_language_pack import get_parser  # type: ignore
except Exception:  # pragma: no cover
    get_parser = None

LANG_FOR_EXT = {
    ".java": "java",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".vue": "vue",
}

STATEMENT_TYPES = {
    "java": {
        "local_variable_declaration", "expression_statement", "if_statement",
        "for_statement", "enhanced_for_statement", "while_statement", "do_statement",
        "return_statement", "throw_statement", "try_statement",
        "switch_statement", "synchronized_statement", "yield_statement",
        "break_statement", "continue_statement", "assert_statement",
        "explicit_constructor_invocation",  # super(...) / this(...) in constructor body
    },
    "python": {
        "expression_statement", "if_statement", "for_statement",
        "while_statement", "return_statement", "raise_statement", "try_statement",
        "with_statement", "import_statement", "import_from_statement",
        "assert_statement", "break_statement", "continue_statement",
    },
    "javascript": {
        "expression_statement", "variable_declaration", "lexical_declaration",
        "if_statement", "for_statement", "for_in_statement", "for_of_statement",
        "while_statement", "do_statement", "return_statement", "throw_statement",
        "try_statement", "switch_statement", "break_statement", "continue_statement",
    },
}
STATEMENT_TYPES["typescript"] = STATEMENT_TYPES["javascript"]
STATEMENT_TYPES["tsx"] = STATEMENT_TYPES["javascript"]

BRANCH_TYPES = {
    "java": {
        "if_statement", "for_statement", "enhanced_for_statement", "while_statement",
        "do_statement", "catch_clause", "switch_label", "ternary_expression",
    },
    "python": {
        "if_statement", "elif_clause", "for_statement", "while_statement",
        "except_clause", "conditional_expression",
    },
    "javascript": {
        "if_statement", "for_statement", "for_in_statement", "for_of_statement",
        "while_statement", "do_statement", "catch_clause", "switch_case",
        "ternary_expression",
    },
}
BRANCH_TYPES["typescript"] = BRANCH_TYPES["javascript"]
BRANCH_TYPES["tsx"] = BRANCH_TYPES["javascript"]

CALL_TYPES = {
    "java": {"method_invocation", "object_creation_expression"},
    "python": {"call"},
    "javascript": {"call_expression", "new_expression"},
}
CALL_TYPES["typescript"] = CALL_TYPES["javascript"]
CALL_TYPES["tsx"] = CALL_TYPES["javascript"]

COMMENT_TYPES = {
    "java": {"line_comment", "block_comment"},
    "python": {"comment"},
    "javascript": {"comment"},
    "typescript": {"comment"},
    "tsx": {"comment"},
}

FUNCTION_TYPES = {
    "java": {"method_declaration", "constructor_declaration"},
    "python": {"function_definition"},
    "javascript": {
        "function_declaration", "method_definition", "arrow_function",
        "function_expression",
    },
}
FUNCTION_TYPES["typescript"] = FUNCTION_TYPES["javascript"]
FUNCTION_TYPES["tsx"] = FUNCTION_TYPES["javascript"]

CLASS_TYPES = {
    "java": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"},
    "python": {"class_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration", "interface_declaration"},
}
CLASS_TYPES["tsx"] = CLASS_TYPES["typescript"]

# ---- Stub / debug / framework patterns (could later move to signals.yaml) ----

# UnsupportedOperationException = "이 연산은 (설계상) 지원하지 않음" — 계약 이행용 의도적 미지원일 수 있음
JAVA_UNSUPPORTED_EXCEPTIONS = {"UnsupportedOperationException"}
# NotImplementedException = "아직 안 만듦" — @Override 여부와 무관하게 미구현 신호로 유지
JAVA_NOT_IMPL_EXCEPTIONS = {"NotImplementedException"}
JAVA_STUB_LITERALS = {"null", "false", "true", "0", "0L", "0.0", "0.0f", "\"\""}
JAVA_STUB_EXPRESSIONS = {
    "new ArrayList<>()", "new HashMap<>()", "new HashSet<>()",
    "new ArrayList()", "new HashMap()", "new HashSet()",
    "Collections.emptyList()", "Collections.emptyMap()", "Collections.emptySet()",
    "List.of()", "Map.of()", "Set.of()", "Optional.empty()",
}
JAVA_DEBUG_CALLS = {"System.out.println", "System.err.println", "printStackTrace"}

# Framework annotations on enclosing class
FRAMEWORK_CLASS_ANN = {
    "controller": {"RestController", "Controller"},
    "service": {"Service"},
    "repository": {"Repository"},
    "mapper": {"Mapper"},  # MapStruct + MyBatis
    "component": {"Component", "Configuration"},
    "entity": {"Entity", "Embeddable", "MappedSuperclass"},
}
# Endpoint mapping annotations
ENDPOINT_ANN = {
    "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
    "RequestMapping",
}

JS_STUB_LITERALS = {"null", "undefined", "false", "true", "0", "''", '""',
                    "[]", "{}", "new Map()", "new Set()"}
JS_DEBUG_CALLS = {"console.log", "console.debug", "console.trace",
                  "console.warn", "console.error"}

# ---- 3단계 상태 그룹화 + 한국어 라벨 ----

VERDICT_STATUS = {
    # 정상 — 의도된 형태
    "HANDLER_DELEGATION":  "ok",
    "HANDLER_PROCESSING":  "ok",
    "SERVICE_DELEGATION":  "ok",
    "DELEGATION":          "ok",
    "ACCESSOR":            "ok",
    "GENERATED":           "ok",
    "DECLARATION":         "ok",
    "ABSTRACT":            "ok",
    "CONSTRUCTOR":         "ok",   # 객체 초기화 — Java 생성자
    # 정상 — 본문 있음 (tier)
    "LOGIC_LIGHT":         "ok",   # 짧은 본문 (5~9 SLOC 등)
    "LOGIC_NORMAL":        "ok",   # 일반 본문
    "LOGIC_COMPLEX":       "ok",   # 큰 본문 / 분기 많음
    "REAL_LOGIC":          "ok",   # legacy alias (호환)
    # 의심
    "HANDLER_BLOATED":     "suspect",  # 컨트롤러에 비즈니스 로직 침입
    "STUB_NOT_IMPL":       "suspect",
    "STUB_PLACEHOLDER":    "suspect",
    "STUB_DEBUG_ONLY":     "suspect",
    "STUB_EMPTY":          "suspect",
    # 알 수 없음
    "STRAIGHT_LINE":       "unknown",
    "UNKNOWN":             "unknown",
}

VERDICT_LABEL_KR = {
    "HANDLER_DELEGATION":  "서비스로 위임",
    "HANDLER_PROCESSING":  "컨트롤러 처리",
    "SERVICE_DELEGATION":  "타 메서드로 위임",
    "DELEGATION":          "위임",
    "ACCESSOR":            "필드 접근자",
    "GENERATED":           "프레임워크 자동 생성",
    "DECLARATION":         "인터페이스 선언",
    "ABSTRACT":            "추상 메서드",
    "CONSTRUCTOR":         "생성자",
    "LOGIC_LIGHT":         "구현됨 (단순)",
    "LOGIC_NORMAL":        "구현됨 (보통)",
    "LOGIC_COMPLEX":       "구현됨 (복잡)",
    "REAL_LOGIC":          "구현됨",          # legacy
    "HANDLER_BLOATED":     "컨트롤러 비대 (로직 침입 의심)",
    "STUB_NOT_IMPL":       "미구현 예외",
    "STUB_PLACEHOLDER":    "임시 반환값",
    "STUB_DEBUG_ONLY":     "디버그 출력만",
    "STUB_EMPTY":          "빈 본문",
    "STRAIGHT_LINE":       "한 줄 본문 (정적 분석 분류 보류)",
    "UNKNOWN":             "정적 분석 분류 보류",
}

STATUS_LABEL_KR = {
    "ok":      "정상",
    "suspect": "의심",
    "unknown": "알 수 없음",
}


# ---- node API adapters (tree-sitter 0.25 has methods, older had properties) ----

def _kind(n):
    k = n.kind
    return k() if callable(k) else k

def _start_byte(n):
    v = n.start_byte
    return v() if callable(v) else v

def _end_byte(n):
    v = n.end_byte
    return v() if callable(v) else v

def _start_pos(n):
    v = n.start_position
    if callable(v):
        v = v()
    if hasattr(v, "row"):
        return (v.row, v.column)
    return (v[0], v[1])

def _end_pos(n):
    v = n.end_position
    if callable(v):
        v = v()
    if hasattr(v, "row"):
        return (v.row, v.column)
    return (v[0], v[1])

def _child_count(n):
    v = n.child_count
    return v() if callable(v) else v

def _named_child_count(n):
    v = n.named_child_count
    return v() if callable(v) else v

def _children(n):
    cc = _child_count(n)
    for i in range(cc):
        yield n.child(i)

def _named_children(n):
    cc = _named_child_count(n)
    for i in range(cc):
        yield n.named_child(i)

def _field(n, name):
    try:
        return n.child_by_field_name(name)
    except Exception:
        return None

def _text(n, source_bytes: bytes) -> str:
    return source_bytes[_start_byte(n):_end_byte(n)].decode("utf-8", errors="ignore")

def _walk_all(root):
    """Yield every node in subtree (pre-order)."""
    stack = [root]
    while stack:
        n = stack.pop()
        yield n
        for c in _children(n):
            stack.append(c)


def _parent(n):
    p = n.parent
    return p() if callable(p) else p


# ---- annotation / context extraction ----

def _annotations(node, source_bytes: bytes) -> list[str]:
    """Return annotation names directly attached to this node (method/class).

    Java: looks under the `modifiers` child for (marker_)annotation nodes.
    JS/TS: looks for `decorator` siblings.
    Python: walks preceding siblings for `decorator` nodes.
    """
    names: list[str] = []
    # Java: modifiers child
    mods = None
    for c in _children(node):
        if _kind(c) == "modifiers":
            mods = c
            break
    if mods is not None:
        for c in _children(mods):
            if _kind(c) in ("annotation", "marker_annotation"):
                name = _annotation_name(c, source_bytes)
                if name:
                    names.append(name)
    # JS/TS decorators may appear as children with kind 'decorator'
    for c in _children(node):
        if _kind(c) == "decorator":
            txt = _text(c, source_bytes).lstrip("@").split("(")[0].strip()
            if txt:
                names.append(txt)
    return names


def _annotation_name(ann_node, source_bytes: bytes) -> str | None:
    nm = _field(ann_node, "name")
    if nm is not None:
        return _text(nm, source_bytes).split("(")[0].strip()
    for c in _children(ann_node):
        k = _kind(c)
        if k in ("identifier", "type_identifier", "scoped_type_identifier"):
            return _text(c, source_bytes).split("(")[0].strip()
    return None


def _enclosing_class(node, class_kinds: set):
    cur = _parent(node)
    while cur is not None:
        if _kind(cur) in class_kinds:
            return cur
        cur = _parent(cur)
    return None


# ---- body shape classifier ----

def _named_stmts(block_node) -> list:
    """Return non-comment named children of a block."""
    out = []
    for c in _named_children(block_node):
        k = _kind(c)
        if k in ("line_comment", "block_comment", "comment"):
            continue
        out.append(c)
    return out


_JAVA_LOG_RECEIVERS = {"log", "logger", "LOG", "LOGGER"}
_JAVA_LOG_METHODS = {"info", "debug", "error", "warn", "trace", "fatal"}


def _is_trivial_log_stmt_java(stmt, source_bytes: bytes) -> bool:
    """True if a statement is just a log/print call (no observable effect on result).

    Tree-sitter's `method_invocation` exposes the receiver under the `object`
    field and the method name under `name`. `_extract_call_target` only returns
    the method name, so we read both fields directly here.
    """
    if _kind(stmt) != "expression_statement":
        return False
    inner = None
    for c in _named_children(stmt):
        inner = c
        break
    if inner is None or _kind(inner) != "method_invocation":
        return False
    name_node = _field(inner, "name")
    method_name = _text(name_node, source_bytes).strip() if name_node is not None else ""
    obj_node = _field(inner, "object")
    obj_text = _text(obj_node, source_bytes).strip() if obj_node is not None else ""

    if obj_text in _JAVA_LOG_RECEIVERS and method_name in _JAVA_LOG_METHODS:
        return True
    # System.out.println / System.err.println / xxx.printStackTrace()
    if (obj_text.endswith(".out") or obj_text.endswith(".err")) \
            and method_name in {"println", "print", "printf"}:
        return True
    if method_name == "printStackTrace":
        return True
    return False


def _classify_java_single_stmt(stmt, source_bytes: bytes) -> str:
    """Shape category for a single statement (extracted so we can reuse on the
    tail of a 'log+...+delegate' multi-statement body)."""
    k = _kind(stmt)
    if k == "throw_statement":
        for c in _walk_all(stmt):
            if _kind(c) == "object_creation_expression":
                t = _field(c, "type")
                if t is not None:
                    name = _text(t, source_bytes).strip()
                    if name in JAVA_UNSUPPORTED_EXCEPTIONS:
                        return "stub_unsupported"
                    if name in JAVA_NOT_IMPL_EXCEPTIONS:
                        return "stub_throw"
                break
        return "single_throw"
    if k == "return_statement":
        expr = None
        for c in _named_children(stmt):
            expr = c
            break
        if expr is None:
            return "empty_return"
        text = " ".join(_text(expr, source_bytes).split())
        if text in JAVA_STUB_LITERALS or text in JAVA_STUB_EXPRESSIONS:
            return "stub_literal"
        ek = _kind(expr)
        if ek == "method_invocation":
            return "delegation"
        if ek in ("field_access", "identifier"):
            return "accessor"
        return "single_return"
    if k == "expression_statement":
        inner = None
        for c in _named_children(stmt):
            inner = c
            break
        if inner is None:
            return "single_statement"
        ik = _kind(inner)
        if ik == "method_invocation":
            tgt = _extract_call_target(inner, source_bytes) or ""
            tail = tgt.split(".")[-1] if tgt else ""
            if tgt in JAVA_DEBUG_CALLS or tail in {"println", "printStackTrace"}:
                return "stub_debug"
            return "delegation"
        if ik == "assignment_expression":
            return "accessor"
        return "single_statement"
    return "single_statement"


def _classify_java_body(body_node, source_bytes: bytes) -> str:
    if body_node is None:
        return "no_body"
    # `block` = regular method body / `constructor_body` = constructor body
    if _kind(body_node) not in ("block", "constructor_body"):
        return "no_body"
    stmts = _named_stmts(body_node)
    if not stmts:
        return "empty"
    if len(stmts) == 1:
        return _classify_java_single_stmt(stmts[0], source_bytes)

    # multi-statement: detect "trivial log statements + final delegation/return"
    # — this is the common Korean enterprise handler pattern (log.debug + return service.x())
    leading_trivial = all(_is_trivial_log_stmt_java(s, source_bytes) for s in stmts[:-1])
    if leading_trivial:
        tail_shape = _classify_java_single_stmt(stmts[-1], source_bytes)
        if tail_shape in ("delegation", "accessor", "stub_literal",
                          "stub_throw", "stub_unsupported", "stub_debug", "single_return"):
            return tail_shape

    return "multi_statement"


def _classify_js_body(body_node, source_bytes: bytes) -> str:
    if body_node is None:
        return "no_body"
    bk = _kind(body_node)
    # arrow function: body may be a single expression
    if bk not in ("statement_block",):
        text = _text(body_node, source_bytes).strip()
        if text in JS_STUB_LITERALS:
            return "stub_literal"
        if bk == "call_expression":
            tgt = _extract_call_target(body_node, source_bytes) or ""
            if tgt in JS_DEBUG_CALLS or tgt.endswith(".log"):
                return "stub_debug"
            return "delegation"
        if bk in ("identifier", "member_expression"):
            return "accessor"
        return "single_statement"
    stmts = _named_stmts(body_node)
    if not stmts:
        return "empty"
    if len(stmts) > 1:
        return "multi_statement"
    s = stmts[0]
    k = _kind(s)
    if k == "throw_statement":
        # JS: "throw new Error('not implemented')" — message-based check (best effort)
        snippet = _text(s, source_bytes)
        if "not implemented" in snippet.lower() or "notimplemented" in snippet.lower():
            return "stub_throw"
        return "single_throw"
    if k == "return_statement":
        expr = None
        for c in _named_children(s):
            expr = c
            break
        if expr is None:
            return "empty_return"
        text = _text(expr, source_bytes).strip()
        if text in JS_STUB_LITERALS:
            return "stub_literal"
        ek = _kind(expr)
        if ek == "call_expression":
            tgt = _extract_call_target(expr, source_bytes) or ""
            if tgt in JS_DEBUG_CALLS or tgt.endswith(".log"):
                return "stub_debug"
            return "delegation"
        if ek in ("identifier", "member_expression"):
            return "accessor"
        return "single_return"
    if k == "expression_statement":
        inner = None
        for c in _named_children(s):
            inner = c
            break
        if inner is None:
            return "single_statement"
        ik = _kind(inner)
        if ik == "call_expression":
            tgt = _extract_call_target(inner, source_bytes) or ""
            if tgt in JS_DEBUG_CALLS or tgt.endswith(".log"):
                return "stub_debug"
            return "delegation"
        if ik in ("assignment_expression",):
            return "accessor"
        return "single_statement"
    return "single_statement"


def _classify_body(body_node, source_bytes: bytes, lang: str) -> str:
    if lang == "java":
        return _classify_java_body(body_node, source_bytes)
    if lang in ("javascript", "typescript", "tsx"):
        return _classify_js_body(body_node, source_bytes)
    # python: very light heuristic
    if lang == "python":
        if body_node is None:
            return "no_body"
        stmts = _named_stmts(body_node)
        if not stmts:
            return "empty"
        # only `pass` ?
        if len(stmts) == 1 and _kind(stmts[0]) == "pass_statement":
            return "empty"
        return "multi_statement" if len(stmts) > 1 else "single_statement"
    return "unknown"


# ---- combined verdict ----

def _verdict(body_class: str, class_kind: str, class_anns: list[str],
             method_anns: list[str], cc: int, sloc: int,
             node_kind: str | None = None) -> str:
    """Produce a coarse verdict label combining body shape and context.

    `node_kind` is the AST kind of the function-like node itself
    (e.g. method_declaration / constructor_declaration). Constructors get
    their own verdict regardless of body classification — even an empty
    constructor is a valid `생성자`, not an abstract method.
    """
    cset = set(class_anns)
    mset = set(method_anns)

    is_interface = class_kind == "interface_declaration"
    is_mapper = bool(cset & FRAMEWORK_CLASS_ANN["mapper"])
    is_controller = bool(cset & FRAMEWORK_CLASS_ANN["controller"])
    is_service = bool(cset & FRAMEWORK_CLASS_ANN["service"])
    is_repo = bool(cset & FRAMEWORK_CLASS_ANN["repository"])
    is_entity = bool(cset & FRAMEWORK_CLASS_ANN["entity"])
    has_endpoint = bool(mset & ENDPOINT_ANN)

    # Constructor short-circuit — same-name "function" is Java's object init,
    # not a method; even with `super(...)` only it is a valid implementation.
    if node_kind == "constructor_declaration":
        return "CONSTRUCTOR"

    # Generated / declaration patterns (no_body is expected)
    if body_class == "no_body":
        if is_mapper:
            return "GENERATED"        # MapStruct / MyBatis Mapper interface
        if is_repo and is_interface:
            return "DECLARATION"      # Spring Data JPA repository
        if is_interface:
            return "DECLARATION"
        return "ABSTRACT"

    # Explicit stub markers
    if body_class == "stub_unsupported":
        # @Override + throw UnsupportedOperationException 는 "지원하지 않는 선택적 연산"
        # 관용구(불변 컬렉션 add(), 읽기전용 스트림 리스너 등) — 상속 계약을 이행하는
        # 의도된 구현이지 미완성이 아니다. 계약 컨텍스트(@Override)가 있을 때만 정상 처리.
        if "Override" in mset:
            return "ABSTRACT"
        return "STUB_NOT_IMPL"
    if body_class == "stub_throw":
        return "STUB_NOT_IMPL"
    if body_class == "stub_literal":
        return "STUB_PLACEHOLDER"
    if body_class == "stub_debug":
        return "STUB_DEBUG_ONLY"
    if body_class == "empty":
        return "STUB_EMPTY"

    # Thin wrappers / delegation are legitimate at framework boundaries
    if body_class == "delegation":
        if has_endpoint or is_controller:
            return "HANDLER_DELEGATION"
        if is_service:
            return "SERVICE_DELEGATION"
        return "DELEGATION"

    if body_class == "accessor":
        if is_entity:
            return "ACCESSOR"
        return "ACCESSOR"

    # multi_statement: layer-aware tiered classification.
    # The old logic flagged any SLOC>=4 method as "REAL_LOGIC (비즈니스 로직)" which
    # over-claimed for Controller handlers and short utility wrappers. We now
    # split into (a) controller-context labels and (b) logic tiers driven by
    # multiple signals, not line count alone.
    if body_class == "multi_statement":
        is_controller_like = is_controller or has_endpoint
        if is_controller_like:
            # Controllers should be thin. Multi-statement controllers are usually
            # validation + a single service call + response wrap. Only flag the
            # body as bloated when it crosses a meaningful threshold (more code
            # AND non-trivial branching).
            if (sloc >= 25 and cc >= 5) or sloc >= 50 or cc >= 8:
                return "HANDLER_BLOATED"
            return "HANDLER_PROCESSING"

        # Non-controller (Service / Util / etc.) — tier by complexity.
        if cc >= 8 or sloc >= 40:
            return "LOGIC_COMPLEX"
        if cc >= 3 or sloc >= 15:
            return "LOGIC_NORMAL"
        if cc >= 2 or sloc >= 5:
            return "LOGIC_LIGHT"
        return "STRAIGHT_LINE"

    # single_throw, single_return, single_statement that wasn't classified above
    if body_class in ("single_throw", "single_return", "single_statement"):
        return "STRAIGHT_LINE"

    return "UNKNOWN"


# ---- core ----

def _count_comment_lines(root, comment_types: set) -> int:
    lines: set[int] = set()
    for n in _walk_all(root):
        if _kind(n) in comment_types:
            sr, _ = _start_pos(n)
            er, _ = _end_pos(n)
            for ln in range(sr, er + 1):
                lines.add(ln)
    return len(lines)


def _file_line_metrics(source_bytes: bytes, root, lang: str) -> dict:
    if not source_bytes:
        return {"total_lines": 0, "blank_lines": 0, "comment_lines": 0, "code_lines": 0, "comment_ratio": 0.0}
    total_lines = source_bytes.count(b"\n") + (0 if source_bytes.endswith(b"\n") else 1)
    blank_lines = sum(1 for line in source_bytes.splitlines() if not line.strip())
    comment_lines = _count_comment_lines(root, COMMENT_TYPES.get(lang, set()))
    code_lines = max(0, total_lines - blank_lines - comment_lines)
    return {
        "total_lines": total_lines,
        "blank_lines": blank_lines,
        "comment_lines": comment_lines,
        "code_lines": code_lines,
        "comment_ratio": round(comment_lines / total_lines, 3) if total_lines else 0.0,
    }


def _identifier_name(node, source_bytes: bytes) -> str | None:
    nm = _field(node, "name")
    if nm is not None:
        return _text(nm, source_bytes)
    for c in _children(node):
        if _kind(c) == "identifier":
            return _text(c, source_bytes)
    return None


def _signature(node, source_bytes: bytes) -> str:
    text = _text(node, source_bytes)
    first = text.split("\n", 1)[0].strip()
    return first.rstrip("{:").strip()


def _parameter_count(node) -> int:
    pl = _field(node, "parameters") or _field(node, "parameter_list")
    if pl is None:
        for c in _children(node):
            k = _kind(c)
            if "parameter" in k or k == "formal_parameters":
                pl = c
                break
    if pl is None:
        return 0
    count = 0
    for c in _children(pl):
        if _kind(c) in (
            "formal_parameter", "parameter", "required_parameter",
            "optional_parameter", "rest_parameter", "default_parameter",
            "typed_parameter", "typed_default_parameter",
            "spread_element", "spread_parameter",
            "identifier",
        ):
            count += 1
    return count


def _extract_call_target(call_node, source_bytes: bytes) -> str | None:
    name_node = _field(call_node, "name") or _field(call_node, "function")
    if name_node is None:
        return None
    txt = _text(name_node, source_bytes)
    return txt.split("(")[0].strip()


def _walk_body_metrics(body_node, source_bytes: bytes, lang: str) -> dict:
    stmt_types = STATEMENT_TYPES.get(lang, set())
    branch_types = BRANCH_TYPES.get(lang, set())
    call_types = CALL_TYPES.get(lang, set())

    statement_count = 0
    cc = 1
    fan_out_names: set[str] = set()
    max_depth = 0

    block_like = {"block", "function_body", "class_body"}
    # explicit depth-tracking DFS
    stack = [(body_node, 0)]
    while stack:
        n, depth = stack.pop()
        k = _kind(n)
        if k in stmt_types:
            statement_count += 1
        if k in branch_types:
            cc += 1
        if k in call_types:
            name = _extract_call_target(n, source_bytes)
            if name:
                fan_out_names.add(name)
        if k == "binary_expression":
            op = _field(n, "operator")
            if op is not None:
                tok = source_bytes[_start_byte(op):_end_byte(op)]
                if tok in (b"&&", b"||"):
                    cc += 1
        new_depth = depth + 1 if k in block_like else depth
        if new_depth > max_depth:
            max_depth = new_depth
        for c in _children(n):
            stack.append((c, new_depth))

    return {
        "statement_count": statement_count,
        "cyclomatic_complexity": cc,
        "fan_out": len(fan_out_names),
        "fan_out_targets": sorted(fan_out_names)[:20],
        "nesting_depth": max_depth,
    }


def _method_metrics(node, source_bytes: bytes, lang: str) -> dict:
    name = _identifier_name(node, source_bytes) or "<anonymous>"
    sr, _ = _start_pos(node)
    er, _ = _end_pos(node)
    start_line = sr + 1
    end_line = er + 1
    total_lines = end_line - start_line + 1

    body = _field(node, "body")
    walk_root = body if body is not None else node
    node_bytes = source_bytes[_start_byte(node):_end_byte(node)]
    blank_lines = sum(1 for line in node_bytes.splitlines() if not line.strip())
    comment_lines = _count_comment_lines(node, COMMENT_TYPES.get(lang, set()))
    sloc = max(0, total_lines - blank_lines - comment_lines)
    body_metrics = _walk_body_metrics(walk_root, source_bytes, lang)
    sig = _signature(node, source_bytes)

    method_anns = _annotations(node, source_bytes)
    enclosing = _enclosing_class(node, CLASS_TYPES.get(lang, set()))
    class_kind = _kind(enclosing) if enclosing is not None else None
    class_name = _identifier_name(enclosing, source_bytes) if enclosing is not None else None
    class_anns = _annotations(enclosing, source_bytes) if enclosing is not None else []

    body_class = _classify_body(body, source_bytes, lang)
    verdict = _verdict(
        body_class, class_kind, class_anns, method_anns,
        body_metrics["cyclomatic_complexity"], sloc,
        node_kind=_kind(node),
    )
    status = VERDICT_STATUS.get(verdict, "unknown")
    label_kr = VERDICT_LABEL_KR.get(verdict, "판정 불가")
    status_kr = STATUS_LABEL_KR[status]

    return {
        "name": name,
        "kind": _kind(node),
        "signature": sig[:200],
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "blank_lines": blank_lines,
        "comment_lines": comment_lines,
        "sloc": sloc,
        "parameter_count": _parameter_count(node),
        **body_metrics,
        # ---- interpretive signals ----
        "body_class": body_class,
        "verdict": verdict,
        "status": status,
        "status_kr": status_kr,
        "label_kr": label_kr,
        "annotations": method_anns,
        "class_name": class_name,
        "class_kind": class_kind,
        "class_annotations": class_anns,
    }


def _collect(root, source_bytes: bytes, lang: str, kinds: set, builder) -> list[dict]:
    out: list[dict] = []
    for n in _walk_all(root):
        if _kind(n) in kinds:
            try:
                out.append(builder(n, source_bytes, lang))
            except Exception as e:
                out.append({"name": "<error>", "error": str(e)})
    return out


def _class_summary(node, source_bytes: bytes, lang: str) -> dict:
    name = _identifier_name(node, source_bytes) or "<anonymous>"
    sr, _ = _start_pos(node)
    er, _ = _end_pos(node)
    return {
        "name": name, "kind": _kind(node),
        "start_line": sr + 1, "end_line": er + 1,
        "annotations": _annotations(node, source_bytes),
    }


_VUE_SCRIPT_RE = re.compile(
    rb"<script\b([^>]*)>(.*?)</script\s*>", re.DOTALL | re.IGNORECASE
)


def _extract_vue_script(source_bytes: bytes) -> tuple[bytes, str]:
    m = _VUE_SCRIPT_RE.search(source_bytes)
    if not m:
        return b"", "javascript"
    attrs = m.group(1).decode("utf-8", errors="ignore").lower()
    body = m.group(2)
    lang = "typescript" if "ts" in attrs else "javascript"
    leading = source_bytes[: m.start(2)].count(b"\n")
    return b"\n" * leading + body, lang


def analyze_file(abs_path: str, ext: str) -> dict:
    if get_parser is None:
        return {"error": "tree-sitter-language-pack not installed"}
    p = Path(abs_path)
    if not p.exists():
        return {"error": f"file not found: {abs_path}"}
    source_bytes = p.read_bytes()

    lang = LANG_FOR_EXT.get(ext.lower())
    if lang is None:
        return {"error": f"unsupported extension: {ext}"}

    parse_lang = lang
    parse_bytes = source_bytes
    note = None
    if lang == "vue":
        parse_bytes, parse_lang = _extract_vue_script(source_bytes)
        if not parse_bytes.strip():
            total = source_bytes.count(b"\n") + (0 if source_bytes.endswith(b"\n") else 1)
            return {
                "lang": "vue", "parsed_as": parse_lang,
                "file_metrics": {
                    "total_lines": total, "blank_lines": 0, "comment_lines": 0,
                    "code_lines": 0, "comment_ratio": 0.0,
                },
                "classes": [], "functions": [], "note": "no <script> block",
            }

    try:
        parser = get_parser(parse_lang)
    except Exception as e:
        return {"error": f"parser load failed for {parse_lang}: {e}"}

    # tree-sitter 0.25+ requires str input; byte offsets reference UTF-8 of that str
    source_str = parse_bytes.decode("utf-8", errors="ignore")
    source_bytes_norm = source_str.encode("utf-8")
    try:
        tree = parser.parse(source_str)
    except Exception as e:
        return {"error": f"parse failed: {e}"}
    root = tree.root_node
    if callable(root):
        root = root()

    file_metrics = _file_line_metrics(source_bytes_norm, root, parse_lang)
    classes = _collect(root, source_bytes_norm, parse_lang, CLASS_TYPES.get(parse_lang, set()), _class_summary)
    functions = _collect(root, source_bytes_norm, parse_lang, FUNCTION_TYPES.get(parse_lang, set()), _method_metrics)

    result = {
        "lang": lang, "parsed_as": parse_lang,
        "file_metrics": file_metrics,
        "classes": classes, "functions": functions,
    }
    if note:
        result["note"] = note
    return result
