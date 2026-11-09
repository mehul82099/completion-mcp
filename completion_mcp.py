#!/usr/bin/env python3
"""completion-mcp - a zero-dependency MCP stdio server demonstrating completion/complete.

Implements the argument-completion utility from the Model Context Protocol
specification (2026-07-28): a client sends `completion/complete` with a
reference to a prompt (`ref/prompt`) or a resource template (`ref/resource`)
plus the argument currently being typed, and the server returns ranked
suggestions.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (one message per
line). Only the Python standard library is used.
"""

import json
import os
import sys
import time

PROTOCOL_VERSIONS = ["2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "completion-mcp", "version": "1.0.0"}

# The specification caps a single completion response at 100 values.
MAX_VALUES = 100

# Sliding-window rate limit for completion/complete, per minute.
# The specification says servers SHOULD rate limit completion requests.
# Set COMPLETION_MCP_RATE_LIMIT=0 to disable.
def _rate_limit_from_env():
    raw = os.environ.get("COMPLETION_MCP_RATE_LIMIT", "60")
    try:
        return int(raw)
    except ValueError:
        return 60

RATE_LIMIT_PER_MINUTE = _rate_limit_from_env()
_rate_window = []  # monotonic timestamps of recent completion/complete calls

# ---------------------------------------------------------------------------
# Demo data (static; the point of this server is the protocol machinery).
# ---------------------------------------------------------------------------

# (ISO 639-1 code, English name). Small static list for demonstration.
LANGUAGES = [
    ("ar", "Arabic"), ("bn", "Bengali"), ("de", "German"), ("en", "English"),
    ("es", "Spanish"), ("fr", "French"), ("gu", "Gujarati"), ("hi", "Hindi"),
    ("id", "Indonesian"), ("it", "Italian"), ("ja", "Japanese"),
    ("kn", "Kannada"), ("ko", "Korean"), ("ml", "Malayalam"),
    ("mr", "Marathi"), ("nl", "Dutch"), ("pa", "Punjabi"),
    ("pt", "Portuguese"), ("ru", "Russian"), ("sv", "Swedish"),
    ("ta", "Tamil"), ("te", "Telugu"), ("th", "Thai"), ("tr", "Turkish"),
    ("ur", "Urdu"), ("vi", "Vietnamese"), ("zh", "Chinese"),
]

REGISTERS = ["formal", "informal", "technical"]

FILE_TYPES = ["py", "md", "txt", "json", "yaml", "toml"]

SNIPPETS = {
    "python": ["hello-world", "http-server", "fibonacci", "list-comprehension"],
    "javascript": ["hello-world", "fetch-json", "debounce"],
    "go": ["hello-world", "http-server", "worker-pool"],
}

# Deliberately > 100 entries so the response cap / hasMore path is exercisable.
KEYWORD_POOL = ["keyword-%03d" % i for i in range(150)]

PROMPTS = [
    {
        "name": "translate",
        "description": "Translate text between two languages.",
        "arguments": [
            {"name": "source_lang", "description": "ISO 639-1 source language code", "required": True},
            {"name": "target_lang", "description": "ISO 639-1 target language code", "required": True},
            {"name": "register", "description": "Style register", "required": False},
        ],
    },
    {
        "name": "grep_docs",
        "description": "Search the demo documentation index.",
        "arguments": [
            {"name": "keyword", "description": "Search keyword", "required": True},
            {"name": "file_type", "description": "Restrict to a file extension", "required": False},
        ],
    },
]

RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "snippet://{language}/{name}",
        "name": "snippets",
        "description": "Static demo code snippets addressed by language and name.",
    },
]


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid_params(message):
    return RpcError(-32602, message)


# ---------------------------------------------------------------------------
# Completion machinery
# ---------------------------------------------------------------------------

def rank_candidates(candidates, prefix):
    """Rank (value, label) candidates against a typed prefix.

    Prefix matches on the value or label come first (in definition order),
    then substring matches once at least 3 characters have been typed
    (shorter prefixes produce too much noise). Values are deduplicated. An
    empty prefix returns every candidate in definition order.
    """
    needle = prefix.lower()
    first, rest = [], []
    for value, label in candidates:
        hay_v, hay_l = value.lower(), label.lower()
        if hay_v.startswith(needle) or hay_l.startswith(needle):
            first.append(value)
        elif len(needle) >= 3 and (needle in hay_v or needle in hay_l):
            rest.append(value)
    seen, out = set(), []
    for value in first + rest:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _language_candidates(exclude=None):
    return [(code, name) for code, name in LANGUAGES if code != exclude]


def _complete_prompt_arg(prompt_name, arg_name, value, context_args):
    if prompt_name == "translate":
        if arg_name == "source_lang":
            return rank_candidates(_language_candidates(), value)
        if arg_name == "target_lang":
            # Context-aware: never suggest the already-chosen source language.
            source = context_args.get("source_lang")
            return rank_candidates(_language_candidates(exclude=source), value)
        if arg_name == "register":
            return rank_candidates([(r, r) for r in REGISTERS], value)
        raise _invalid_params("unknown argument for prompt 'translate': %r" % arg_name)
    if prompt_name == "grep_docs":
        if arg_name == "keyword":
            return rank_candidates([(k, k) for k in KEYWORD_POOL], value)
        if arg_name == "file_type":
            return rank_candidates([(f, f) for f in FILE_TYPES], value)
        raise _invalid_params("unknown argument for prompt 'grep_docs': %r" % arg_name)
    raise _invalid_params("unknown prompt: %r" % prompt_name)


def _complete_template_arg(uri, arg_name, value, context_args):
    if uri != RESOURCE_TEMPLATES[0]["uriTemplate"]:
        raise _invalid_params("unknown resource template: %r" % uri)
    if arg_name == "language":
        return rank_candidates([(lang, lang) for lang in SNIPPETS], value)
    if arg_name == "name":
        language = context_args.get("language")
        if language in SNIPPETS:
            names = SNIPPETS[language]
        else:
            names = [n for group in SNIPPETS.values() for n in group]
        return rank_candidates([(n, n) for n in names], value)
    raise _invalid_params("unknown template argument: %r" % arg_name)


def handle_complete(params):
    if not isinstance(params, dict):
        raise _invalid_params("completion/complete requires params")
    ref = params.get("ref")
    if not isinstance(ref, dict):
        raise _invalid_params("params.ref must be an object")
    argument = params.get("argument")
    if not isinstance(argument, dict) or not isinstance(argument.get("name"), str):
        raise _invalid_params("params.argument.name must be a string")
    arg_name = argument.get("name")
    arg_value = argument.get("value", "")
    if not isinstance(arg_value, str):
        raise _invalid_params("params.argument.value must be a string")
    context = params.get("context") or {}
    context_args = context.get("arguments") or {}
    if not isinstance(context_args, dict):
        raise _invalid_params("params.context.arguments must be an object")

    ref_type = ref.get("type")
    if ref_type == "ref/prompt":
        values = _complete_prompt_arg(ref.get("name"), arg_name, arg_value, context_args)
    elif ref_type == "ref/resource":
        values = _complete_template_arg(ref.get("uri"), arg_name, arg_value, context_args)
    else:
        raise _invalid_params("unsupported ref type: %r" % ref_type)

    total = len(values)
    capped = values[:MAX_VALUES]
    return {
        "resultType": "complete",
        "completion": {
            "values": capped,
            "total": total,
            "hasMore": total > len(capped),
        },
    }


# ---------------------------------------------------------------------------
# Rate limiting (completion/complete only)
# ---------------------------------------------------------------------------

def _rate_limit_allows():
    if RATE_LIMIT_PER_MINUTE <= 0:
        return True
    now = time.monotonic()
    cutoff = now - 60.0
    while _rate_window and _rate_window[0] <= cutoff:
        _rate_window.pop(0)
    if len(_rate_window) >= RATE_LIMIT_PER_MINUTE:
        return False
    _rate_window.append(now)
    return True


# ---------------------------------------------------------------------------
# Other methods
# ---------------------------------------------------------------------------

def _negotiate_protocol(params):
    requested = None
    if isinstance(params, dict):
        requested = params.get("protocolVersion")
        meta = params.get("_meta")
        if requested is None and isinstance(meta, dict):
            requested = meta.get("io.modelcontextprotocol/protocolVersion")
    if requested in PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def handle_initialize(params):
    return {
        "protocolVersion": _negotiate_protocol(params),
        "capabilities": {
            "completions": {},
            "prompts": {},
            "resources": {},
        },
        "serverInfo": dict(SERVER_INFO),
    }


def handle_prompts_get(params):
    if not isinstance(params, dict):
        raise _invalid_params("prompts/get requires params")
    name = params.get("name")
    if name == "translate":
        args = params.get("arguments") or {}
        source = args.get("source_lang", "<source_lang>")
        target = args.get("target_lang", "<target_lang>")
        register = args.get("register", "formal")
        text = ("Translate the following text from %s to %s in a %s register:\n\n"
                "<paste text here>" % (source, target, register))
        return {
            "description": PROMPTS[0]["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    if name == "grep_docs":
        args = params.get("arguments") or {}
        keyword = args.get("keyword", "<keyword>")
        text = "Find documentation entries matching %r and summarize them." % keyword
        return {
            "description": PROMPTS[1]["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
        }
    raise _invalid_params("unknown prompt: %r" % name)


def handle_request(method, params):
    if method == "initialize":
        return handle_initialize(params)
    if method == "ping":
        return {}
    if method == "prompts/list":
        return {"prompts": PROMPTS}
    if method == "prompts/get":
        return handle_prompts_get(params)
    if method == "resources/templates/list":
        return {"resourceTemplates": RESOURCE_TEMPLATES}
    if method == "completion/complete":
        if not _rate_limit_allows():
            raise RpcError(-32603, "rate limit exceeded for completion/complete")
        return handle_complete(params)
    raise RpcError(-32601, "method not found: %r" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            _emit({"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                   "error": {"code": -32600, "message": "invalid request"}})
            continue
        method = message.get("method")
        if "id" not in message:
            # Notification: known ones are accepted silently, unknown ones ignored.
            continue
        request_id = message.get("id")
        try:
            result = handle_request(method, message.get("params"))
        except RpcError as exc:
            _emit({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:  # defensive: never crash the stdio loop
            _emit({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": -32603, "message": "internal error: %s" % exc}})
        else:
            _emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def _emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
