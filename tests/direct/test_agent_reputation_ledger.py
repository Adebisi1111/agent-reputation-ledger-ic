import json

import pytest

def _future(contract, secs=86400):
    return int(contract.now()) + secs


def _past(contract, secs=86400):
    return int(contract.now()) - secs
STAKE = 6000000000000000000  # 6 GEN


def _hex(addr):
    if isinstance(addr, (bytes, bytearray)):
        from genlayer.py.types import Address
        return Address(bytes(addr)).as_hex
    return str(addr)


def _deploy(direct_deploy):
    return direct_deploy("contracts/agent_reputation_ledger.py")


def _register(contract, direct_vm, who, amount=STAKE):
    direct_vm.sender = who
    direct_vm.value = amount
    contract.register()
    direct_vm.value = 0


def _mock(direct_vm, decision, body="Delivered."):
    direct_vm.clear_mocks()
    direct_vm.mock_web(r".*e\.test.*", {"status": 200, "body": body})
    direct_vm.mock_llm(r".*", json.dumps({"decision": decision}))


def _advance(direct_vm, to_unix):
    """Warp the deterministic tx clock to a given Unix second."""
    from datetime import datetime, timezone
    direct_vm.warp(datetime.fromtimestamp(to_unix, tz=timezone.utc)
                   .isoformat().replace("+00:00", "Z"))


def _rep(contract, who):
    return json.loads(contract.get_reputation(agent=who))


# --------------------------------------------------------------------------
# Setup / authorization
# --------------------------------------------------------------------------


def test_register_and_create_job(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_alice)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    job = json.loads(contract.getJob("job-1"))
    assert job["exists"] is True
    assert job["issuer"].lower() == _hex(direct_alice).lower()
    assert job["agent"].lower() == _hex(direct_bob).lower()
    assert job["deadline"] > int(contract.now())
    assert job["recorded"] is False
    assert job["expired"] is False


def test_deadline_must_be_future(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("deadline must be in the future"):
        contract.createJob(job_id="j", agent=direct_bob,
                           evidence_url="https://e.test/x",
                           claimed="done", deadline=_past(contract))


def test_evidence_url_reuse_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    with direct_vm.expect_revert("already used"):
        contract.createJob(job_id="job-2", agent=direct_bob,
                           evidence_url="https://e.test/1",
                           claimed="done", deadline=_future(contract))


# --------------------------------------------------------------------------
# STEWARD REQUEST 1: resolution callable by issuer, or permissionlessly
# after the deadline, and the deadline is enforced.
# --------------------------------------------------------------------------


def test_agent_can_resolve_before_deadline(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "DELIVERED")
    direct_vm.sender = direct_bob
    assert contract.resolve("job-1") == "DELIVERED"
    assert _rep(contract, direct_bob)["completed"] == 1


def test_issuer_can_resolve_so_agent_cannot_stall(direct_vm, direct_deploy, direct_alice, direct_bob):
    """The issuer may force resolution even while the agent stays silent."""
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "UNDELIVERED", body="Nothing was delivered.")
    direct_vm.sender = direct_alice          # issuer, not the agent
    assert contract.resolve("job-1") == "UNDELIVERED"
    out = _rep(contract, direct_bob)
    assert out["failed"] == 1
    assert out["slashed_count"] == 1


def test_third_party_blocked_before_deadline(direct_vm, direct_deploy,
                                             direct_alice, direct_bob, direct_charlie):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "DELIVERED")
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("only the agent or issuer may resolve"):
        contract.resolve("job-1")


def test_anyone_can_resolve_after_deadline(direct_vm, direct_deploy,
                                           direct_alice, direct_bob, direct_charlie):
    """Deadline enforced: once passed, ANY caller may resolve.

    The job is created with a short deadline, then the contract clock is moved
    past it, so this exercises the real expired path rather than a guard.
    """
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    deadline = int(contract.now()) + 60
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=deadline)

    _advance(direct_vm, deadline + 60)
    assert json.loads(contract.getJob("job-1"))["expired"] is True

    _mock(direct_vm, "UNDELIVERED", body="Nothing delivered.")
    direct_vm.sender = direct_charlie          # unrelated third party
    assert contract.resolve("job-1") == "UNDELIVERED"
    out = _rep(contract, direct_bob)
    assert out["failed"] == 1
    assert out["slashed_count"] == 1


def test_resolve_expired_settles_abandoned_job(direct_vm, direct_deploy,
                                               direct_alice, direct_bob, direct_charlie):
    """An agent cannot escape a bad verdict by never submitting."""
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob, amount=10000000000000000000)
    direct_vm.sender = direct_alice
    deadline = int(contract.now()) + 60
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=deadline)

    # Before expiry a stranger cannot force it.
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("Deadline has not passed yet"):
        contract.resolveExpired("job-1")

    _advance(direct_vm, deadline + 60)
    assert contract.resolveExpired("job-1") == "UNDELIVERED"
    out = _rep(contract, direct_bob)
    assert out["staked"] == 9000000000000000000
    assert out["effective_stake"] == out["staked"]
    assert json.loads(contract.getJob("job-1"))["verdict"] == "UNDELIVERED"


def test_job_resolved_once(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "DELIVERED")
    direct_vm.sender = direct_bob
    contract.resolve("job-1")
    with direct_vm.expect_revert("already recorded"):
        contract.resolve("job-1")


# --------------------------------------------------------------------------
# STEWARD REQUEST 2: cumulative slashing cannot underflow effective stake
# or subtract the same slash twice.
# --------------------------------------------------------------------------


def test_slash_deducted_exactly_once(direct_vm, direct_deploy, direct_alice, direct_bob):
    """effective_stake must equal staked — the slash is not subtracted twice."""
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob, amount=10000000000000000000)  # 10 GEN
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "UNDELIVERED", body="Not delivered.")
    direct_vm.sender = direct_bob
    contract.resolve("job-1")

    out = _rep(contract, direct_bob)
    assert out["staked"] == 9000000000000000000          # 10 - 10%
    assert out["slashed_total"] == 1000000000000000000   # recorded once
    assert out["effective_stake"] == out["staked"]       # NOT staked - slashed_total


def test_repeated_slashes_never_underflow(direct_vm, direct_deploy,
                                          direct_alice, direct_bob):
    """Ten consecutive losses: stake decays toward zero and never goes negative."""
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob, amount=10000000000000000000)

    expected = 10000000000000000000
    running_slashed = 0
    for i in range(1, 11):
        direct_vm.sender = direct_alice
        direct_vm.value = 0
        contract.createJob(job_id=f"job-{i}", agent=direct_bob,
                           evidence_url=f"https://e.test/{i}",
                           claimed="done", deadline=_future(contract))
        _mock(direct_vm, "UNDELIVERED", body="Not delivered.")
        direct_vm.sender = direct_bob
        contract.resolve(f"job-{i}")

        slash = (expected * 10) // 100
        expected -= slash
        running_slashed += slash

        out = _rep(contract, direct_bob)
        assert out["staked"] == expected, f"slash {i}: stake drifted"
        assert out["staked"] >= 0, f"slash {i}: stake went negative"
        assert out["effective_stake"] == out["staked"], f"slash {i}: double-subtracted"
        assert out["slashed_total"] == running_slashed
        assert out["score"] >= 0, f"slash {i}: score underflowed"

    out = _rep(contract, direct_bob)
    assert out["slashed_count"] == 10
    assert out["failed"] == 10
    assert out["slashed_total"] <= 10000000000000000000


def test_score_and_tier_stay_sane_after_slashing(direct_vm, direct_deploy,
                                                 direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob, amount=10000000000000000000)
    for i in range(1, 8):
        direct_vm.sender = direct_alice
        direct_vm.value = 0
        contract.createJob(job_id=f"job-{i}", agent=direct_bob,
                           evidence_url=f"https://e.test/{i}",
                           claimed="done", deadline=_future(contract))
        _mock(direct_vm, "UNDELIVERED", body="Not delivered.")
        direct_vm.sender = direct_bob
        contract.resolve(f"job-{i}")
        out = _rep(contract, direct_bob)
        # A repeatedly slashed agent must never look maximally trusted.
        assert out["tier"] in ("UNPROVEN", "NEW", "ESTABLISHED")
        assert out["score"] >= 0
        assert out["effective_stake"] >= 0


def test_disputed_does_not_slash(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "DISPUTED", body="Unclear.")
    direct_vm.sender = direct_bob
    assert contract.resolve("job-1") == "DISPUTED"
    out = _rep(contract, direct_bob)
    assert out["failed"] == 1
    assert out["slashed_count"] == 0
    assert out["staked"] == STAKE


def test_empty_evidence_is_disputed_not_delivered(direct_vm, direct_deploy,
                                                 direct_alice, direct_bob):
    """An unreadable source (e.g. a PDF renders as empty text) is not proof."""
    contract = _deploy(direct_deploy)
    _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    direct_vm.clear_mocks()
    direct_vm.mock_web(r".*e\.test.*", {"status": 200, "body": ""})
    direct_vm.mock_llm(r".*", json.dumps({"decision": "DELIVERED"}))
    direct_vm.sender = direct_bob
    assert contract.resolve("job-1") == "DISPUTED"
    assert _rep(contract, direct_bob)["completed"] == 0


def test_unregistered_agent_cannot_be_resolved(direct_vm, direct_deploy,
                                               direct_alice, direct_charlie):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_charlie,
                       evidence_url="https://e.test/1",
                       claimed="done", deadline=_future(contract))
    _mock(direct_vm, "DELIVERED")
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Agent not registered"):
        contract.resolve("job-1")


def test_register_below_minimum_reverts(direct_vm, direct_deploy, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    with direct_vm.expect_revert("Stake below minimum"):
        contract.register()
