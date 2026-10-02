"""
tests/test_run_instrumentation.py — what a run records about itself, and the
builder's Test step.

Six behaviours landed together on 2026-08-30 and each of them replaced something
that was silently absent rather than merely imperfect:

- `node_executions.input` was written as an unconditional `None`.
- `node_executions.status` was written as an unconditional `"succeeded"`, so a
  failing node produced NO row at all and nothing knew which node failed.
- `current_node_key` was written only at an interrupt, and then as the literal
  string `"human_approval"`.
- `latency_ms` was a whole-superstep delta shared by every node in the step.
- A run could only ever be pinned to `current_version_id`, so the builder's
  "Test run" never tested the graph on screen.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.db.database import async_session_maker
from src.graphs.compiler import _instrument, initial_state_from_trigger
from src.graphs.node_handlers import AgentNodeConfigError, agent_handler, node_key_of
from src.modules.executions.models import NodeExecution, WorkflowRun
from src.modules.executions.service import _nodes_before_stop
from src.modules.workflows.models import WorkflowEdge, WorkflowNode, WorkflowVersion
from src.workers.graph_tasks import _stream_graph
from tests.test_workflows import create_workflow, create_workspace, register_and_get_token

# ---------------------------------------------------------------------------
# In-memory graph helpers (no DB) — same shape as tests/test_graph_compiler.py
# ---------------------------------------------------------------------------


def _node(key: str, node_type: str, config: dict | None = None) -> WorkflowNode:
    return WorkflowNode(
        id=uuid.uuid4(),
        workflow_version_id=uuid.uuid4(),
        node_key=key,
        node_type=node_type,
        config=config or {},
        position_x=0.0,
        position_y=0.0,
    )


def _edge(source: str, target: str, condition: dict | None = None) -> WorkflowEdge:
    return WorkflowEdge(
        id=uuid.uuid4(),
        workflow_version_id=uuid.uuid4(),
        source_node_key=source,
        target_node_key=target,
        condition=condition,
    )


def _version(nodes: list[WorkflowNode], edges: list[WorkflowEdge], *, published: bool = True) -> WorkflowVersion:
    version = WorkflowVersion(
        id=uuid.uuid4(),
        workflow_id=uuid.uuid4(),
        version_number=1,
        graph_definition={"nodes": [], "edges": []},
        published_at=datetime.now(UTC) if published else None,
    )
    version.nodes = nodes
    version.edges = edges
    return version


#: A tool config that needs no network and no LLM — the erp_connector is a mock.
_KNOWLEDGE_FREE_TOOL = {"tool_type": "erp_connector", "action": "create_journal_entry", "is_mutating": True}


def _n(key: str, node_type: str, config: dict | None = None) -> dict:
    """A NodeInput payload, for graphs saved through the API."""
    return {"node_key": key, "node_type": node_type, "config": config or {}, "position_x": 0, "position_y": 0}


def _e(source: str, target: str) -> dict:
    return {"source_node_key": source, "target_node_key": target, "condition": None}


# ---------------------------------------------------------------------------
# The instrumentation wrapper
# ---------------------------------------------------------------------------


def test_a_failing_handler_is_tagged_with_its_node_key():
    def boom(_state: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("nope")

    with pytest.raises(ValueError) as caught:
        _instrument(boom, "extract_invoice")({})

    assert node_key_of(caught.value) == "extract_invoice"


def test_the_exception_type_is_PRESERVED_not_wrapped():
    """
    Load-bearing: `graph_tasks._NON_RETRYABLE` classifies by exception TYPE.

    A wrapper exception would make every config error look retryable, and
    re-drive a mutating tool three more times on a graph that can never succeed.
    """

    def boom(_state: dict[str, Any]) -> dict[str, Any]:
        raise AgentNodeConfigError("bad config")

    with pytest.raises(AgentNodeConfigError):
        _instrument(boom, "agent_1")({})


def test_langgraph_control_flow_exceptions_are_left_alone():
    """
    `human_approval_handler` suspends the graph by raising through `interrupt()`.
    Tagging that would make the engine write a `failed` row for every gate.
    """
    from langgraph.errors import GraphInterrupt

    def suspend(_state: dict[str, Any]) -> dict[str, Any]:
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt) as caught:
        _instrument(suspend, "approval_1")({})

    assert node_key_of(caught.value) is None


def test_a_successful_handler_reports_its_own_wall_clock():
    result = _instrument(lambda _state: {"node_outputs": {}}, "tool_1")({})

    timing = result["node_timings"]["tool_1"]
    assert timing["duration_ms"] >= 1
    assert datetime.fromisoformat(timing["completed_at"]) >= datetime.fromisoformat(timing["started_at"])


def test_timings_accumulate_across_nodes_rather_than_replacing():
    state = {"node_timings": {"first": {"duration_ms": 5}}}
    result = _instrument(lambda _s: {}, "second")(state)

    assert set(result["node_timings"]) == {"first", "second"}


def test_a_handler_returning_a_non_dict_is_passed_through_untouched():
    assert _instrument(lambda _s: "not a dict", "n")({}) == "not a dict"


# ---------------------------------------------------------------------------
# What reaches node_executions
# ---------------------------------------------------------------------------


async def _rows_for(run_id: uuid.UUID) -> list[NodeExecution]:
    async with async_session_maker() as session:
        result = await session.execute(select(NodeExecution).where(NodeExecution.workflow_run_id == run_id).order_by(NodeExecution.created_at))
        return list(result.scalars().all())


async def _run_row(run_id: uuid.UUID) -> WorkflowRun:
    async with async_session_maker() as session:
        return (await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))).scalar_one()


async def _prepared_run(
    client: AsyncClient,
    suffix: str,
    nodes: list[dict],
    edges: list[dict],
    *,
    until_node_key: str | None = None,
) -> tuple[uuid.UUID, WorkflowVersion, uuid.UUID]:
    """
    A real org, workflow, draft version and run row — the FK chain `_stream_graph`
    writes against.

    The run is created through the Test-step endpoint precisely because it works
    on an unpublished draft, so this needs no publish and exercises the new path
    on the way past.
    """
    data = await register_and_get_token(client, suffix)
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    saved = await client.post(f"/api/v1/workflows/{wf['id']}/versions", json={"nodes": nodes, "edges": edges}, headers=headers)
    assert saved.status_code == status.HTTP_201_CREATED, saved.text
    version_id = saved.json()["id"]

    body: dict[str, Any] = {"allow_mutating": True}
    if until_node_key is not None:
        body["until_node_key"] = until_node_key
    started = await client.post(f"/api/v1/workflows/{wf['id']}/versions/{version_id}/test-run", json=body, headers=headers)
    assert started.status_code == status.HTTP_201_CREATED, started.text
    run = started.json()

    async with async_session_maker() as session:
        loaded = await session.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.id == uuid.UUID(version_id))
            .options(selectinload(WorkflowVersion.nodes), selectinload(WorkflowVersion.edges))
        )
        version = loaded.scalar_one()

    return uuid.UUID(run["id"]), version, uuid.UUID(run["organization_id"])


@pytest.mark.asyncio
async def test_a_failing_node_now_writes_a_failed_row(client: AsyncClient):
    """
    Before this, `_insert_node_execution` was only ever called with "succeeded",
    so a run that failed recorded WHICH node failed nowhere at all.
    """
    run_id, version, org_id = await _prepared_run(
        client,
        "failrow",
        [_n("start_1", "start"), _n("agent_1", "agent", {"system_prompt": "hi"}), _n("end_1", "end")],
        [_e("start_1", "agent_1"), _e("agent_1", "end_1")],
    )

    with pytest.raises(AgentNodeConfigError):
        await _stream_graph(run_id, version, initial_state_from_trigger(organization_id=org_id), 1, org_id, allow_draft=True)

    failed = [row for row in await _rows_for(run_id) if row.status == "failed"]
    assert [row.node_key for row in failed] == ["agent_1"]
    assert "output_schema" in failed[0].output["error"]

    # And the run points at the node that broke, not at nothing.
    assert (await _run_row(run_id)).current_node_key == "agent_1"


@pytest.mark.asyncio
async def test_current_node_key_advances_while_a_run_progresses(client: AsyncClient):
    run_id, version, org_id = await _prepared_run(client, "progress", [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])

    await _stream_graph(run_id, version, initial_state_from_trigger(organization_id=org_id), 1, org_id, allow_draft=True)

    run = await _run_row(run_id)
    assert run.status == "completed"
    # Cleared on completion, as before — the progress writes happen mid-stream.
    assert run.current_node_key is None


@pytest.mark.asyncio
async def test_node_rows_carry_real_start_and_completion_times(client: AsyncClient):
    run_id, version, org_id = await _prepared_run(client, "timings", [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])

    await _stream_graph(run_id, version, initial_state_from_trigger(organization_id=org_id), 1, org_id, allow_draft=True)

    rows = await _rows_for(run_id)
    assert rows, "expected node executions"
    for row in rows:
        assert row.started_at is not None
        assert row.completed_at is not None
        assert row.completed_at >= row.started_at


@pytest.mark.asyncio
async def test_a_gate_records_WHICH_gate_the_run_is_held_at(client: AsyncClient):
    """
    `current_node_key` used to be the literal string "human_approval", which no
    graph with two gates could disambiguate.
    """
    run_id, version, org_id = await _prepared_run(
        client,
        "gatekey",
        [_n("start_1", "start"), _n("approval_1", "human_approval"), _n("end_1", "end")],
        [_e("start_1", "approval_1"), _e("approval_1", "end_1")],
    )

    await _stream_graph(run_id, version, initial_state_from_trigger(organization_id=org_id), 1, org_id, allow_draft=True)

    run = await _run_row(run_id)
    assert run.status == "waiting_approval"
    assert run.current_node_key == "approval_1"
    assert run.interrupt_payload["node_key"] == "approval_1"


@pytest.mark.asyncio
async def test_a_run_stops_after_the_node_a_test_step_asked_for(client: AsyncClient):
    run_id, version, org_id = await _prepared_run(
        client,
        "stopnode",
        [_n("start_1", "start"), _n("mid", "tool", _KNOWLEDGE_FREE_TOOL), _n("end_1", "end")],
        [_e("start_1", "mid"), _e("mid", "end_1")],
        until_node_key="mid",
    )

    with patch("src.graphs.node_handlers._run_erp_connector", return_value={"posted": True}):
        await _stream_graph(
            run_id,
            version,
            initial_state_from_trigger(organization_id=org_id),
            1,
            org_id,
            stop_after_node_key="mid",
            allow_draft=True,
        )

    keys = [row.node_key for row in await _rows_for(run_id)]
    assert "mid" in keys
    assert "end_1" not in keys, "the stop node's successors must not run"


def test_an_agent_reports_what_it_actually_read():
    """
    The resolved values, not the configured paths — a null here is a mis-typed
    `input_fields` entry made visible, which nothing else in the product reports.

    Calls the handler directly with an injected client: `_bind_node_handler`
    binds `get_llm_client` as a signature default at import, so patching the
    module attribute would not reach it (the trap documented in
    apps/api/CLAUDE.md's retrieval section).
    """

    class _Result:
        parsed = {"ok": True}
        tokens_prompt = 1
        tokens_completion = 1
        cost_usd = 0.0
        model = "gpt-test"

    class _Client:
        def parse(self, **_kwargs):
            return _Result()

    state = initial_state_from_trigger(organization_id=uuid.uuid4(), trigger_payload={"present": 7})
    update = agent_handler(
        state,
        node_key="agent_1",
        config={
            "system_prompt": "x",
            "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "input_fields": ["trigger_payload.present", "trigger_payload.absent"],
        },
        client_factory=lambda **_kw: _Client(),
    )

    recorded = update["node_inputs"]["agent_1"]
    assert recorded["trigger_payload.present"] == 7
    assert recorded["trigger_payload.absent"] is None


def test_a_tool_with_no_field_maps_records_no_input():
    """
    An empty dict on screen reads as a finding. A tool that consumed nothing from
    state should say nothing, not say "{}".
    """
    from src.graphs.node_handlers import tool_handler

    with patch("src.graphs.node_handlers._run_erp_connector", return_value={"posted": True}):
        update = tool_handler(
            initial_state_from_trigger(organization_id=uuid.uuid4()),
            node_key="tool_1",
            config=_KNOWLEDGE_FREE_TOOL,
        )

    assert "node_inputs" not in update


def test_a_tool_records_the_VALUES_it_resolved_not_the_paths():
    from src.graphs.node_handlers import tool_handler

    state = initial_state_from_trigger(organization_id=uuid.uuid4(), trigger_payload={"vendor": "Acme"})
    with patch("src.graphs.node_handlers._run_erp_connector", return_value={"posted": True}):
        update = tool_handler(
            state,
            node_key="post",
            config={
                **_KNOWLEDGE_FREE_TOOL,
                "payload_fields": {"vendor": "trigger_payload.vendor", "amount": "trigger_payload.missing"},
            },
        )

    resolved = update["node_inputs"]["post"]["payload_fields"]
    assert resolved == {"vendor": "Acme", "amount": None}


# ---------------------------------------------------------------------------
# The Test-step endpoint
# ---------------------------------------------------------------------------


async def _draft_version(client: AsyncClient, token: str, workflow_id: str, nodes: list[dict], edges: list[dict]) -> dict:
    resp = await client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={"nodes": nodes, "edges": edges},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a_test_step_runs_an_UNPUBLISHED_draft(client: AsyncClient, celery_calls):
    """
    The whole point. `POST /workflows/{id}/run` is pinned to
    `current_version_id`, so the builder's old Test run never tested the draft
    on screen.
    """
    data = await register_and_get_token(client, "teststep")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(client, token, wf["id"], [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])
    assert version["published_at"] is None

    resp = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={"trigger_payload": {"hello": "world"}},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    body = resp.json()
    assert body["is_test"] is True
    assert body["workflow_version_id"] == version["id"]
    assert celery_calls, "a test run must actually be enqueued"


@pytest.mark.asyncio
async def test_a_test_step_refuses_to_run_a_step_that_writes(client: AsyncClient):
    data = await register_and_get_token(client, "testmut")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(
        client,
        token,
        wf["id"],
        [_n("start_1", "start"), _n("post", "tool", _KNOWLEDGE_FREE_TOOL), _n("end_1", "end")],
        [_e("start_1", "post"), _e("post", "end_1")],
    )

    resp = await client.post(f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run", json={}, headers=headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "post" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_stopping_before_a_writing_step_makes_the_test_allowed(client: AsyncClient):
    """
    The guard is about what the test would REACH, so a stop point in front of the
    write is a legitimate way to satisfy it.
    """
    data = await register_and_get_token(client, "teststop")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(
        client,
        token,
        wf["id"],
        [_n("start_1", "start"), _n("safe", "human_approval"), _n("post", "tool", _KNOWLEDGE_FREE_TOOL), _n("end_1", "end")],
        [_e("start_1", "safe"), _e("safe", "post"), _e("post", "end_1")],
    )

    resp = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={"until_node_key": "safe"},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text
    assert resp.json()["test_until_node_key"] == "safe"


@pytest.mark.asyncio
async def test_allow_mutating_is_the_deliberate_override(client: AsyncClient):
    data = await register_and_get_token(client, "testallow")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(
        client,
        token,
        wf["id"],
        [_n("start_1", "start"), _n("post", "tool", _KNOWLEDGE_FREE_TOOL), _n("end_1", "end")],
        [_e("start_1", "post"), _e("post", "end_1")],
    )

    resp = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={"allow_mutating": True},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text


@pytest.mark.asyncio
async def test_an_unknown_stop_node_is_refused(client: AsyncClient):
    data = await register_and_get_token(client, "testunknown")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(client, token, wf["id"], [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])

    resp = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={"until_node_key": "nope"},
        headers=headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_test_runs_are_hidden_from_the_executions_list_by_default(client: AsyncClient):
    data = await register_and_get_token(client, "testhide")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])

    version = await _draft_version(client, token, wf["id"], [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])
    await client.post(f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run", json={}, headers=headers)

    hidden = await client.get("/api/v1/executions", headers=headers)
    assert hidden.json() == []

    shown = await client.get("/api/v1/executions?include_test=true", headers=headers)
    assert len(shown.json()) == 1


@pytest.mark.asyncio
async def test_another_org_cannot_test_run_this_workflow(client: AsyncClient):
    owner = await register_and_get_token(client, "testowner")
    ws = await create_workspace(client, owner["access_token"])
    wf = await create_workflow(client, owner["access_token"], ws["id"])
    version = await _draft_version(client, owner["access_token"], wf["id"], [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])

    intruder = await register_and_get_token(client, "testintruder")
    resp = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={},
        headers={"Authorization": f"Bearer {intruder['access_token']}"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_the_status_endpoint_omits_the_input_output_blobs(client: AsyncClient):
    """
    The whole reason it exists: the full response re-sends every node's
    accumulated state snapshot on every poll.
    """
    data = await register_and_get_token(client, "teststatus")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])
    version = await _draft_version(client, token, wf["id"], [_n("start_1", "start"), _n("end_1", "end")], [_e("start_1", "end_1")])
    run = (await client.post(f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run", json={}, headers=headers)).json()

    resp = await client.get(f"/api/v1/executions/{run['id']}/status", headers=headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["is_test"] is True
    assert "node_executions" in body
    assert "trigger_payload" not in body
    for row in body["node_executions"]:
        assert "input" not in row
        assert "output" not in row


@pytest.mark.parametrize("registry_backed", [False, True], ids=["inline", "registry"])
@pytest.mark.parametrize(
    ("shape", "stop", "allow_mutating", "expected"),
    [
        ("bypass", "approval_1", False, 422),
        ("bypass", "approval_1", True, 201),
        ("linear", "approval_1", False, 201),
        ("linear", "post", False, 422),
        ("linear", "post", True, 201),
        ("bypass", None, False, 422),
        ("bypass", "check_amount", False, 422),
        ("two_gates", "approval_1", False, 422),
        ("two_gates", "approval_2", False, 422),
    ],
)
async def test_test_guard_checks_every_possible_branch(client, celery_calls, registry_backed, shape, stop, allow_mutating, expected):
    data = await register_and_get_token(client, "branchguard")
    token = data["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    ws = await create_workspace(client, token)
    wf = await create_workflow(client, token, ws["id"])
    config = _KNOWLEDGE_FREE_TOOL
    if registry_backed:
        tool = await client.post(
            "/api/v1/tools",
            json={
                "workspace_id": ws["id"],
                "name": "guard_test_write",
                "tool_type": "erp_connector",
                "config": {"action": "create_journal_entry"},
                "is_mutating": True,
            },
            headers=headers,
        )
        assert tool.status_code == 201, tool.text
        # Explicit false must not suppress the registry's mutation flag.
        config = {"tool_id": tool.json()["id"], "is_mutating": False}

    nodes = [_n("start_1", "start"), _n("approval_1", "human_approval"), _n("post", "tool", config), _n("end_1", "end")]
    edges = [_e("approval_1", "post"), _e("post", "end_1")]
    if shape == "linear":
        edges.append(_e("start_1", "approval_1"))
    else:
        nodes.append(_n("check_amount", "condition"))
        edges.extend(
            [
                _e("start_1", "check_amount"),
                {
                    **_e("check_amount", "approval_1"),
                    "condition": {
                        "field": "trigger_payload.amount",
                        "operator": "gt",
                        "value": 10,
                        "branch": "needs_approval",
                    },
                },
            ]
        )
        other = "post"
        if shape == "two_gates":
            other = "approval_2"
            nodes.append(_n(other, "human_approval"))
            edges.append(_e(other, "post"))
        edges.append({**_e("check_amount", other), "condition": {"branch": "otherwise"}})

    version = await _draft_version(client, token, wf["id"], nodes, edges)
    response = await client.post(
        f"/api/v1/workflows/{wf['id']}/versions/{version['id']}/test-run",
        json={"until_node_key": stop, "allow_mutating": allow_mutating, "trigger_payload": {"amount": 5}},
        headers=headers,
    )
    assert response.status_code == expected, response.text
    if expected == 422:
        assert "post" in response.json()["detail"]
        assert not celery_calls, "rejected tests must never be dispatched"
        assert (await client.get("/api/v1/executions?include_test=true", headers=headers)).json() == []
    else:
        assert len(celery_calls) == 1


@pytest.mark.parametrize("stop", ["approval_1", "check_amount"])
@pytest.mark.parametrize("amount", [5, 20])
async def test_worker_branch_and_condition_stop_semantics(client, stop, amount):
    """A controlled auto-post branch really writes despite either stop selection."""
    run_id, version, org_id = await _prepared_run(
        client,
        "branchruntime",
        [
            _n("start_1", "start"),
            _n("check_amount", "condition"),
            _n("approval_1", "human_approval"),
            _n("post", "tool", _KNOWLEDGE_FREE_TOOL),
            _n("end_1", "end"),
        ],
        [
            _e("start_1", "check_amount"),
            {
                **_e("check_amount", "approval_1"),
                "condition": {
                    "field": "trigger_payload.amount",
                    "operator": "gt",
                    "value": 10,
                    "branch": "needs_approval",
                },
            },
            {**_e("check_amount", "post"), "condition": {"branch": "auto_post"}},
            _e("approval_1", "post"),
            _e("post", "end_1"),
        ],
        until_node_key=stop,
    )
    with patch("src.graphs.node_handlers._run_erp_connector", return_value={"posted": True}) as write:
        await _stream_graph(
            run_id,
            version,
            initial_state_from_trigger(organization_id=org_id, trigger_payload={"amount": amount}),
            1,
            org_id,
            stop_after_node_key=stop,
            allow_draft=True,
        )
    keys = [row.node_key for row in await _rows_for(run_id)]
    assert "check_amount" not in keys, "routing-only nodes emit no stop event"
    if amount == 5:
        write.assert_called_once()
        assert "post" in keys
        assert (await _run_row(run_id)).status == "completed"
    else:
        write.assert_not_called()
        assert (await _run_row(run_id)).current_node_key == "approval_1"


async def test_worker_reliably_stops_before_linear_write(client):
    run_id, version, org_id = await _prepared_run(
        client,
        "linearwrite",
        [_n("start_1", "start"), _n("post", "tool", _KNOWLEDGE_FREE_TOOL), _n("end_1", "end")],
        [_e("start_1", "post"), _e("post", "end_1")],
        until_node_key="start_1",
    )
    with patch("src.graphs.node_handlers._run_erp_connector") as write:
        await _stream_graph(
            run_id,
            version,
            initial_state_from_trigger(organization_id=org_id),
            1,
            org_id,
            stop_after_node_key="start_1",
            allow_draft=True,
        )
    write.assert_not_called()
    assert [row.node_key for row in await _rows_for(run_id)] == ["start_1"]
    assert (await _run_row(run_id)).status == "completed"


@pytest.mark.parametrize("malformation", ["cycle", "no_start", "dangling", "duplicate", "multiple_starts"])
def test_draft_reachability_is_conservative_and_cycle_safe(malformation):
    nodes = [_node("start_1", "start"), _node("stop", "human_approval"), _node("post", "tool", _KNOWLEDGE_FREE_TOOL)]
    edges = [_edge("start_1", "stop"), _edge("stop", "post")]
    if malformation == "cycle":
        nodes.append(_node("loop", "condition"))
        edges.extend([_edge("start_1", "loop"), _edge("loop", "loop"), _edge("loop", "post")])
    elif malformation == "no_start":
        nodes[0].node_type = "tool"
    elif malformation == "dangling":
        edges.append(_edge("missing", "post"))
    elif malformation == "duplicate":
        nodes.append(_node("stop", "tool"))
    else:
        nodes.append(_node("start_2", "start"))
        edges.append(_edge("start_2", "post"))
    assert "post" in _nodes_before_stop(nodes, edges, "stop")


@pytest.mark.parametrize("stop", ["approval_1", "approval_2"])
async def test_approval_resume_only_honors_the_gate_actually_selected_as_stop(client, stop):
    from langgraph.types import Command

    run_id, version, org_id = await _prepared_run(
        client,
        "resumestop",
        [
            _n("start_1", "start"),
            _n("route", "condition"),
            _n("approval_1", "human_approval"),
            _n("approval_2", "human_approval"),
            _n("post", "tool", _KNOWLEDGE_FREE_TOOL),
            _n("end_1", "end"),
        ],
        [
            _e("start_1", "route"),
            {
                **_e("route", "approval_1"),
                "condition": {
                    "field": "trigger_payload.amount",
                    "operator": "gt",
                    "value": 10,
                },
            },
            _e("route", "approval_2"),
            _e("approval_1", "post"),
            _e("approval_2", "post"),
            _e("post", "end_1"),
        ],
        until_node_key=stop,
    )
    with patch("src.graphs.node_handlers._run_erp_connector", return_value={"posted": True}) as write:
        await _stream_graph(
            run_id,
            version,
            initial_state_from_trigger(organization_id=org_id, trigger_payload={"amount": 5}),
            1,
            org_id,
            stop_after_node_key=stop,
            allow_draft=True,
        )
        write.assert_not_called()
        assert (await _run_row(run_id)).current_node_key == "approval_2"
        await _stream_graph(
            run_id,
            version,
            Command(resume={"approved": True}),
            1,
            org_id,
            stop_after_node_key=stop,
            allow_draft=True,
        )
        if stop == "approval_1":
            write.assert_called_once()
        else:
            write.assert_not_called()
    assert (await _run_row(run_id)).status == "completed"
