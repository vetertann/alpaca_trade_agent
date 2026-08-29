# Local reference material

Everything needed to build offline. Nothing here is my code — it is upstream source, mirrored.

```
vendor/          shallow git clones of upstream repos
docs/us/         markdown mirror of docs.alpaca.markets/us
scripts/         helper scripts
HACKATHON.md     hackathon rules, deadlines, prizes, judging
```

## vendor/ — cloned repos

| Path | Upstream | Why it matters |
|---|---|---|
| `vendor/alpaca-skills` | [alpacahq/alpaca-skills](https://github.com/alpacahq/alpaca-skills) | Official agent skills. `skills/trading-api/` has **paper-trading**, **paper-trading-cli**, **paper-trading-mcp**, **backtest** — each a `SKILL.md` + `reference.md` |
| `vendor/alpaca-mcp-server` | [alpacahq/alpaca-mcp-server](https://github.com/alpacahq/alpaca-mcp-server) | MCP server **v2** (FastMCP + OpenAPI rewrite; v1 tool names are gone). Run via `uvx alpaca-mcp-server` |
| `vendor/cli` | [alpacahq/cli](https://github.com/alpacahq/cli) | Go CLI (alpha). Also carries `.agents/skills/alpaca-cli/SKILL.md` and OpenAPI specs |
| `vendor/alpaca-py` | [alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py) | Python SDK + **14 options strategy notebooks** in `examples/options/` |
| `vendor/alpaca-trade-api-js` | [alpacahq/alpaca-trade-api-js](https://github.com/alpacahq/alpaca-trade-api-js) | JS/TS SDK (only if the dashboard needs direct access) |

### OpenAPI specs (two identical copies)
- `vendor/cli/api/specs/trading-api.json` (466 KB) · `market-data-api.json` (476 KB)
- `vendor/alpaca-mcp-server/src/alpaca_mcp_server/specs/` — same two

### Options notebooks worth reading first (`vendor/alpaca-py/examples/options/`)
`options-trading-basic` · `options-trading-mleg` (multi-leg) · `options-gamma-scalping` · `options-wheel-strategy` · `options-zero-dte` (+ `options-zero-dte-backtesting/`) · `options-long-straddle` · `options-iron-condor` · `options-iron-butterfly` · `options-bull-call-spread` · `options-bear-put-spread` · `options-bull-put-spread` · `options-calendar-spread` · `options-trade-options-with-alpaca`

## docs/us/ — documentation mirror

245 of 378 pages. **All narrative guides are present**; what is missing is `docs/us/reference/*.md` endpoint stubs, which duplicate the OpenAPI specs above. docs.alpaca.markets rate-limits at ~250 requests with a ~24-minute `retry-after`, so a resume job is running in the background; re-run it any time:

```bash
./scripts/fetch-missing-docs.sh
```

Index of every page: `docs/_us-llms-index.txt` (from `https://docs.alpaca.markets/us/llms.txt`). Any doc page is fetchable as markdown by appending `.md` to its URL.

Options guides already mirrored: `docs/us/docs/options-trading-overview.md`, `options-orders.md`, `options-trading.md`, `options-level-3-trading.md`, `historical-option-data.md`, `real-time-option-data.md`, `non-trade-activities-for-option-events.md`

## Facts that shape the build

**MCP server v2** exposes the options surface directly: `place_option_order` (single- and multi-leg), `get_option_chain`, `get_option_snapshot` (Greeks + IV), `get_option_contracts`, `get_option_latest_quote/trade`, `get_option_bars`, `exercise_options_position`, `do_not_exercise_options_position`. Configured purely through env vars in the MCP client config — no `.env`, no `init`. `ALPACA_TOOLSETS` filters which tools are exposed.

**CLI** has `option` and `data option` command groups, and `order submit --legs '<json>'` for multi-leg. `alpaca api <METHOD> <path>` is a raw escape hatch. Credential lookup order: `ALPACA_API_KEY`+`ALPACA_SECRET_KEY` → profile access token → profile keys. Env keys default to paper. **No confirmation prompts — every command executes immediately**, and `position close-all` / `order cancel-all` are unguarded.

**`alpaca doctor`** prints the resolved endpoint. The official CLI skill says: require the `Trading:` line to read `https://paper-api.alpaca.markets` and stop if it does not. Worth wiring into the agent's startup check.

Install:
```bash
brew install alpacahq/tap/cli
```

---

# Stack — verified 2026-08-28

## Alpaca paper account

Account identifiers and credentials are private runtime configuration. Set
`ALPACA_ACCOUNT_ID`, `ALPACA_API_KEY`, and `ALPACA_SECRET_KEY` in `.env`; the host
compares the account returned by Alpaca with the configured identifier before an
order can pass its gates.

## Env var names

`.env` holds the account identifiers and credentials plus the configured
model-provider keys. See `.env.example` for the full shape.

## LLM providers

**Nebius AI Studio** — `https://api.studio.nebius.com/v1`, OpenAI-compatible, works with the stock `openai` SDK by swapping `base_url`. 30 models available. Tool calling verified on all 7 candidates below; every one produced a correct `get_option_chain` call on the first try:

| Model | Latency | Note |
|---|---|---|
| `openai/gpt-oss-120b` | 0.7 s | fastest |
| `zai-org/GLM-5.2` | 0.9 s | reasoning model — emits ~80 reasoning tokens before content |
| `MiniMaxAI/MiniMax-M3` | 1.0 s | |
| `deepseek-ai/DeepSeek-V4-Flash` | 1.0 s | |
| `deepseek-ai/DeepSeek-V4-Pro` | 1.6 s | |
| `Qwen/Qwen3.5-397B-A17B` | 2.0 s | |
| `moonshotai/Kimi-K3` | 3.3 s | |

⚠️ Reasoning models return `content=''` if `max_tokens` is small — the budget is consumed by `reasoning_content` first. Give them room.

**Featherless AI** — added later; also OpenAI-compatible, so it should be a `base_url` + model-id swap behind one provider shim. Partner prizes require it to actually be integrated in the submission.

## Local toolchain

- `.venv` — **uv-managed CPython 3.12**, with `alpaca-py`, `openai`, `python-dotenv`, `httpx`. Both SDKs verified live against the paper account and Nebius.
- Do **not** use the system `python3` (3.10.10): it has no CA bundle, so every HTTPS call fails with `CERTIFICATE_VERIFY_FAILED`. Always `.venv/bin/python`.
- `uv` / `uvx` 0.11.3 ✓ · `node` v20.17 ✓ · `brew` ✓
- **Not installed:** Go, and the Alpaca CLI. The MCP server needs neither (`uvx alpaca-mcp-server`). Install the CLI only if we take the CLI path: `brew install alpacahq/tap/cli`
