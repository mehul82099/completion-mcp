# completion-mcp

A zero-dependency, single-file Python MCP server that implements the
**argument completion** utility of the Model Context Protocol:
`completion/complete`.

Most minimal MCP server examples skip completion entirely, so clients have
nothing to autocomplete against. This repo is a small, fully tested reference
for the endpoint: what to declare, how to rank suggestions, how to use
`context.arguments`, how the 100-value cap and `hasMore`/`total` fields work,
and which errors to return.

## What it does

- Declares the `completions` capability (`"completions": {}`) during
  `initialize`.
- Answers `completion/complete` for both reference types from the spec:
  - `ref/prompt` - two demo prompts (`translate`, `grep_docs`) with
    completable arguments.
  - `ref/resource` - the demo resource template `snippet://{language}/{name}`.
- Ranks suggestions: prefix matches on the value or its label first, then
  substring matches once at least 3 characters have been typed.
- Honors `context.arguments`: when completing `target_lang`, the already
  chosen `source_lang` is excluded; when completing a snippet `name`, the
  already chosen `language` narrows the list.
- Caps responses at 100 values (per the spec) and reports `total` and
  `hasMore`. The `keyword` argument completes from a 150-entry pool so this
  path is real, not theoretical.
- Returns the 2026-07-28 result envelope: `"resultType": "complete"` wrapping
  the `completion` object.
- Rate limits `completion/complete` (the spec says servers SHOULD): a sliding
  window of 60 calls/minute, configurable with `COMPLETION_MCP_RATE_LIMIT`
  (set to `0` to disable). Over the limit you get a JSON-RPC `-32603` error;
  the spec recommends rate limiting but assigns it no dedicated error code.
- Spec-shaped errors: `-32602` for an unknown prompt, template, argument, or
  ref type; `-32601` for unknown methods; `-32700` for unparseable input.

## Use case

A reference implementation and test harness for anyone adding autocompletion
to their own MCP server. We could not find an existing standalone
zero-dependency Python server centered on this endpoint: a GitHub repository
search for `mcp server completion complete autocomplete python` returned 0
results on 2026-11-09. (Scoped to that query and date; SDKs such as the
official Python SDK do implement the endpoint.)

## Quickstart

No dependencies, Python 3.9+:

```sh
python completion_mcp.py
```

The server speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (one message
per line). Example session:

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
{"jsonrpc": "2.0", "id": 2, "method": "completion/complete", "params": {"ref": {"type": "ref/prompt", "name": "translate"}, "argument": {"name": "target_lang", "value": "h"}, "context": {"arguments": {"source_lang": "hi"}}}}
```

Response to id 2:

```json
{"jsonrpc": "2.0", "id": 2, "result": {"resultType": "complete", "completion": {"values": [], "total": 0, "hasMore": false}}}
```

`"hi"` was the only match for `"h"` and the context excluded it - Hindi was
already chosen as the source language.

## Tests

```sh
python -m unittest -v
```

30 stdio-level tests spawn the real server process and drive it over
stdin/stdout, covering capability declaration, both ref types,
`context.arguments` behavior, ranking, the 100-value cap with
`total`/`hasMore`, every documented error path, rate limiting, and batch id
alignment. CI runs the suite on ubuntu-24.04 and windows-latest against
Python 3.9 and 3.13.

## Protocol notes

- `initialize` accepts the protocol version either as
  `params.protocolVersion` (legacy clients) or in
  `params._meta["io.modelcontextprotocol/protocolVersion"]` (2026-07-28
  style); unknown versions fall back to the newest supported one.
- `prompts/list`, `prompts/get`, `resources/templates/list`, and `ping` are
  implemented so the completion references resolve against real entries.
- Notifications (no `id`) are accepted and never answered, per JSON-RPC.

## Honest limits

- Demo data is static (a language list, three snippet groups, a synthetic
  150-entry keyword pool). The protocol machinery is the point; wire your own
  data source into `rank_candidates`.
- Transport is newline-delimited JSON-RPC, the framing used by this repo's
  test harness. It has not been exercised against the TypeScript reference
  client's stdio framing or a real desktop client.
- The ranking is deliberately simple (prefix, then substring). The spec
  leaves ranking to the server and only requires relevance ordering.

## Sources

- [MCP specification 2026-07-28 - Completion](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/completion)
- [MCP specification 2026-07-28 - Pagination](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination) (cursor model referenced for `hasMore` semantics)

## License

MIT - Copyright (c) 2026 Mehul Jhabak. See [LICENSE](LICENSE).
