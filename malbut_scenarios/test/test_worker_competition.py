"""Tests for the scenario-only two-worker claim coordination."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from malbut_agent_server.adapters.outbound.sqlite_action_repository import (
    SQLiteActionRepository,
)
from malbut_agent_server.ports.action_repository import ActionClaim
from malbut_scenarios import worker_competition as competition_module
from malbut_scenarios.worker_competition import (
    CompetingApprovedActionWorker,
    CoordinatedActionRepository,
    WORKER_COMPETITION_OBSERVATION_FILENAME,
    WorkerCompetitionCoordinator,
    WorkerCompetitionError,
    read_worker_competition_observation,
    worker_competition_observation_path,
)


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            'CREATE TABLE robot_actions (state TEXT NOT NULL)'
        )
        connection.commit()
    finally:
        connection.close()


def _publish_pending(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO robot_actions (state) VALUES ('PENDING_PREFLIGHT')"
        )
        connection.commit()
    finally:
        connection.close()


def _seed_production_pending_action(path: Path) -> None:
    """Insert one schema-valid action after production initialization."""
    arguments_json = json.dumps(
        {'location': '거실'},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    arguments_digest = hashlib.sha256(
        arguments_json.encode('utf-8')
    ).hexdigest()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            '''
            INSERT INTO robot_actions (
                schema_version, action_id, operation_id,
                confirmation_request_id, proposal_fingerprint,
                arguments_digest, arguments_json, target_binding_digest,
                user_id, conversation_id, session_instance_id, generation,
                conversation_revision, decision_id, tool_name,
                target_room_name, target_room_category,
                confirmation_state_evidence_id,
                confirmation_state_observed_at,
                confirmation_safety_policy_revision,
                state, revision, created_at, updated_at,
                dispatch_expires_at, result_code, simulation,
                physical_authorized
            ) VALUES (
                1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, 'PENDING_PREFLIGHT', 1, 100.0, 100.0, 200.0,
                NULL, 1, 0
            )
            ''',
            (
                'action-test',
                'operation-test',
                'confirmation-test',
                'a' * 64,
                arguments_digest,
                arguments_json,
                'b' * 64,
                'user-test',
                'conversation-test',
                'session-test',
                1,
                1,
                'decision-test',
                'navigate',
                '거실',
                'living_room',
                'state-test',
                100.0,
                'malbut-safety-v1',
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_two_claim_outcomes_finish_before_winner_is_released(
    tmp_path,
) -> None:
    database = tmp_path / 'runtime.sqlite3'
    _database(database)
    coordinator = WorkerCompetitionCoordinator(
        str(database),
        timeout_seconds=2.0,
    )
    loser_called = threading.Event()
    release_loser = threading.Event()
    calls = []
    claimed = object()

    def winner_operation():
        calls.append('winner')
        return claimed

    def loser_operation():
        calls.append('loser')
        loser_called.set()
        assert release_loser.wait(timeout=1.0)
        return None

    def compete(index, operation):
        coordinator.await_preclaim(index)
        return coordinator.claim(index, operation)

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(compete, 0, winner_operation)
        loser = pool.submit(compete, 1, loser_operation)
        time.sleep(0.05)
        assert calls == []

        _publish_pending(database)
        assert loser_called.wait(timeout=1.0)
        assert winner.done() is False
        release_loser.set()

        assert winner.result(timeout=1.0) is claimed
        assert loser.result(timeout=1.0) is None

    assert sorted(calls) == ['loser', 'winner']
    observation_path = (
        tmp_path / WORKER_COMPETITION_OBSERVATION_FILENAME
    )
    observation = read_worker_competition_observation(
        observation_path
    )
    assert observation.fault_profile == 'competing_workers'
    assert observation.contender_count == 2
    assert observation.winner_count == 1
    assert observation.nonwinner_count == 1
    assert oct(os.lstat(observation_path).st_mode & 0o777) == '0o600'


def test_two_real_sqlite_repositories_produce_one_claim_winner(
    tmp_path,
) -> None:
    """Cross the production BEGIN IMMEDIATE/CAS with two connections."""
    database = tmp_path / 'runtime.sqlite3'
    repositories = (
        SQLiteActionRepository(str(database)),
        SQLiteActionRepository(str(database)),
    )
    _seed_production_pending_action(database)
    coordinator = WorkerCompetitionCoordinator(
        str(database),
        timeout_seconds=2.0,
    )
    adapters = tuple(
        CoordinatedActionRepository(
            repository,
            coordinator,
            contender=index,
        )
        for index, repository in enumerate(repositories)
    )

    def compete(index: int):
        coordinator.await_preclaim(index)
        return adapters[index].claim_next(
            f'worker-{index}',
            now=101.0,
            lease_for=20.0,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(
                future.result(timeout=3.0)
                for future in (
                    pool.submit(compete, 0),
                    pool.submit(compete, 1),
                )
            )

        claims = tuple(
            result for result in results if isinstance(result, ActionClaim)
        )
        assert len(claims) == 1
        assert sum(result is None for result in results) == 1
        assert claims[0].action.action_id == 'action-test'
        assert claims[0].fence == 1
        assert read_worker_competition_observation(
            coordinator.observation_path
        ).winner_count == 1
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                'SELECT state, claim_fence FROM robot_actions'
            ).fetchone()
        assert row == ('CLAIMED', 1)
    finally:
        coordinator.close()
        for repository in repositories:
            repository.close()


def test_two_winners_fail_closed_without_publishing_evidence(
    tmp_path,
) -> None:
    database = tmp_path / 'runtime.sqlite3'
    _database(database)
    _publish_pending(database)
    coordinator = WorkerCompetitionCoordinator(
        str(database),
        timeout_seconds=1.0,
    )

    def compete(index):
        coordinator.await_preclaim(index)
        return coordinator.claim(index, object)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(compete, index) for index in (0, 1)]
        for result in results:
            with pytest.raises(
                WorkerCompetitionError,
                match='worker_competition_outcome_invalid',
            ):
                result.result(timeout=1.0)

    assert not (
        tmp_path / WORKER_COMPETITION_OBSERVATION_FILENAME
    ).exists()


def test_close_unblocks_a_preapproval_contender_without_claiming(
    tmp_path,
) -> None:
    database = tmp_path / 'runtime.sqlite3'
    _database(database)
    coordinator = WorkerCompetitionCoordinator(
        str(database),
        timeout_seconds=2.0,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(coordinator.await_preclaim, 0)
        time.sleep(0.05)
        coordinator.close()
        with pytest.raises(
            WorkerCompetitionError,
            match='worker_competition_closed',
        ):
            waiting.result(timeout=1.0)


def test_observation_contract_rejects_stale_or_extended_files(
    tmp_path,
) -> None:
    database = tmp_path / 'runtime.sqlite3'
    _database(database)
    observation_path = worker_competition_observation_path(str(database))
    observation_path.write_text(
        '{"fault_profile":"competing_workers",'
        '"contender_count":2,"winner_count":1,'
        '"nonwinner_count":1,"worker_id":"private"}',
        encoding='utf-8',
    )

    with pytest.raises(
        WorkerCompetitionError,
        match='worker_competition_observation_invalid',
    ):
        read_worker_competition_observation(observation_path)
    observation_path.write_text(
        '{"contender_count":2,"fault_profile":"competing_workers",'
        '"nonwinner_count":1,"winner_count":1,"winner_count":1}',
        encoding='utf-8',
    )
    observation_path.chmod(0o600)
    with pytest.raises(
        WorkerCompetitionError,
        match='worker_competition_observation_invalid',
    ):
        read_worker_competition_observation(observation_path)
    with pytest.raises(
        WorkerCompetitionError,
        match='worker_competition_observation_invalid',
    ):
        WorkerCompetitionCoordinator(str(database))


def test_diagnostics_do_not_render_private_paths(tmp_path) -> None:
    database = tmp_path / 'private-runtime.sqlite3'
    _database(database)
    coordinator = WorkerCompetitionCoordinator(str(database))

    assert str(database) not in repr(coordinator)
    assert 'private-runtime' not in repr(coordinator)


def test_coordinated_repository_gates_only_first_claim_and_forwards_port(
) -> None:
    """The scenario adapter must not alter the production repository port."""
    events = []
    values = {
        'get': object(),
        'find': object(),
        'claim-first': object(),
        'claim-second': object(),
        'intent': object(),
        'blocked': object(),
        'started': object(),
        'finished': object(),
    }

    class Repository:
        def __init__(self):
            self.claim_count = 0

        def get(self, action_id):
            events.append(('get', action_id))
            return values['get']

        def find_by_confirmation(self, confirmation_request_id):
            events.append(('find', confirmation_request_id))
            return values['find']

        def claim_next(self, worker_id, *, now, lease_for):
            self.claim_count += 1
            events.append(('claim', worker_id, now, lease_for))
            return values[
                'claim-first' if self.claim_count == 1 else 'claim-second'
            ]

        def record_dispatch_intent(self, claim, authorization, *, now):
            events.append(('intent', claim, authorization, now))
            return values['intent']

        def block(self, claim, *, result_code, now):
            events.append(('block', claim, result_code, now))
            return values['blocked']

        def mark_started(self, intent, *, now):
            events.append(('started', intent, now))
            return values['started']

        def finish(self, intent, state, *, result_code, now):
            events.append(('finish', intent, state, result_code, now))
            return values['finished']

        def recover_uncertain_after_restart(self, *, now):
            events.append(('recover', now))
            return 3

    class Coordinator:
        def __init__(self):
            self.calls = []

        def claim(self, contender, operation):
            self.calls.append(contender)
            events.append(('gate', contender))
            return operation()

    repository = Repository()
    coordinator = Coordinator()
    adapter = CoordinatedActionRepository(
        repository,
        coordinator,
        contender=1,
    )
    claim = object()
    authorization = object()
    intent = object()
    state = object()

    assert adapter.get('action-1') is values['get']
    assert adapter.find_by_confirmation('confirmation-1') is values['find']
    assert adapter.claim_next(
        'worker-1', now=10.0, lease_for=20.0
    ) is values['claim-first']
    assert adapter.claim_next(
        'worker-1', now=11.0, lease_for=20.0
    ) is values['claim-second']
    assert adapter.record_dispatch_intent(
        claim, authorization, now=12.0
    ) is values['intent']
    assert adapter.block(
        claim, result_code='BLOCKED', now=13.0
    ) is values['blocked']
    assert adapter.mark_started(intent, now=14.0) is values['started']
    assert adapter.finish(
        intent,
        state,
        result_code='DONE',
        now=15.0,
    ) is values['finished']
    assert adapter.recover_uncertain_after_restart(now=16.0) == 3

    assert coordinator.calls == [1]
    assert [event for event in events if event[0] == 'gate'] == [
        ('gate', 1),
    ]
    assert [event for event in events if event[0] == 'claim'] == [
        ('claim', 'worker-1', 10.0, 20.0),
        ('claim', 'worker-1', 11.0, 20.0),
    ]
    assert 'private' not in repr(adapter)


def test_competing_worker_enters_preclaim_gate_once_before_normal_runs(
    monkeypatch,
    tmp_path,
) -> None:
    """Only the first run samples a claim after the scenario rendezvous."""
    events = []
    results = iter((object(), object()))

    monkeypatch.setattr(
        competition_module.ApprovedActionWorker,
        '__init__',
        lambda self, *args, **kwargs: events.append('worker.init'),
    )
    monkeypatch.setattr(
        competition_module.ApprovedActionWorker,
        'run_once',
        lambda self: events.append('worker.run') or next(results),
    )
    coordinator = WorkerCompetitionCoordinator(
        str(tmp_path / 'runtime.sqlite3'),
    )
    monkeypatch.setattr(
        coordinator,
        'await_preclaim',
        lambda contender: events.append(('gate.preclaim', contender)),
    )
    worker = CompetingApprovedActionWorker(
        competition_coordinator=coordinator,
        contender=1,
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first is not second
    assert events == [
        'worker.init',
        ('gate.preclaim', 1),
        'worker.run',
        'worker.run',
    ]
    coordinator.close()
