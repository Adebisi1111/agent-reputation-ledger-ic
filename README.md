# Agent Reputation Ledger — Stake-Weighted Slashing Primitive

A GenLayer Intelligent Contract for building transparent, tamper-resistant reputation for autonomous agents. Reputation is backed by value at risk (staked GEN). Agents stake to participate, and lost disputes trigger slashing. This is an economic-security primitive — not just a counter.

## Why it's distinct

| Common reputation contracts | This primitive |
|---|---|
| Job-counting (success rate) | **Stake-weighted** scoring |
| No economic consequence | **Slashing** — lost disputes burn stake |
| Simple counters | **Value at risk** determines tier |
|Issuer trusts oracle | **Decentralized consensus** verifies delivery |

## How consensus works

1. **Issuer** calls `createJob()` — stores job on-chain with unique ID, authorized agent, evidence URL, and claimed delivery. Each evidence URL is globally deduped (no relabeling).
2. **Authorized agent** calls `record_delivery(job_id)` — triggers LLM consensus:
   - **Leader** fetches the live evidence URL, builds a decision prompt, runs it through an LLM
   - **Validator** re-runs the leader task and compares the `decision` field
   - Only matching decisions are accepted (comparative consensus)
3. **Verdict** updates reputation:
   - `DELIVERED` → +1 completed
   - `UNDELIVERED` → +1 failed + **10% stake slashed**
   - `DISPUTED` → +1 failed (no slash)

## Contract API

| Method | Type | Description |
|---|---|---|
| `register()` | write (payable) | Stake GEN to become a tracked agent (min 1 GEN) |
| `createJob(job_id, agent, evidence_url, claimed, resolve_block)` | write | Issuer creates a job with unique ID + authorized agent |
| `record_delivery(job_id)` | write | Agent records delivery; triggers consensus + slashing |
| `get_reputation(agent)` | view | Returns stake-weighted reputation as JSON |
| `getJob(job_id)` | view | Returns job details |

## Reputation formula

```
base     = (completed × staked) ÷ total        # stake-weighted success
penalty  = slash_points                         # cumulative GEN slashed
score    = base - penalty  (clamped at 0)
effective_stake = staked - penalty
```

## Tiers

| Tier | Requirement |
|---|---|
| `TRUSTED` | ≥5 GEN staked, score ≥3 GEN |
| `ESTABLISHED` | ≥1 GEN staked, score ≥0.5 GEN |
| `NEW` | ≥1 GEN staked |
| `UNPROVEN` | Below minimum stake |

## Safeguards

- **Authorization** — only the authorized agent can record delivery for their job
- **Replay protection** — each job recorded once
- **Evidence dedup** — each evidence URL used once globally (prevents relabeling)
- **Fetch failure handling** — wraps `gl.nondet.web.render` in try/except, defaults to `UNDELIVERED`
- **Malformed decisions** — validates LLM output against `ALLOWED` set, defaults to `UNDELIVERED`
- **Score clamp** — never goes below 0

## Run it

```bash
# Lint
genvm-lint check contracts/agent_reputation_ledger.py

# Test (direct mode)
pytest tests/direct/test_agent_reputation_ledger.py -v

# Deploy
genlayer deploy --contract contracts/agent_reputation_ledger.py
```
