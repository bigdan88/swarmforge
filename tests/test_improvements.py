"""Behavioral regressions for evaluation, evidence policy and extracted services."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from swarmforge.agent.harness import FunctionHarness
from swarmforge.agent.runtime import AgentResult
from swarmforge.config import ConfigError, ReviewConfig, parse_config
from swarmforge.db import Store
from swarmforge.effective_config import effective_config
from swarmforge.evaluation import run_suite, summarize
from swarmforge.github import Issue
from swarmforge.knowledge import Vault, write_distilled
from swarmforge.orchestrator import Orchestrator
from swarmforge.services.assessment import assess_gate
from swarmforge.services.delivery import deliver_local
from swarmforge.services.runs import request_cancel
from swarmforge.testing import TestEvidence, evaluate_criteria
from swarmforge.workspace import GitError

from .conftest import ROOT, base_raw


def test_export_does_not_serialize_credentials_or_extra_commands(cfg):
    cfg.providers['fake'].extra_headers = {'Authorization': 'secret-header'}
    cfg.roles['planner'].extra_body = {'api_key': 'secret-body'}
    cfg.project.agent_env = {'PASSWORD': 'secret-env'}
    cfg.project.test_command = 'echo secret-command'
    exported = json.dumps(effective_config(cfg))
    assert 'secret-' not in exported
    assert effective_config(cfg)['roles']['developer_backend']['runtime'] == 'builtin'
    assert 'require_push_approval' in effective_config(cfg)['github']


def test_profile_cannot_change_delivery_permissions(tmp_path):
    profile = tmp_path / 'team.yaml'
    profile.write_text('roles: {}\nauto_merge: {enabled: true}\n')
    raw = base_raw(tmp_path)
    raw['team_profile'] = 'team.yaml'
    with pytest.raises(ConfigError, match='only description and roles'):
        parse_config(raw, tmp_path / 'config.yaml')
    profile.write_text('roles:\n  planner:\n    model: faster-fake\n')
    cfg = parse_config(raw, tmp_path / 'config.yaml')
    assert cfg.roles['planner'].model == 'faster-fake'
    assert not cfg.auto_merge.enabled
    assert raw['roles']['planner']['model'] == 'qwen-fake'


def test_unexecuted_mapping_is_not_satisfied():
    ev = TestEvidence('cycle0', 'pytest', 0, False, 1, passed=1, junit_found=True,
                      cases={'tests.test_a.test_ok': 'passed'})
    results = evaluate_criteria({'AC1': ['tests/test_a.py::test_ok', 'tests/test_b.py::test_missing']},
                               [('AC1', 'both behaviors')], ev)
    assert results[0]['status'] == 'unverified'
    args = dict(criteria=results, tester_report=None, review=None, failed_tasks=[], task_rows=[],
                scope=set(), visual_findings=[])
    assert not assess_gate(ev, policy=ReviewConfig(), **args)
    assert assess_gate(ev, policy=ReviewConfig(require_verified_criteria=True), **args)[0]['kind'] == 'acceptance'
    ev.junit_found = False
    assert assess_gate(ev, policy=ReviewConfig(require_junit=True), **{**args, 'criteria': []})[0]['kind'] == 'tests'


@pytest.mark.parametrize('runtime', ['builtin', 'pi'])
async def test_harness_contract_cancels_before_launch(runtime):
    calls = []
    async def execute(spec):
        calls.append(spec)
        return AgentResult(status='completed', value={'runtime': runtime})
    harness = FunctionHarness(execute)
    spec = SimpleNamespace(should_cancel=lambda: True)
    assert (await harness.run(spec)).status == 'cancelled'
    assert not calls
    spec.should_cancel = lambda: False
    result = await harness.run(spec)
    assert result.ok and result.value['runtime'] == runtime


async def test_local_delivery_failure_retains_recovery_information(tmp_path):
    (tmp_path / '.git').mkdir()
    def fail(_):
        raise GitError('destination is locked')
    ws = SimpleNamespace(deliver_to_local=fail, integration_branch='swarmforge/result')
    result = await deliver_local(ws, str(tmp_path))
    assert result.warning == 'destination is locked'
    assert 'swarmforge/result' in result.reason and 'delivery into' in result.reason


def test_cancellation_service_does_not_modify_terminal_runs(tmp_path):
    store = Store(tmp_path / 'runs.db')
    store.create_run('done', state='COMPLETED')
    store.create_run('working', state='TASK_EXECUTION')
    assert not request_cancel(store, 'done')
    assert request_cancel(store, 'working')
    assert store.cancel_requested('working')
    with pytest.raises(ValueError): request_cancel(store, 'unknown')


async def test_reviewed_mode_keeps_independent_acceptance_criteria(cfg, tmp_path):
    orch = Orchestrator(cfg, Store(tmp_path / 'db'), mode='reviewed')
    orch.run_id = 'reviewed'
    orch.store.create_run(orch.run_id)
    orch.issue = Issue('Small task', 'Implement it')
    plan = await orch._make_plan('')
    assert len(plan.tasks) == 1 and plan.tasks[0].role == cfg.developer_roles[0]
    assert [c.id for c in plan.acceptance_criteria] == ['AC1']


async def test_reviewed_workflow_runs_tester_and_reuses_its_mapping(cfg, tmp_path, monkeypatch):
    from swarmforge import orchestrator as module
    from swarmforge.plan import Criterion
    orch = Orchestrator(cfg, Store(tmp_path / 'db'), mode='reviewed', echo=None)
    orch.run_id = 'reviewed'
    orch.store.create_run(orch.run_id)
    orch.ws = SimpleNamespace(repo=tmp_path)
    orch.artifacts = tmp_path / 'artifacts'
    orch.sandbox = object()
    orch.plan = SimpleNamespace(acceptance_criteria=[Criterion('AC1', 'the request')])
    called = []
    async def tester(cycle):
        called.append(cycle)
        return {'criteria_tests': {'AC1': ['tests/test_feature.py::test_it']}, 'defects': []}
    async def ensure(*_): return ''
    async def tests(*_):
        return TestEvidence('cycle', 'pytest', 0, False, 1, passed=1, junit_found=True,
                            cases={'tests.test_feature.test_it': 'passed'})
    monkeypatch.setattr(orch, '_run_tester', tester)
    monkeypatch.setattr(orch, '_ensure_deps', ensure)
    monkeypatch.setattr(module, 'run_tests', tests)
    await orch._testing(0)
    await orch._testing(1)
    assert called == [0]
    assert orch._criteria_results[0]['status'] == 'satisfied'


def test_exact_knowledge_duplicate_preserves_provenance(tmp_path):
    vault = Vault(tmp_path)
    original = vault.write('Useful lesson', 'Always check the result.', type='pitfall', repo='r', run='first')
    write_distilled(vault, [{'title': 'Same lesson', 'body': 'Always check the result.', 'type': 'pitfall',
                             'tags': [], 'links': []}], repo='r', run='second', source='agent:tester', existing=[original])
    notes = vault.notes()
    assert len(notes) == 1 and notes[0].run == 'first'
    assert 'second' in notes[0].body


def test_suite_has_distinct_cases_and_external_hidden_tests():
    from swarmforge.variants import load_suite
    _, cases = load_suite(ROOT / 'bench/suite.yaml')
    assert len(cases) >= 10 and len({c['name'] for c in cases}) == len(cases)
    for case in cases:
        assert not Path(case['acceptance']).is_relative_to(case['repo_template'])
        assert Path(case['issue']).is_file()


def test_summary_requires_successful_hidden_evidence():
    base = dict(mode='swarm', final_state='COMPLETED', elapsed_s=10, cost_usd=.1,
                repair_cycles=0, human_interventions=0, calls_without_cost=0)
    rows = [{**base, 'hidden_acceptance': {'total': 0, 'passed': 0}},
            {**base, 'hidden_acceptance': {'total': 2, 'passed': 2}}]
    summary = summarize(rows)['swarm']
    assert summary['successful'] == 1 and summary['cost_per_success_usd'] == .2


async def test_comparison_pins_source_and_stops_on_unknown_cost(cfg, tmp_path, monkeypatch):
    from swarmforge import evaluation
    template = tmp_path / 'template'
    template.mkdir()
    (template / 'app.py').write_text('x = 1\n')
    issue = tmp_path / 'issue.md'
    issue.write_text('Do the work')
    acceptance = tmp_path / 'acceptance'
    acceptance.mkdir()
    suite = tmp_path / 'suite.yaml'
    suite.write_text(yaml.safe_dump({'cases': [{'name':'one', 'repo_template': str(template),
                                               'issue':str(issue), 'acceptance':str(acceptance)}]}))
    store = Store(tmp_path / 'db')
    seen = []
    class FakeOrchestrator:
        def __init__(self, config, store, **kwargs):
            self.config, self.store = copy.deepcopy(config), store
            assert not config.github.create_pr and not config.auto_merge.enabled
            assert config.limits.one_run_per_repo and config.limits.same_repo == 'coordinate'
        async def execute(self, **kwargs):
            seen.append(kwargs['repo'])
            assert Path(kwargs['issue_file']).read_text() == 'Do the work'
            assert Path(kwargs['issue_file']) != issue
            issue.write_text('Changed during the evaluation')
            self.store.create_run('r', mode='single', state='COMPLETED')
            return 'r'
    monkeypatch.setattr(evaluation, 'metrics', lambda *_: dict(run_id='r', mode='single',
                        final_state='COMPLETED', elapsed_s=1, cost_usd=.1, repair_cycles=0,
                        human_interventions=0, calls_without_cost=1))
    async def hidden(*_): return {'total':1, 'passed':1}
    monkeypatch.setattr(evaluation, 'evaluate_hidden', hidden)
    result = await run_suite(cfg, store, suite, runs=3, modes=['single','swarm'], budget=1,
                             orchestrator_factory=FakeOrchestrator, echo=lambda _: None)
    assert result['status'] == 'unpriced_calls_hold' and len(seen) == 1
    assert len(result['pins']['one']) == 40
    assert 'issue.md' in result['input_digests']['one']


async def test_pinned_source_survives_original_branch_changes(cfg, sample_repo, tmp_path):
    from swarmforge.benchmark import freeze_source
    from swarmforge.workspace import git
    frozen = await freeze_source(cfg, str(sample_repo), tmp_path / 'frozen')
    before = frozen.base_sha
    (sample_repo / 'new-file.txt').write_text('Later work')
    git(sample_repo, 'add', 'new-file.txt')
    git(sample_repo, '-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-m', 'advance')
    assert git(sample_repo, 'rev-parse', 'HEAD').strip() != before
    assert git(frozen.repo, 'rev-parse', 'HEAD').strip() == before
    assert not (frozen.repo / 'new-file.txt').exists()


def test_variant_inspection_matches_launch_without_mutating_base(cfg, tmp_path):
    from swarmforge.effective_config import resolve_variant
    store = Store(tmp_path / 'db')
    store.variant_add('v1', {'roles': {'planner': {'max_steps': 22}}}, None, 'experiment')
    cfg.active_variant = 'v1'
    resolved, warning = resolve_variant(cfg, store)
    assert warning is None and resolved.roles['planner'].max_steps == 22
    assert cfg.roles['planner'].max_steps != 22
    assert Orchestrator(cfg, store).cfg.roles['planner'].max_steps == 22


def test_provider_outage_retry_does_not_expand_coding_effort(cfg):
    from swarmforge.postmortem import escalate
    retry = escalate(cfg, 2, category='model_unavailable', budget_cap=3.)
    assert retry.limits.budget_usd <= cfg.limits.budget_usd
    assert retry.limits.budget_usd <= 3.
    assert retry.limits.max_repair_cycles == cfg.limits.max_repair_cycles
    assert retry.roles['developer_backend'].max_steps == cfg.roles['developer_backend'].max_steps
    assert retry.limits.busy_wait_s > cfg.limits.busy_wait_s


def test_healing_summary_counts_chains_and_only_additional_cost(tmp_path, monkeypatch):
    from swarmforge.services.healing import healing_summary
    store = Store(tmp_path / 'db')
    store.create_run('original', state='FAILED')
    store.create_run('retry1', state='FAILED', heal_of='original')
    store.create_run('retry2', state='COMPLETED', heal_of='retry1')
    monkeypatch.setattr(store, 'cost_summary', lambda rid: {'total': 10 if rid == 'original' else .1})
    result = healing_summary(store)[0]
    assert result['chains'] == 1 and result['attempts'] == 2 and result['recovered'] == 1
    assert result['additional_cost_usd'] == .2 and result['cost_per_recovery_usd'] == .2
