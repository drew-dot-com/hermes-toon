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
little USDC (0.50 is opened into the channel on the first fetch, refundable when
the channel settles) and about 0.01 SOL for the one channel-open transaction.
Use a dedicated wallet. The sidecar listens on `127.0.0.1:3502`; check it with
`curl localhost:3502/health`. Full options in
[anonfetch/payer/README.md](https://github.com/drew-dot-com/anonfetch/tree/main/payer).

### 2. The plugin

```sh
hermes plugins install 'https://github.com/drew-dot-com/hermes-toon#src/hermes_toon'
```

Then in `~/.hermes/.env`:

```
TOON_PAYER_URL=http://127.0.0.1:3502
```

and in `~/.hermes/config.yaml`:

```yaml
web:
  extract_backend: toon
```

Keep your search backend as it is: `toon` is extract only, and Hermes routes
search elsewhere. Restart Hermes; `hermes tools` shows "TOON paid fetch" with
the paid badge.

## When to turn it on

The free extract tiers Hermes ships with are the right default. Pick `toon`
when a page refuses datacenter IPs or serves cloaked content to bots, or when
you want a receipt: a content hash somebody else can check against their own
fetch.

## How the money moves

```
Hermes web_extract -> GET /extract?url=  (localhost sidecar)
  -> one ILP packet with a signed cumulative claim -> TOON connector
  -> anonfetch app -> Anyone circuit -> exit -> origin
  <- markdown + hashes in the FULFILL <- ...
```

The sidecar signs an off-chain claim per fetch on one channel; nothing goes on
chain per request. The node redeems claims in batches. The first fetch opens the
channel (one transaction). Deleting the sidecar's `channel-store.json` and
running again opens another channel, so keep it.

## Development

```sh
uv venv && uv pip install -e . pytest pyyaml
pytest                       # stubs the Hermes helpers
HERMES_SRC=/path/to/hermes-agent pytest   # against a real checkout
```

`catalog/hermes-toon.yaml` is the proposed Hermes plugin-catalog entry.

## License

MIT.
