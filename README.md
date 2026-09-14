# Agent Reputation Ledger — Stake-Weighted Slashing Primitive

A GenLayer Intelligent Contract for building transparent, tamper-resistant reputation for autonomous agents. Reputation is backed by value at risk (staked GEN). Agents stake to participate, and lost disputes trigger slashing. This is an economic-security primitive — not just a counter.

- **Contract:** `contracts/agent_reputation_ledger_v2.py`
- **Deployed (Bradbury):** `0x45B53778e9aC074B0175109f9b70004023926ed5`
- **Explorer:** https://explorer-bradbury.genlayer.com/address/0x45B53778e9aC074B0175109f9b70004023926ed5

## Why it's distinct

| Common reputation contracts | This primitive |
|---|---|
| Job-counting (success rate) | **Stake-weighted** scoring |
| No economic consequence | **Slashing** — lost disputes burn stake |
| Simple counters | **Value at risk** determines tier |
| Issuer trusts oracle | **Decentralized consensus** verifies delivery |

## How consensus works

1. **Issuer** calls `createJob()` — stores the job on-chain with a unique ID, the authorized agent, a repo URL, test command, requirements, and a **resolve deadline** (Unix seconds).
2. **`resolve(job_id)`** triggers consensus:
   - The leader evaluates all 4 dimensions (functional, quality, security, completeness) in a **single non-deterministic prompt**
   - Validators re-run the full evaluation and compare **every stored score** within ±15 tolerance
   - Only matching scores are accepted (comparative consensus)
3. **Verdict** updates reputation:
   - `PASS` → +1 completed
   - `PARTIAL` → +1 completed
   - `FAIL` → +1 failed **+ 10% of remaining stake slashed**

### Who may resolve, and when

| Caller | Before the deadline | After the deadline |
|---|---|---|
| Authorized agent | ✅ `resolve()` | ✅ `resolve()` |
| Issuer | ✅ `resolve()` | ✅ `resolve()` |
| **Anyone** | ❌ rejected | ✅ `resolve()` — permissionless |

Additionally, **`resolveExpired(job_id)`** lets any caller settle an abandoned job as `FAIL` once its deadline has passed.

### Verdict weighting

| Dimension | Weight | What it measures |
|---|---|---|
| Functional | 40% | Does it work? |
| Quality | 25% | Is it well-built? |
| Security | 25% | Is it safe? |
| Completeness | 10% | Is everything present? |

Final score = weighted average. Thresholds: ≥70 PASS, ≥40 PARTIAL, <40 FAIL.

### Reputation tiers

| Tier | Requirement |
|---|---|
| `TRUSTED` | ≥5 GEN staked, score ≥70 |
| `ESTABLISHED` | ≥1 GEN staked, score ≥50 |
| `NEW` | ≥1 GEN staked |
| `UNPROVEN` | Below minimum stake |

## Safeguards

- **Deadline enforced** — must be in the future at creation; `expired` is exposed on-chain
- **No silent escape** — permissionless resolution and `resolveExpired()` after the deadline
- **Authorization** — before the deadline, only the agent or issuer may resolve
- **Replay protection** — each job resolves once
- **Evidence dedup** — each repo URL is used once globally
- **Slash applied once** — capped at remaining stake
- **Single non-deterministic flow** — validators re-run the full evaluation and compare every stored score, not just the verdict
- **Validator error classification** — a validator agrees with a failing leader only when it reproduces the same error

## Run it

```bash
# Lint
genvm-lint check contracts/agent_reputation_ledger_v2.py

# Test (direct mode, no Studio needed)
pytest tests/direct/test_agent_reputation_ledger.py -v

# Deploy
genlayer deploy --contract contracts/agent_reputation_ledger_v2.py
```
