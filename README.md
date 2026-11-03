# hermes-toon

A [Hermes Agent](https://github.com/nousresearch/hermes-agent) web-extract
provider that pays a [TOON](https://github.com/toon-protocol) node per fetch.
Each `web_extract` sends one micropayment (about $0.001 in USDC) over a payment
channel; the node fetches the page through an
[Anyone network](https://anyone.io) exit and answers with the page as markdown
plus a reproducible content hash. The destination sees an Anyone exit, never
your IP, and never the node's.

> **Disclosure.** Once `TOON_PAYER_URL` is set and `web.extract_backend: toon`
> is selected, every `web_extract` the agent runs spends real money from the
> payer sidecar's channel, without a confirmation per call. The sidecar's daily
> cap (`PAYER_DAILY_CAP`, default $0.10) and origin allowlist
> (`PAYER_ALLOWED_ORIGINS`, default: any origin) are the spending controls.
> Fund the sidecar's wallet with a small dedicated amount.

## What you get per URL

| field | meaning |
| --- | --- |
| `content` | the page as markdown, Readability + Turndown under [wuzzy/crawl v1](https://github.com/memetic-block/wuzzy/blob/main/VERIFY.md) |
| `metadata.content_hash` | sha256 of the canonical markdown, the hash an independent crawl of the same URL reproduces |
| `metadata.raw_hash` | sha256 of the exact bytes the origin served |
| `metadata.exit` | `anyone`: fetched through an Anyone network exit |
| `metadata.price` | what this fetch cost, in USDC base units |
| `metadata.fetched_at`, `metadata.job_id` | when, and the node's job id for the receipt |

Hermes passes the model only `url`, `title`, `content` and `error`, never
`metadata`, so the plugin also appends a one-line receipt to the page text
(`content` and `raw_content`), after a `---` rule: what was paid and to which
route, the exit, the content hash, the job id and the time. `content_hash`
covers the page above the rule, not the receipt. A failed fetch has no content and an `error` that starts
with "No page content was retrieved."

Bodies are capped at 24 KiB of markdown (`metadata.truncated` says when); the
hash always covers the whole document.

## Install

Two pieces: a local **payer sidecar** (Node 22, holds the channel and the
spending rules) and this **plugin** (Python, talks to the sidecar).

### 1. The payer sidecar

```sh
git clone https://github.com/drew-dot-com/anonfetch
cd anonfetch/payer
npm install
SOLANA_KEYPAIR=/path/to/keypair.json PAYER_DAILY_CAP=100000 node server.mjs
```

The keypair is a Solana mainnet wallet in `solana-keygen` JSON format holding a
little USDC: the first fetch locks a deposit into a payment channel, and you
get back whatever you have not spent when the channel closes. The deposit is
`PAYER_CHANNEL_DEPOSIT` (default 0.50) raised to the node's minimum, which is
1.00 USDC on the default node, so fund the wallet with at least 1.00.
Opening needs no SOL, because the node co-signs the open and pays the fee and
rent; closing the channel later is the one step that
costs SOL. Use a dedicated wallet. The sidecar listens on `127.0.0.1:3502`;
check it with `curl localhost:3502/health`. Full options in
[anonfetch/payer/README.md](https://github.com/drew-dot-com/anonfetch/tree/main/payer).

### 2. The plugin

Three steps: install, enable, select.

**Install.** Hermes warns that this is a custom (unreviewed) source, since the
plugin is not in the Hermes catalog yet, and asks for `TOON_PAYER_URL`. Answer
`http://127.0.0.1:3502`, or skip and set it in `~/.hermes/.env` later.

```sh
hermes plugins install 'https://github.com/drew-dot-com/hermes-toon#src/hermes_toon'
```

```
TOON_PAYER_URL=http://127.0.0.1:3502
```

**Enable.** Hermes asks before it installs the plugin's one Python dependency
(`httpx`); answer `y`. An install run without a terminal skips this and
leaves the plugin disabled, so run it yourself either way.

```sh
hermes plugins enable web-toon
```

**Select.** In `~/.hermes/config.yaml`:

```yaml
web:
  extract_backend: toon
```

Keep your search backend as it is: `toon` is extract only, and Hermes routes
search elsewhere. Start a new session (and `hermes gateway restart` if you run
the gateway); `hermes tools` shows "TOON paid fetch" with the paid badge.

## When to turn it on

The free extract tiers Hermes ships with are the right default. Pick `toon`
when a page refuses datacenter IPs or serves cloaked content to bots, or when
you want a receipt: a content hash somebody else can check against their own
fetch.

## How the money moves

```
Hermes web_extract -> GET /extract?url=  (localhost sidecar)
  -> one ILP packet with a signed x402 voucher -> TOON connector
  -> anonfetch app -> Anyone circuit -> exit -> origin
  <- markdown + hashes in the FULFILL <- ...
```

Each fetch is one ILP packet carrying a signed x402 `batch-settlement`
voucher for the channel's running total; nothing goes on chain per request,
and the node settles vouchers in batches. The first fetch opens the channel
(one transaction, paid by the node). The sidecar keeps its channel state in
`~/.anonfetch-payer/` (`channels.json` and `channels.peers.json`). Keep both:
without the channel config the channel can be neither found nor closed, so
its deposit stays locked.

## Second plugin: toon-node

`src/toon_node` gives an agent three tools over its own
[toon](https://github.com/toon-protocol/toon_cli) agent node, in toolset `toon`:

| Tool | What it does | Cost |
| --- | --- | --- |
| `toon_post` | Publishes a kind 1 note to a TOON relay, paying its price | 0.001 USDC on the default relay |
| `toon_read` | Reads notes from that relay | free |
| `toon_status` | Network, wallet balances read from the chains, channels, spending limit | free |

`toon` runs on Linux only, so on a Mac the node lives in a Lima VM. The plugin
reaches it through `vm/toon-bridge`, one script in the VM that takes only
`post` or `status`, receives user text as JSON on stdin, caps posts at 50 a
day, and keeps the wallet passphrase inside the VM. Install the script at
`~/bin/toon-bridge` in the VM, write the node's public facts (network,
addresses, agent identity) to `~/.toon-bridge/wallet.json` there, then:

```sh
hermes plugins install 'https://github.com/drew-dot-com/hermes-toon#src/toon_node'
```

and add `toon` to a platform's toolsets. Settings: `TOON_LIMACTL`, `TOON_VM`,
`TOON_BRIDGE`, `TOON_RELAY_URL`, `TOON_BASE_RPC`, `TOON_SOLANA_RPC`.

## Third plugin: charter-tool

`src/charter_tool` gives an agent one tool, `charter_rent` in toolset `charter`,
over the operator's own `toon` node: rent one short lease on a
[Charter](https://github.com/drew-dot-com/charter) compute host and run one
job in it.

| Job | Pass | What comes back | Cost on the mainnet host |
| --- | --- | --- | --- |
| fetch | `urls`: 1 to 5 http(s) URLs | each page's text, cut to 2 KiB | 0.001 USDC to open plus 0.005 USDC per 30 second interval |
| python | `script`: Python 3, at most 8 KiB, no network, NumPy available | stdout (4 KiB), stderr (2 KiB), exit code | the same |

A lease pays per interval while the job runs, so a call costs the opening fee
plus one tick per interval used, never more than the cap allows: with the
default cap of 6 intervals that is 0.031 USDC at most. Every answer carries
`spent` (USDC base units, as `charter` reports it), `spentUsdc` and a `price`
line stating the schedule and the caps in force, so the agent can tell its user
what the call cost. Prices are the offer's (an offer lives 45 minutes and the host
republishes it); `charter offers` shows them.

> **Disclosure.** Each `charter_rent` call spends real USDC from the node
> wallet with no confirmation per call. The plugin runs the `charter` binary
> (or your bridge command) as a subprocess with a minimal environment and hands
> it the path in `TOON_PASSPHRASE_FILE`; it never reads the passphrase itself,
> and never reads a `.env` file. The caps below and the node's own spending
> limit are the controls. Fund the node with a small dedicated amount.

### Install

You need, on the machine that runs Hermes, a
[`toon`](https://github.com/toon-protocol/toon_cli) agent node 0.3 or 0.4
with a funded wallet (Linux only), and the `charter` CLI built from its repo
(Node 22 or later; there is no npm package yet):

```sh
git clone https://github.com/drew-dot-com/charter && cd charter
npm ci && npm run build && npm install -g --prefix ~/.local .
charter offers --relay wss://relay.mainnet.toonprotocol.dev
```

`charter offers` prints each live offer as `<host pubkey>:<offer id>` with its
prices; that string is your `CHARTER_OFFER_ID`. Then:

```sh
hermes plugins install 'https://github.com/drew-dot-com/hermes-toon#src/charter_tool'
hermes plugins enable charter-tool
```

and add `charter` to a platform's toolsets in `~/.hermes/config.yaml`. The tool
is offered only when `charter` can be found (or a bridge command is set), so a
Hermes without either simply does not list it.

### Settings

All optional except the two the `charter` CLI needs. Put them in
`~/.hermes/.env`; Hermes's profile scope wins over the process environment.

| Variable | Meaning | Default |
| --- | --- | --- |
| `TOON_PASSPHRASE_FILE` | file holding the node wallet's passphrase; handed to `charter`, never read here | required without a bridge |
| `CHARTER_OFFER_ID` | `<host pubkey>:<offer id>` from `charter offers` | required without a bridge |
| `CHARTER_RELAY_URL` | the relay that carries offers | `wss://relay.mainnet.toonprotocol.dev` |
| `CHARTER_MAX_INTERVALS` | intervals one call may pay for, 1 to 60 | `6` (0.031 USDC at most per call) |
| `CHARTER_DAILY_LEASES` | leases a UTC day; `0` refuses every call | `5` |
| `CHARTER_ALLOWED_IDS` | who may call it, as `platform:user_id` entries, e.g. `telegram:123,discord:456` | empty: everyone Hermes already lets talk to the agent |
| `CHARTER_BIN` | path to the `charter` binary | `charter` on PATH |
| `CHARTER_STATE_DIR` | where the day counter and script temp files live | `~/.charter-tool` |
| `CHARTER_BRIDGE_CMD` | a command that rents on another machine (below) | unset |

A malformed cap refuses every call by variable name. The day counter is taken
under a file lock and written before `charter` starts, so a crash or a failed
lease still counts; a counter that cannot be read counts as the cap reached.

### When the node is on another machine

`toon` runs on Linux only, so on a Mac the node and `charter` live in a Lima
VM. Set `CHARTER_BRIDGE_CMD` to a command line that reaches
`vm/charter-bridge` there, and the plugin runs it with the job as JSON on stdin
instead of running `charter` itself:

```
CHARTER_BRIDGE_CMD=~/.local/lima/bin/limactl shell toon -- bash -lc '~/bin/charter-bridge rent'
```

The bridge is one script in the VM that takes only `rent`, keeps the
passphrase file, offer id and relay in its own `~/.config/charter/bridge.env`
(so `TOON_PASSPHRASE_FILE` and `CHARTER_OFFER_ID` are not needed on the Hermes
side), caps itself at 6 intervals and 5 leases a day, and hands `charter` a
minimal environment. The plugin's own day counter applies as well.

**Upgrading from 0.2.0:** the tool is no longer closed to one person. If you
relied on `CHARTER_ALLOWED_IDS`, keep it set; it works as before. If you used
the Lima bridge, set `CHARTER_BRIDGE_CMD` as above, since the old
`TOON_LIMACTL`, `TOON_VM` and `CHARTER_BRIDGE` variables are gone.

## Development

```sh
uv venv && uv pip install -e . pytest pyyaml
pytest                       # stubs the Hermes helpers
HERMES_SRC=/path/to/hermes-agent pytest   # against a real checkout
```

`catalog/hermes-toon.yaml` is the web-toon entry as first proposed to the Hermes
plugin catalog (listed there as `web-toon` since 2026-10-02); `docs/catalog-entry-draft.yaml`
is the draft entry for charter-tool, and `docs/charter-tool-design.md` says why 0.3.0 is
shaped as it is.

## License

MIT.
