import asyncio
import queue
import traceback
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import cached_property
from multiprocessing import Queue
from multiprocessing.managers import DictProxy, ListProxy
from multiprocessing.synchronize import Event, Lock
from typing import Any, Literal, Self

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sanic import Sanic
from sanic.log import Colors, logger
from tortoise import timezone
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.core.exceptions import ErrorCode, KaloscopeException, NotFoundException
from app.core.flow.context import OUTPUT_KEY, RETVAL_KEY, START_KEY, Context
from app.core.flow.edge import Source, Target
from app.core.flow.fetcher import RepoFetcher
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, NodeGroup
from app.models.flow import (
    FlowFootprint,
    FlowGraph,
    FlowInstance,
    FlowJob,
    FlowLog,
    GraphRef,
    JobState,
)

# the execution policy for the flow jobs
type ExecutionPolicy = Literal["date", "cron", "interval", "immediate"]

# the interval unit for the interval trigger
type IntervalUnit = Literal["weeks", "days", "hours", "minutes", "seconds"]


class JobAction(Enum):
    """The actions for the flow jobs."""

    PAUSE = auto()
    RESUME = auto()
    DELETE = auto()
    REFRESH = auto()


@dataclass(frozen=True, kw_only=True)
class EventWrapper:
    """The event to be consumed by the flow engine."""

    policy: ExecutionPolicy = field(kw_only=False)
    graph_id: int = field(kw_only=False)
    bootparams: Mapping[str, Any] | None = field(kw_only=False, default=None)
    repeatable: bool = True
    recoverable: bool = False
    recovery_id: int | None = None
    job_id: int | None = None
    run_date: datetime | None = None
    cron_expr: str | None = None
    interval: tuple[int, IntervalUnit] | None = None
    interval_start: datetime | None = None
    interval_end: datetime | None = None

    def __str__(self):
        return f"Event(graph_id={self.graph_id}, bootparams={self.bootparams})"

    @classmethod
    def from_inst(cls, inst: FlowInstance) -> Self:
        """Wrap the flow instance into an event.

        Args:
            inst: The flow instance.

        Returns:
            The event wrapper.
        """
        return cls(
            "immediate",
            inst.graph_id,
            inst.bootparams,
            repeatable=inst.repeatable,
            recoverable=True,
            recovery_id=inst.id,
        )

    @classmethod
    def from_job(cls, job: FlowJob) -> Self:
        """Wrap the flow job into an event.

        Args:
            job: The flow job.

        Returns:
            The event wrapper.
        """
        if job.interval_num and job.interval_unit:
            interval = (job.interval_num, job.interval_unit.value)
        else:
            interval = None

        return cls(
            job.trigger.value,
            job.graph_id,
            job.bootparams,
            repeatable=job.repeatable,
            recoverable=job.recoverable,
            job_id=job.id,
            run_date=job.run_date,
            cron_expr=job.cron_expr,
            interval=interval,
            interval_start=job.interval_start,
            interval_end=job.interval_end,
        )


@dataclass(kw_only=True)
class NodeWrapper:
    """The node date wrapper."""

    node_id: str = field(kw_only=False)
    node_type: str
    node_data: dict[str, Any]
    # the bound input handle used for the node execution
    input_handle: InputHandle | None = field(init=False, default=None)

    def bind(self, handle_id: str, loop_id: str | None = None) -> Self:
        """Bind the input handle to the node.

        Args:
            handle_id: The handle ID.
            loop_id: The loop node ID.

        Returns:
            The node data wrapper.
        """
        self.input_handle = InputHandle.create(handle_id, loop_id)
        return self

    @property
    def loop_id(self) -> str | None:
        """Get the loop node ID from the input handle.

        Returns:
            The loop node ID.
        """
        return self.input_handle.loop_id if self.input_handle else None


class FlowEngine:
    """The flow engine for managing the flow execution and scheduling."""

    _JOB_LISTENER = "flow_job_listener"
    _EVENT_CONSUMER = "flow_event_consumer"

    def __init__(self, app: Sanic):
        """Initialize the flow engine.

        Args:
            app: The Sanic application instance.
        """
        self._app = app
        self._fetcher = RepoFetcher(app)
        self._scheduler = AsyncIOScheduler(options={"event_loop": app.loop})
        self._num_workers = sum(1 for w in app.m.workers.values() if w.get("server"))

    @cached_property
    def _lock(self) -> Lock:
        return self._app.shared_ctx.flow_lock

    @cached_property
    def _events(self) -> Queue:
        return self._app.shared_ctx.flow_events

    @cached_property
    def _reload_flag(self) -> Event:
        return self._app.shared_ctx.flow_reload_flag

    @cached_property
    def _job_actions(self) -> DictProxy[int, JobAction]:
        return self._app.shared_ctx.flow_job_actions

    async def _reload(self):
        """Reload the running flow instances and jobs from the database."""
        if self._lock.acquire(block=False):
            try:
                if not self._reload_flag.is_set():
                    for inst in await FlowInstance.filter(prev_id__isnull=True):
                        self._events.put(EventWrapper.from_inst(inst))
                    for job in await FlowJob.filter(state=JobState.RUNNING):
                        self._events.put(EventWrapper.from_job(job))
                    self._reload_flag.set()
            finally:
                self._lock.release()

    async def start(self):
        """Start the flow engine."""
        await self._reload()
        await self._fetcher.start()
        self._scheduler.start()
        self._app.add_task(self._job_listener(), name=self._JOB_LISTENER)
        self._app.add_task(self._event_consumer(), name=self._EVENT_CONSUMER)

    async def shutdown(self):
        """Shutdown the flow engine."""
        self._reload_flag.clear()
        await self._fetcher.shutdown()
        self._scheduler.shutdown()
        await self._app.cancel_task(self._JOB_LISTENER)
        await self._app.cancel_task(self._EVENT_CONSUMER)

    def payload(self) -> float:
        """Calculate the payload based on the number of workers and tasks.

        Returns:
            The payload of the engine.
        """
        factor = (1 - (1 / self._num_workers)) / 100
        if factor == 0:
            return 0.2
        return 0.2 + sum(1 for _ in self._app.tasks) * factor

    async def _event_consumer(self):
        """Consume events from the queue and process them."""
        while True:
            try:
                if not self._events.empty():
                    event: EventWrapper = self._events.get_nowait()
                    if event.policy == "immediate":
                        if event.graph_id is not None:
                            # run the task immediately in the background
                            self._app.add_task(
                                self.execute(
                                    event.graph_id,
                                    event.bootparams,
                                    repeatable=event.repeatable,
                                    recoverable=event.recoverable,
                                    recovery_id=event.recovery_id,
                                    asynchronous=None,
                                )
                            )
                    elif event.job_id is not None:
                        trigger = None
                        if event.policy == "date" and event.run_date is not None:
                            trigger = DateTrigger(
                                run_date=event.run_date, timezone=pytz.utc
                            )
                        elif event.policy == "cron" and event.cron_expr is not None:
                            trigger = CronTrigger.from_crontab(
                                event.cron_expr, timezone=pytz.utc
                            )
                        elif event.policy == "interval" and event.interval is not None:
                            num, unit = event.interval
                            trigger = IntervalTrigger(
                                weeks=num if unit == "weeks" else 0,
                                days=num if unit == "days" else 0,
                                hours=num if unit == "hours" else 0,
                                minutes=num if unit == "minutes" else 0,
                                seconds=num if unit == "seconds" else 0,
                                start_date=event.interval_start,
                                end_date=event.interval_end,
                                timezone=pytz.utc,
                            )
                        # add the job to the scheduler
                        if trigger is not None:
                            self._scheduler.add_job(
                                self.execute,
                                trigger,
                                kwargs={
                                    "graph_id": event.graph_id,
                                    "bootparams": event.bootparams,
                                    "repeatable": event.repeatable,
                                    "recoverable": event.recoverable,
                                    "asynchronous": True,
                                },
                                id=str(event.job_id),
                            )
                await asyncio.sleep(self.payload())
            except queue.Empty:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Failed to consume the flow event!", exc_info=True)
                await asyncio.sleep(1)

    async def _job_listener(self):
        """Listen for the actions and perform the corresponding operations."""
        while True:
            try:
                for id in list(self._job_actions.keys()):
                    job_id = str(id)
                    if self._scheduler.get_job(job_id) is not None:
                        # remove the job from the scheduler
                        self._scheduler.remove_job(job_id)
                        if self._job_actions.get(id) == JobAction.REFRESH:
                            # add the job to the scheduler again
                            await self.add_job(id)

                        self._job_actions.pop(id)
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Failed to process the job action!", exc_info=True)
                await asyncio.sleep(1)

    async def add_job(self, job_id: int):
        """Add the flow job to the broker.

        Args:
            job_id: The flow job ID.
        """
        job = FlowJob.filter(id=job_id, state__not=JobState.RUNNING)
        if await job.update(state=JobState.RUNNING) == 1:
            self._events.put(EventWrapper.from_job(await FlowJob.get(id=job_id)))

    async def pause_job(self, job_id: int):
        """Pause the flow job.

        Args:
            job_id: The flow job ID.
        """
        job = FlowJob.filter(id=job_id, state=JobState.RUNNING)
        if await job.update(state=JobState.PAUSED) == 1:
            self._job_actions[job_id] = JobAction.PAUSE

    async def resume_job(self, job_id: int):
        """Resume the flow job.

        Args:
            job_id: The flow job ID.
        """
        await self.add_job(job_id)

    async def refresh_job(self, job_id: int):
        """Refresh the flow job.

        Args:
            job_id: The flow job ID.
        """
        job = FlowJob.filter(id=job_id, state=JobState.RUNNING)
        if await job.update(state=JobState.PENDING) == 1:
            self._job_actions[job_id] = JobAction.REFRESH

    async def delete_job(self, job_id: int):
        """Delete the flow job.

        Args:
            job_id: The flow job ID.
        """
        if await FlowJob.filter(id=job_id, state=JobState.RUNNING).exists():
            self._job_actions[job_id] = JobAction.DELETE
        await FlowJob.filter(id=job_id).delete()

    async def execute(
        self,
        graph_id: int,
        bootparams: Mapping[str, Any] | None = None,
        *,
        repeatable: bool = True,
        recoverable: bool = False,
        recovery_id: int | None = None,
        asynchronous: bool | None = False,
    ) -> Any:
        """Execute the flow graph.

        Args:
            graph_id: The flow graph ID for execution.
            bootparams: The boot parameters for execution.
            repeatable: Whether the execution is repeatable.
            recoverable: Whether the execution is recoverable.
            recovery_id: The flow instance ID for recovery.
            asynchronous: Whether to run the task in the background.

        Returns:
            The execution result.
        """
        event = EventWrapper(
            "immediate",
            graph_id,
            bootparams,
            repeatable=repeatable,
            recoverable=recoverable,
            recovery_id=recovery_id,
        )
        try:
            # run the task in the background
            if asynchronous:
                return self._events.put(event)

            # run the task immediately
            logger.info(
                f"{Colors.BOLD}{Colors.SANIC}Flow execution started:"
                f"{Colors.BLUE} %s{Colors.END} %s",
                id(event),
                event,
            )
            async with FlowTask.from_event(event) as task:
                await task.run()
                # mark the task as cleanable if it is completed
                task.cleanable = True
                logger.info(
                    f"{Colors.BOLD}{Colors.SANIC}Flow execution completed:"
                    f"{Colors.BLUE} %s{Colors.END}",
                    id(event),
                )
                return await task.retval()
        except Exception:
            if asynchronous is False:
                raise
            else:
                logger.error(
                    f"{Colors.BOLD}{Colors.RED}Flow execution failed:"
                    f"{Colors.BLUE} %s{Colors.END}",
                    id(event),
                    exc_info=True,
                )
        finally:
            if asynchronous is not None and recovery_id is not None:
                # execute the next flow instance if it exists
                next = await FlowInstance.get_or_none(prev_id=recovery_id)
                if next is not None:
                    await FlowInstance.filter(id=next.id).update(prev_id=None)
                    await self.execute(
                        next.graph_id,
                        next.bootparams,
                        repeatable=next.repeatable,
                        recoverable=True,
                        recovery_id=next.id,
                        asynchronous=next.asynchronous,
                    )

    async def execute_batch(
        self,
        graph_ids: list[GraphRef],
        bootparams: Mapping[str, Any] | None = None,
        *,
        repeatable: bool = True,
        recoverable: bool = False,
    ):
        """Execute a batch of flow graphs.

        Args:
            graph_ids: A list of flow graph IDs for execution.
            bootparams: The boot parameters for execution.
            repeatable: Whether the execution is repeatable.
            recoverable: Whether the execution is recoverable.
        """
        if not recoverable:
            # execute the flow graphs without recovery
            for ref in graph_ids:
                graph = await FlowGraph.get(id=ref.graph_id)
                await self.execute(
                    graph.id,
                    bootparams,
                    repeatable=repeatable,
                    recoverable=recoverable,
                    asynchronous=ref.asynchronous,
                )
        else:
            # execute the flow graphs with recovery
            first: FlowInstance | None = None
            prev_id: int | None = None
            async with in_transaction("default"):
                # persist the flow instances before execution
                for ref in graph_ids:
                    graph = await FlowGraph.get(id=ref.graph_id)
                    inst = await FlowInstance.create(
                        graph_id=graph.id,
                        definition=graph.definition,
                        bootparams=bootparams,
                        context={},
                        repeatable=repeatable,
                        asynchronous=ref.asynchronous,
                        prev_id=prev_id,
                    )
                    prev_id = inst.id
                    if first is None:
                        first = inst
            if first is not None:
                # execute the first flow instance
                await self.execute(
                    first.graph_id,
                    first.bootparams,
                    repeatable=first.repeatable,
                    recoverable=True,
                    recovery_id=first.id,
                    asynchronous=first.asynchronous,
                )


class FlowTask(ABC):
    """The base class for all flow tasks."""

    __slots__ = (
        "graph_id",
        "bootparams",
        "repeatable",
        "cleanable",
        "started_at",
        "_definition",
        "_nodes",
        "_incomers",
        "_outgoers",
        "_context",
        "_merge_lock",
    )

    _running_lock: Lock | None = None
    _running_tasks: ListProxy | None = None

    def __init__(self, graph_id: int, bootparams: Mapping[str, Any], repeatable: bool):
        self.graph_id = graph_id
        self.bootparams = bootparams
        self.repeatable = repeatable
        self.cleanable = False
        self.started_at = timezone.now()
        # the flow graph definition
        self._definition: dict
        self._nodes: dict[str, NodeWrapper] | None = None
        self._incomers: dict[Target, list[Source]] | None = None
        self._outgoers: dict[Source, list[Target]] | None = None
        # the context for the flow task
        self._context: Context
        self._merge_lock = asyncio.Lock()

    @classmethod
    def from_event(cls, event: EventWrapper) -> "FlowTask":
        """Factory method to create a flow task from an event.

        Args:
            event: The event wrapper.

        Raises:
            KaloscopeException: If the task is already running.

        Returns:
            The flow task instance.
        """
        task_symbol = (event.graph_id, event.bootparams or {})
        if not event.repeatable and task_symbol in cls.running_tasks():
            raise KaloscopeException(ErrorCode.FLOW_ALREADY_RUNNING)
        if not event.recoverable:
            return TransientTask(*task_symbol, event.repeatable)
        else:
            return PersistentTask(*task_symbol, event.repeatable, event.recovery_id)

    @classmethod
    def running_lock(cls) -> Lock:
        if cls._running_lock is None:
            running_lock: Lock = Sanic.get_app().shared_ctx.flow_lock
            cls._running_lock = running_lock
        return cls._running_lock

    @classmethod
    def running_tasks(cls) -> ListProxy[tuple]:
        if cls._running_tasks is None:
            running_tasks: ListProxy = Sanic.get_app().shared_ctx.flow_running_tasks
            cls._running_tasks = running_tasks
        return cls._running_tasks

    async def __aenter__(self) -> Self:
        # check if the task is already running
        if not self.repeatable:
            with self.running_lock():
                task_symbol = (self.graph_id, self.bootparams)
                if task_symbol in self.running_tasks():
                    raise KaloscopeException(ErrorCode.FLOW_ALREADY_RUNNING)
                self.running_tasks().append(task_symbol)
        try:
            # initialize the flow task
            await self.initialize()
        except Exception as e:
            await self.__aexit__(type(e), e, e.__traceback__)
            raise
        return self

    @abstractmethod
    async def initialize(self):
        raise NotImplementedError

    async def __aexit__(self, exc_type, exc_value, traceback):
        # remove the task from the running tasks
        if not self.repeatable:
            self.running_tasks().remove((self.graph_id, self.bootparams))
        # mark the task as cleanable if an exception is raised
        if exc_type is not None and not issubclass(exc_type, asyncio.CancelledError):
            self.cleanable = True
        # clean up the task resources
        await self.cleanup()
        return None

    @abstractmethod
    async def cleanup(self):
        raise NotImplementedError

    async def graph_definition(self) -> dict[str, Any]:
        """Get the published flow graph definition from the database.

        Raises:
            ValueError: If the flow graph does not exist.

        Returns:
            The flow graph object.
        """
        graph = await FlowGraph.get_or_none(
            id=self.graph_id,
            # state__in=[GraphState.PUBLISHED, GraphState.MODIFIED],
            definition__not_isnull=True,
        )
        if graph is None or graph.definition is None:
            raise NotFoundException(ErrorCode.FLOW_NOT_FOUND)
        return graph.definition

    @property
    def nodes(self) -> dict[str, NodeWrapper]:
        """Extract the nodes from the flow graph definition.

        Returns:
            A dictionary of node ID to node wrapper mapping.
        """
        if self._nodes is None:
            nodes = {}
            for node in self._definition["nodes"]:
                node_id = node["id"]
                node_data: dict = node["data"]
                node_type: str = node_data.pop("$schema")
                nodes[node_id] = NodeWrapper(
                    node_id, node_type=node_type, node_data=node_data
                )
            self._nodes = nodes
        return self._nodes

    @property
    def incomers(self) -> dict[Target, list[Source]]:
        """Construct the target-sources mapping from the edges.

        Returns:
            A dictionary of target-sources mapping.
        """
        if self._incomers is None:
            incomers = {}
            for edge in self._definition["edges"]:
                target = Target(edge["target"], handle_id=edge["targetHandle"])
                source = Source(edge["source"], handle_id=edge["sourceHandle"])
                if target in incomers:
                    incomers[target].append(source)
                else:
                    incomers[target] = [source]
            self._incomers = incomers
        return self._incomers

    @property
    def outgoers(self) -> dict[Source, list[Target]]:
        """Construct the source-targets mapping from the edges.

        Returns:
            A dictionary of source-targets mapping.
        """
        if self._outgoers is None:
            outgoers = {}
            for edge in self._definition["edges"]:
                source = Source(edge["source"], handle_id=edge["sourceHandle"])
                target = Target(edge["target"], handle_id=edge["targetHandle"])
                if source in outgoers:
                    outgoers[source].append(target)
                else:
                    outgoers[source] = [target]
            self._outgoers = outgoers
        return self._outgoers

    def start_nodes(self) -> list[NodeWrapper]:
        """Get the start nodes to begin the flow execution.

        Returns:
            The list of start nodes.
        """
        start_type = self.bootparams.get(START_KEY)
        if start_type is not None:
            return [n for n in self.nodes.values() if n.node_type == start_type]
        start_types = [s.node_type for s in Node.schemas if s.group == NodeGroup.START]
        return [n for n in self.nodes.values() if n.node_type in start_types]

    @asynccontextmanager
    async def context(self, node: NodeWrapper):
        """Create a context manager for the node execution.

        Args:
            node: The node data wrapper.

        Raises:
            Any exception raised during the node execution.

        Yields:
            The context for the node execution.
        """
        ctx = None
        loop_id = None
        started_at = None
        try:
            # deep copy the context for each node execution
            ctx = self._context.copy()
            loop_id = node.loop_id
            if loop_id is not None:
                ctx.bind_loop(self.nodes[loop_id].node_data)
            started_at = timezone.now()
            yield ctx
        except Exception as e:
            if not isinstance(e, CancellationSignal):
                await self.log_error(node, traceback.format_exc())
            raise
        finally:
            if ctx is not None and started_at is not None:
                # update the context with the changes from the node execution
                if ctx.is_modified():
                    async with self._merge_lock:
                        self._context.update(ctx)
                # get the output handle from the node data
                output = node.node_data.get(OUTPUT_KEY)
                if output is not None:
                    if output.get("from_snapshot") is True:
                        # remove the output handle if it is a snapshot
                        node.node_data.pop(OUTPUT_KEY)
                    else:
                        # call the footprint method implemented by the subclass
                        ended_at = timezone.now()
                        await self.footprint(node, started_at, ended_at, ctx, loop_id)

    @abstractmethod
    async def footprint(
        self,
        node: NodeWrapper,
        started_at: datetime,
        ended_at: datetime,
        ctx: Context,
        loop_id: str | None,
    ):
        """Create a footprint for the node execution.

        Args:
            node: The node data wrapper.
            started_at: The time when the execution started.
            ended_at: The time when the execution ended.
            ctx: The context for the node execution.
            loop_id: The loop node ID.
        """
        raise NotImplementedError

    async def run(
        self, nodes: list[NodeWrapper] | None = None, *, source: Source | None = None
    ):
        """Run the flow task.

        Args:
            nodes: The specific nodes to execute.
            source: The edge source for the nodes to execute.
        """
        _root = False
        if nodes is None:
            # if no nodes are provided, start from the start nodes
            _root = True
            nodes = self.start_nodes()
        if not nodes:
            return

        async def _run(node: NodeWrapper):
            """Run a single node and its outgoers recursively.

            Args:
                node: The node to execute.
            """
            input = node.input_handle
            if input is not None:
                tgt = Target(node.node_id, handle_id=input.id)
                sources = self.incomers.get(tgt)
            else:
                sources = None

            output = None
            async with self.context(node) as context:
                # call the executor for the node type
                output = await Node.executors[node.node_type](
                    graph_id=self.graph_id,
                    node_id=node.node_id,
                    node_data=node.node_data,
                    context=context,
                    input_handle=input,
                    source=source,
                    sources=sources,
                )

            if output is not None:
                # get the target nodes from the outgoers mapping
                src = Source(node.node_id, handle_id=output.id)
                targets = self.outgoers.get(src)
                if targets:
                    # run the next nodes recursively
                    next_nodes = [
                        self.nodes[t.node_id].bind(t.handle_id, output.loop_id)
                        for t in targets
                    ]
                    await self.run(next_nodes, source=src)

        async def _wait(tasks: Iterable[asyncio.Task]):
            """Wait for the tasks to complete and handle exceptions.

            Args:
                tasks: The iterable of asyncio tasks to wait for.

            Raises:
                e: CancellationSignal if any task is cancelled.
            """
            # wait until the first exception occurs or all tasks are done
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )

            # handle the exceptions raised by the done tasks
            try:
                for done_task in done:
                    done_task.result()
            except CancellationSignal:
                if pending:
                    # cancel all pending tasks if a cancellation signal is raised
                    for pending_task in pending:
                        pending_task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                if not _root:
                    # raise the cancellation signal to the caller
                    raise
            except Exception:
                # ignore branch failures so sibling branches can continue
                pass

            # wait for the pending tasks to complete
            if pending:
                await _wait(pending)

        # create a task for each node and wait for them to complete
        await _wait(asyncio.create_task(_run(node)) for node in nodes)

    async def retval(self) -> Any:
        """Get the the execution result from the context as the return value.

        Returns:
            The return value.
        """
        value = self._context.get(RETVAL_KEY)
        await self.log_success(value)
        return value

    async def log_success(self, retval: Any):
        """Log the successful execution of the flow task.

        Args:
            retval: The return value of the flow task.
        """
        await FlowLog.create(
            graph_id=self.graph_id,
            bootparams=self.bootparams,
            started_at=self.started_at,
            ended_at=timezone.now(),
            retval=retval,
        )
        await self.clear_logs(preserve=5)

    async def log_error(self, node: NodeWrapper, exc_info: str):
        """Log the exception during the node execution.

        Args:
            node: The node data wrapper.
            exc_info: The exception information.
        """
        input_id = node.input_handle.id if node.input_handle else None
        await FlowLog.create(
            graph_id=self.graph_id,
            bootparams=self.bootparams,
            started_at=self.started_at,
            node_id=node.node_id,
            node_type=node.node_type,
            node_data=node.node_data,
            input_id=input_id,
            exc_info=exc_info,
        )
        await self.clear_logs(preserve=5)

    async def clear_logs(self, *, preserve: int):
        """Clear the old logs for the flow graph."""
        filter = Q(graph_id=self.graph_id)
        await FlowLog.filter(
            filter,
            started_at__not_in=sorted(
                await FlowLog.filter(filter)
                .group_by("started_at")
                .values_list("started_at", flat=True),
                reverse=True,
            )[:preserve],
        ).delete()


class TransientTask(FlowTask):
    """A transient task that runs in memory and does not persist the state."""

    __slots__ = ()

    def __init__(self, graph_id: int, bootparams: Mapping[str, Any], repeatable: bool):
        super().__init__(graph_id, bootparams, repeatable)

    async def initialize(self):
        # initialize the definition and context
        self._definition = await self.graph_definition()
        self._context = await Context.create(self.graph_id, self.bootparams)

    async def cleanup(self):
        pass

    async def footprint(self, node: NodeWrapper, *args, **kwargs):
        # remove the output handle
        node.node_data.pop(OUTPUT_KEY, None)


class PersistentTask(FlowTask):
    """A persistent task that persists the state in the database."""

    __slots__ = ("inst_id", "inst")

    def __init__(
        self,
        graph_id: int,
        bootparams: Mapping[str, Any],
        repeatable: bool,
        recovery_id: int | None = None,
    ):
        super().__init__(graph_id, bootparams, repeatable)
        self.inst_id = recovery_id
        self.inst: FlowInstance | None = None

    async def initialize(self):
        footprints = []
        if self.inst_id is None:
            # create a new flow instance
            self.inst = await FlowInstance.create(
                graph_id=self.graph_id,
                definition=await self.graph_definition(),
                bootparams=self.bootparams,
                repeatable=self.repeatable,
                context={},
            )
            self.inst_id = self.inst.id
        else:
            # recover the existing flow instance
            self.inst = await FlowInstance.get(id=self.inst_id)
            footprints = await self.inst.footprints.all()
        # initialize the definition and context
        self._definition = self.inst.definition
        self._context = await Context.create(
            self.graph_id, self.bootparams, self.inst.context
        )
        # recover the node data from the footprints
        for footprint in footprints:
            self.nodes[footprint.node_id].node_data = footprint.node_data

    async def cleanup(self):
        # delete the flow instance and footprints
        if self.cleanable and self.inst is not None:
            await self.inst.delete()

    async def footprint(
        self,
        node: NodeWrapper,
        started_at: datetime,
        ended_at: datetime,
        ctx: Context,
        loop_id: str | None,
    ):
        if self.inst_id is None:
            return
        if ctx.is_modified():
            # update the flow instance context
            await FlowInstance.filter(id=self.inst_id).update(context=ctx.storage)
        if node.node_id == loop_id:
            # delete the existing footprints for the loop node
            await FlowFootprint.filter(
                Q(inst_id=self.inst_id, loop_id=loop_id) & ~Q(node_id=node.node_id)
            ).delete()
            await FlowFootprint.filter(node_id=node.node_id).update(
                node_data=node.node_data, ended_at=ended_at
            )
        else:
            # create a new footprint
            await FlowFootprint.create(
                inst_id=self.inst_id,
                node_id=node.node_id,
                node_type=node.node_type,
                node_data=node.node_data,
                loop_id=loop_id,
                started_at=started_at,
                ended_at=ended_at,
            )
        # remove the output handle
        node.node_data.pop(OUTPUT_KEY, None)
