# ADR-0001: MCP client adapter strategy

- **Status:** Accepted
- **Date:** 2026-09-15
- **Deciders:** SentinelFlow maintainers

## Context

SentinelFlow agents must reach engineering systems through MCP rather than direct Python imports. The MCP Python SDK moved to v2.x (`MCPServer`, stateless protocol revision `2026-07-28`), and LangChain's MCP integration landscape changed at the same time:

- `langchain-mcp-adapters` is unmaintained and pinned to `mcp<2.0.0`, so it cannot coexist with the current official SDK.
- MCP support moved into `langchain[mcp]` as `MCPAdapter`, but that integration is beta and built on third-party `fastmcp` 4.x rather than the official `mcp` package.

We need one client boundary that:

1. Loads tools from multiple MCP servers (our `sentinelflow-context` server and GitHub's read-only server).
2. Validates tool inputs and treats outputs as untrusted data.
3. Remains testable with fakes in unit and graph tests.
4. Does not force two incompatible MCP stacks into production.

## Decision

Implement a **typed MCP client adapter** as the default, built on the official `mcp` 2.x SDK `Client` API. Expose a narrow port (list tools, call tool, read resource) that the graph and agents depend on.

Document **`langchain[mcp]` `MCPAdapter`** as an alternative implementation behind the same port, selectable via configuration for experimentation. Do not depend on it in the default path.

Our custom MCP server (`mcp_server/`) will also use the official SDK (`MCPServer`), keeping server and default client on one stack.

## Consequences

### Positive

- Single MCP dependency tree aligned with the current spec and SDK.
- The adapter boundary is explicit, reviewable, and defensible in interviews.
- Contract tests can swap in fakes without LangChain beta API churn.
- Server authoring and client consumption share one mental model and one set of transport semantics.

### Negative

- We maintain adapter code that `MCPAdapter` would partially provide out of the box.
- We must implement tool-name namespacing and error mapping ourselves (LangChain's adapter handles some of this automatically).

### Neutral

- LangChain `MCPAdapter` remains available for A/B comparison or rapid prototyping if the port interface is kept stable.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| `langchain-mcp-adapters` / `MultiServerMCPClient` | Unmaintained; incompatible with `mcp` 2.x |
| `langchain[mcp]` `MCPAdapter` as default | Beta API; pulls in second MCP stack (`fastmcp` 4.x) |
| Direct Python function calls to MCP tool handlers | Violates the MCP boundary thesis; fixtures would bypass the integration under test |

## References

- [SENTINELFLOW_PLAN.md](../../SENTINELFLOW_PLAN.md) — Section 6, MCP boundary
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
- [LangChain MCP migration guide](https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters)
