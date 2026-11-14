"""
CodeSentry - an automated code reviewer for Python and Java.

It flags common syntax errors and the loop / logic mistakes beginners make most,
and points to the exact line of each one.

Personal rebuild of an automated code-review tool I made during my internship
at Metabo (this is new code, not the company's).

Usage:
    python review.py my_code.py
    python review.py MyCode.java
    python review.py --stats some_folder/     # how often each mistake appears, as probabilities
"""

import ast
import json
import os
import re
import sys

# Every issue is a dict: {"line": int, "kind": "syntax" | "logic" | "warning", "rule": str, "message": str, "tip": str}


def issue(line, kind, rule, message, tip=""):
    return {"line": line, "kind": kind, "rule": rule, "message": message, "tip": tip}


# =====================================================================
# Python
# =====================================================================

def review_python(code):
    # 1. Syntax: Python stops at the first syntax error, so we report that one.
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        name = "Indentation error" if isinstance(e, IndentationError) else "Syntax error"
        rule = "indentation" if isinstance(e, IndentationError) else "syntax"
        return [issue(e.lineno or 1, "syntax", rule, f"{name}: {e.msg}", syntax_tip(e.msg))]

    issues = []
    for node in ast.walk(tree):

        # 2. while True with no way out
        if isinstance(node, ast.While) and is_true(node.test) and not has_exit(node.body):
            issues.append(issue(node.lineno, "logic", "infinite-loop",
                                "Possible infinite loop: 'while True' has no break or return.",
                                "Add a break (or return) when the loop's job is done."))

        # 3. while loop whose counter never changes
        if isinstance(node, ast.While) and not is_true(node.test):
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            changed = assigned_names(node.body)
            calls = any(isinstance(n, ast.Call) for n in ast.walk(node.test))
            if names and not (names & changed) and not calls and not has_exit(node.body):
                v = ", ".join(sorted(names))
                issues.append(issue(node.lineno, "logic", "loop-var-not-updated",
                                    f"Loop condition uses {v}, but the loop never changes it, so it may never stop.",
                                    f"Update {v} inside the loop, e.g. i += 1."))

        # 4. range(len(x) + 1) -> one step too far
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call) and getattr(node.iter.func, "id", "") == "range":
            args = node.iter.args
            stop = args[1] if len(args) >= 2 else (args[0] if args else None)
            if (isinstance(stop, ast.BinOp) and isinstance(stop.op, ast.Add) and is_len_call(stop.left)
                    and isinstance(stop.right, ast.Constant) and stop.right.value == 1):
                issues.append(issue(node.lineno, "logic", "off-by-one",
                                    "Off-by-one: range(len(...) + 1) goes one index past the end (IndexError).",
                                    "Use range(len(x)). The last valid index is len(x) - 1."))

        # 5. loop that always stops after the first pass
        if isinstance(node, (ast.For, ast.While)) and node.body and isinstance(node.body[-1], (ast.Return, ast.Break)) \
                and not any(isinstance(s, (ast.If, ast.Try)) for s in node.body):
            issues.append(issue(node.body[-1].lineno, "logic", "loop-runs-once",
                                "This loop always stops after its first pass, because the return/break isn't inside an if.",
                                "Move the return/break inside an if, or after the loop."))

        # 6. changing a list while looping over it
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name):
            target = node.iter.id
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name) and sub.func.value.id == target
                        and sub.func.attr in ("remove", "append", "pop", "insert", "clear")):
                    issues.append(issue(sub.lineno, "logic", "modify-while-iterating",
                                        f"'{target}' is changed while the loop is going through it, so items get skipped or the loop never ends.",
                                        f"Loop over a copy (for x in {target}[:]) or build a new list."))
                    break

        # 7. comparisons with None / literals
        if isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                left = node.left
                if isinstance(op, (ast.Eq, ast.NotEq)) and (is_none(right) or is_none(left)):
                    issues.append(issue(node.lineno, "warning", "none-comparison",
                                        "Comparing with None using == or !=.", "Use 'is None' or 'is not None'."))
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant) and right.value is not None \
                        and not isinstance(right.value, bool):
                    issues.append(issue(node.lineno, "logic", "is-literal",
                                        "'is' checks identity, not value, so this comparison can be wrong.",
                                        "Use == to compare values."))

        # 8. mutable default argument
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.args.defaults + node.args.kw_defaults:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    issues.append(issue(node.lineno, "logic", "mutable-default",
                                        f"Function '{node.name}' uses a list/dict as a default value, which is shared between calls.",
                                        "Use None as the default and create the list inside the function."))

        # 9. bare except
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append(issue(node.lineno, "warning", "bare-except",
                                "Bare 'except:' hides every error, including typos.",
                                "Catch a specific error, e.g. except ValueError:"))

        # 10. assigned but never used (inside functions)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stored, loaded = {}, set()
            for n in ast.walk(node):
                if isinstance(n, ast.Name):
                    if isinstance(n.ctx, ast.Store): stored.setdefault(n.id, n.lineno)
                    else: loaded.add(n.id)
            for name, line in stored.items():
                if name not in loaded and not name.startswith("_"):
                    issues.append(issue(line, "warning", "unused-variable",
                                        f"Variable '{name}' is assigned but never used.",
                                        "Remove it, or check for a typo where it should be used."))

    return dedupe(issues)


def syntax_tip(msg):
    m = msg.lower()
    if "expected ':'" in m: return "Add a colon at the end of the if / for / while / def line."
    if "indent" in m: return "Make sure the lines in each block line up with the same number of spaces."
    if "never closed" in m or "unexpected eof" in m or "was not closed" in m: return "A bracket or quote was opened but never closed."
    if "unterminated string" in m: return "A string is missing its closing quote."
    if "invalid syntax" in m and "maybe you meant '==' " in m: return "Use == to compare, = to assign."
    if "'=' " in m or "maybe you meant '=='" in m: return "Use == to compare, = to assign."
    return "Check this line and the one above it for typos."


def is_true(n): return isinstance(n, ast.Constant) and n.value is True
def is_none(n): return isinstance(n, ast.Constant) and n.value is None
def is_len_call(n): return isinstance(n, ast.Call) and getattr(n.func, "id", "") == "len"


def has_exit(body):
    """True if the block can leave the loop (break / return / raise / sys.exit), not counting nested loops' breaks."""
    for stmt in body:
        for n in walk_no_nested_loops(stmt):
            if isinstance(n, (ast.Break, ast.Return, ast.Raise)): return True
            if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in ("exit", "quit"): return True
    return False


def walk_no_nested_loops(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.For, ast.While, ast.FunctionDef)):
            # a return inside a nested loop still leaves; a break only leaves the nested loop
            for n in ast.walk(child):
                if isinstance(n, (ast.Return, ast.Raise)): yield n
            continue
        yield from walk_no_nested_loops(child)


def assigned_names(body):
    names = set()
    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): names.add(n.id)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
                names.add(n.func.value.id)  # e.g. items.pop() changes items
    return names


def dedupe(issues):
    seen, out = set(), []
    for i in sorted(issues, key=lambda i: (i["line"], i["rule"])):
        key = (i["line"], i["rule"])
        if key not in seen:
            seen.add(key); out.append(i)
    return out


# =====================================================================
# Java (pattern based: Java has no built-in parser in Python)
# =====================================================================

CONTROL = re.compile(r"^\s*(if|for|while|switch|else|do|try|catch|finally)\b")
DECL = re.compile(r"^\s*(public|private|protected|static|final|abstract|class|interface|enum|@|import|package)\b")


def strip_java(code):
    """Blanks out comments and the inside of strings/chars, keeping line structure, so brackets in text are ignored."""
    out, i, n = [], 0, len(code)
    unclosed = []
    line = 1
    while i < n:
        c = code[i]
        if code.startswith("//", i):
            while i < n and code[i] != "\n": out.append(" "); i += 1
        elif code.startswith("/*", i):
            while i < n and not code.startswith("*/", i):
                out.append("\n" if code[i] == "\n" else " "); line += code[i] == "\n"; i += 1
            out.append("  "); i += 2
        elif c in "\"'":
            q, start_line = c, line
            out.append(c); i += 1
            while i < n and code[i] != q and code[i] != "\n":
                if code[i] == "\\": out.append(" "); i += 1
                out.append(" "); i += 1
            if i < n and code[i] == q: out.append(q); i += 1
            else: unclosed.append(start_line)
        else:
            if c == "\n": line += 1
            out.append(c); i += 1
    return "".join(out), unclosed


def review_java(code):
    issues = []
    clean, unclosed = strip_java(code)
    lines = clean.split("\n")
    raw = code.split("\n")

    for ln in unclosed:
        issues.append(issue(ln, "syntax", "unclosed-string", "This string or char is missing its closing quote.",
                            "Add the closing \" (or ') before the end of the line."))

    # brackets
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    skip = set(unclosed)  # an unclosed string hides the rest of its line, so don't count brackets there
    for ln, text in enumerate(lines, 1):
        if ln in skip: continue
        for ch in text:
            if ch in "([{": stack.append((ch, ln))
            elif ch in ")]}":
                if stack and stack[-1][0] == pairs[ch]: stack.pop()
                else:
                    issues.append(issue(ln, "syntax", "bracket-mismatch", f"Unexpected '{ch}' with no matching opening bracket.",
                                        "Check that every (, [ and { has a partner."))
    for ch, ln in stack:
        closing = {"(": ")", "[": "]", "{": "}"}[ch]
        issues.append(issue(ln, "syntax", "bracket-unclosed", f"'{ch}' opened here is never closed.", f"Add the matching '{closing}'."))

    for ln, text in enumerate(lines, 1):
        s = text.strip()
        nxt = next((l.strip() for l in lines[ln:] if l.strip()), "")

        # missing semicolon
        if ln not in skip and s and not s.endswith((";", "{", "}", ",", "(", "+", "-", "*", "/", "&&", "||", "?", ":", "=", "->", ".")) \
                and not CONTROL.match(s) and not DECL.match(s) and not s.startswith(("}", "case ", "default", "//", "*", "/*", ".")) \
                and not nxt.startswith(("{", ".", "+", "-", "&&", "||", "?", ":", ")")) \
                and (re.search(r"[\w\])\"']$", s)) and (re.search(r"=|\(|\breturn\b|\+\+|--|\bbreak\b|\bcontinue\b", s)) \
                and not re.match(r"^[\w<>\[\],\s]+\s+\w+\s*\([^;]*\)\s*(throws [\w, ]+)?$", s):
            issues.append(issue(ln, "syntax", "missing-semicolon", "Missing semicolon at the end of this statement.", "Add ; at the end of the line."))

        # if (x = 5)
        m = re.match(r"^\s*(?:\}\s*else\s+)?if\s*\((.*)\)", text)
        if m and re.search(r"(?<![=!<>+\-*/%&|^])=(?!=)", m.group(1)):
            issues.append(issue(ln, "logic", "assign-in-if", "Single '=' inside an if condition assigns instead of comparing.",
                                "Use == to compare."))

        # if (...); / for (...); / while (...);  -> empty body
        if re.match(r"^\s*(if|for|while)\s*\(.*\)\s*;", text) and not re.match(r"^\s*\}\s*while", raw[ln - 1]):
            issues.append(issue(ln, "logic", "empty-body", "The semicolon right after the condition ends the statement, so the block below always runs (or the loop does nothing).",
                                "Remove the ; after the closing bracket."))

        # comparing strings with ==
        if re.search(r'(==|!=)\s*"|"\s*(==|!=)', raw[ln - 1]):
            issues.append(issue(ln, "logic", "string-equals", "Strings compared with == check if they are the same object, not the same text.",
                                'Use a.equals("text").'))

        # for loops: off-by-one and wrong direction
        fm = re.match(r"^\s*for\s*\(\s*(?:int\s+)?(\w+)\s*=\s*([^;]*);\s*([^;]*);\s*([^)]*)\)", raw[ln - 1])
        if fm:
            var, init, cond, upd = fm.groups()
            if re.search(rf"\b{var}\s*<=\s*[\w.]+\.(length|size\(\)|length\(\))", cond):
                issues.append(issue(ln, "logic", "off-by-one", "Off-by-one: '<= length' goes one past the last index (ArrayIndexOutOfBoundsException).",
                                    "Use < instead of <=."))
            going_up = re.search(rf"{var}\s*\+\+|\+\+\s*{var}|{var}\s*\+=", upd)
            going_down = re.search(rf"{var}\s*--|--\s*{var}|{var}\s*-=", upd)
            if (going_up and re.search(rf"\b{var}\s*>", cond)) or (going_down and re.search(rf"\b{var}\s*<", cond)):
                issues.append(issue(ln, "logic", "wrong-direction", f"The loop changes {var} in the wrong direction for its condition, so it may never stop.",
                                    "Use ++ with < and -- with >."))

        # while loops
        wm = re.match(r"^\s*while\s*\((.*)\)\s*\{?\s*$", text)
        if wm:
            body = block_after(lines, ln)
            cond = wm.group(1).strip()
            if cond == "true" and not re.search(r"\b(break|return|throw|System\.exit)\b", body):
                issues.append(issue(ln, "logic", "infinite-loop", "Possible infinite loop: 'while (true)' has no break or return.",
                                    "Add a break when the loop's job is done."))
            elif cond != "true":
                vars_ = set(re.findall(r"\b([a-zA-Z_]\w*)\b", cond)) - {"true", "false", "null", "length", "size"}
                vars_ = {v for v in vars_ if not re.search(rf"\b{v}\s*\(", cond) and not re.search(rf"\.\s*{v}\b", cond)}
                if vars_ and "(" not in cond and not any(re.search(rf"\b{v}\s*(\+\+|--|[+\-*/]?=(?!=))|(\+\+|--)\s*{v}\b", body) for v in vars_) \
                        and not re.search(r"\b(break|return|throw)\b", body):
                    v = ", ".join(sorted(vars_))
                    issues.append(issue(ln, "logic", "loop-var-not-updated", f"Loop condition uses {v}, but the loop never changes it, so it may never stop.",
                                        f"Update {v} inside the loop, e.g. i++."))

    return dedupe(issues)


def block_after(lines, ln):
    """Text of the { ... } block starting on line ln (1-based)."""
    depth, started, buf = 0, False, []
    for text in lines[ln - 1:]:
        for ch in text:
            if ch == "{": depth += 1; started = True
            elif ch == "}": depth -= 1
        buf.append(text)
        if started and depth <= 0: break
    return "\n".join(buf[0:])[(buf[0].find("{") + 1) if buf and "{" in buf[0] else 0:]


# =====================================================================
# Entry points
# =====================================================================

def detect_language(code):
    if re.search(r"\b(public|private|static)\s+\w+|System\.out|;\s*$|\bint\s+\w+\s*=", code, re.M) and not re.search(r"^\s*def\s+\w+\(.*\):", code, re.M):
        return "java"
    return "python"


def review(code, lang="auto"):
    if lang == "auto": lang = detect_language(code)
    issues = review_python(code) if lang == "python" else review_java(code)
    return {"language": lang, "issues": issues}


def review_json(code, lang="auto"):
    return json.dumps(review(code, lang))


RULE_NAMES = {
    "syntax": "Syntax error", "indentation": "Indentation error", "missing-semicolon": "Missing semicolon",
    "bracket-mismatch": "Bracket mismatch", "bracket-unclosed": "Unclosed bracket", "unclosed-string": "Unclosed string",
    "infinite-loop": "Infinite loop", "loop-var-not-updated": "Loop counter never updated", "off-by-one": "Off-by-one loop",
    "loop-runs-once": "Loop runs only once", "modify-while-iterating": "List changed while looping",
    "wrong-direction": "Loop counts the wrong way", "empty-body": "Stray semicolon after condition",
    "assign-in-if": "= instead of == in if", "string-equals": "Strings compared with ==", "is-literal": "'is' used instead of ==",
    "mutable-default": "List/dict as default argument", "none-comparison": "== None", "bare-except": "Bare except",
    "unused-variable": "Unused variable",
}


def print_report(path, result):
    colors = {"syntax": "\033[91m", "logic": "\033[93m", "warning": "\033[94m"}
    lines = open(path, encoding="utf-8", errors="ignore").read().split("\n")
    if not result["issues"]:
        print(f"{path}: no issues found ({result['language']})"); return
    print(f"{path} ({result['language']}): {len(result['issues'])} issue(s)\n")
    for i in result["issues"]:
        code_line = lines[i["line"] - 1].strip() if i["line"] - 1 < len(lines) else ""
        print(f"{colors[i['kind']]}line {i['line']:>4} [{i['kind'].upper()}]\033[0m {i['message']}")
        print(f"           {code_line}")
        if i["tip"]: print(f"           tip: {i['tip']}")
    print("\a", end="")  # terminal bell


def stats(folder):
    """Reviews every .py and .java file in a folder and prints how likely each mistake type is."""
    counts, files = {}, 0
    for root, _, names in os.walk(folder):
        for name in names:
            if name.endswith((".py", ".java")):
                files += 1
                code = open(os.path.join(root, name), encoding="utf-8", errors="ignore").read()
                for i in review(code, "java" if name.endswith(".java") else "python")["issues"]:
                    counts[i["rule"]] = counts.get(i["rule"], 0) + 1
    total = sum(counts.values())
    print(f"Reviewed {files} files, found {total} issues.\n")
    for rule, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        p = c / total
        print(f"{RULE_NAMES.get(rule, rule):32s} {c:4d}   P = {p:.2f}  {'#' * round(p * 40)}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--stats":
        stats(sys.argv[2])
    elif len(sys.argv) == 2:
        path = sys.argv[1]
        lang = "java" if path.endswith(".java") else "python" if path.endswith(".py") else "auto"
        print_report(path, review(open(path, encoding="utf-8", errors="ignore").read(), lang))
    else:
        print(__doc__)
