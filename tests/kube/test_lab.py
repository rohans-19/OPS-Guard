from opsguard.kube.lab import lab_manifest


def test_lab_identities_separate_setup_mutation_and_observation():
    manifest = lab_manifest("opsguard-test",{"checkout-api":{"service":"checkout","replicas":1}},"print('fixture')","fixture:approved")
    roles = {obj["metadata"]["name"]:obj["rules"] for obj in manifest["items"] if obj["kind"] == "Role"}
    assert set(roles) == {"setup","reader","executor","observer"}
    for role in ("reader","observer"):
        assert all(set(rule["verbs"]) <= {"get","list","watch"} for rule in roles[role])
    assert all(rule["resourceNames"] == ["checkout-api"] for rule in roles["executor"])
    assert all(set(rule["verbs"]) <= {"update","patch"} for rule in roles["executor"])
    assert all(rule["verbs"] == ["create"] for rule in roles["setup"])
    assert all(obj["metadata"]["namespace"] == "opsguard-test" for obj in manifest["items"])
