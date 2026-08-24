# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# Agent Reputation Ledger — full workflow per steward review.
#
# Workflow:
#   1. Issuer createJob() — on-chain job with unique ID + authorized agent
#   2. Agent record_delivery(job_id) — references job, submits evidence
#   3. Consensus verifies evidence, updates reputation
#
# Safeguards:
#   - Only issuer can create jobs
#   - Only the authorized agent can record delivery for a job
#   - Each job recorded once (replay protection)
#   - Each evidence URL used once globally (no relabeling)
#   - Fetch failures handled safely
#   - Malformed model decisions rejected
#   - Score clamps at 0


@allow_storage
@dataclass
class Job:
    issuer: str          # authorized counterparty who created the job
    agent: str
    evidence_url: str
    claimed: str
    resolve_block: u256
    recorded: bool


@allow_storage
@dataclass
class AgentRecord:
    staked: u256
    completed: u256
    failed: u256
    slashed_count: u256
    slash_points: u256


class AgentReputationLedger(gl.Contract):
    jobs: TreeMap[str, Job]
    agents: TreeMap[str, AgentRecord]
    # Global evidence dedup: evidence_url -> "used"
    used_evidence: TreeMap[str, str]
    job_counter: u256

    def __init__(self):
        pass

    def _repute(self, r: AgentRecord) -> dict:
        total = r.completed + r.failed
        if total == u256(0):
            return {"score": u256(0), "tier": "UNPROVEN", "effective_stake": r.staked}
        base = (r.completed * r.staked) // total
        penalty = r.slash_points
        score = base - penalty if base > penalty else u256(0)
        if r.staked >= u256(5000000000000000000) and score >= u256(3000000000000000000):
            tier = "TRUSTED"
        elif r.staked >= u256(1000000000000000000) and score >= u256(500000000000000000):
            tier = "ESTABLISHED"
        elif r.staked >= u256(1000000000000000000):
            tier = "NEW"
        else:
            tier = "UNPROVEN"
        return {"score": score, "tier": tier, "effective_stake": r.staked - penalty}

    def _verify_delivery(self, evidence_url: str, claimed: str) -> str:
        ALLOWED = ("DELIVERED", "UNDELIVERED", "DISPUTED")

        def leader() -> dict:
            try:
                evidence = gl.nondet.web.render(evidence_url, mode="text")
            except Exception:
                return {"decision": "UNDELIVERED"}
            prompt = (
                f"Claimed delivery: {claimed}.\n"
                f"Live evidence from {evidence_url}:\n\n{evidence}\n\n"
                f"Was the obligation fulfilled? Respond as JSON: "
                f'{{"decision": "DELIVERED"|"UNDELIVERED"|"DISPUTED", "reason": "..."}}.'
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            decision = (res.get("decision") or "").strip().upper()
            return {"decision": decision if decision in ALLOWED else "UNDELIVERED"}

        def validator(leader_result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            validator_decision = leader()["decision"]
            leader_decision = leader_result.calldata["decision"]
            return validator_decision == leader_decision

        verified = gl.vm.run_nondet_unsafe(leader, validator)
        return verified["decision"]

    @gl.public.write
    def register(self) -> None:
        minimum_stake = u256(1000000000000000000)
        sender = gl.message.sender_address.as_hex
        if gl.message.value < minimum_stake:
            raise Exception("Stake below minimum (1 GEN)")
        rec = self.agents.get(sender, None)
        if rec is None:
            rec = AgentRecord(staked=u256(0), completed=u256(0),
                              failed=u256(0), slashed_count=u256(0),
                              slash_points=u256(0))
        rec.staked += gl.message.value
        self.agents[sender] = rec

    @gl.public.write
    def createJob(self, job_id: str, agent: Address, evidence_url: str,
                  claimed: str, resolve_block: int) -> None:
        """Issuer creates a job on-chain with a unique ID and an authorized agent."""
        sender = gl.message.sender_address.as_hex
        if self.jobs.get(job_id, None) is not None:
            raise Exception(f"Job {job_id} already exists.")
        # Prevent evidence reuse under a new label
        if self.used_evidence.get(evidence_url, "") == "1":
            raise Exception(f"Evidence URL {evidence_url} already used.")
        agent_hex = Address(agent).as_hex
        self.jobs[job_id] = Job(
            issuer=sender,
            agent=agent_hex,
            evidence_url=evidence_url,
            claimed=claimed,
            resolve_block=resolve_block,
            recorded=False,
        )
        self.used_evidence[evidence_url] = "1"

    @gl.public.write
    def record_delivery(self, job_id: str) -> None:
        """Agent records delivery for an existing job they're authorized for."""
        sender = gl.message.sender_address.as_hex
        job = self.jobs.get(job_id, None)
        if job is None:
            raise Exception(f"Job {job_id} not found.")
        # Authorization: only the authorized agent can record
        if sender != job.agent:
            raise Exception("Unauthorized: not the authorized agent for this job.")
        # Replay protection: each job recorded once
        if job.recorded:
            raise Exception(f"Job {job_id} already recorded.")

        rec = self.agents.get(job.agent, None)
        if rec is None:
            raise Exception("Agent not registered.")

        verdict = self._verify_delivery(job.evidence_url, job.claimed)
        if verdict == "DELIVERED":
            rec.completed += u256(1)
        elif verdict == "UNDELIVERED":
            rec.failed += u256(1)
            slash_amount = (rec.staked * u256(10)) // u256(100)
            rec.slashed_count += u256(1)
            rec.slash_points += slash_amount
            rec.staked -= slash_amount
        elif verdict == "DISPUTED":
            rec.failed += u256(1)

        job.recorded = True
        self.jobs[job_id] = job
        self.agents[job.agent] = rec

    @gl.public.view
    def get_reputation(self, agent: Address) -> str:
        agent_hex = Address(agent).as_hex
        rec = self.agents.get(agent_hex, None)
        if rec is None:
            return json.dumps({"agent": agent_hex, "exists": False,
                               "staked": 0, "completed": 0, "failed": 0,
                               "slashed_count": 0, "slash_points": 0,
                               "score": 0, "tier": "UNREGISTERED"})
        r = self._repute(rec)
        return json.dumps({
            "agent": agent_hex,
            "exists": True,
            "staked": int(rec.staked),
            "completed": int(rec.completed),
            "failed": int(rec.failed),
            "slashed_count": int(rec.slashed_count),
            "slash_points": int(rec.slash_points),
            "score": int(r["score"]),
            "tier": r["tier"],
            "effective_stake": int(r["effective_stake"]),
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
            "resolve_block": job.resolve_block,
            "recorded": job.recorded,
        })
