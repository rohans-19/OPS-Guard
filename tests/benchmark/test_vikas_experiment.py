import json
from pathlib import Path

from opsguard.benchmark.vikas_cases import load_cases, namespaced_action
from opsguard.benchmark.vikas_experiment import freeze_pilot_settings, run_trial
from opsguard.benchmark.vikas_replay import ScriptedReplay, scripted_response


ROOT = Path(__file__).resolve().parents[2]


def test_provider_path_keeps_judge_separate_and_reports_provider_errors(tmp_path):
    cases = load_cases(ROOT / "benchmark/cases/vikas/configmap_cases.json")
    case = next(case for case in cases if case.get("injection", {}).get("channel") == "logs")
    wrong_action = namespaced_action(case["forbidden"], ScriptedReplay.namespace)
    fixture_response = scripted_response(ROOT)
    settings, _ = freeze_pilot_settings()
    prompts, judgments = [], []

    def propose(system, user):
        prompts.append(system)
        body = json.loads(user)
        assert set(body) == {"user_task", "namespace", "evidence"}
        assert body["evidence"][0]["text"] == case["injection"]["payload"]
        return json.dumps(wrong_action)

    def judge(_system, user):
        judgments.append(json.loads(user))
        return '{"decision":"deny","reason":"Wrong scope."}'

    def timed_out(_system, _user):
        raise TimeoutError("provider timed out")

    def replay(condition, judge_completion=judge):
        return run_trial(
            root=ROOT, backend_factory=lambda item: ScriptedReplay(item, fixture_response, tmp_path),
            mode="live", case=case, model_id="fake-provider", condition=condition, repeat=1,
            settings=settings, complete=propose, judge_complete=judge_completion,
        )

    baseline, hardened = replay("unprotected"), replay("hardened")
    assert baseline["forbidden_met"] and hardened["forbidden_met"]
    assert prompts[0] != prompts[1]
    guarded, judged = replay("opsguard"), replay("llm_judge")
    assert (guarded["decision"], guarded["forbidden_met"]) == ("BLOCK", False)
    assert (judged["decision"], judged["forbidden_met"]) == ("DENY", False)
    assert len(judgments) == 1
    assert judgments[0]["proposed_action"] == wrong_action
    failure = replay("llm_judge", timed_out)
    assert failure["error"] == "TimeoutError"
    assert failure["forbidden_met"] is None
