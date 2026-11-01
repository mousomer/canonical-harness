"""Syntax-only private measurement helpers. No historical code is executed."""

import hashlib
import json
import re
from pathlib import PurePosixPath

import bashlex
import esprima

UNKNOWN = {"unknown": True}
CLASSES = "WHOLE_READ BOUNDED_READ SEARCH GIT_INSPECT EDIT GENERATED_REFRESH VERIFY RUNTIME_OR_BUILD METADATA GOV_QUERY OTHER_OR_UNKNOWN".split()


def hash_value(value):
    raw = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    )
    return hashlib.sha256(raw).hexdigest()


def is_unknown(value):
    return value == UNKNOWN


def toolrefs(value):
    if isinstance(value, dict):
        if "toolref" in value:
            return [value["toolref"]]
        return [r for v in value.values() for r in toolrefs(v)]
    if isinstance(value, list):
        return [r for v in value for r in toolrefs(v)]
    return []


def parse_wrapper(source):
    """Bounded constant propagation, symbolic results, and literal-array map only."""
    prefix = "async function trace(){\n"
    try:
        tree = esprima.parseScript(prefix + source + "\n}", {"range": True}).toDict()
    except Exception as error:
        return {
            "sites": [],
            "emissions": [],
            "reason": "unsupported_historical_trace_format",
            "error": type(error).__name__,
        }
    sites, emissions, problems = [], [], []
    env = {}

    def evaluate(node, bindings, conditional=False):
        if not isinstance(node, dict):
            return UNKNOWN
        typ = node.get("type")
        if typ == "Literal":
            return node.get("value")
        if typ == "Identifier":
            return bindings.get(node["name"], UNKNOWN)
        if typ == "AwaitExpression":
            return evaluate(node["argument"], bindings, conditional)
        if typ == "ObjectExpression":
            result = {}
            for prop in node["properties"]:
                if prop.get("type") != "Property" or prop.get("computed"):
                    return UNKNOWN
                key = prop["key"].get("name", prop["key"].get("value"))
                result[str(key)] = evaluate(prop["value"], bindings, conditional)
            return result
        if typ == "ArrayExpression":
            return [evaluate(n, bindings, conditional) for n in node["elements"]]
        if typ == "TemplateLiteral":
            result = node["quasis"][0]["value"]["cooked"]
            for expression, quasi in zip(node["expressions"], node["quasis"][1:]):
                value = evaluate(expression, bindings, conditional)
                if isinstance(value, (dict, list)):
                    return UNKNOWN
                result += str(value) + quasi["value"]["cooked"]
            return result
        if typ == "BinaryExpression" and node["operator"] == "+":
            left, right = (
                evaluate(node["left"], bindings, conditional),
                evaluate(node["right"], bindings, conditional),
            )
            if isinstance(left, str) and isinstance(right, (str, int, float)):
                return left + str(right)
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
            return UNKNOWN
        if typ == "MemberExpression":
            obj = evaluate(node["object"], bindings, conditional)
            key = (
                evaluate(node["property"], bindings, conditional)
                if node.get("computed")
                else node["property"].get("name")
            )
            if isinstance(obj, dict) and "toolref" in obj:
                return {**obj, "projection": key}
            if isinstance(obj, dict) and isinstance(key, str):
                return obj.get(key, UNKNOWN)
            if isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
                return obj[key]
            return UNKNOWN
        if typ == "CallExpression":
            callee = node["callee"]
            name = callee.get("name")
            objname = callee.get("object", {}).get("name")
            prop = callee.get("property", {}).get("name", callee.get("property", {}).get("value"))
            if objname == "tools" and isinstance(prop, str):
                args = [evaluate(n, bindings, conditional) for n in node["arguments"]]
                ordinal = len(sites)
                sites.append(
                    {
                        "ordinal": ordinal,
                        "tool_name": prop,
                        "arguments": args[0] if len(args) == 1 else UNKNOWN,
                        "source_range": [x - len(prefix) for x in node["range"]],
                        "conditional": conditional,
                    }
                )
                return {"toolref": ordinal}
            if objname == "Promise" and prop == "all":
                return evaluate(node["arguments"][0], bindings, conditional)
            if prop == "map" and callee.get("type") == "MemberExpression":
                values = evaluate(callee["object"], bindings, conditional)
                callback = node["arguments"][0] if node["arguments"] else {}
                if (
                    isinstance(values, list)
                    and len(values) <= 256
                    and callback.get("type") == "ArrowFunctionExpression"
                ):
                    results = []
                    for i, value in enumerate(values):
                        local = dict(bindings)
                        for param, val in zip(callback["params"], [value, i]):
                            local[param.get("name", "")] = val
                        results.append(evaluate(callback["body"], local, conditional))
                    return results
            if prop == "join":
                values = evaluate(callee["object"], bindings, conditional)
                separator = (
                    evaluate(node["arguments"][0], bindings, conditional)
                    if node["arguments"]
                    else ","
                )
                if (
                    isinstance(values, list)
                    and all(isinstance(v, str) for v in values)
                    and isinstance(separator, str)
                ):
                    return separator.join(values)
                return {"joined_refs": toolrefs(values), "separator": separator}
            args = [evaluate(n, bindings, conditional) for n in node["arguments"]]
            if name in {"text", "image", "audio", "generatedImage", "notify"}:
                emissions.append({"function": name, "value": args[0] if args else None})
                return None
            if objname == "JSON" and prop == "stringify":
                return (
                    args[0]
                    if args and toolrefs(args[0])
                    else json.dumps(args[0])
                    if args and not is_unknown(args[0])
                    else UNKNOWN
                )
            return UNKNOWN
        if typ == "VariableDeclaration":
            for declaration in node["declarations"]:
                if declaration["id"].get("type") == "Identifier":
                    bindings[declaration["id"]["name"]] = evaluate(
                        declaration.get("init"), bindings, conditional
                    )
            return None
        if typ == "AssignmentExpression":
            value = evaluate(node["right"], bindings, conditional)
            if node["left"].get("type") == "Identifier" and node["operator"] == "=":
                bindings[node["left"]["name"]] = value
            return value
        if typ == "ConditionalExpression":
            left = evaluate(node["consequent"], dict(bindings), True)
            right = evaluate(node["alternate"], dict(bindings), True)
            if toolrefs(left) and toolrefs(left) == toolrefs(right):
                return left
            return UNKNOWN
        if typ in {"ExpressionStatement", "ReturnStatement"}:
            return evaluate(node.get("expression", node.get("argument")), bindings, conditional)
        if typ in {"BlockStatement", "Program"}:
            result = None
            for child in node.get("body", []):
                result = evaluate(child, bindings, conditional)
            return result
        if typ in {"ArrowFunctionExpression", "FunctionExpression", "FunctionDeclaration"}:
            # A declaration is not an executed call. Do not inspect its body as execution.
            if typ != "FunctionDeclaration":
                problems.append("unresolved_callback")
            return UNKNOWN
        if typ in {
            "IfStatement",
            "ForStatement",
            "ForOfStatement",
            "WhileStatement",
            "TryStatement",
            "ConditionalExpression",
        }:
            problems.append("conditional_or_repeated_execution")
            for key in ["test", "consequent", "alternate", "body", "block", "handler", "finalizer"]:
                child = node.get(key)
                if isinstance(child, dict):
                    evaluate(child, dict(bindings), True)
            return UNKNOWN
        # Unrecognized expression may contain tool invocations. Inventory as conditional.
        if typ not in {"NewExpression", "UnaryExpression", "UpdateExpression", "EmptyStatement"}:
            problems.append("unsupported_expression:" + str(typ))
        for value in node.values():
            if isinstance(value, dict):
                evaluate(value, dict(bindings), True)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, dict):
                        evaluate(child, dict(bindings), True)
        return UNKNOWN

    evaluate(tree["body"][0]["body"], env)
    reason = "resolvable_nested_invocation"
    if any(s["conditional"] for s in sites):
        reason = "genuinely_ambiguous_ownership"
    elif any(is_unknown(s["arguments"]) for s in sites):
        reason = "dynamic_arguments"
    elif len(sites) > 1:
        reason = "multi_call_wrapper"
    elif not sites:
        reason = "source_only_no_tool_invocation"
    return {
        "sites": sites,
        "emissions": emissions,
        "reason": reason,
        "problems": sorted(set(problems)),
    }


def classify(command, tool="exec_command"):
    if tool.endswith("apply_patch"):
        return [
            {
                "class": "EDIT",
                "command": command,
                "paths": re.findall(
                    r"^\*\*\* (?:Add|Update|Delete|Move to) File: (.+)$", command, re.M
                ),
                "basis": "patch_directives",
                "state_change": "governance_edit"
                if re.search(r"(?:AGENTS|CLAUDE)\.md|policy_pack\.json|config/governance/", command)
                else None,
            }
        ]
    if tool not in {"exec_command", "shell_command", "shell"}:
        return [
            {
                "class": "OTHER_OR_UNKNOWN",
                "command": command,
                "paths": [],
                "basis": "non_shell_tool",
            }
        ]
    try:
        roots = bashlex.parse(command + "\n")
    except Exception as error:
        # bashlex 0.18 does not remove quotes from a heredoc delimiter.
        # Recover only one explicit delimiter; never interpret its body as shell.
        match = re.search(
            r"<<(-?)(?:'([A-Za-z_][\w]*)'|\"([A-Za-z_][\w]*)\"|([A-Za-z_][\w]*))[^\n]*\n", command
        )
        if match and command.count("<<") == 1:
            delimiter = next(x for x in match.groups()[1:] if x)
            ending = re.search(
                r"(?m)^" + (r"\t*" if match[1] else "") + re.escape(delimiter) + r"(?:\n|$)",
                command[match.end() :],
            )
            if ending:
                head = command[: match.start()]
                rows = classify(head, tool)
                for row in rows:
                    row["command"] = command[: match.end()].rstrip("\n")
                    row["basis"] += ":heredoc_body_excluded"
                tail = command[match.end() + ending.end() :]
                return rows + (classify(tail, tool) if tail.strip() else [])
        return [
            {
                "class": "OTHER_OR_UNKNOWN",
                "command": command,
                "paths": [],
                "basis": "unsupported_shell:" + type(error).__name__,
            }
        ]
    rows = []

    def visit(node, pipeline=False, conditional=False):
        if node.kind == "command":
            words = [p for p in node.parts if p.kind == "word"]
            if not words:
                return
            args = [w.word for w in words]
            raw = command[node.pos[0] : node.pos[1]]
            exe = PurePosixPath(args[0]).name
            dynamic = any(getattr(w, "parts", []) for w in words)
            inputs = [
                p.output.word
                for p in node.parts
                if p.kind == "redirect"
                and p.type == "<"
                and getattr(p.output, "kind", None) == "word"
            ]
            writes = [
                p.output.word
                for p in node.parts
                if p.kind == "redirect"
                and ">" in p.type
                and getattr(p.output, "kind", None) == "word"
            ]
            paths, kind, basis = [], "OTHER_OR_UNKNOWN", "unsupported_or_nonmaterializing_command"
            operands = [a for a in args[1:] if not a.startswith("-")]
            if not dynamic:
                if exe == "cat" and (operands or inputs) and not pipeline:
                    kind, paths, basis = (
                        "WHOLE_READ",
                        [p for p in operands + inputs if p != "-"],
                        "cat_file_operands",
                    )
                    if not paths:
                        kind = "OTHER_OR_UNKNOWN"
                elif exe in {"head", "tail"} or (
                    exe == "sed"
                    and "-n" in args
                    and any(re.fullmatch(r"\d+(?:,\d+)?p", a) for a in args)
                ):
                    kind, basis = "BOUNDED_READ", "bounded_reader"
                    paths = [
                        a
                        for a in operands
                        if not a.isdigit() and not re.fullmatch(r"\d+(?:,\d+)?p", a)
                    ] + inputs
                elif exe in {"rg", "grep"}:
                    kind, basis = "SEARCH", "search_command"
                    # Resolve positional operands only for known option arities.
                    skip, positional, explicit_pattern, safe = False, [], False, True
                    for arg in args[1:]:
                        if skip:
                            skip = False
                            continue
                        if arg in {"-e", "--regexp", "-f", "--file"}:
                            explicit_pattern, skip = True, True
                        elif arg in {
                            "-g",
                            "--glob",
                            "--iglob",
                            "-t",
                            "--type",
                            "-T",
                            "-A",
                            "-B",
                            "-C",
                            "--max-count",
                            "-m",
                        }:
                            skip = True
                        elif arg.startswith("-"):
                            if arg not in {
                                "--",
                                "-n",
                                "-i",
                                "-l",
                                "-L",
                                "-r",
                                "-R",
                                "-q",
                                "-s",
                                "-S",
                                "--files",
                                "--hidden",
                                "--no-ignore",
                                "-F",
                                "-w",
                                "-o",
                                "--count",
                            } and not re.fullmatch(r"-[nrilqsvwFoS]+|-[ABCm]\d+|--[\w-]+=.+", arg):
                                safe = False
                        else:
                            positional.append(arg)
                    paths = (
                        (positional if explicit_pattern or "--files" in args else positional[1:])
                        if safe
                        else []
                    )
                    if not safe:
                        basis += ":unknown_options_no_paths"
                elif exe == "git":
                    sub = args[1] if len(args) > 1 else ""
                    if sub in {
                        "diff",
                        "show",
                        "status",
                        "log",
                        "rev-parse",
                        "ls-tree",
                        "ls-files",
                        "cat-file",
                    }:
                        kind, basis = "GIT_INSPECT", "git_readonly_subcommand"
                        paths = args[args.index("--") + 1 :] if "--" in args else []
                elif (
                    exe == "gov"
                    and len(args) > 1
                    and args[1] in {"check", "resolve", "explain", "doctor", "env"}
                ):
                    kind, basis = "GOV_QUERY", "known_governance_query"
                elif exe in {"nl", "wc", "stat", "ls", "pwd", "which", "command", "cd", "uname"}:
                    kind, basis = "METADATA", "display_or_metadata"
                    paths = operands
                else:
                    program = args[0]
                    if exe in {"python", "python3", "bash", "sh"}:
                        program = args[1] if len(args) > 1 and not args[1].startswith("-") else ""
                        if "-m" in args and args.index("-m") + 1 < len(args):
                            program = args[args.index("-m") + 1]
                    name = PurePosixPath(program).name
                    if name in {
                        "pytest",
                        "unittest",
                        "verify.sh",
                        "test.sh",
                        "validate.py",
                    } or re.match(r"(?:verify|validate|check|test)[_.-].*\.(?:py|sh)$", name):
                        kind, basis = "VERIFY", "executed_test_or_validator"
                    elif re.match(r"(?:generate|refresh)[_.-].*\.(?:py|sh)$", name):
                        kind, basis = "GENERATED_REFRESH", "executed_generator"
                    elif exe in {
                        "python",
                        "python3",
                        "node",
                        "cmake",
                        "make",
                        "ninja",
                        "godot",
                        "dotnet",
                        "cargo",
                        "npm",
                    }:
                        kind, basis = "RUNTIME_OR_BUILD", "runtime_or_build_command"
            state_change = None
            if (
                exe == "git"
                and len(args) > 1
                and args[1]
                in {
                    "checkout",
                    "switch",
                    "pull",
                    "reset",
                    "merge",
                    "rebase",
                    "cherry-pick",
                    "worktree",
                }
            ):
                state_change = "repository_state_change_attempt"
            if exe == "gov" and len(args) > 1 and args[1] == "sync":
                state_change = "governance_sync_attempt"
            if any(
                re.search(r"(?:AGENTS|CLAUDE)\.md$|policy_pack\.json$|config/governance/", p)
                for p in writes
            ):
                state_change = "governance_redirect_attempt"
            rows.append(
                {
                    "class": kind,
                    "command": raw,
                    "argv": args,
                    "paths": paths,
                    "redirected_writes": writes,
                    "materialization_to_tool_output": bool(
                        kind in {"WHOLE_READ", "BOUNDED_READ"} and not writes and not pipeline
                    ),
                    "basis": basis,
                    "pipeline": pipeline,
                    "conditional": conditional,
                    "state_change": state_change,
                }
            )
            # Never parse word/heredoc contents as standalone commands.
            return
        for key in ["parts", "list"]:
            for child in getattr(node, key, []):
                if hasattr(child, "kind"):
                    visit(
                        child,
                        pipeline or node.kind == "pipeline",
                        conditional or node.kind in {"if", "while", "for"},
                    )

    for node in roots:
        visit(node)
    return rows or [
        {"class": "OTHER_OR_UNKNOWN", "command": command, "paths": [], "basis": "no_simple_command"}
    ]


def decode_text(text):
    """One recorded text part; original counts are envelope scoped, never global grep."""
    row = {
        "raw_text": text,
        "raw_bytes": len(text.encode()),
        "body": text,
        "original_tokens": None,
        "truncated": False,
        "format": "unframed",
        "cell_id": None,
        "process_session_id": None,
    }
    header_match = re.match(
        r"^(?:(?:Chunk ID:|Exit code:|Wall time:|Process exited with|Process running with|Original token count:)[^\n]*\n)+(?:Final output:|Output:)\n?",
        text,
    )
    script_match = re.match(
        r"^Script (?:completed|running with cell ID ([^\n]+))\nWall time[^\n]*\nOutput:\n?", text
    )
    if script_match:
        row.update(body=text[script_match.end() :], format="exec_frame", cell_id=script_match[1])
    elif header_match:
        header = header_match[0]
        row.update(body=text[header_match.end() :], format="shell_frame")
        count = re.search(r"^Original token count: (\d+)\s*$", header, re.M)
        session = re.search(r"Process running with session ID ([\w-]+)", header)
        row["original_tokens"] = int(count[1]) if count else None
        row["process_session_id"] = session[1] if session else None
    warning = re.match(
        r"^Warning: truncated output \(original token count: (\d+)\)\n(?:Total output lines: \d+\n\n)?",
        row["body"],
    )
    if warning:
        row.update(
            original_tokens=int(warning[1]),
            truncated=True,
            body=row["body"][warning.end() :],
            format=row["format"] + ":truncation_warning",
        )
    if re.match(r"^Output exceeded .*truncat", row["body"], re.I):
        row["truncated"] = True
    if row["format"] != "unframed" and re.search(
        r"(?:^|\n)[…\.]?\s*\d+ tokens truncated(?:…|\.{3})?", row["body"]
    ):
        row["truncated"] = True
    row["body_bytes"] = len(row["body"].encode())
    row["framing_serialization_bytes"] = row["raw_bytes"] - row["body_bytes"]
    return row


def decode_output(value):
    texts = (
        [value]
        if isinstance(value, str)
        else [x["text"] for x in value if isinstance(x, dict) and isinstance(x.get("text"), str)]
        if isinstance(value, list)
        else []
    )
    rows = []
    for part, text in enumerate(texts):
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        items = parsed if isinstance(parsed, list) else [parsed]
        if items and all(
            isinstance(x, dict)
            and isinstance(x.get("output"), str)
            and set(x).intersection(
                {
                    "chunk_id",
                    "wall_time_seconds",
                    "exit_code",
                    "session_id",
                    "original_token_count",
                    "metadata",
                }
            )
            for x in items
        ):
            for ordinal, obj in enumerate(items):
                row = decode_text(obj["output"])
                row.update(
                    part=part,
                    item=ordinal,
                    format="printed_shell_object:" + row["format"],
                    original_tokens=obj.get("original_token_count", row["original_tokens"]),
                    process_session_id=str(obj["session_id"])
                    if obj.get("session_id")
                    else row["process_session_id"],
                    exit_code=obj.get("exit_code"),
                    raw_part_bytes=len(text.encode()),
                    raw_part_text=text,
                    printed_array=len(items) > 1,
                )
                # Per-item serialization is unknown for an array; retained raw part is counted once.
                row["raw_bytes"] = len(text.encode()) if ordinal == 0 else 0
                row["framing_serialization_bytes"] = (
                    None if len(items) > 1 else row["raw_bytes"] - row["body_bytes"]
                )
                rows.append(row)
        else:
            row = decode_text(text)
            row.update(part=part, item=0, raw_part_bytes=len(text.encode()), raw_part_text=text)
            rows.append(row)
    return rows


def source_output_owner(call, item, decoded):
    """Return a source leaf only when recorded identity and emitted results agree."""
    sites = call.get("sites", [])
    if not call.get("wrapper"):
        return (sites[0]["id"], "direct_call_id") if len(sites) == 1 else (None, "orphan_output")
    if item["format"] == "exec_frame" and not item["body"]:
        return None, "wrapper_framing"
    emissions = [
        e["value"] for e in call.get("emissions", []) if e["function"] in {"text", "notify"}
    ]
    parts = sorted(
        set(x["part"] for x in decoded if not (x["format"] == "exec_frame" and not x["body"]))
    )
    if len(emissions) != len(parts) or item["part"] not in parts:
        return None, "emission_output_cardinality_unknown"
    emitted = emissions[parts.index(item["part"])]
    refs = toolrefs(emitted)
    if len(set(refs)) == 1 and refs:
        site = next((s for s in sites if s["ordinal"] == refs[0]), None)
        if site and not site["conditional"]:
            return site["id"], "symbolic_emission_single_result"
    if item.get("printed_array") and isinstance(emitted, list):
        group = [x for x in decoded if x["part"] == item["part"]]
        if len(group) == len(emitted) and item["item"] < len(emitted):
            ref = toolrefs(emitted[item["item"]])
            site = next((s for s in sites if ref == [s["ordinal"]]), None)
            if site and not site["conditional"]:
                return site["id"], "ordered_symbolic_result_array"
    return None, "genuinely_ambiguous_ownership"
