import json


def _hex(addr):
    if isinstance(addr, bytes):
        return "0x" + addr.hex()
    return str(addr)


def test_register_and_create_job(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Steward asked: create each job on-chain with unique ID + authorized counterparty."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", resolve_block=1000)
    job = json.loads(contract.getJob("job-1"))
    assert job["exists"] is True
    assert job["issuer"].lower() == _hex(direct_alice).lower()
    assert job["agent"].lower() == _hex(direct_bob).lower()
    assert job["recorded"] is False


def test_authorized_agent_records_delivery(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Steward asked: only authorized agent can record delivery."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", resolve_block=1000)
    direct_vm.sender = direct_bob
    direct_vm.mock_web(r".*e\.test.*", {"status": 200, "body": "Delivered."})
    direct_vm.mock_llm(r".*", json.dumps({"decision": "DELIVERED"}))
    contract.record_delivery("job-1")
    out = json.loads(contract.get_reputation(agent=direct_bob))
    assert out["completed"] == 1
    assert out["failed"] == 0


def test_unauthorized_write_reverts(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """Steward asked: test unauthorized writes."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", resolve_block=1000)
    direct_vm.sender = direct_charlie
    try:
        contract.record_delivery("job-1")
        raise AssertionError("expected revert: charlie cannot record for bob")
    except Exception as e:
        assert "Unauthorized" in str(e) or "unauthorized" in str(e).lower()


def test_duplicate_evidence_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Steward asked: test duplicate evidence. Same evidence URL can't be reused."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/same",
                       claimed="done", resolve_block=1000)
    try:
        contract.createJob(job_id="job-2", agent=direct_bob,
                           evidence_url="https://e.test/same",
                           claimed="different", resolve_block=2000)
        raise AssertionError("expected revert: same evidence reused")
    except Exception as e:
        assert "already used" in str(e).lower()


def test_fetch_failure_handled(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Steward asked: test fetch failures."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://bad-url.test/nonexistent",
                       claimed="done", resolve_block=1000)
    direct_vm.sender = direct_bob
    direct_vm.mock_web(r".*bad-url.*", Exception("fetch failed"))
    direct_vm.mock_llm(r".*", json.dumps({"decision": "DELIVERED"}))
    contract.record_delivery("job-1")
    out = json.loads(contract.get_reputation(agent=direct_bob))
    assert out["failed"] == 1
    assert out["completed"] == 0


def test_low_score_dispute_arithmetic(direct_vm, direct_deploy, direct_alice):
    """Steward asked: test low-score dispute arithmetic. Score clamps at 0."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 1000000000000000000
    contract.register()
    for i in range(5):
        direct_vm.sender = direct_alice
        contract.createJob(job_id=f"job-{i}", agent=direct_alice,
                           evidence_url=f"https://e.test/{i}",
                           claimed="done", resolve_block=i + 100)
        direct_vm.mock_web(r".*e\.test.*", {"status": 200, "body": "No delivery."})
        direct_vm.mock_llm(r".*", json.dumps({"decision": "UNDELIVERED"}))
        contract.record_delivery(f"job-{i}")
    out = json.loads(contract.get_reputation(agent=direct_alice))
    assert out["score"] == 0
    assert out["failed"] == 5
    assert out["tier"] == "UNPROVEN"


def test_malformed_model_decision_rejected(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Steward asked: reject malformed model decisions."""
    contract = direct_deploy("contracts/agent_reputation_ledger.py")
    direct_vm.sender = direct_alice
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_bob
    direct_vm.value = 6000000000000000000
    contract.register()
    direct_vm.sender = direct_alice
    contract.createJob(job_id="job-1", agent=direct_bob,
                       evidence_url="https://e.test/1",
                       claimed="done", resolve_block=1000)
    direct_vm.sender = direct_bob
    direct_vm.mock_web(r".*e\.test.*", {"status": 200, "body": "evidence"})
    direct_vm.mock_llm(r".*", json.dumps({"decision": "MAYBE"}))
    contract.record_delivery("job-1")
    out = json.loads(contract.get_reputation(agent=direct_bob))
    assert out["failed"] == 1
    assert out["completed"] == 0
