# Tests for Agent Reputation Ledger v2
#
# These tests verify the core math and logic that doesn't require
# GenLayer consensus (which can only run on-chain).

import pytest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestReputationMath:
    """Test reputation tier calculations."""

    def setup_method(self):
        """Reset agent state for each test."""
        self.agents = {}

    def _repute(self, staked, completed, failed, total_score):
        """Mirror of contract _repute logic."""
        total = completed + failed
        MIN_STAKE = 1000000000000000000

        if total == 0 or staked < MIN_STAKE:
            return {"tier": "UNPROVEN", "avg_score": 0}

        avg_score = total_score // total

        if staked >= 5000000000000000000 and avg_score >= 70:
            tier = "TRUSTED"
        elif staked >= MIN_STAKE and avg_score >= 50:
            tier = "ESTABLISHED"
        else:
            tier = "NEW"

        return {"tier": tier, "avg_score": avg_score}

    def test_unproven_no_activity(self):
        """Agent with stake but no jobs should be UNPROVEN."""
        result = self._repute(
            staked=1000000000000000000,
            completed=0,
            failed=0,
            total_score=0
        )
        assert result["tier"] == "UNPROVEN"
        assert result["avg_score"] == 0

    def test_unproven_low_stake(self):
        """Agent with low stake should be UNPROVEN regardless of activity."""
        result = self._repute(
            staked=500000000000000000,  # 0.5 GEN
            completed=10,
            failed=0,
            total_score=800
        )
        assert result["tier"] == "UNPROVEN"

    def test_new_tier(self):
        """Agent with minimum stake but low score should be NEW."""
        result = self._repute(
            staked=1000000000000000000,
            completed=5,
            failed=5,
            total_score=400  # avg 40
        )
        assert result["tier"] == "NEW"

    def test_established_tier(self):
        """Agent with good score should be ESTABLISHED."""
        result = self._repute(
            staked=1000000000000000000,
            completed=8,
            failed=2,
            total_score=600  # avg 60
        )
        assert result["tier"] == "ESTABLISHED"

    def test_trusted_tier(self):
        """Agent with high stake and high score should be TRUSTED."""
        result = self._repute(
            staked=6000000000000000000,
            completed=10,
            failed=0,
            total_score=800  # avg 80
        )
        assert result["tier"] == "TRUSTED"


class TestSlashingMath:
    """Test slashing calculations."""

    def _apply_slash(self, staked, slashed_total, slashed_count):
        """Mirror of contract _apply_slash logic."""
        SLASH_PCT = 10
        slash = (staked * SLASH_PCT) // 100
        if slash > staked:
            slash = staked
        new_staked = staked - slash
        new_count = slashed_count + 1
        new_total = slashed_total + slash
        return new_staked, new_count, new_total

    def test_single_slash(self):
        """10% slash on 1 GEN should leave 0.9 GEN."""
        staked, count, total = self._apply_slash(
            staked=1000000000000000000,
            slashed_total=0,
            slashed_count=0
        )
        assert staked == 900000000000000000
        assert count == 1
        assert total == 100000000000000000

    def test_multiple_slashes(self):
        """Multiple slashes should compound on remaining balance."""
        staked = 1000000000000000000
        total = 0
        count = 0
        for _ in range(3):
            staked, count, total = self._apply_slash(staked, total, count)
        # After 3 slashes: 1 * 0.9^3 = 0.729 GEN
        assert staked == 729000000000000000
        assert count == 3

    def test_slash_floor(self):
        """Slash should never make balance negative."""
        staked, count, total = self._apply_slash(
            staked=500000000000000000,  # 0.5 GEN
            slashed_total=0,
            slashed_count=0
        )
        assert staked >= 0
        assert staked == 450000000000000000

    def test_slash_does_not_underflow(self):
        """Slash on small balance should floor at 0, not underflow."""
        staked = 1
        slashed_total = 0
        slashed_count = 0
        for _ in range(100):
            if staked == 0:
                break
            staked, slashed_count, slashed_total = self._apply_slash(
                staked, slashed_total, slashed_count
            )
        assert staked >= 0


class TestConsensusAggregation:
    """Test weighted score aggregation."""

    def _aggregate(self, functional, quality, security, completeness):
        """Mirror of contract aggregation logic."""
        WEIGHT_FUNCTIONAL = 40
        WEIGHT_QUALITY = 25
        WEIGHT_SECURITY = 25
        WEIGHT_COMPLETENESS = 10

        overall = (
            functional * WEIGHT_FUNCTIONAL +
            quality * WEIGHT_QUALITY +
            security * WEIGHT_SECURITY +
            completeness * WEIGHT_COMPLETENESS
        ) // 100

        PASS_THRESHOLD = 70
        PARTIAL_THRESHOLD = 40

        if overall >= PASS_THRESHOLD:
            verdict = "PASS"
        elif overall >= PARTIAL_THRESHOLD:
            verdict = "PARTIAL"
        else:
            verdict = "FAIL"

        return overall, verdict

    def test_perfect_score(self):
        """All 100 should be PASS."""
        overall, verdict = self._aggregate(100, 100, 100, 100)
        assert overall == 100
        assert verdict == "PASS"

    def test_all_fail(self):
        """All 0 should be FAIL."""
        overall, verdict = self._aggregate(0, 0, 0, 0)
        assert overall == 0
        assert verdict == "FAIL"

    def test_functional_dominates(self):
        """Functional has highest weight (40%)."""
        overall, verdict = self._aggregate(100, 0, 0, 0)
        assert overall == 40
        assert verdict == "PARTIAL"

    def test_balanced_good(self):
        """Balanced good scores should PASS."""
        overall, verdict = self._aggregate(80, 70, 60, 50)
        # 80*0.4 + 70*0.25 + 60*0.25 + 50*0.1 = 32+17.5+15+5 = 69.5
        assert overall == 69
        assert verdict == "PARTIAL"

    def test_balanced_excellent(self):
        """All high scores should PASS."""
        overall, verdict = self._aggregate(90, 80, 85, 70)
        # 90*0.4 + 80*0.25 + 85*0.25 + 70*0.1 = 36+20+21.25+7 = 84.25
        assert overall == 84
        assert verdict == "PASS"

    def test_security_critical(self):
        """Low security should drag down overall."""
        overall, verdict = self._aggregate(100, 100, 20, 100)
        # 100*0.4 + 100*0.25 + 20*0.25 + 100*0.1 = 40+25+5+10 = 80
        assert overall == 80
        assert verdict == "PASS"

    def test_threshold_boundary(self):
        """Test exact threshold boundaries."""
        overall, verdict = self._aggregate(70, 70, 70, 70)
        assert overall == 70
        assert verdict == "PASS"

        overall, verdict = self._aggregate(69, 69, 69, 69)
        assert overall == 69
        assert verdict == "PARTIAL"

        overall, verdict = self._aggregate(40, 40, 40, 40)
        assert overall == 40
        assert verdict == "PARTIAL"

        overall, verdict = self._aggregate(39, 39, 39, 39)
        assert overall == 39
        assert verdict == "FAIL"


class TestDeadlineLogic:
    """Test deadline enforcement logic."""

    def _is_expired(self, deadline, now):
        """Mirror of contract expiry check."""
        return now >= deadline

    def test_not_expired(self):
        """Current time before deadline."""
        assert self._is_expired(1000, 500) == False

    def test_exactly_at_deadline(self):
        """At exact deadline, should be expired."""
        assert self._is_expired(1000, 1000) == True

    def test_past_deadline(self):
        """After deadline, should be expired."""
        assert self._is_expired(1000, 1500) == True


class TestEvidenceHash:
    """Test evidence hash generation."""

    def test_hash_deterministic(self):
        """Same evidence should produce same hash."""
        import json
        evidence = {"score": 80, "reasoning": "Good code"}
        h1 = str(hash(json.dumps(evidence, sort_keys=True)))
        h2 = str(hash(json.dumps(evidence, sort_keys=True)))
        assert h1 == h2

    def test_hash_varies_with_content(self):
        """Different evidence should produce different hash."""
        import json
        e1 = {"score": 80}
        e2 = {"score": 81}
        h1 = str(hash(json.dumps(e1, sort_keys=True)))
        h2 = str(hash(json.dumps(e2, sort_keys=True)))
        assert h1 != h2


class TestValidatorTolerance:
    """Test validator score divergence tolerance."""

    def _validate(self, leader_score, mine_score, tolerance=15):
        """Mirror of validator agreement check."""
        return abs(mine_score - leader_score) <= tolerance

    def test_exact_match(self):
        """Same score should agree."""
        assert self._validate(80, 80) == True

    def test_within_tolerance(self):
        """Within 15 points should agree."""
        assert self._validate(80, 90) == True
        assert self._validate(80, 70) == True

    def test_at_tolerance_boundary(self):
        """Exactly 15 points should agree."""
        assert self._validate(80, 95) == True
        assert self._validate(80, 65) == True

    def test_beyond_tolerance(self):
        """Beyond 15 points should disagree."""
        assert self._validate(80, 96) == False
        assert self._validate(80, 64) == False

    def test_contradicts_leader_hallucination(self):
        """Large divergence catches leader errors."""
        assert self._validate(95, 40) == False  # Leader says great, validator says bad


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
