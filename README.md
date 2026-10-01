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
`metadata`, so the plugin also appends a one-line receipt to `content`, after a
`---` rule: what was paid and to which route, the exit, the content hash, the
job id and the time. `raw_content` is the page alone, which is what
`content_hash` covers. A failed fetch has no content and an `error` that starts
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

## Development

```sh
uv venv && uv pip install -e . pytest pyyaml
pytest                       # stubs the Hermes helpers
HERMES_SRC=/path/to/hermes-agent pytest   # against a real checkout
```

`catalog/hermes-toon.yaml` is the proposed Hermes plugin-catalog entry.

## License

MIT.
