# charter-tool 0.3.0: a plugin any Hermes operator can install

Written 2026-11-03, before the change. charter-tool 0.2.0 was built for one personal
agent: its tool text said only Drew Pierson may use it, every caller was gated by
`CHARTER_ALLOWED_IDS`, and the only path to the `charter` CLI was a `charter-bridge`
script inside a Lima VM. This note says what 0.3.0 does instead.

## What the tool does

`charter_rent` rents one short lease on a Charter host and runs one job in it, then
returns what the job produced:

- `urls`: 1 to 5 http(s) URLs, fetched by the lease; the page texts come back.
- `script`: one Python 3 script of at most 8 KiB, run with no network; stdout, stderr
  and the exit code come back.

Every call is a real payment from the operator's `toon` node wallet. Prices on the
Charter mainnet host (hourly since 2026-11-04): 0.001 USDC to open the lease, then 0.007 USDC
per one hour interval. The tool's answer carries the actual spend (`spent`, in USDC base units, and
`spentUsdc`) and a `price` line stating the schedule and the cap in force, so the agent
can repeat the cost to its user.

## What the operator provides

| what | how | default |
| --- | --- | --- |
| the `charter` CLI (`npm install -g` from drew-dot-com/charter) and a `toon` node 0.3 or 0.4, both on PATH of the Hermes process | or `CHARTER_BIN` naming the binary | `charter` on PATH |
| the node wallet's passphrase file, handed to `charter` as `TOON_PASSPHRASE_FILE` | `TOON_PASSPHRASE_FILE` | none; required unless a bridge is used |
| the offer to rent, `<host pubkey>:<offer id>`, found with `charter offers` | `CHARTER_OFFER_ID` | none; required unless a bridge is used |
| the relay carrying offers | `CHARTER_RELAY_URL` | `wss://relay.mainnet.toonprotocol.dev` |
| how many one hour intervals one call may pay for | `CHARTER_MAX_INTERVALS` | 1 (0.008 USDC at most per call); ceiling 24 |
| how many leases a UTC day | `CHARTER_DAILY_LEASES` | 5; `0` refuses every call |
| who may call it | `CHARTER_ALLOWED_IDS`, `platform:user_id` entries | empty: everyone the operator already allows on the platform |
| where the day counter lives | `CHARTER_STATE_DIR` | `~/.charter-tool` |
| a bridge instead of a local `charter` (a Mac with the node in a Lima VM) | `CHARTER_BRIDGE_CMD`, a command line run with the job as JSON on stdin | unset: run `charter` here |

Settings are read at call time through Hermes's `agent.secret_scope.get_secret_str`
when it is importable (catalog rule 11: a profile's values win over the process
environment), else from the environment. The plugin reads no `.env` file itself and
never opens the passphrase file: `charter` does, from the path it is given.

## Spending controls, in order

1. The allowlist, when set, refuses an unknown or unlisted caller before anything runs.
2. Bad arguments are refused before anything runs or is counted.
3. Missing settings are refused by variable name, never by value.
4. The day counter is taken under a file lock and written whole before `charter` starts,
   so a crash cannot bypass the cap. A counter that cannot be read counts as the cap
   reached (the bridge's rule, kept).
5. `charter rent --max-intervals N` bounds one lease; the node's own spending limit,
   which this plugin never touches, bounds everything.

## The bridge stays, as an option

`vm/charter-bridge` is unchanged: the fixed command line, the JSON on stdin, its own
constant cap of 5 leases a day and its own `bridge.env`. When `CHARTER_BRIDGE_CMD` is set
the plugin runs that command instead of `charter`, and both caps apply.

## Out of scope

Choosing an offer for the operator (an `offers` tool), reading lease status, and the
Hermes catalog entry itself, which is drafted in `docs/catalog-entry-draft.yaml` and
opened as a separate PR once this one is merged.
