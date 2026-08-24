# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from genlayer import *

# Agent Reputation Ledger — evidence-based delivery verification with
# stake-weighted slashing.
#
# Time handling follows docs.genlayer.com "Transaction Context": the GenVM clock
# is pinned to the transaction datetime and is identical across validators, so
# int(datetime.now(timezone.utc).timestamp()) is deterministic and safe for
# expiry arithmetic. Block numbers are deliberately NOT exposed by the context,
# which is why deadlines are Unix seconds rather than a block height.
#
# STEWARD REQUEST 1 — resolution cannot be dodged, and the deadline is enforced:
#   - before the deadline: only the agent or the issuer may resolve()
#   - after  the deadline: ANYONE may resolve() permissionlessly
#   - after  the deadline: ANYONE may settle an abandoned job via resolveExpired()
#   An agent therefore cannot avoid an unfavourable verdict by staying silent.
#
# STEWARD REQUEST 2 — cumulative slashing cannot underflow or double-count:
#   `staked` is the single source of truth and is debited once per slash, capped
#   at the remaining balance. `slashed_total` is a cumulative RECORD ONLY and is
#   never subtracted from stake again, so effective_stake == staked. Previously
#   _repute() subtracted slash_points a second time, which underflowed u256 after
#   ~7 losses and made a heavily slashed agent appear maximally trusted.


@allow_storage
@dataclass
class Job:
    issuer: str
    agent: str
    evidence_url: str
    claimed: str
    deadline: u256          # Unix seconds
    recorded: bool
    verdict: str


@allow_storage
@dataclass
class AgentRecord:
    staked: u256            # live stake; the ONLY value slashing debits
    completed: u256
    failed: u256
    slashed_count: u256
    slashed_total: u256     # cumulative record; never re-subtracted


MIN_STAKE = 1000000000000000000        # 1 GEN
SLASH_PCT = 10
ALLOWED = ("DELIVERED", "UNDELIVERED", "DISPUTED")


class AgentReputationLedger(gl.Contract):
    jobs: TreeMap[str, Job]
    agents: TreeMap[str, AgentRecord]
    used_evidence: TreeMap[str, str]

    # ---------- time (deterministic per Transaction Context docs) ----------

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    # ---------- reputation ----------

    def _repute(self, r: AgentRecord) -> dict:
        """Stake weighted by completion ratio.

        `staked` already has every slash debited, so slashed_total is NOT
        subtracted here — that double-count was the reported underflow.
        """
        total = int(r.completed) + int(r.failed)
        staked = int(r.staked)
        if total == 0:
            return {"score": 0, "tier": "UNPROVEN", "effective_stake": staked}
        score = (int(r.completed) * staked) // total
        if staked >= 5000000000000000000 and score >= 3000000000000000000:
            tier = "TRUSTED"
        elif staked >= MIN_STAKE and score >= 500000000000000000:
            tier = "ESTABLISHED"
        elif staked >= MIN_STAKE:
            tier = "NEW"
        else:
            tier = "UNPROVEN"
        return {"score": score, "tier": tier, "effective_stake": staked}

    def _apply_slash(self, rec: AgentRecord) -> AgentRecord:
        """Debit SLASH_PCT of the REMAINING stake, exactly once, floored at 0."""
        staked = int(rec.staked)
        slash = (staked * SLASH_PCT) // 100
        if slash > staked:                      # defensive; cannot go negative
            slash = staked
        rec.staked = u256(staked - slash)
        rec.slashed_count += u256(1)
        rec.slashed_total = u256(int(rec.slashed_total) + slash)
        return rec

    # ---------- consensus verification ----------

    def _verify_delivery(self, evidence_url: str, claimed: str) -> str:
        def work() -> dict:
            try:
                evidence = gl.nondet.web.render(evidence_url, mode="text")
            except Exception:
                # Transient fetch failure: not proof of delivery, not a loss.
                raise gl.vm.UserError("EVIDENCE_UNREACHABLE")
            if not evidence:
                # An empty render (e.g. a PDF, which yields no text) proves nothing.
                return {"decision": "DISPUTED"}
            prompt = (
                f"Claimed delivery: {claimed}.\n"
                f"Live evidence from {evidence_url}:\n\n{evidence[:6000]}\n\n"
                f"Was the obligation fulfilled? Respond as JSON: "
                f'{{"decision": "DELIVERED"|"UNDELIVERED"|"DISPUTED", "reason": "..."}}.'
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            decision = (res.get("decision") or "").strip().upper()
            if decision not in ALLOWED:
                # Malformed model output: disagree so consensus rotates leader.
                raise gl.vm.UserError("MALFORMED_DECISION")
            return {"decision": decision}

        def validator(leaders_res) -> bool:
            # Error classification per the Error Handling docs: agree only when
            # we reproduce the same deterministic outcome as the leader.
            if not isinstance(leaders_res, gl.vm.Return):
                leader_msg = getattr(leaders_res, "message", "")
                try:
                    work()
                    return False            # leader errored, we succeeded
                except gl.vm.UserError as e:
                    return str(e.message) == str(leader_msg)
                except Exception:
                    return False
            try:
                mine = work()
            except Exception:
                return False
            return mine["decision"] == leaders_res.calldata["decision"]

        try:
            verified = gl.vm.run_nondet_unsafe(work, validator)
        except gl.vm.UserError as e:
            # Unreachable evidence or malformed output must not silently pass.
            raise gl.vm.UserError(f"Verification failed: {e.message}")
        return verified["decision"]

    # ---------- writes ----------

    @gl.public.write.payable
    def register(self) -> None:
        sender = gl.message.sender_address.as_hex
        if int(gl.message.value) < MIN_STAKE:
            raise gl.vm.UserError("Stake below minimum (1 GEN)")
        rec = self.agents.get(sender, None)
        if rec is None:
            rec = AgentRecord(staked=u256(0), completed=u256(0), failed=u256(0),
                              slashed_count=u256(0), slashed_total=u256(0))
        rec.staked += gl.message.value
        self.agents[sender] = rec

    @gl.public.write
    def createJob(self, job_id: str, agent: Address, evidence_url: str,
                  claimed: str, deadline: int) -> None:
        """Issuer creates a job with a unique ID and a Unix-seconds deadline."""
        sender = gl.message.sender_address.as_hex
        if not job_id:
            raise gl.vm.UserError("job_id required")
        if self.jobs.get(job_id, None) is not None:
            raise gl.vm.UserError(f"Job {job_id} already exists.")
        if not evidence_url.startswith("http"):
            raise gl.vm.UserError("evidence_url must be http(s)")
        if self.used_evidence.get(evidence_url, "") == "1":
            raise gl.vm.UserError(f"Evidence URL {evidence_url} already used.")
        if int(deadline) <= self._now():
            raise gl.vm.UserError("deadline must be in the future")
        self.jobs[job_id] = Job(
            issuer=sender,
            agent=Address(agent).as_hex,
            evidence_url=evidence_url,
            claimed=claimed,
            deadline=u256(int(deadline)),
            recorded=False,
            verdict="",
        )
        self.used_evidence[evidence_url] = "1"

    @gl.public.write
    def resolve(self, job_id: str) -> str:
        """Verify the cited evidence and update reputation.

        Agent or issuer before the deadline; ANYONE once it has passed.
        """
        sender = gl.message.sender_address.as_hex
        job = self.jobs.get(job_id, None)
        if job is None:
            raise gl.vm.UserError(f"Job {job_id} not found.")
        if job.recorded:
            raise gl.vm.UserError(f"Job {job_id} already recorded.")

        expired = self._now() >= int(job.deadline)
        if not expired and sender != job.agent and sender != job.issuer:
            raise gl.vm.UserError(
                "Before the deadline only the agent or issuer may resolve; "
                "after it, anyone may."
            )

        rec = self.agents.get(job.agent, None)
        if rec is None:
            raise gl.vm.UserError("Agent not registered.")

        verdict = self._verify_delivery(job.evidence_url, job.claimed)
        if verdict == "DELIVERED":
            rec.completed += u256(1)
        elif verdict == "UNDELIVERED":
            rec.failed += u256(1)
            rec = self._apply_slash(rec)
        else:
            rec.failed += u256(1)           # DISPUTED: no slash

        job.recorded = True
        job.verdict = verdict
        self.jobs[job_id] = job
        self.agents[job.agent] = rec
        return verdict

    @gl.public.write
    def resolveExpired(self, job_id: str) -> str:
        """Permissionlessly settle an abandoned job as UNDELIVERED after its
        deadline, so silence is not an escape from an unfavourable verdict."""
        job = self.jobs.get(job_id, None)
        if job is None:
            raise gl.vm.UserError(f"Job {job_id} not found.")
        if job.recorded:
            raise gl.vm.UserError(f"Job {job_id} already recorded.")
        if self._now() < int(job.deadline):
            raise gl.vm.UserError("Deadline has not passed yet.")
        rec = self.agents.get(job.agent, None)
        if rec is None:
            raise gl.vm.UserError("Agent not registered.")
        rec.failed += u256(1)
        rec = self._apply_slash(rec)
        job.recorded = True
        job.verdict = "UNDELIVERED"
        self.jobs[job_id] = job
        self.agents[job.agent] = rec
        return "UNDELIVERED"

    # ---------- views ----------

    @gl.public.view
    def get_reputation(self, agent: Address) -> str:
        agent_hex = Address(agent).as_hex
        rec = self.agents.get(agent_hex, None)
        if rec is None:
            return json.dumps({"agent": agent_hex, "exists": False, "staked": 0,
                               "completed": 0, "failed": 0, "slashed_count": 0,
                               "slashed_total": 0, "score": 0,
                               "tier": "UNREGISTERED", "effective_stake": 0})
        r = self._repute(rec)
        return json.dumps({
            "agent": agent_hex,
            "exists": True,
            "staked": int(rec.staked),
            "completed": int(rec.completed),
            "failed": int(rec.failed),
            "slashed_count": int(rec.slashed_count),
            "slashed_total": int(rec.slashed_total),
            "score": r["score"],
            "tier": r["tier"],
            "effective_stake": r["effective_stake"],
        })

    @gl.public.view
    def getJob(self, job_id: str) -> str:
        job = self.jobs.get(job_id, None)
        if job is None:
            return json.dumps({"job_id": job_id, "exists": False})
        return json.dumps({
            "job_id": job_id,
            "exists": True,
            "issuer": job.issuer,
            "agent": job.agent,
            "evidence_url": job.evidence_url,
            "claimed": job.claimed,
            "deadline": int(job.deadline),
            "recorded": job.recorded,
            "verdict": job.verdict,
            "expired": self._now() >= int(job.deadline),
        })

    @gl.public.view
    def now(self) -> str:
        return str(self._now())
