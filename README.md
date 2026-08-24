# Agent Reputation Ledger — Stake-Weighted Slashing Primitive

A GenLayer Intelligent Contract for building transparent, tamper-resistant reputation for autonomous agents. Reputation is backed by value at risk (staked GEN). Agents stake to participate, and lost disputes trigger slashing. This is an economic-security primitive — not just a counter.

- **Contract:** `contracts/agent_reputation_ledger.py`
- **Deployed (Bradbury):** `0x06657F3D9611F8795dB386c46B64661b8EB780d5`
- **Explorer:** https://explorer-bradbury.genlayer.com/address/0x06657F3D9611F8795dB386c46B64661b8EB780d5

## Why it's distinct

| Common reputation contracts | This primitive |
|---|---|
| Job-counting (success rate) | **Stake-weighted** scoring |
| No economic consequence | **Slashing** — lost disputes burn stake |
| Simple counters | **Value at risk** determines tier |
| Issuer trusts oracle | **Decentralized consensus** verifies delivery |

## How consensus works

1. **Issuer** calls `createJob()` — stores the job on-chain with a unique ID, the authorized agent, an evidence URL, the claimed delivery, and a **resolve deadline** (Unix seconds). Each evidence URL is globally deduped, so a failed job cannot be relabelled and retried.
2. **`resolve(job_id)`** triggers consensus over the cited evidence:
   - The leader fetches the live evidence URL and judges it with an LLM
   - Validators re-run the same work and compare the `decision` field
   - Only matching decisions are accepted (comparative consensus)
3. **Verdict** updates reputation:
   - `DELIVERED` → +1 completed
   - `UNDELIVERED` → +1 failed **+ 10% of remaining stake slashed**
   - `DISPUTED` → +1 failed, no slash

### Who may resolve, and when

An agent must not be able to escape an unfavourable verdict by staying silent, so resolution is not the agent's privilege alone:

| Caller | Before the deadline | After the deadline |
|---|---|---|
| Authorized agent | ✅ `resolve()` | ✅ `resolve()` |
| Issuer | ✅ `resolve()` | ✅ `resolve()` |
| **Anyone** | ❌ rejected | ✅ `resolve()` — permissionless |

Additionally, **`resolveExpired(job_id)`** lets any caller settle an abandoned job as `UNDELIVERED` once its deadline has passed. Silence is therefore a losing strategy: the job resolves against the agent either way.

The deadline is enforced on-chain. `createJob()` rejects a deadline that is already in the past, and `getJob()` exposes an `expired` flag.

**Why Unix seconds, not a block height:** the GenVM transaction context deliberately exposes no block number or block hash ([Transaction Context](https://docs.genlayer.com/developers/intelligent-contracts/features/transaction-context)). It does expose the transaction datetime, and the standard-library clock is pinned to it, so `int(datetime.now(timezone.utc).timestamp())` is identical across validators and safe for expiry arithmetic.

## Contract API

| Method | Type | Description |
|---|---|---|
| `register()` | write (payable) | Stake GEN to become a tracked agent (min 1 GEN) |
| `createJob(job_id, agent, evidence_url, claimed, deadline)` | write | Issuer creates a job; `deadline` is Unix seconds and must be in the future |
| `resolve(job_id)` | write | Verify cited evidence via consensus and update reputation. Agent/issuer before the deadline; anyone after |
| `resolveExpired(job_id)` | write | Permissionlessly settle an abandoned job as `UNDELIVERED` after its deadline |
| `get_reputation(agent)` | view | Stake-weighted reputation as JSON |
| `getJob(job_id)` | view | Job details, including `deadline`, `expired`, and `verdict` |
| `now()` | view | The contract's deterministic transaction clock (Unix seconds) |

## Reputation and slashing accounting

`staked` is the single source of truth. A slash debits it **exactly once**, capped at the remaining balance, so stake can never go negative:

```
slash        = (staked × 10) ÷ 100        # of the REMAINING stake
staked      -= slash                      # debited once, floored at 0
slashed_total += slash                    # cumulative RECORD ONLY
```

`slashed_total` is never subtracted from stake again, so:

```
score           = (completed × staked) ÷ (completed + failed)
effective_stake = staked
```

Ten consecutive losses on a 10 GEN stake decay it toward zero without ever underflowing:

| Slash | staked | slashed_total | effective_stake |
|---|---|---|---|
| 1 | 9.000 | 1.000 | 9.000 |
| 4 | 6.561 | 3.439 | 6.561 |
| 7 | 4.783 | 5.217 | 4.783 |
| 10 | 3.487 | 6.513 | 3.487 |

An earlier revision debited `staked` *and* accumulated `slash_points`, then subtracted `slash_points` from `staked` a second time when computing `effective_stake`. That double-count crossed zero at the 7th slash and wrapped `u256`, making a heavily slashed agent read as maximally trusted. Slash amounts are now recorded separately from the live balance and never applied twice.

## Tiers

| Tier | Requirement |
|---|---|
| `TRUSTED` | ≥5 GEN staked, score ≥3 GEN |
| `ESTABLISHED` | ≥1 GEN staked, score ≥0.5 GEN |
| `NEW` | ≥1 GEN staked |
| `UNPROVEN` | Below minimum stake |

## Safeguards

- **Deadline enforced** — must be in the future at creation; `expired` is exposed on-chain
- **No silent escape** — permissionless resolution and `resolveExpired()` after the deadline
- **Authorization** — before the deadline, only the agent or issuer may resolve
- **Replay protection** — each job resolves once
- **Evidence dedup** — each evidence URL is used once globally
- **Slash applied once** — capped at remaining stake; `effective_stake == staked`
- **Unreachable evidence** — raises `EVIDENCE_UNREACHABLE` rather than being treated as a delivery failure, so a transient network error cannot slash an agent
- **Empty render is not proof** — a source that renders to empty text (e.g. a PDF, which `web.render(mode="text")` returns as `""`) resolves `DISPUTED`, never `DELIVERED`
- **Malformed model output** — raises so consensus rotates to a new leader instead of silently defaulting
- **Validator error classification** — a validator agrees with a failing leader only when it reproduces the same error, per the [Error Handling](https://docs.genlayer.com/developers/intelligent-contracts/features/error-handling) guidance

## Tests

`tests/direct/test_agent_reputation_ledger.py` — 16 direct-mode tests, all passing, with web and LLM mocked.

**Deadline and permissions**
- agent resolves before the deadline
- issuer resolves before the deadline, so a silent agent cannot stall
- unrelated third party is rejected before the deadline
- third party resolves after the deadline (clock warped past it)
- `resolveExpired()` refuses before the deadline and settles the job after
- `createJob()` rejects a past deadline
- a job resolves only once

**Slashing accounting**
- one slash debits stake exactly once; `effective_stake == staked`
- ten consecutive slashes never underflow and never drift
- score and tier stay sane after repeated slashing
- `DISPUTED` does not slash

**Verification safety**
- empty evidence resolves `DISPUTED`, not `DELIVERED`
- unregistered agent cannot be resolved
- evidence URL reuse rejected
- stake below the minimum rejected

## Run it

```bash
# Lint
genvm-lint check contracts/agent_reputation_ledger.py

# Test (direct mode, no Studio needed)
pytest tests/direct/test_agent_reputation_ledger.py -v

# Deploy
genlayer deploy --contract contracts/agent_reputation_ledger.py
```

## Verified on Bradbury

Against `0x06657F3D9611F8795dB386c46B64661b8EB780d5`:

- `createJob()` with a past deadline → rejected (`FINISHED_WITH_ERROR`)
- `resolveExpired()` before the deadline → rejected
- after the deadline, `getJob()` reported `expired: true` and `resolveExpired()` settled the job `UNDELIVERED` with the agent never submitting
- four consecutive slashes: `staked` 10 → 9 → 8.1 → 7.29 → 6.561 GEN, with `effective_stake == staked` at every step (the earlier formula would have reported 3.122 GEN at that point, on its way to underflow)
