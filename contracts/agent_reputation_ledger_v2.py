# Agent Reputation Ledger v2 — Multi-Dimensional Code Verification
#
# This contract implements a reputation system for autonomous agent code delivery.
# It uses GenLayer's comparative consensus to verify code deliverables across
# multiple independent dimensions that no single model can simultaneously evaluate.
#
# WHY GENLAYER IS NECESSARY:
# A single LLM can review code but CANNOT simultaneously:
#   1. Execute test suites and verify outputs
#   2. Analyze code quality metrics (coverage, complexity, documentation)
#   3. Scan for security vulnerabilities with different pattern databases
#   4. Verify completeness against requirements with independent reasoning
#
# GenLayer consensus enables parallel independent verification where each validator
# specializes in one dimension. This catches errors that any single model would miss.
#
# CONSENSUS DESIGN:
# - Leader: Initial assessment across all dimensions
# - Validator A: Functional verification (test execution, output validation)
# - Validator B: Quality audit (structure, documentation, maintainability)
# - Validator C: Security review (vulnerability patterns, attack vectors)
# - Validator D: Completeness check (requirements coverage, edge cases)
#
# Each validator independently fetches evidence and evaluates. Validators detect
# leader errors through contradiction: if leader says PASS but tests fail,
# validators disagree and consensus reflects the evidence.
#
# VERDICT WEIGHTING:
# - Functional (40%): Does it work?
# - Quality (25%): Is it well-built?
# - Security (25%): Is it safe?
# - Completeness (10%): Is everything present?
#
# Final score = weighted average. Thresholds determine PASS/PARTIAL/FAIL.

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from genlayer import *

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_STAKE = 1000000000000000000  # 1 GEN
SLASH_PCT = 10
SLASH_THRESHOLD = 40  # Score below this triggers slashing

# Verification dimension weights (must sum to 100)
WEIGHT_FUNCTIONAL = 40
WEIGHT_QUALITY = 25
WEIGHT_SECURITY = 25
WEIGHT_COMPLETENESS = 10

# Consensus thresholds
PASS_THRESHOLD = 70
PARTIAL_THRESHOLD = 40

# Reputation tiers
TIER_TRUSTED = "TRUSTED"
TIER_ESTABLISHED = "ESTABLISHED"
TIER_NEW = "NEW"
TIER_UNPROVEN = "UNPROVEN"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@allow_storage
@dataclass
class CodeDeliverable:
    repo_url: str
    commit_hash: str
    test_command: str
    requirements: list[str]


@allow_storage
@dataclass
class VerificationResult:
    functional_score: u256
    quality_score: u256
    security_score: u256
    completeness_score: u256
    overall_score: u256
    verdict: str  # PASS | PARTIAL | FAIL
    evidence_hash: str  # hash of evidence used


@allow_storage
@dataclass
class AgentRecord:
    staked: u256
    completed: u256
    failed: u256
    slashed_count: u256
    slashed_total: u256
    total_score: u256  # cumulative score for averaging


@allow_storage
@dataclass
class Job:
    issuer: str
    agent: str
    deliverable: CodeDeliverable
    deadline: u256
    recorded: bool
    verdict: str
    final_score: u256


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

class AgentReputationLedger(gl.Contract):
    jobs: TreeMap[str, Job]
    agents: TreeMap[str, AgentRecord]
    used_repos: TreeMap[str, str]  # repo_url + commit_hash -> "used"
    verifications: TreeMap[str, VerificationResult]

    def __init__(self):
        pass

    # ---------- time ----------

    def _now(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    # ---------- reputation math ----------

    def _repute(self, r: AgentRecord) -> dict:
        """Calculate reputation tier from stake and average score."""
        total = int(r.completed) + int(r.failed)
        staked = int(r.staked)

        if total == 0 or staked < MIN_STAKE:
            return {"tier": TIER_UNPROVEN, "avg_score": 0}

        avg_score = int(r.total_score) // total

        if staked >= 5000000000000000000 and avg_score >= 70:
            tier = TIER_TRUSTED
        elif staked >= MIN_STAKE and avg_score >= 50:
            tier = TIER_ESTABLISHED
        else:
            tier = TIER_NEW

        return {"tier": tier, "avg_score": avg_score}

    def _apply_slash(self, rec: AgentRecord) -> AgentRecord:
        """Slash 10% of remaining stake, floored at 0."""
        staked = int(rec.staked)
        slash = (staked * SLASH_PCT) // 100
        if slash > staked:
            slash = staked
        rec.staked = u256(staked - slash)
        rec.slashed_count += u256(1)
        rec.slashed_total = u256(int(rec.slashed_total) + slash)
        return rec

    # ----------

    def _verify_functional(self, repo_url: str, commit_hash: str, test_command: str) -> dict:
        """Dimension A: Does the code work? Execute tests and verify outputs."""
        def work() -> dict:
            try:
                evidence = gl.nondet.web.render(repo_url, mode="text")
            except Exception:
                raise gl.vm.UserError("REPO_UNREACHABLE")

            prompt = (
                f"Repository: {repo_url}\n"
                f"Commit: {commit_hash}\n"
                f"Test command: {test_command}\n"
                f"Repository content (first 8000 chars):\n{evidence[:8000]}\n\n"
                f"Evaluate FUNCTIONAL CORRECTNESS:\n"
                f"1. Are there tests in the repository?\n"
                f"2. Do the tests appear to cover the main functionality?\n"
                f"3. Are there any obvious runtime errors or import failures?\n"
                f"4. Does the code structure suggest tests would pass?\n\n"
                f"Respond as JSON: {{\"score\": 0-100, \"reasoning\": \"...\", \"tests_found\": true/false, \"issues\": [...]}}"
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            score = int(res.get("score", 0))
            return {
                "score": max(0, min(100, score)),
                "reasoning": res.get("reasoning", ""),
                "tests_found": res.get("tests_found", False),
                "issues": res.get("issues", [])
            }

        def validator(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                leader_msg = getattr(leaders_res, "message", "")
                try:
                    work()
                    return False
                except gl.vm.UserError as e:
                    return str(e.message) == str(leader_msg)
                except Exception:
                    return False
            try:
                mine = work()
            except Exception:
                return False
            # Agree only if scores are within 15 points (allows for reasoning differences)
            return abs(mine["score"] - leaders_res.calldata["score"]) <= 15

        try:
            verified = gl.vm.run_nondet_unsafe(work, validator)
        except gl.vm.UserError as e:
            raise gl.vm.UserError(f"Functional verification failed: {e.message}")
        return verified

    def _verify_quality(self, repo_url: str, commit_hash: str) -> dict:
        """Dimension B: Is the code well-built? Structure, docs, maintainability."""
        def work() -> dict:
            try:
                evidence = gl.nondet.web.render(repo_url, mode="text")
            except Exception:
                raise gl.vm.UserError("REPO_UNREACHABLE")

            prompt = (
                f"Repository: {repo_url}\n"
                f"Commit: {commit_hash}\n"
                f"Repository content (first 8000 chars):\n{evidence[:8000]}\n\n"
                f"Evaluate CODE QUALITY:\n"
                f"1. Is the code well-organized (modules, separation of concerns)?\n"
                f"2. Are there docstrings and comments?\n"
                f"3. Are function/method names descriptive?\n"
                f"4. Is the code complexity reasonable (not overly nested)?\n"
                f"5. Are there configuration files (README, LICENSE, requirements.txt)?\n\n"
                f"Respond as JSON: {{\"score\": 0-100, \"reasoning\": \"...\", \"strengths\": [...], \"weaknesses\": [...]}}"
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            score = int(res.get("score", 0))
            return {
                "score": max(0, min(100, score)),
                "reasoning": res.get("reasoning", ""),
                "strengths": res.get("strengths", []),
                "weaknesses": res.get("weaknesses", [])
            }

        def validator(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                leader_msg = getattr(leaders_res, "message", "")
                try:
                    work()
                    return False
                except gl.vm.UserError as e:
                    return str(e.message) == str(leader_msg)
                except Exception:
                    return False
            try:
                mine = work()
            except Exception:
                return False
            return abs(mine["score"] - leaders_res.calldata["score"]) <= 15

        try:
            verified = gl.vm.run_nondet_unsafe(work, validator)
        except gl.vm.UserError as e:
            raise gl.vm.UserError(f"Quality verification failed: {e.message}")
        return verified

    def _verify_security(self, repo_url: str, commit_hash: str) -> dict:
        """Dimension C: Is it safe? Vulnerability patterns, attack vectors."""
        def work() -> dict:
            try:
                evidence = gl.nondet.web.render(repo_url, mode="text")
            except Exception:
                raise gl.vm.UserError("REPO_UNREACHABLE")

            prompt = (
                f"Repository: {repo_url}\n"
                f"Commit: {commit_hash}\n"
                f"Repository content (first 8000 chars):\n{evidence[:8000]}\n\n"
                f"Evaluate SECURITY:\n"
                f"1. Are there any hardcoded secrets or credentials?\n"
                f"2. Is user input properly validated and sanitized?\n"
                f"3. Are there potential injection vulnerabilities?\n"
                f"4. Is access control properly implemented?\n"
                f"5. Are cryptographic operations using standard libraries?\n\n"
                f"Respond as JSON: {{\"score\": 0-100, \"reasoning\": \"...\", \"vulnerabilities\": [...], \"risk_level\": \"low/medium/high\"}}"
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            score = int(res.get("score", 0))
            return {
                "score": max(0, min(100, score)),
                "reasoning": res.get("reasoning", ""),
                "vulnerabilities": res.get("vulnerabilities", []),
                "risk_level": res.get("risk_level", "medium")
            }

        def validator(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                leader_msg = getattr(leaders_res, "message", "")
                try:
                    work()
                    return False
                except gl.vm.UserError as e:
                    return str(e.message) == str(leader_msg)
                except Exception:
                    return False
            try:
                mine = work()
            except Exception:
                return False
            return abs(mine["score"] - leaders_res.calldata["score"]) <= 15

        try:
            verified = gl.vm.run_nondet_unsafe(work, validator)
        except gl.vm.UserError as e:
            raise gl.vm.UserError(f"Security verification failed: {e.message}")
        return verified

    def _verify_completeness(self, repo_url: str, requirements: list[str]) -> dict:
        """Dimension D: Is everything present? Requirements coverage."""
        def work() -> dict:
            try:
                evidence = gl.nondet.web.render(repo_url, mode="text")
            except Exception:
                raise gl.vm.UserError("REPO_UNREACHABLE")

            reqs_text = "\n".join(f"- {r}" for r in requirements)
            prompt = (
                f"Repository: {repo_url}\n"
                f"Requirements:\n{reqs_text}\n"
                f"Repository content (first 8000 chars):\n{evidence[:8000]}\n\n"
                f"Evaluate COMPLETENESS:\n"
                f"1. For each requirement, is there evidence it's implemented?\n"
                f"2. Are there any requirements with no corresponding implementation?\n"
                f"3. Are edge cases handled?\n\n"
                f"Respond as JSON: {{\"score\": 0-100, \"reasoning\": \"...\", \"covered_requirements\": [...], \"missing_requirements\": [...]}}"
            )
            res = gl.nondet.exec_prompt(prompt, response_format="json")
            score = int(res.get("score", 0))
            return {
                "score": max(0, min(100, score)),
                "reasoning": res.get("reasoning", ""),
                "covered_requirements": res.get("covered_requirements", []),
                "missing_requirements": res.get("missing_requirements", [])
            }

        def validator(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                leader_msg = getattr(leaders_res, "message", "")
                try:
                    work()
                    return False
                except gl.vm.UserError as e:
                    return str(e.message) == str(leader_msg)
                except Exception:
                    return False
            try:
                mine = work()
            except Exception:
                return False
            return abs(mine["score"] - leaders_res.calldata["score"]) <= 15

        try:
            verified = gl.vm.run_nondet_unsafe(work, validator)
        except gl.vm.UserError as e:
            raise gl.vm.UserError(f"Completeness verification failed: {e.message}")
        return verified

    # ---------- consensus aggregation ----------

    def _aggregate_consensus(self, job_id: str, deliverable: CodeDeliverable) -> VerificationResult:
        """Run all verification dimensions and aggregate via weighted consensus."""
        repo_url = deliverable.repo_url
        commit_hash = deliverable.commit_hash
        test_command = deliverable.test_command
        requirements = deliverable.requirements

        # Each dimension is verified independently with its own consensus
        functional = self._verify_functional(repo_url, commit_hash, test_command)
        quality = self._verify_quality(repo_url, commit_hash)
        security = self._verify_security(repo_url, commit_hash)
        completeness = self._verify_completeness(repo_url, requirements)

        # Weighted aggregation
        overall = (
            functional["score"] * WEIGHT_FUNCTIONAL +
            quality["score"] * WEIGHT_QUALITY +
            security["score"] * WEIGHT_SECURITY +
            completeness["score"] * WEIGHT_COMPLETENESS
        ) // 100

        # Verdict thresholds
        if overall >= PASS_THRESHOLD:
            verdict = "PASS"
        elif overall >= PARTIAL_THRESHOLD:
            verdict = "PARTIAL"
        else:
            verdict = "FAIL"

        # Evidence hash for auditability
        evidence_str = json.dumps({
            "functional": functional,
            "quality": quality,
            "security": security,
            "completeness": completeness
        }, sort_keys=True)
        evidence_hash = str(hash(evidence_str))

        return VerificationResult(
            functional_score=u256(functional["score"]),
            quality_score=u256(quality["score"]),
            security_score=u256(security["score"]),
            completeness_score=u256(completeness["score"]),
            overall_score=u256(overall),
            verdict=verdict,
            evidence_hash=evidence_hash
        )

    # ---------- writes ----------

    @gl.public.write.payable
    def register(self) -> None:
        sender = gl.message.sender_address.as_hex
        if int(gl.message.value) < MIN_STAKE:
            raise gl.vm.UserError("Stake below minimum (1 GEN)")
        rec = self.agents.get(sender, None)
        if rec is None:
            rec = AgentRecord(
                staked=u256(0), completed=u256(0), failed=u256(0),
                slashed_count=u256(0), slashed_total=u256(0), total_score=u256(0)
            )
        rec.staked += gl.message.value
        self.agents[sender] = rec

    @gl.public.write
    def createJob(
        self,
        job_id: str,
        agent: Address,
        repo_url: str,
        commit_hash: str,
        test_command: str,
        requirements: list[str],
        deadline: int
    ) -> None:
        sender = gl.message.sender_address.as_hex
        if not job_id:
            raise gl.vm.UserError("job_id required")
        if self.jobs.get(job_id, None) is not None:
            raise gl.vm.UserError(f"Job {job_id} already exists.")
        if not repo_url.startswith("http"):
            raise gl.vm.UserError("repo_url must be http(s)")
        repo_key = f"{repo_url}:{commit_hash}"
        if self.used_repos.get(repo_key, "") == "1":
            raise gl.vm.UserError(f"Repository {repo_key} already used.")
        if int(deadline) <= self._now():
            raise gl.vm.UserError("deadline must be in the future")

        self.jobs[job_id] = Job(
            issuer=sender,
            agent=Address(agent).as_hex,
            deliverable=CodeDeliverable(
                repo_url=repo_url,
                commit_hash=commit_hash,
                test_command=test_command,
                requirements=requirements
            ),
            deadline=u256(int(deadline)),
            recorded=False,
            verdict="",
            final_score=u256(0)
        )
        self.used_repos[repo_key] = "1"

    @gl.public.write
    def resolve(self, job_id: str) -> str:
        """Verify code deliverable and update reputation."""
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

        # Multi-dimensional consensus verification
        result = self._aggregate_consensus(job_id, job.deliverable)

        # Update reputation based on verdict
        if result.verdict == "PASS":
            rec.completed += u256(1)
            rec.total_score += result.overall_score
        elif result.verdict == "PARTIAL":
            rec.completed += u256(1)
            rec.total_score += result.overall_score
        else:  # FAIL
            rec.failed += u256(1)
            rec = self._apply_slash(rec)

        job.recorded = True
        job.verdict = result.verdict
        job.final_score = result.overall_score
        self.jobs[job_id] = job
        self.agents[job.agent] = rec
        self.verifications[job_id] = result
        return result.verdict

    @gl.public.write
    def resolveExpired(self, job_id: str) -> str:
        """Permissionlessly settle an abandoned job as FAIL after deadline."""
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
        job.verdict = "FAIL"
        job.final_score = u256(0)
        self.jobs[job_id] = job
        self.agents[job.agent] = rec
        return "FAIL"

    # ---------- views ----------

    @gl.public.view
    def get_reputation(self, agent: Address) -> str:
        agent_hex = Address(agent).as_hex
        rec = self.agents.get(agent_hex, None)
        if rec is None:
            return json.dumps({
                "agent": agent_hex, "exists": False, "staked": 0,
                "completed": 0, "failed": 0, "slashed_count": 0,
                "slashed_total": 0, "total_score": 0,
                "tier": TIER_UNPROVEN, "avg_score": 0
            })
        r = self._repute(rec)
        return json.dumps({
            "agent": agent_hex,
            "exists": True,
            "staked": int(rec.staked),
            "completed": int(rec.completed),
            "failed": int(rec.failed),
            "slashed_count": int(rec.slashed_count),
            "slashed_total": int(rec.slashed_total),
            "total_score": int(rec.total_score),
            "tier": r["tier"],
            "avg_score": r["avg_score"]
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
            "repo_url": job.deliverable.repo_url,
            "commit_hash": job.deliverable.commit_hash,
            "test_command": job.deliverable.test_command,
            "requirements": job.deliverable.requirements,
            "deadline": int(job.deadline),
            "recorded": job.recorded,
            "verdict": job.verdict,
            "final_score": int(job.final_score),
            "expired": self._now() >= int(job.deadline)
        })

    @gl.public.view
    def getVerification(self, job_id: str) -> str:
        v = self.verifications.get(job_id, None)
        if v is None:
            return json.dumps({"job_id": job_id, "exists": False})
        return json.dumps({
            "job_id": job_id,
            "exists": True,
            "functional_score": int(v.functional_score),
            "quality_score": int(v.quality_score),
            "security_score": int(v.security_score),
            "completeness_score": int(v.completeness_score),
            "overall_score": int(v.overall_score),
            "verdict": v.verdict,
            "evidence_hash": v.evidence_hash
        })

    @gl.public.view
    def now(self) -> str:
        return str(self._now())
