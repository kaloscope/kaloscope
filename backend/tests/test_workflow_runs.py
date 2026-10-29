"""Unit tests for workflow trial runs and their bounded repair loop."""

import asyncio
import threading
from multiprocessing import Queue
from types import SimpleNamespace

import httpx
import pytest

from app.core.ai import history, tools
from app.core.ai.queries import trial_run, workflow_logs
from app.core.exceptions import ForbiddenException
from app.core.flow import context as flow_context
from app.core.flow.engine import FlowEngine, FlowTask
from app.models.assistant import (
    WORKFLOW_PATH,
    AssistantProposal,
    AssistantToolCall,
    AssistantTurn,
    ConversationCreate,
    ProposalConfirm,
    ProposalDecision,
)
from app.models.flow import FlowDraftRevision, FlowGraph, FlowInstance, FlowLog
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import Permissions, UserRole
from app.services import flow as flow_service
from app.services.assistant import AssistantService
from app.services.assistant_proposals import AssistantProposalService as Proposals
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowRunService
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_ingestion import scraper
from tests.test_indexer_auth_authoring import runtime as runtime
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_workflow_outputs import (
    JSON_FORMATTER,
    MOVIE_HTML,
    SEARCH_TEMPLATE,
    search_page,
)

URL = "https://source.test/search?q={{ keyword|quote('') }}&page={{ page_num }}"
PARAMS = {"keyword": "Lantern", "page_num": 2, "page_size": 1}
UNTITLED = SEARCH_TEMPLATE.replace('"title": {{ item.name|tojson }},', "")


def search_graph(url=URL, template=SEARCH_TEMPLATE, method="GET"):
    """Build a complete `search_start -> http -> search_end` graph.

    Args:
        url: Search request template.
        template: `search_end` response template.
        method: HTTP method of the request node.

    Returns:
        Compressed graph accepted by static validation.
    """
    fields = [
        ("start", {"$schema": "search_start"}),
        (
            "http",
            {
                "$schema": "http",
                "method": method,
                "url": url,
                "formatter": JSON_FORMATTER,
            },
        ),
        ("end", {"$schema": "search_end", "response": template}),
    ]
    return {
        "nodes": [
            {
                "id": id,
                "type": "node",
                "position": {"x": index * 800, "y": 0},
                "data": data,
            }
            for index, (id, data) in enumerate(fields)
        ],
        "edges": [
            {
                "id": f"{source}-{target}",
                "source": source,
                "sourceHandle": "output",
                "target": target,
                "targetHandle": "input",
            }
            for source, target in (("start", "http"), ("http", "end"))
        ],
    }


def control_graph():
    """Build a looped conditional search beside an always-failing branch.

    Returns:
        Compressed graph where variables feed a condition and a page loop whose
        last response reaches `search_end`, while `probe` fails independently.
    """
    fields = {
        "start": {"$schema": "search_start"},
        "vars": {
            "$schema": "variables",
            "variables": '{"query": {{ keyword|tojson }}, '
            '"pages": [1, {{ page_num }}]}',
        },
        "check": {"$schema": "condition", "expression": "{{ query|length > 0 }}"},
        "loop": {"$schema": "loop", "expression": "{{ pages }}", "varname": "page"},
        "http": {
            "$schema": "http",
            "method": "GET",
            "url": "https://source.test/search?q={{ query|quote('') }}&page={{ page }}",
            "formatter": JSON_FORMATTER,
        },
        "end": {"$schema": "search_end", "response": SEARCH_TEMPLATE},
        "probe": {
            "$schema": "http",
            "method": "GET",
            "url": "https://down.test/ping",
            "formatter": JSON_FORMATTER,
        },
    }
    links = [
        ("start", "output", "vars", "input"),
        ("vars", "output", "check", "input"),
        ("check", "is_true", "loop", "input"),
        ("loop", "loop_start", "http", "input"),
        ("http", "output", "loop", "loop_continue"),
        ("loop", "output", "end", "input"),
        ("start", "output", "probe", "input"),
    ]
    return {
        "nodes": [
            {
                "id": id,
                "type": "node",
                "position": {"x": index * 800, "y": 0},
                "data": data,
            }
            for index, (id, data) in enumerate(fields.items())
        ],
        "edges": [
            {
                "id": f"{source}-{handle}-{target}",
                "source": source,
                "sourceHandle": handle,
                "target": target,
                "targetHandle": target_handle,
            }
            for source, handle, target, target_handle in links
        ],
    }


class Engine:
    """Dispatch trial runs to the real engine as background tasks of this loop."""

    def __init__(self):
        """Prepare the real engine without workers, schedules or queues."""
        self.real = FlowEngine.__new__(FlowEngine)
        self.real.__dict__["_events"] = Queue()
        self.dispatched = []
        self.tasks = []
        self.fail = None
        self.hold = None
        self.pause = None
        self.gate = None

    def enqueue_instance(self, instance):
        """Queue an exact persisted instance like the engine consumer.

        Args:
            instance: Persisted run to execute in the background.

        Raises:
            Exception: The configured queue failure, once.
        """
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        self.dispatched.append(instance.bootparams)

        async def run():
            """Wait for an optional release, then execute through the engine."""
            if self.hold is not None:
                await self.hold.wait()
            await self.real.execute(
                instance.graph_id,
                instance.bootparams,
                recoverable=True,
                recovery_id=instance.id,
                asynchronous=None,
            )

        self.tasks.append(asyncio.create_task(run()))

    async def drain(self):
        """Wait for every dispatched run to finish."""
        await asyncio.gather(*self.tasks)


@pytest.fixture
def engine(monkeypatch, runtime):
    """Route trial dispatches to an in-loop engine and a synthetic source.

    Args:
        monkeypatch: Fixture restoring the patched services.
        runtime: Shared HTTP node application stub.

    Returns:
        Engine recording dispatches and the requests the source received.
    """
    fake = Engine()
    fake.seen = []

    async def remote(request):
        """Serve search pages; any other path refuses the connection.

        Search pages wait for the optional gate, so other branches finish first.

        Args:
            request: Workflow HTTP request.

        Returns:
            JSON page or HTML movie cards.

        Raises:
            httpx.ConnectError: For unknown paths.
        """
        fake.seen.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/search" and request.url.host == "source.test":
            if fake.gate is not None:
                await fake.gate.wait()
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json=search_page(page))
        if request.url.host == "movies.example.test":
            return httpx.Response(200, text=MOVIE_HTML)
        raise httpx.ConnectError("refused", request=request)

    runtime.ctx.httpx = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    monkeypatch.setattr(
        FlowRunService,
        "app_ctx",
        classmethod(lambda cls: SimpleNamespace(flow_engine=fake)),
    )
    monkeypatch.setattr(FlowTask, "_running_tasks", [])
    monkeypatch.setattr(FlowTask, "_running_lock", threading.Lock())
    submit = FlowRunService.submit

    async def delayed_submit(cls, *args):
        """Hold the submission lock after dispatch for cancellation/race coverage.

        Args:
            *args: Exact definition and request passed to the real service.

        Returns:
            The instance already accepted by the queue.
        """
        instance = await submit(*args)
        if fake.pause is not None:
            await fake.pause.wait()
        return instance

    monkeypatch.setattr(FlowRunService, "submit", classmethod(delayed_submit))
    return fake


async def indexer(document=None, **fields):
    """Create a workflow whose published definition differs from its draft.

    Args:
        document: Saved draft; defaults to the working search graph.
        **fields: Model overrides such as `state` or `definition`.

    Returns:
        Created workflow at draft version 3.
    """
    values = {
        "name": "Source",
        "category": "indexer",
        "state": "modified",
        "definition": search_graph(URL.replace("/search", "/old")),
        "draft": document or search_graph(),
        "draft_version": 3,
        **fields,
    }
    return await FlowGraph.create(**values)


async def trial(graph, user, *, document=None, key="key-1", entry="search_start", **p):
    """Run a saved draft through the shared trial service.

    Args:
        graph: Workflow to run.
        user: Calling account.
        document: Content to run; defaults to the saved draft.
        key: Request key.
        entry: Start node type.
        **p: Startup parameters; defaults to a page-2 search.

    Returns:
        Shared trial result.
    """
    return await trial_run(
        graph.id,
        document or graph.draft,
        target={"workflow_id": graph.id, "draft_version": 3, "source": "saved_draft"},
        entry=entry,
        params=p or dict(PARAMS),
        key=key,
        user=user,
    )


def test_exact_draft(engine):
    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            before = await FlowGraph.get(id=graph.id).values()

            result = await trial(graph, owner)
            replayed = await trial(graph, owner)
            conflict = await trial(graph, owner, keyword="Other")

            assert result["status"] == "completed", result
            assert result["evidence"] == {
                "static_validation": "passed",
                "submitted": True,
                "completed": True,
                "output_check": "passed",
            }
            # the draft ran, not the published definition, with the given params
            assert engine.seen == [("/search", {"q": "Lantern", "page": "2"})]
            assert result["target"]["matches_published"] is False
            assert result["target"]["entry"] == "search_start"
            assert result["output"]["items"][0]["title"] == "Lantern 2"
            assert result["effects"] == ["sends real HTTP requests"]
            assert result["rerun"] == "allowed" and "next" not in result
            assert replayed["replayed"] and replayed["status"] == "completed"
            assert conflict["status"] == "conflict" and not conflict["executed"]
            assert len(engine.dispatched) == 1
            # nothing was saved or published and the instance was cleaned up
            assert await FlowGraph.get(id=graph.id).values() == before
            assert not await FlowInstance.exists()
            log = await FlowLog.get(id=result["output_log_id"])
            assert log.bootparams["$trial"]["key"] == "key-1"
            assert log.bootparams["$start"] == "search_start"

    asyncio.run(run())


def test_rotated_logs(engine):
    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            original = await trial(graph, owner)
            for number in range(5):
                await trial(graph, owner, key=f"later-{number}")
            assert not await FlowLog.exists(id=original["output_log_id"])

            replay = await trial(graph, owner)
            conflict = await trial(graph, owner, page_num=1)

            assert replay["replayed"] and replay["status"] == "unknown"
            assert conflict["status"] == "conflict"
            assert len(engine.dispatched) == 6

    asyncio.run(run())


@pytest.mark.parametrize(
    ("document", "status", "check", "detail"),
    [
        (
            search_graph(URL.replace("source.test", "down.test")),
            "completed_with_errors",
            "failed",
            "http",
        ),
        (search_graph(template=UNTITLED), "completed", "failed", "items.0.title"),
    ],
    ids=["node-failure", "business-output"],
)
def test_failures(engine, document, status, check, detail):
    async def run():
        async with database() as (owner, _):
            graph = await indexer(document)

            result = await trial(graph, owner)

            assert result["status"] == status, result
            assert result["evidence"]["output_check"] == check
            if status == "completed_with_errors":
                # the failing node is located and its original log can be paged
                assert result["errors"][0]["node_id"] == detail
                assert result["errors"][0]["node_type"] == "http"
                assert result["output"] is None
            else:
                paths = [issue["path"] for issue in result["output_issues"]]
                assert detail in paths
            assert "2 repair runs left" in result["next"]

    asyncio.run(run())


def test_unknown(engine, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("context unavailable")

    monkeypatch.setattr(flow_context.Context, "create", broken)

    async def run():
        async with database() as (owner, _):
            graph = await indexer()

            result = await trial(graph, owner)

            # setup failed before any node ran, so no record proves an outcome
            assert result["status"] == "unknown" and result["executed"] is True
            assert result["evidence"]["completed"] is False
            assert "not run again" in result["next"]
            assert not await FlowInstance.exists() and not await FlowLog.exists()

            replay = await trial(graph, owner)
            assert replay["replayed"] and replay["status"] == "unknown"
            assert len(engine.dispatched) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("entry", "params", "document", "path"),
    [
        ("details_start", PARAMS, None, "entry"),
        ("auth_start", PARAMS, None, "entry"),
        ("ingest_start", PARAMS, None, "entry"),
        ("search_start", {"$start": "auth_start"}, None, "params"),
        ("search_start", {"keyword": "x" * 9000}, None, "params"),
        (
            "search_start",
            PARAMS,
            search_graph(url=""),
            "document",
        ),
    ],
    ids=["missing", "login", "category", "reserved", "oversized", "static"],
)
def test_rejected(engine, entry, params, document, path):
    async def run():
        async with database() as (owner, _):
            graph = await indexer(document)

            result = await trial(graph, owner, entry=entry, **params)

            assert result["status"] == "invalid" and result["executed"] is False
            assert result["issues"][0]["path"] == path, result["issues"]
            assert engine.dispatched == [] and engine.seen == []
            assert not await FlowInstance.exists()

    asyncio.run(run())


def test_background(engine, monkeypatch):
    monkeypatch.setattr(flow_service, "TRIAL_WAIT", 0.3)
    engine.hold = asyncio.Event()

    async def run():
        async with database() as (owner, _):
            graph = await indexer()

            running = await trial(graph, owner)
            pending = await workflow_logs(graph.id, 5, owner)
            again = await trial(graph, owner)
            engine.hold.set()
            await engine.drain()
            done = await trial(graph, owner)
            logs = await workflow_logs(graph.id, 5, owner)

            # the reference returns at once and is never dispatched twice
            assert running["status"] == "running" and "later" in running["next"]
            assert running["evidence"]["completed"] is False
            assert pending["running_trials"] == ["key-1"] and pending["runs"] == []
            assert again["status"] == "running" and again["replayed"]
            assert done["status"] == "completed" and done["replayed"]
            assert len(engine.dispatched) == 1
            assert logs["runs"][0]["run_key"] == "key-1"
            assert "running_trials" not in logs

    asyncio.run(run())


@pytest.mark.parametrize(
    ("keyword", "pages", "check"),
    [("Lantern", ["1", "2"], "passed"), ("", [], "failed")],
    ids=["looped", "skipped"],
)
def test_control_flow(engine, keyword, pages, check):
    async def run():
        async with database() as (owner, _):
            graph = await indexer(control_graph())

            result = await trial(graph, owner, keyword=keyword, page_num=2, page_size=1)

            # variables reach the condition and every loop request
            searched = [params for url, params in engine.seen if url == "/search"]
            assert searched == [{"q": keyword, "page": page} for page in pages]
            assert ("/ping", {}) in engine.seen
            # the sibling branch failed independently of the search output
            assert result["status"] == "completed_with_errors", result
            assert [error["node_id"] for error in result["errors"]] == ["probe"]
            assert result["evidence"]["completed"] is True
            assert result["evidence"]["output_check"] == check
            if keyword:
                assert result["output"]["items"][0]["title"] == "Lantern 2"
            else:
                assert result["output"] is None and result["output_issues"]
            assert "repair runs left" in result["next"]

    asyncio.run(run())


def test_running_logs(engine, monkeypatch):
    monkeypatch.setattr(flow_service, "TRIAL_WAIT", 0.3)
    engine.gate = asyncio.Event()

    async def run():
        async with database() as (owner, _):
            graph = await indexer(control_graph())

            running = await trial(graph, owner)
            pending = await workflow_logs(graph.id, 5, owner)
            engine.gate.set()
            await engine.drain()
            done = await trial(graph, owner)
            logs = await workflow_logs(graph.id, 5, owner)

            # an early branch error is not the outcome of a run still in progress
            assert running["status"] == "running", running
            assert running["errors"][0]["node_id"] == "probe"
            assert pending["runs"][0]["status"] == "running"
            assert pending["runs"][0]["run_key"] == "key-1"
            assert pending["running_trials"] == ["key-1"]
            assert done["status"] == "completed_with_errors" and done["replayed"]
            assert done["evidence"]["output_check"] == "passed"
            assert logs["runs"][0]["status"] == "completed_with_errors"
            assert "running_trials" not in logs
            assert len(engine.dispatched) == 1

    asyncio.run(run())


def test_cancelled_dispatch(engine):
    engine.pause = asyncio.Event()
    engine.hold = asyncio.Event()

    async def run():
        async with database() as (owner, _):
            graph = await indexer()

            call = asyncio.create_task(trial(graph, owner))
            while not engine.dispatched:
                await asyncio.sleep(0.01)
            call.cancel()
            engine.pause.set()
            with pytest.raises(asyncio.CancelledError):
                await call
            # the queued run starts only after its caller was cancelled
            engine.hold.set()
            await engine.drain()
            retried = await trial(graph, owner)

            # the queued run keeps its claim, so the retry reads it
            assert retried["status"] == "completed", retried
            assert retried["replayed"]
            assert len(engine.dispatched) == 1

    asyncio.run(run())


def test_concurrent_submission(engine):
    engine.pause = asyncio.Event()

    async def run():
        async with database() as (owner, _):
            graph = await indexer()

            first = asyncio.create_task(trial(graph, owner))
            while not engine.dispatched:
                await asyncio.sleep(0.01)
            racing = await trial(graph, owner)
            engine.pause.set()
            submitted = await first
            later = await trial(graph, owner)

            # a call racing the submission reports it instead of dispatching again
            assert racing["status"] == "submitting" and racing["executed"] is None
            assert submitted["status"] == "completed" and "replayed" not in submitted
            assert later["status"] == "completed" and later["replayed"]
            assert len(engine.dispatched) == 1

    asyncio.run(run())


def test_interrupted_submission(engine):
    engine.fail = RuntimeError("queue unavailable")

    async def run():
        async with database() as (owner, _):
            graph = await indexer()

            with pytest.raises(RuntimeError):
                await trial(graph, owner)
            claimed = await FlowInstance.exists()
            retried = await trial(graph, owner)

            # a failed dispatch releases its claim, so the retry runs exactly once
            assert not claimed
            assert retried["status"] == "completed" and "replayed" not in retried
            assert len(engine.dispatched) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("document", "effects", "repeatable"),
    [
        (search_graph(), ["sends real HTTP requests"], True),
        (
            search_graph(method="POST"),
            ["sends real HTTP requests", "uses POST, which may change data"],
            False,
        ),
        (
            {
                "nodes": [
                    *search_graph()["nodes"],
                    {"id": "note", "data": {"$schema": "text"}},
                    {"id": "spare", "data": {"$schema": "script"}},
                ],
                "edges": [
                    *search_graph()["edges"],
                    {"id": "x", "source": "http", "target": "note"},
                ],
            },
            ["sends real HTTP requests", "writes files in media libraries"],
            False,
        ),
    ],
    ids=["get", "post", "file-write"],
)
def test_effects(document, effects, repeatable):
    # only nodes reachable from the entry count; the unlinked script is ignored
    assert FlowRunService.effects(document, "search_start") == (effects, repeatable)


def test_ingest_preview(engine, tmp_path):
    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Movies", category="ingest", state="draft", draft=scraper()
            )
            nfo = tmp_path / "Lantern.nfo"

            result = await trial_run(
                graph.id,
                graph.draft,
                target={"workflow_id": graph.id, "draft_version": 0},
                entry="ingest_start",
                params={
                    "nfo_type": "movie",
                    "title": "Lantern",
                    "nfo_path": str(nfo),
                    "item_id": 1,
                },
                key="movie",
                user=owner,
            )

            # interactive ingest runs only preview candidates
            assert result["status"] == "completed", result
            assert engine.dispatched[0]["$manual"] is True
            assert result["evidence"]["output_check"] == "passed"
            assert result["output"][0]["title"] == "Lantern & Rain"
            assert result["target"]["matches_published"] is False
            assert not nfo.exists()

    asyncio.run(run())


CANVAS_URL = URL + "&size={{ page_size }}"
FIX = {"op": "set_field", "node_id": "end", "field_id": "response"}
RUN = {"entry": "search_start", "params": PARAMS}


def latest_snapshot(body):
    """Read the newest proposal snapshot ID from the model's visible context.

    Args:
        body: Model request, whose tool results may be compacted.

    Returns:
        Snapshot ID of the latest preview.
    """
    import re

    found = re.findall(
        r'proposal_snapshot_id\\?": ?(\d+)', dumps(body["messages"]).decode()
    )
    return int(found[-1])


def scripted(steps, streams, *, verbose=False):
    """Build a scripted provider whose steps may read the model context.

    Args:
        steps: Per-request callables returning tool calls, or `None` to finish.
        streams: List collecting streamed request bodies.
        verbose: Add long narration to force active-tool compaction.

    Returns:
        HTTP handler for the mocked provider.
    """
    from tests.test_tv_ingestion import summary

    def handler(upstream):
        """Answer one model request within the unchanged context allowance.

        Args:
            upstream: Provider request.

        Returns:
            Streamed tool calls or the final answer.
        """
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        if not body.get("stream"):
            return summary(body, "Run and repair the workflow.")
        streams.append(body)
        step = len(streams) - 1
        calls = steps[step](body) if step < len(steps) else None
        if not calls:
            return response(
                [chunk({"role": "assistant", "content": "Reported the run."}, "stop")]
            )
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "Checking the run. " * 300 if verbose else "",
                        "tool_calls": [
                            call_delta(
                                name, dumps(args).decode(), f"{step}-{index}", index
                            )
                            for index, (name, args) in enumerate(calls)
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    return handler


def names(body):
    """List the tool names offered in one model request.

    Args:
        body: Model request.

    Returns:
        Offered function names.
    """
    return {tool["function"]["name"] for tool in body["tools"]}


async def converse(
    conversation_id, owner, prompt, context=None, key="trial-turn", **values
):
    """Run one persisted turn and return its tool results.

    Args:
        conversation_id: Owned conversation.
        owner: Calling account.
        prompt: User message.
        context: Optional page context.
        key: Turn request ID.
        **values: Other request fields, such as `retry_of`.

    Returns:
        Final event and tool results in call order.
    """
    events = [
        event
        async for event in AssistantService.persistent_stream(
            conversation_id,
            request(
                key,
                prompt=prompt,
                **({"context": context} if context else {}),
                **values,
            ),
            owner,
        )
    ]
    return events[-1], [e.data for e in events if e.event == "tool_result"]


@pytest.mark.parametrize("decision", ["keep", "revert"])
@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
@pytest.mark.parametrize("recipe", [False, True], ids=["ordinary", "recipe"])
def test_repair_conversation(monkeypatch, engine, verbose, decision, recipe):
    streams, base = [], {}
    proposal = {}

    def fixed(body):
        proposal.update(base, snapshot_id=latest_snapshot(body))
        return [("validate_workflow", proposal)]

    steps = [
        lambda body: (
            [("validate_workflow", base)] if "run_workflow" not in names(body) else None
        ),
        lambda body: (
            [("run_workflow", {**base, **RUN})]
            if "run_workflow" in names(body)
            else None
        ),
        lambda body: (
            [
                (
                    "preview_workflow_draft",
                    {**base, "changes": [{**FIX, "value": SEARCH_TEMPLATE}]},
                )
            ]
            if "items.0.title" in dumps(body["messages"]).decode()
            else None
        ),
        fixed,
        lambda body: [("run_workflow", {**proposal, **RUN})],
    ]
    if recipe:
        steps.insert(
            0, lambda body: [("get_workflow_reference", {"topic": "json_indexer"})]
        )
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))

    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Source",
                category="indexer",
                state="draft",
                draft=search_graph(template=UNTITLED),
            )
            saved = graph.draft
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            # the visible canvas has an unsaved request edit
            canvas = search_graph(CANVAS_URL, UNTITLED)
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                flow_service.GraphDocument.model_validate(canvas),
                "canvas",
                owner,
            )
            base.update(workflow_id=graph.id, draft_version=0, snapshot_id=snapshot.id)

            done, results = await converse(
                conversation["id"],
                owner,
                "Run this search for Lantern and fix any failure.",
                {
                    "page_type": "workflow_designer",
                    "route": f"{WORKFLOW_PATH}/{graph.id}",
                    "resource_id": graph.id,
                    "snapshot_id": snapshot.id,
                    "draft_version": 0,
                    "unsaved": True,
                    "editable": True,
                    "selected_node_ids": [],
                },
            )

            assert done.event == "done", done
            assert all(result["ok"] for result in results), results
            first, second = [r["data"] for r in results if r["name"] == "run_workflow"]
            assert first["target"]["source"] == "canvas"
            assert first["evidence"]["output_check"] == "failed"
            assert second["target"]["source"] == "proposal"
            assert second["status"] == "completed"
            assert second["evidence"]["output_check"] == "passed"
            # both runs executed the unsaved canvas request, never the saved one
            assert [params.get("size") for _, params in engine.seen] == ["1", "1"]
            assert len(engine.dispatched) == 2
            if verbose:
                assert any(
                    "Completed tool exchange" in dumps(body["messages"]).decode()
                    for body in streams
                )
            await graph.refresh_from_db()
            assert graph.draft == saved and graph.draft_version == 0
            assert graph.definition is None and graph.state == "draft"

            preview = next(r for r in results if r["name"] == "preview_workflow_draft")
            record = await AssistantProposal.get(id=preview["data"]["proposal_id"])
            identity = {
                "workflow_id": record.workflow_id,
                "base_version": record.base_version,
                "digest": record.digest,
            }
            if decision == "keep":
                body = ProposalConfirm(
                    **identity, base_snapshot_id=record.base_snapshot_id
                )
                saved_result = await Proposals.confirm(
                    conversation["id"], record.id, body, owner
                )
                # a lost save response recovers the single recorded save
                repeated = await Proposals.confirm(
                    conversation["id"], record.id, body, owner
                )
                recovered = await Proposals.outcome(
                    conversation["id"], record.id, owner
                )
                await graph.refresh_from_db()
                assert saved_result["proposal"] == repeated["proposal"]
                assert recovered["proposal"]["status"] == "saved"
                assert graph.draft_version == 1 and graph.definition is None
                nodes = {node["id"]: node["data"] for node in graph.draft["nodes"]}
                assert nodes["end"]["response"] == SEARCH_TEMPLATE
                assert nodes["http"]["url"] == CANVAS_URL
            else:
                cancelled = await Proposals.cancel(
                    conversation["id"], record.id, ProposalDecision(**identity), owner
                )
                original = await Sessions.read_snapshot(
                    conversation["id"], record.base_snapshot_id, owner
                )
                await graph.refresh_from_db()
                assert cancelled["proposal"]["status"] == "cancelled"
                assert graph.draft == saved and graph.draft_version == 0
                # Revert restores the captured canvas with its unsaved edit
                nodes = {n["id"]: n["data"] for n in original.document["nodes"]}
                assert nodes["http"]["url"] == CANVAS_URL

    asyncio.run(run())


def test_repair_limit(monkeypatch, engine):
    streams, base, proposals = [], {}, []

    def preview(attempt):
        """Propose another still-broken mapping.

        Args:
            attempt: Repair number, which changes the reviewed content.

        Returns:
            Step producing the preview call.
        """
        value = f"{{# repair {attempt} #}}{UNTITLED}"
        return lambda body: [
            ("preview_workflow_draft", {**base, "changes": [{**FIX, "value": value}]})
        ]

    def checked(*extra):
        """Validate the newest proposal, then run it once or more.

        Args:
            *extra: Additional identical run calls.

        Returns:
            Step producing validation and run calls.
        """

        def step(body):
            proposals.append({**base, "snapshot_id": latest_snapshot(body)})
            run = ("run_workflow", {**proposals[-1], **RUN})
            return [("validate_workflow", proposals[-1]), run, *[run for _ in extra]]

        return step

    steps = [
        lambda body: [("validate_workflow", base)],
        lambda body: [("run_workflow", {**base, **RUN})],
        preview(1),
        checked(),
        preview(2),
        checked("replay"),
        preview(3),
        checked(),
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Source",
                category="indexer",
                state="draft",
                draft=search_graph(template=UNTITLED),
            )
            base.update(workflow_id=graph.id, draft_version=0)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )

            done, results = await converse(
                conversation["id"], owner, "Run the Source search and repair it."
            )

            runs = [r["data"] for r in results if r["name"] == "run_workflow"]
            assert done.event == "done", done
            assert [run["status"] for run in runs] == [
                "completed",
                "completed",
                "completed",
                "completed",
                "refused",
            ]
            assert [run["target"]["source"] for run in runs[:4]] == [
                "saved_draft",
                "proposal",
                "proposal",
                "proposal",
            ]
            # the repeated call replays the second repair instead of running again
            assert runs[3]["replayed"] and runs[3]["run_key"] == runs[2]["run_key"]
            assert "2 repair runs left" in runs[0]["next"]
            assert "Repair limit reached" in runs[2]["next"]
            assert "Repair limit" in runs[4]["message"]
            assert len(engine.dispatched) == 3

    asyncio.run(run())


def test_side_effect_rerun(monkeypatch, engine):
    streams, base = [], {}
    steps = [
        lambda body: [("validate_workflow", base)],
        lambda body: [("run_workflow", {**base, **RUN})],
        lambda body: [
            (
                "preview_workflow_draft",
                {**base, "changes": [{**FIX, "value": SEARCH_TEMPLATE}]},
            )
        ],
        lambda body: [
            ("validate_workflow", {**base, "snapshot_id": latest_snapshot(body)}),
            ("run_workflow", {**base, "snapshot_id": latest_snapshot(body), **RUN}),
        ],
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Source",
                category="indexer",
                state="draft",
                draft=search_graph(template=UNTITLED, method="POST"),
            )
            base.update(workflow_id=graph.id, draft_version=0)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )

            done, results = await converse(
                conversation["id"], owner, "Run the Source search and fix it."
            )

            first, second = [r["data"] for r in results if r["name"] == "run_workflow"]
            assert done.event == "done", done
            assert first["rerun"] == "ask" and "ask the user" in first["next"]
            assert second["status"] == "refused"
            assert "ask the user" in second["message"]
            assert len(engine.dispatched) == 1

    asyncio.run(run())


@pytest.mark.parametrize("record", ["error", "interrupted"])
def test_uncertain_rerun(monkeypatch, engine, record):
    streams, base = [], {}
    changed = {**RUN, "params": {**PARAMS, "page_num": 1}}
    steps = [
        lambda body: [("validate_workflow", base)],
        lambda body: [("run_workflow", {**base, **RUN})],
        lambda body: None,
        # the retried request may not start a different run after an unseen one
        lambda body: [("validate_workflow", base)],
        lambda body: [
            ("run_workflow", {**base, **changed}),
            ("run_workflow", {**base, **RUN}),
        ],
    ]
    configure(monkeypatch, scripted(steps, streams))
    real = tools.trial_run

    async def lost(*args, **kwargs):
        """Dispatch the run, then lose the response like a failed call.

        Args:
            *args: Shared trial arguments.
            **kwargs: Shared trial options.

        Raises:
            RuntimeError: After the run has been dispatched.
        """
        monkeypatch.setattr(tools, "trial_run", real)
        await real(*args, **kwargs)
        raise RuntimeError("response lost")

    monkeypatch.setattr(tools, "trial_run", lost)

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            base.update(workflow_id=graph.id, draft_version=3)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )

            _, first = await converse(conversation["id"], owner, "Run the search.")
            turn = await AssistantTurn.get(conversation_id=conversation["id"])
            await AssistantTurn.filter(id=turn.id).update(status="interrupted")
            if record == "interrupted":
                # an interrupted stream leaves no result for its running call
                await AssistantToolCall.filter(name="run_workflow").update(
                    status="interrupted", result=None
                )
            done, results = await converse(
                conversation["id"],
                owner,
                "Run the search.",
                key="trial-retry",
                retry_of=turn.id,
            )

            other, same = [r["data"] for r in results if r["name"] == "run_workflow"]
            assert first[-1]["error"]["code"] == "internal_server_error"
            assert done.event == "done", done
            assert other["status"] == "refused" and "ask the user" in other["message"]
            assert same["status"] == "completed" and same["replayed"]
            assert len(engine.dispatched) == 1

    asyncio.run(run())


def test_superseded_snapshot(monkeypatch, engine):
    streams, base = [], {}
    steps = [
        lambda body: [("validate_workflow", base), ("run_workflow", {**base, **RUN})],
        lambda body: [
            (
                "preview_workflow_draft",
                {**base, "changes": [{**FIX, "value": SEARCH_TEMPLATE}]},
            )
        ],
        # the canvas now shows the proposal, so the earlier run cannot verify it
        lambda body: [
            ("run_workflow", {**base, **RUN}),
            ("run_workflow", {**base, "snapshot_id": latest_snapshot(body), **RUN}),
        ],
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Source",
                category="indexer",
                state="draft",
                draft=search_graph(template=UNTITLED),
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                flow_service.GraphDocument.model_validate(graph.draft),
                "canvas",
                owner,
            )
            base.update(workflow_id=graph.id, draft_version=0, snapshot_id=snapshot.id)

            done, results = await converse(
                conversation["id"],
                owner,
                "Run this search and fix it.",
                {
                    "page_type": "workflow_designer",
                    "route": f"{WORKFLOW_PATH}/{graph.id}",
                    "resource_id": graph.id,
                    "snapshot_id": snapshot.id,
                    "draft_version": 0,
                    "unsaved": False,
                    "editable": True,
                    "selected_node_ids": [],
                },
            )

            first, stale, fixed = [r for r in results if r["name"] == "run_workflow"]
            assert done.event == "done", done
            assert first["data"]["evidence"]["output_check"] == "failed"
            assert stale["error"]["code"] == "assistant_canvas_changed"
            assert fixed["data"]["target"]["source"] == "proposal"
            assert fixed["data"]["evidence"]["output_check"] == "passed"
            assert len(engine.dispatched) == 2

    asyncio.run(run())


def test_parity(monkeypatch, engine):
    streams, base = [], {}
    steps = [
        lambda body: [("validate_workflow", base)],
        lambda body: [("run_workflow", {**base, **RUN})],
    ]
    configure(monkeypatch, scripted(steps, streams))
    request_id = "6b0f3c6e-6d3c-4a51-9d55-3c4f3f1d2a01"

    async def run():
        async with application() as app:
            admin = app.shared_ctx.sessions["admin"]
            graph = await indexer()
            base.update(workflow_id=graph.id, draft_version=3)
            arguments = {"request_id": request_id, **base, **RUN}
            read_only = await call(app, "run_workflow", arguments, token="admin")
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="run", scope=MCPTokenScope.READ_WRITE),
            )
            token = write["token"]
            mcp = await call(app, "run_workflow", arguments, token=token)
            replay = await call(app, "run_workflow", arguments, token=token)
            changed = await call(
                app,
                "run_workflow",
                {**arguments, "params": {**PARAMS, "page_num": 1}},
                token=token,
            )
            stale = await call(
                app, "run_workflow", {**arguments, "draft_version": 2}, token=token
            )
            other = await indexer(name="Other")
            # one request ID never runs a second workflow
            elsewhere = await call(
                app, "run_workflow", {**arguments, "workflow_id": other.id}, token=token
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            done, results = await converse(
                conversation["id"], admin, "Run the Source search for Lantern."
            )
            assistant = results[-1]["data"]
            turn = await AssistantTurn.get(conversation_id=conversation["id"])

            assert read_only["isError"] and "read-only" in str(read_only)
            assert not mcp.get("isError"), mcp
            local = mcp["structuredContent"]
            assert local["status"] == "completed" and "replayed" not in local
            assert replay["structuredContent"]["replayed"]
            assert changed["structuredContent"]["status"] == "conflict"
            assert elsewhere["structuredContent"]["status"] == "conflict"
            assert stale["isError"]
            assert done.event == "done", done
            # both callers share the service, so only identities differ
            for result in (local, assistant):
                result.pop("run_key")
                result.pop("replayed", None)
                result["target"].pop("snapshot_id", None)
                result.pop("output_log_id")
            assert loads(dumps(assistant)) == local
            assert len(engine.dispatched) == 2
            assert graph.id in turn.resource_ids

            # after a newer save the request stays readable, never runnable
            await FlowDraftRevision.create(
                graph_id=graph.id, version=3, draft=graph.draft
            )
            await FlowGraph.filter(id=graph.id).update(draft_version=4)
            await FlowDraftRevision.filter(graph_id=graph.id).delete()
            saved = await call(app, "run_workflow", arguments, token=token)
            outdated = await call(
                app,
                "run_workflow",
                {**arguments, "request_id": "6b0f3c6e-6d3c-4a51-9d55-3c4f3f1d2a02"},
                token=token,
            )
            assert saved["structuredContent"]["replayed"]
            assert saved["structuredContent"]["status"] == "completed"
            assert outdated["isError"] and "conflict" in str(outdated).lower()
            assert len(engine.dispatched) == 2

            # current permissions are rechecked for logs and recorded history
            admin.role = UserRole.USER
            admin.perms = Permissions(indexer_ids=[])
            with pytest.raises(ForbiddenException):
                await workflow_logs(graph.id, 5, admin)
            with pytest.raises(ForbiddenException):
                await FlowRunService.run(
                    graph.id,
                    graph.draft,
                    entry="search_start",
                    params={},
                    key="x",
                    user=admin,
                )
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": turn.id}).decode(),
                admin,
                conversation_id=conversation["id"],
                call_id="history",
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())


@pytest.mark.parametrize("designer", [True, False], ids=["canvas", "saved"])
def test_stale_reference(monkeypatch, engine, designer):
    streams, base = [], {}
    steps = [
        lambda body: [("validate_workflow", base)],
        lambda body: [
            # the canvas turn may not run the saved draft or an older version
            ("run_workflow", {**base, "snapshot_id": None, **RUN}),
            ("run_workflow", {**base, "draft_version": 1, **RUN}),
        ],
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Source",
                category="indexer",
                state="draft",
                draft=search_graph(),
                draft_version=2,
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            context = None
            base.update(workflow_id=graph.id, draft_version=2)
            if designer:
                snapshot = await Sessions.snapshot(
                    conversation["id"],
                    graph.id,
                    2,
                    flow_service.GraphDocument.model_validate(search_graph(CANVAS_URL)),
                    "canvas",
                    owner,
                )
                base["snapshot_id"] = snapshot.id
                context = {
                    "page_type": "workflow_designer",
                    "route": f"{WORKFLOW_PATH}/{graph.id}",
                    "resource_id": graph.id,
                    "snapshot_id": snapshot.id,
                    "draft_version": 2,
                    "unsaved": True,
                    "editable": True,
                    "selected_node_ids": [],
                }

            done, results = await converse(
                conversation["id"], owner, "Run the Source search.", context
            )

            saved, older = [r for r in results if r["name"] == "run_workflow"]
            assert done.event == "done", done
            if designer:
                assert saved["error"]["code"] == "assistant_canvas_changed"
            else:
                # without a canvas the current saved draft is the reviewed content
                assert saved["ok"] and saved["data"]["status"] == "completed"
            assert not older["ok"] and older["error"]["code"] in {
                "assistant_canvas_changed",
                "flow_draft_conflict",
            }
            assert len(engine.dispatched) == (0 if designer else 1)

    asyncio.run(run())
